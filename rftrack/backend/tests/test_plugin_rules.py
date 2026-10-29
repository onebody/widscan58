"""S7 三支内置规则插件的**判定规则**本身（运行时/队列/启停在 `test_plugins.py`）。

这个文件只回答一个问题：**给定若干帧与生效配置，这支插件该不该说话、说什么**。所以它不需要
数据库、不需要网络、不需要 FastAPI——插件的输入是 `FrameBatch`（采集层已经归属好的点批），
输出是 `Finding`（还没写库的判定）。把这一层单独可测的理由是任务书 §10.5 那条硬边界：
"不得把 RSSI 说成距离/方向/坐标/真实轨迹"——那必须能在没有一帧真机数据的情况下被证伪。

三条纪律在这里都有对应的测试：

- **不说没依据的话**：基线没攒够就不产出（不是"第一帧就异常"）。
- **同一件事只说一次**：上升沿触发，持续抬高不再重复产出——每帧一条判定就是给自己制造写放大，
  而 SD 卡只有那一张（R11）。
- **场地与格子之间不串**：A 节点的基线不告诉 B 节点，也不告诉 A 节点的另一个信道格子。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.plugins.alerting import AlertEngine
from app.plugins.anomaly import AnomalyDetector
from app.plugins.base import FrameBatch, SlotPoint
from app.plugins.interference import InterferenceCorrelator

CONFIG = {"anomaly_rise_db": 10.0}
BASELINE_WINDOW = 16  # 与 `anomaly.py` 的常数同值；这里显式写出来，改一处就会红。

FORBIDDEN_EVIDENCE_KEYS = {
    "distance_m",
    "distance",
    "direction_deg",
    "direction",
    "bearing",
    "latitude",
    "longitude",
    "lat",
    "lon",
    "track",
    "confidence",
}


def moment(offset_ms: int = 0) -> datetime:
    return datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc) + timedelta(
        milliseconds=offset_ms
    )


def batch(rssi_by_slot: dict[int, float], *, node_id: int = 1, offset_ms: int = 0,
          site_id: int = 1, gateway_id: int = 1, source: str = "real") -> FrameBatch:
    return FrameBatch(
        site_id=site_id,
        gateway_id=gateway_id,
        node_id=node_id,
        source=source,
        ts=moment(offset_ms),
        seq=offset_ms,
        points=tuple(
            SlotPoint(slot=slot, rssi=rssi, ema=rssi) for slot, rssi in rssi_by_slot.items()
        ),
    )


def feed_frames(plugin, rssi: float, *, count: int, node_id: int = 1, slot: int = 0,
                start_ms: int = 0) -> list:
    findings = []
    for index in range(count):
        findings.extend(
            plugin.feed(
                batch({slot: rssi}, node_id=node_id, offset_ms=start_ms + index * 100),
                CONFIG,
            )
        )
    return findings


# --- anomaly-detector --------------------------------------------------------


def test_anomaly_stays_silent_until_its_own_baseline_is_full():
    """从第一帧就是 -70 的格子不许被判成"抬高了"：没有基线就没有"相对"，这话只能是编的。"""
    plugin = AnomalyDetector()
    assert feed_frames(plugin, -70.0, count=BASELINE_WINDOW + 4) == []


def test_anomaly_fires_when_a_slot_rises_over_its_quiet_baseline():
    """攒满基线后突然抬高 25 dB → 一条 `slot_rise`，证据里能看见基线与抬升量。"""
    plugin = AnomalyDetector()
    feed_frames(plugin, -95.0, count=BASELINE_WINDOW)
    findings = feed_frames(plugin, -70.0, count=1, start_ms=BASELINE_WINDOW * 100)

    assert [f.kind for f in findings] == ["slot_rise"]
    assert findings[0].slot == 0
    assert findings[0].metric == "rssi"
    assert findings[0].value == pytest.approx(-70.0)
    assert findings[0].threshold == pytest.approx(10.0)
    assert findings[0].evidence["baseline_rssi"] == pytest.approx(-95.0)
    assert findings[0].evidence["rise_db"] == pytest.approx(25.0)
    assert findings[0].evidence["window"] == BASELINE_WINDOW


def test_anomaly_does_not_refire_while_the_level_stays_up():
    """持续抬高只说一次：每帧一条判定等于把写放大从摘要层搬到判定层（R11 同一件事）。"""
    plugin = AnomalyDetector()
    feed_frames(plugin, -95.0, count=BASELINE_WINDOW)
    findings = feed_frames(plugin, -70.0, count=12, start_ms=BASELINE_WINDOW * 100)
    assert [f.kind for f in findings] == ["slot_rise"]


def test_anomaly_rearms_after_the_slot_quiet_down_again():
    """回到安静一侧再抬高，是**第二次**事件，就该再说一次。"""
    plugin = AnomalyDetector()
    feed_frames(plugin, -95.0, count=BASELINE_WINDOW)
    feed_frames(plugin, -70.0, count=1, start_ms=BASELINE_WINDOW * 100)
    second = feed_frames(plugin, -95.0, count=BASELINE_WINDOW, start_ms=3_000)
    third = feed_frames(plugin, -70.0, count=1, start_ms=3_000 + BASELINE_WINDOW * 100)

    assert second == []
    assert [f.kind for f in third] == ["slot_rise"]


def test_anomaly_keeps_a_separate_baseline_per_node_and_per_slot():
    """节点 1 的格子 0 抬高，不该让节点 2 或格子 1 说话——串了就不是"这块板"的判定了。"""
    plugin = AnomalyDetector()
    feed_frames(plugin, -95.0, count=BASELINE_WINDOW)
    feed_frames(plugin, -95.0, count=BASELINE_WINDOW, node_id=2)
    feed_frames(plugin, -95.0, count=BASELINE_WINDOW, slot=1)

    assert feed_frames(plugin, -70.0, count=1, node_id=2, slot=1, start_ms=2_000) == []
    findings = feed_frames(plugin, -70.0, count=1, start_ms=2_100)
    assert [f.kind for f in findings] == ["slot_rise"]


def test_anomaly_rise_threshold_is_read_from_config_not_hardcoded():
    """`anomaly_rise_db=20` 时抬 15 dB 不算事；同一份读数在 5 的配置下就算——阈值属配置层。"""
    plugin = AnomalyDetector()
    quiet = batch({0: -95.0})
    for _ in range(BASELINE_WINDOW):
        plugin.feed(quiet, {"anomaly_rise_db": 5.0})
    high = {"anomaly_rise_db": 20.0}
    assert plugin.feed(batch({0: -80.0}, offset_ms=1_000), high) == []
    assert [f.kind for f in plugin.feed(batch({0: -80.0}, offset_ms=1_100), {"anomaly_rise_db": 5.0})] == [
        "slot_rise"
    ]


def test_anomaly_evidence_carries_no_position_or_confidence_claim():
    """任务书 §10.5：不得把 RSSI 说成距离/方向/坐标/置信度——连字段名都不许出现。"""
    plugin = AnomalyDetector()
    feed_frames(plugin, -95.0, count=BASELINE_WINDOW)
    findings = feed_frames(plugin, -70.0, count=1, start_ms=BASELINE_WINDOW * 100)
    assert findings
    assert FORBIDDEN_EVIDENCE_KEYS.isdisjoint(findings[0].evidence)


def test_anomaly_state_is_bounded_and_says_how_much_it_evicted():
    """状态按 (节点, 格子) 攒，条数必须可上界；到顶丢最老的，并把丢了多少报出来。

    没有这条，一块板被换过几百次的场地就会把内存当历史用——而 Pi 上可用内存只有约 460 MB。
    """
    plugin = AnomalyDetector(capacity=4)
    for node_id in range(6):
        plugin.feed(batch({0: -95.0}, node_id=node_id), CONFIG)

    assert plugin.stats()["tracked"] == 4
    assert plugin.stats()["evicted"] == 2


# --- interference-correlator --------------------------------------------------

CORRELATE = {"threshold_db": -85.0, "interference_min_nodes": 2, "interference_window_s": 5}
#: 帧间隔 100 ms，所以 `5 s` 的窗口就是 50 帧：测试里靠它决定"上一条还在不在窗口内"。
FRAME_MS = 100


def feed_over(plugin, config, *, node_id: int, slot: int = 0, rssi: float = -60.0,
              count: int = 1, start_ms: int = 0, site_id: int = 1) -> list:
    findings = []
    for index in range(count):
        findings.extend(
            plugin.feed(
                batch(
                    {slot: rssi},
                    node_id=node_id,
                    site_id=site_id,
                    offset_ms=start_ms + index * FRAME_MS,
                ),
                config,
            )
        )
    return findings


def test_interference_needs_two_distinct_nodes_above_the_effective_threshold():
    """一台越限只是"这台板看到了强信号"，两台同时越限才谈得上"像是同一篇干扰"。"""
    plugin = InterferenceCorrelator()
    assert feed_over(plugin, CORRELATE, node_id=1) == []
    assert [f.kind for f in feed_over(plugin, CORRELATE, node_id=2)] == [
        "multi_node_interference"
    ]


def test_two_slots_on_the_same_node_are_not_two_nodes():
    """一块板八个格子同时抬高是**这块板**的事，不是"多节点相关"——按节点去重才有意义。"""
    plugin = InterferenceCorrelator()
    feed_over(plugin, CORRELATE, node_id=1, slot=0)
    assert feed_over(plugin, CORRELATE, node_id=1, slot=1, start_ms=FRAME_MS) == []


def test_interference_stays_under_the_threshold():
    """-90 在 `-85` 的配置下不算越限；把生效阈值改成 -95 就是越限——数值来自配置层。"""
    plugin = InterferenceCorrelator()
    assert feed_over(plugin, CORRELATE, node_id=1, rssi=-90.0) == []
    assert feed_over(plugin, CORRELATE, node_id=2, rssi=-90.0, start_ms=FRAME_MS) == []

    loose = {**CORRELATE, "threshold_db": -95.0}
    other = InterferenceCorrelator()
    feed_over(other, loose, node_id=1, rssi=-90.0)
    assert [f.kind for f in feed_over(other, loose, node_id=2, rssi=-90.0, start_ms=FRAME_MS)] == [
        "multi_node_interference"
    ]


def test_interference_ignores_readings_that_fell_out_of_the_window():
    """窗口 5 s：相隔 10 s 的两次越限不该被说成"同时"——时间轴用的是观测时刻，不是墙钟。"""
    plugin = InterferenceCorrelator()
    feed_over(plugin, CORRELATE, node_id=1)
    assert feed_over(plugin, CORRELATE, node_id=2, start_ms=10_000) == []


def test_interference_does_not_reach_across_sites():
    """A 场地两台越限才是 A 场地的事；B 场地那台再响也不能替 A 场地凑数（§5 场地隔离）。"""
    plugin = InterferenceCorrelator()
    feed_over(plugin, CORRELATE, node_id=1)
    assert feed_over(plugin, CORRELATE, node_id=2, site_id=2, start_ms=FRAME_MS) == []
    assert plugin.stats()["sites"] == 2


def test_interference_fires_once_while_the_condition_holds_and_rearms_after_quiet():
    """条件成立期间只报一次；掉回一下再成立才是第二次事件（与 anomaly 同一条上升沿纪律）。"""
    plugin = InterferenceCorrelator()
    feed_over(plugin, CORRELATE, node_id=1)
    fired = feed_over(plugin, CORRELATE, node_id=2)
    assert [f.kind for f in fired] == ["multi_node_interference"]

    # 两台都继续抬高：条件一直成立，不再重复产出。
    again = feed_over(plugin, CORRELATE, node_id=1, count=3, start_ms=2 * FRAME_MS)
    again += feed_over(plugin, CORRELATE, node_id=2, count=3, start_ms=2 * FRAME_MS)
    assert again == []

    # 只有节点 1 还在响，节点 2 的读数掉出窗口 → 条件不再成立；节点 2 回来就是第二次。
    feed_over(plugin, CORRELATE, node_id=1, count=60, start_ms=3 * FRAME_MS)
    rearmed = feed_over(plugin, CORRELATE, node_id=2, start_ms=9_000)
    assert [f.kind for f in rearmed] == ["multi_node_interference"]


def test_interference_evidence_names_the_nodes_and_their_readings_only():
    """证据要能点开看：哪些节点、各自最高读到多少、生效阈值与窗口——**不含**距离/方向/坐标。"""
    plugin = InterferenceCorrelator()
    feed_over(plugin, CORRELATE, node_id=1, rssi=-70.0)
    findings = feed_over(plugin, CORRELATE, node_id=2, rssi=-60.0, start_ms=FRAME_MS)
    assert len(findings) == 1

    evidence = findings[0].evidence
    assert findings[0].threshold == -85.0
    assert {node["node_id"] for node in evidence["nodes"]} == {1, 2}
    assert {node["max_rssi"] for node in evidence["nodes"]} == {-70.0, -60.0}
    assert evidence["window_s"] == 5
    assert FORBIDDEN_EVIDENCE_KEYS.isdisjoint(evidence)
    for node in evidence["nodes"]:
        assert FORBIDDEN_EVIDENCE_KEYS.isdisjoint(node)


def test_interference_state_is_bounded_per_site():
    """每个场地的在窗读数条数有上界：打满丢最老的，并把丢掉的条数报出来。"""
    plugin = InterferenceCorrelator(capacity=3)
    for index in range(5):
        feed_over(plugin, CORRELATE, node_id=index + 1, start_ms=index * FRAME_MS)
    assert plugin.stats()["dropped"] >= 2



# --- alert-engine -------------------------------------------------------------

ALERT = {"threshold_db": -85.0, "alert_hold_s": 30}
SECOND_MS = 1_000


def test_alert_candidate_marks_the_slot_that_crossed():
    """绝对越限是 `alert-engine` 的活：越过生效 `threshold_db` 就成一条候选，带上格子与数值。"""
    plugin = AlertEngine()
    findings = feed_over(plugin, ALERT, node_id=1, slot=2, rssi=-60.0)

    assert [f.kind for f in findings] == ["threshold_crossing"]
    assert findings[0].slot == 2
    assert findings[0].metric == "rssi"
    assert findings[0].value == -60.0
    assert findings[0].threshold == -85.0


def test_no_candidate_below_the_effective_threshold():
    """-90 在 `-85` 下不越限；把生效阈值改成 -95 就越——数值只在配置层有一份。"""
    plugin = AlertEngine()
    assert feed_over(plugin, ALERT, node_id=1, rssi=-90.0) == []
    loose = {**ALERT, "threshold_db": -95.0}
    assert [f.kind for f in feed_over(plugin, loose, node_id=1, rssi=-90.0,
                                     start_ms=FRAME_MS)] == ["threshold_crossing"]


def test_the_hold_window_suppresses_repeats_of_the_same_slot():
    """同一格在抑制窗内只留一条：跳频下每秒一帧，不设抑制就是把判定表当成第二个摘要表写（R11）。"""
    plugin = AlertEngine()
    findings = feed_over(plugin, ALERT, node_id=1, rssi=-60.0, count=30)
    assert [f.kind for f in findings] == ["threshold_crossing"]


def test_a_fresh_candidate_after_the_hold_expires():
    """抑制窗过期后的再次越限是新的一条——"还在响"与"又响了一次"在页面上是两件事。"""
    plugin = AlertEngine()
    feed_over(plugin, ALERT, node_id=1, rssi=-60.0)
    findings = feed_over(plugin, ALERT, node_id=1, rssi=-60.0, start_ms=31 * SECOND_MS)
    assert [f.kind for f in findings] == ["threshold_crossing"]


def test_the_hold_is_per_node_and_per_slot():
    """节点 1 的格子 0 在抑制窗内，不该顺带压住格子 1 或另一块板的同一格子。"""
    plugin = AlertEngine()
    feed_over(plugin, ALERT, node_id=1, slot=0, rssi=-60.0)
    assert [f.kind for f in feed_over(plugin, ALERT, node_id=1, slot=1, rssi=-60.0,
                                     start_ms=FRAME_MS)] == ["threshold_crossing"]
    assert [f.kind for f in feed_over(plugin, ALERT, node_id=2, slot=0, rssi=-60.0,
                                     start_ms=2 * FRAME_MS)] == ["threshold_crossing"]


def test_alert_candidate_evidence_carries_the_reading_and_no_position():
    """证据里要有 ema 与 seq：rssi 毛刺而 ema 没跟上，是运营者分辨"闪一下"与"持续压着"的依据。"""
    plugin = AlertEngine()
    findings = feed_over(plugin, ALERT, node_id=1, slot=3, rssi=-60.0)
    evidence = findings[0].evidence

    assert evidence["ema"] == -60.0
    assert evidence["seq"] == 0
    assert FORBIDDEN_EVIDENCE_KEYS.isdisjoint(evidence)


def test_alert_state_is_bounded_and_counts_what_it_dropped():
    """抑制状态按 (节点, 格子) 攒，条数有上界；到顶丢最老的并计数。"""
    plugin = AlertEngine(capacity=2)
    for index in range(4):
        feed_over(plugin, ALERT, node_id=index, slot=0, rssi=-60.0, start_ms=index * FRAME_MS)

    assert plugin.stats()["tracked"] == 2
    assert plugin.stats()["evicted"] == 2


# --- 阈值边界：三支插件对"算不算越过"的定义各自钉死 ----------------------------


def test_alert_exactly_at_the_threshold_is_not_a_crossing():
    """"越过"取严格高于：正好等于生效阈值的那一帧不算候选。定义必须写下来，否则两支插件
    会对同一帧给出不一致的说法，而页面上看不出哪个是"官方口径"。"""
    plugin = AlertEngine()
    assert feed_over(plugin, ALERT, node_id=1, rssi=-85.0) == []
    assert [f.kind for f in feed_over(plugin, ALERT, node_id=1, rssi=-84.9,
                                     start_ms=FRAME_MS)] == ["threshold_crossing"]


def test_interference_exactly_at_the_threshold_does_not_count():
    """与 `alert-engine` 同一条严格高于口径：等于阈值不进入相关窗口。"""
    plugin = InterferenceCorrelator()
    feed_over(plugin, CORRELATE, node_id=1, rssi=-85.0)
    assert feed_over(plugin, CORRELATE, node_id=2, rssi=-85.0, start_ms=FRAME_MS) == []


def test_anomaly_a_rise_exactly_equal_to_the_configured_rise_does_count():
    """相对规则取"达到即算"：抬到正好 `anomaly_rise_db` 就是这次事件本身，不必再多半 dB。"""
    plugin = AnomalyDetector()
    feed_frames(plugin, -95.0, count=BASELINE_WINDOW)
    assert [f.kind for f in feed_frames(plugin, -85.0, count=1,
                                       start_ms=BASELINE_WINDOW * 100)] == ["slot_rise"]


# --- 契约：登记的插件、真实实现、插件读的键，三者必须对得上 --------------------


def test_every_builtin_plugin_declared_in_the_registry_has_an_implementation():
    """`BUILTIN_PLUGINS` 里登记了的，`IMPLEMENTATIONS` 里就得有——少一个就是"页面有一支不会动的插件"。

    这一条是"不做动态代码加载"的正面保证：两边的集合只能由代码同时改，没有任何运行期发现机制
    能把它们错开，所以集合相等是可达成的、也是必须钉住的。
    """
    from app.plugins import IMPLEMENTATIONS
    from app.services.registry import BUILTIN_PLUGINS

    assert sorted(IMPLEMENTATIONS) == sorted(item.plugin_id for item in BUILTIN_PLUGINS)


def test_every_implementation_consumes_the_same_call_shape():
    """三支实现都构造得起、都吃得下同一个 `feed(batch, config)` 并回数——运行时只按这一个面调它们。"""
    from app.plugins import IMPLEMENTATIONS

    config = {
        "anomaly_rise_db": 10.0,
        "threshold_db": -85.0,
        "interference_min_nodes": 2,
        "interference_window_s": 5,
        "alert_hold_s": 30,
    }
    quiet = batch({slot: -95.0 for slot in range(8)})
    hot = batch({slot: -40.0 for slot in range(8)}, node_id=2, offset_ms=1_000)

    for plugin_id, cls in sorted(IMPLEMENTATIONS.items()):
        plugin = cls()
        assert isinstance(plugin.feed(quiet, config), list), plugin_id
        assert isinstance(plugin.feed(hot, config), list), plugin_id
        assert isinstance(plugin.stats(), dict), plugin_id


def test_every_config_key_a_plugin_reads_is_declared_in_the_config_layer():
    """插件从 `config` 里读的每个键都必须在键注册表里、并且有平台默认值。

    运行时的取值是"按键取生效值"：键不在注册表里就等于插件运行期 `KeyError`，而崩掉的是一支
    插件（不是采集）——那正是 supervisor 要拦的事，但更便宜的是压根不让它发生。
    """
    from app.plugins import IMPLEMENTATIONS
    from app.services import config_layer

    needed = {
        key
        for cls in IMPLEMENTATIONS.values()
        for key in cls.CONFIG_KEYS
    }
    assert needed, "插件一个键都不读，说明接线没做"

    for key in sorted(needed):
        spec = config_layer.spec_for(key)
        assert spec is not None, f"{key} 没有进键注册表"
        assert config_layer.platform_default(Settings(), spec) is not None, (
            f"{key} 没有平台默认值，插件运行期会拿到 None"
        )
        assert "site" in spec.layers, f"{key} 不能按场地覆写，与场地分级调参的口径不符"


def test_the_three_rules_read_different_keys():
    """"相对离群"与"绝对越限"必须是**不同**的键：共用一条阈值就等于两支插件说同一句话。"""
    from app.plugins import IMPLEMENTATIONS

    assert IMPLEMENTATIONS["anomaly-detector"].CONFIG_KEYS == ("anomaly_rise_db",)
    assert "threshold_db" in IMPLEMENTATIONS["alert-engine"].CONFIG_KEYS
    assert "anomaly_rise_db" not in IMPLEMENTATIONS["alert-engine"].CONFIG_KEYS
