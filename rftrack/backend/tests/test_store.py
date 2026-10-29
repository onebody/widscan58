"""接入层的两只桶：每接入机一条**有界队列** + 一个**内存环形缓冲**。

规则是落地方案 §5 与 §2.4/§6.5 定死的，不是这里选的：

- 队列满载时**丢最旧**并 `dropped++`，**禁止阻塞 ingest 的读循环**。所以 `offer()` 必须是
  同步的、不 await 的：一个 `await queue.put()` 在满载时会把 WebSocket 读循环整条卡住，
  表现出来就是"网页端看着还连着，但数据停格了"——最难查的那种故障。
- **每接入机一条队列**，不是每节点一条：一台接入机的字节速率有上限，按接入机排队天然隔离
  "起点塔打满不影响看台"；按节点排队则队列数随节点数线性增长，且每队都得重复解一次帧。
- 环形缓冲键 `(gateway_id, transport_addr, slot)`，每节点每 slot 只留最近 N 个点；
  总点数受 `MAX_GATEWAYS × MAX_NODES_PER_GATEWAY × slots × 深度` 约束，这个预算必须能读出来，
  否则长跑时"内存涨到哪里算涨完"没人能答。
"""

import asyncio

import pytest

from app.config import Settings
from app.services.store import GatewayQueue, SampleStore


# --- 有界队列 ---------------------------------------------------------------


def test_the_queue_holds_exactly_its_limit_without_dropping():
    queue = GatewayQueue(gateway_id=1, limit=4)
    assert [queue.offer(point) for point in range(4)] == ["queued"] * 4
    assert queue.depth == 4
    assert queue.dropped == 0


def test_a_full_queue_drops_the_oldest_and_the_new_one_lands():
    """丢的必须是最旧那一个：读侧要的永远是"现在"，不是"三秒前"。"""
    queue = GatewayQueue(gateway_id=1, limit=2)
    queue.offer(100)
    queue.offer(200)
    assert queue.offer(300) == "dropped_oldest"
    assert queue.dropped == 1
    assert queue.depth == 2
    assert [queue.get_nowait(), queue.get_nowait()] == [200, 300]


def test_the_dropped_counter_is_the_running_total_not_this_calls_result():
    queue = GatewayQueue(gateway_id=1, limit=1)
    for point in range(10):
        queue.offer(point)
    assert queue.dropped == 9
    assert queue.get_nowait() == 9


def test_offer_never_needs_awaiting():
    """这条就是对"禁止阻塞 ingest 读循环"的断言：`offer` 是普通同步调用，
    在事件循环内外都立刻返回——读循环里一旦出现 `await put()`，背压时卡住的是整条 WS。"""
    queue = GatewayQueue(gateway_id=1, limit=2)
    assert [queue.offer(1), queue.offer(2), queue.offer(3)] == [
        "queued",
        "queued",
        "dropped_oldest",
    ]
    # 队列此时仍是满的，所以又是一次丢最旧——这条要证明的是"在循环里也不挂起"。
    assert asyncio.run(_offer_inside_loop(queue)) == "dropped_oldest"


async def _offer_inside_loop(queue):
    return queue.offer(4)


def test_one_gateway_filling_up_leaves_another_untouched():
    """多接入机并跑：起点塔打满不能吃掉看台的配额，也不能碰它的计数。"""
    pit = GatewayQueue(gateway_id=1, limit=2)
    stand = GatewayQueue(gateway_id=2, limit=8)
    for point in range(10):
        pit.offer(point)
    for point in range(3):
        stand.offer(point)
    assert (pit.depth, pit.dropped) == (2, 8)
    assert (stand.depth, stand.dropped) == (3, 0)


def test_a_queue_can_be_replaced_without_losing_the_counters():
    """重连会新建一条队列，但 `dropped` 是这台接入机的累计事实，不能跟着清零。"""
    queue = GatewayQueue(gateway_id=1, limit=1)
    queue.offer(1)
    queue.offer(2)
    queue.replace()
    assert (queue.depth, queue.dropped) == (0, 1)
    queue.offer(3)
    assert queue.dropped == 1


def test_stats_are_what_health_reads():
    queue = GatewayQueue(gateway_id=7, limit=3)
    queue.offer(1)
    assert queue.stats() == {
        "gateway_id": 7,
        "limit": 3,
        "depth": 1,
        "dropped": 0,
        "peak": 1,
        "received": 1,
    }


# --- 环形缓冲 ---------------------------------------------------------------


def point(t_ms=1000, value=-60.0):
    return {"t_ms": t_ms, "value": value}


def store(depth=5, budget=1000):
    """两个数都必须显式给：一只不声明上界的环形缓冲，就是我们正在防的那种"有界"。"""
    return SampleStore(depth_per_slot=depth, budget_points=budget)


def test_a_series_keeps_only_the_last_n_points():
    s = store(depth=5)
    for n in range(9):
        s.push(1, "/dev/a", 0, point(t_ms=n))
    assert [row["t_ms"] for row in s.series(1, "/dev/a", 0)] == [4, 5, 6, 7, 8]


def test_slots_do_not_share_a_ring():
    s = store(depth=3)
    s.push(1, "/dev/a", 0, point())
    s.push(1, "/dev/a", 1, point(t_ms=222))
    assert s.series(1, "/dev/a", 0) == [point()]
    assert [row["t_ms"] for row in s.series(1, "/dev/a", 1)] == [222]


def test_the_same_addr_on_two_gateways_is_two_series():
    """键里带 gateway_id 就是为了这个：同名串口在两台机器上是两块板。"""
    s = store(depth=4)
    s.push(1, "/dev/widscan58-2", 0, point(t_ms=1))
    s.push(2, "/dev/widscan58-2", 0, point(t_ms=2))
    assert [row["t_ms"] for row in s.series(1, "/dev/widscan58-2", 0)] == [1]
    assert [row["t_ms"] for row in s.series(2, "/dev/widscan58-2", 0)] == [2]


def test_forgetting_a_node_releases_every_slot_it_held():
    s = store(depth=4, budget=64)
    for slot in range(8):
        s.push(1, "/dev/a", slot, point())
    assert s.points == 8
    assert s.forget(1, "/dev/a") == 8
    assert s.points == 0
    assert s.series(1, "/dev/a", 0) == []


def test_an_unknown_series_reads_empty_instead_of_raising():
    """趋势图问一块没有数据的板是常态：回空序列，不得抛。"""
    assert store().series(9, "/dev/none", 0) == []


def test_the_point_budget_is_the_documented_product():
    """§5："总点数受 MAX_GATEWAYS×MAX_NODES 约束"——这条必须算得出、读得到。"""
    settings = Settings(max_gateways=4, max_nodes_per_gateway=8)
    s = SampleStore.from_settings(settings, slots_per_node=8)
    assert s.budget_points == 4 * 8 * 8 * settings.ring_depth_per_slot


def test_exceeding_the_budget_evicts_the_stalest_series_and_says_so():
    """预算不是装饰：真超了要丢最旧并计数，否则长跑就是无界吃 RAM 的 OOM 剧本。

    这里是兜底——正常情况下节点数由 `max_nodes_per_gateway` 在 ingest 就卡住了，
    走到这条说明有调用方绕过了接入层，必须留下可见的数字而不是静默涨内存。
    """
    s = store(depth=2, budget=3)
    for slot in range(3):
        s.push(1, "/dev/a", slot, point(t_ms=slot))
    s.push(1, "/dev/a", 3, point(t_ms=3))
    assert s.points == 3
    assert s.dropped_points == 1
    # 被逐的是"最久没被写过"的那条序列，不是刚推进来的
    assert s.series(1, "/dev/a", 0) == []
    assert [row["t_ms"] for row in s.series(1, "/dev/a", 3)] == [3]


def test_a_hot_series_is_never_the_eviction_victim():
    """正在出数的槽位不能因为点数多就被逐——那等于把live 数据喂给了历史。"""
    s = store(depth=2, budget=4)
    for n in range(4):
        s.push(1, "/dev/a", 0, point(t_ms=n))  # 热点：一直往同一条写
    s.push(1, "/dev/a", 1, point(t_ms=100))
    s.push(1, "/dev/a", 2, point(t_ms=200))
    assert [row["t_ms"] for row in s.series(1, "/dev/a", 0)] == [2, 3]
    assert s.points == 4


def test_stats_report_the_live_footprint():
    s = store(depth=3, budget=10)
    s.push(1, "/dev/a", 0, point())
    assert s.stats() == {"series": 1, "points": 1, "budget": 10, "dropped_points": 0}

