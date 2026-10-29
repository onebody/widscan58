"""S6 观测数据：`signal_event`（一条观测事实）+ `sample_summary`（桶聚合的采样摘要）。

与 `002_layout` 同一套防御式写法：`create_all + stamp` 那条路已经把结构建好了，本脚本
在那台机器上一次都不会执行；直接 `alembic upgrade head` 那条路才需要它自己建。两条路
产出的结构必须逐 PRAGMA 事实相等（`tests/test_migrations.py` 钉这条），所以**列序与类型
要和 `app/models.py` 的声明顺序一模一样**。

序号说明：方案草稿写的是 `003_events`，但 `003_import` 已经占了那个号（见 `003_import`
的 docstring），链是线性的，所以这里是 `004_events`。方案 §3/§7 的表已按实建序号回写。

判据（方案 §3）：`(node_id, slot, ts_bucket)` 是摘要的唯一键，`(site_id, node_id, slot,
ts_bucket)` 是趋势取数的索引，且"按 `site_id` 过滤不得走全表扫"——索引存在不等于走了索引，
所以读侧另有一条 EXPLAIN QUERY PLAN 断言（`tests/test_samples.py`）。

Revision ID: 004_events
Revises: 003_import
Create Date: 2026-10-29
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "004_events"
down_revision = "003_import"
branch_labels = None
depends_on = None

#: `(表名, 列与约束, 索引名 → 列)`。索引和表分开列，是因为 `create_all` 那条路上
#: 索引由模型的 `__table_args__` 生成，顺序天然与这里不同；对拍比的是"有没有、建在哪些列上"。
TABLES: list[tuple[str, list, dict[str, list[str]]]] = [
    (
        "signal_event",
        [
            sa.Column("event_id", sa.Integer(), nullable=False),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("gateway_id", sa.Integer(), nullable=False),
            sa.Column("node_id", sa.Integer(), nullable=True),
            sa.Column("slot", sa.Integer(), nullable=True),
            sa.Column("freq_khz", sa.Integer(), nullable=True),
            sa.Column("kind", sa.String(length=32), nullable=False),
            sa.Column("metric", sa.String(length=16), nullable=True),
            sa.Column("value", sa.Float(), nullable=True),
            sa.Column("quality", sa.String(length=16), nullable=True),
            sa.Column("source", sa.String(length=8), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(["site_id"], ["site.site_id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(
                ["gateway_id"], ["gateway.gateway_id"], ondelete="RESTRICT"
            ),
            sa.ForeignKeyConstraint(["node_id"], ["node.node_id"], ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("event_id"),
        ],
        {"ix_event_site_ts": ["site_id", "ts"]},
    ),
    (
        "sample_summary",
        [
            sa.Column("node_id", sa.Integer(), nullable=False),
            sa.Column("slot", sa.Integer(), nullable=False),
            sa.Column("ts_bucket", sa.Integer(), nullable=False),
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("gateway_id", sa.Integer(), nullable=False),
            sa.Column("bucket_s", sa.Integer(), nullable=False),
            sa.Column("sample_count", sa.Integer(), nullable=False),
            sa.Column("ema_count", sa.Integer(), nullable=False),
            sa.Column("rssi_mean", sa.Float(), nullable=True),
            sa.Column("rssi_min", sa.Float(), nullable=True),
            sa.Column("rssi_max", sa.Float(), nullable=True),
            sa.Column("rssi_last", sa.Float(), nullable=True),
            sa.Column("ema_mean", sa.Float(), nullable=True),
            sa.Column("ema_min", sa.Float(), nullable=True),
            sa.Column("ema_max", sa.Float(), nullable=True),
            sa.Column("ema_last", sa.Float(), nullable=True),
            sa.Column("source", sa.String(length=8), nullable=False),
            sa.Column("last_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["node_id"], ["node.node_id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["site_id"], ["site.site_id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(
                ["gateway_id"], ["gateway.gateway_id"], ondelete="RESTRICT"
            ),
            # 草案那句"唯一键 (node_id, slot, ts_bucket)"直接做成复合主键：聚合写是幂等的，
            # 同一格同一桶重放必须落在同一行上，而不是多长出一行。
            sa.PrimaryKeyConstraint("node_id", "slot", "ts_bucket"),
        ],
        {"ix_summary_site_node_slot_bucket": ["site_id", "node_id", "slot", "ts_bucket"]},
    ),
]


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _table_names()
    for name, columns, indexes in TABLES:
        if name not in existing:
            op.create_table(name, *columns)
            existing = _table_names()
        have = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(name)}
        for index_name, index_columns in indexes.items():
            if index_name not in have:
                op.create_index(index_name, name, index_columns)


def downgrade() -> None:
    for name, _columns, indexes in reversed(TABLES):
        if name not in _table_names():
            continue
        have = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(name)}
        for index_name in indexes:
            if index_name in have:
                op.drop_index(index_name, table_name=name)
        op.drop_table(name)
