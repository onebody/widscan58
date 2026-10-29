"""S7 插件运行时：`plugin_registry` + `plugin_state` + `plugin_log` + `plugin_finding`。

与 `004_events` 同一套防御式写法，列序/类型/索引照抄 `app/models.py` 的声明顺序——
`tests/test_migrations.py::test_create_all_path_and_alembic_upgrade_path_produce_the_same_schema`
比的是 PRAGMA 事实（列含 pk 标记、外键、唯一约束、索引名与列序），两条建库路径差一个列序就会红。

**草稿的 005 行只列了三张表**（方案 §3：`plugin_registry` + `plugin_state` + `plugin_log`），
这里加了第四张 `plugin_finding`。理由不是"顺手多建一张"：`SignalEvent` 的 docstring 明写
"写它的必须是采集层本身，不是推理——推理结果属 S7/S8 的表"，而 S8 才建 `alert`/`alert_rule`。
S7 的三支规则总得把判定落在某处，落在 `signal_event` 里就是把推导冒充成观测事实（任务书 §10.5
禁止的那件事），所以本阶段自有这一张；S8 的告警复核从它派生，不改它的行。

`plugin_registry.scope_id` **故意不建外键**：它指向 site/relay/gateway 哪张表由同一行的 `scope`
决定，多态指向没法建 FK，硬要建就会锁死其中一档。校验因此落在服务层（写 `scope` 时必须证明
`scope_id` 在那张表里真实存在），这条不是遗漏，是设计。

Revision ID: 005_plugins
Revises: 004_events
Create Date: 2026-10-30
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "005_plugins"
down_revision = "004_events"
branch_labels = None
depends_on = None

#: `(表名, 列与约束, 索引名 → 列)`。含义与 `004_events` 相同：索引单独列，因为 `create_all`
#: 那条路上索引由模型的 `__table_args__` 生成、顺序天然不同，对拍比的是"有没有、建在哪些列上"。
TABLES: list[tuple[str, list, dict[str, list[str]]]] = [
    (
        "plugin_registry",
        [
            sa.Column("plugin_id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("version", sa.String(length=32), nullable=False),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("scope", sa.String(length=16), nullable=False),
            sa.Column("scope_id", sa.Integer(), nullable=True),
            sa.Column("queue_limit", sa.Integer(), nullable=True),
            sa.Column("config_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("plugin_id"),
        ],
        {},
    ),
    (
        "plugin_state",
        [
            sa.Column("plugin_id", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_tick_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("errors", sa.Integer(), nullable=False),
            sa.Column("restarts", sa.Integer(), nullable=False),
            sa.Column("processed", sa.Integer(), nullable=False),
            sa.Column("produced", sa.Integer(), nullable=False),
            sa.Column("dropped", sa.Integer(), nullable=False),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("stats_json", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["plugin_id"], ["plugin_registry.plugin_id"], ondelete="RESTRICT"
            ),
            sa.PrimaryKeyConstraint("plugin_id"),
        ],
        {},
    ),
    (
        "plugin_log",
        [
            sa.Column("log_id", sa.Integer(), nullable=False),
            sa.Column("plugin_id", sa.String(length=64), nullable=False),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("level", sa.String(length=8), nullable=False),
            sa.Column("event", sa.String(length=32), nullable=False),
            sa.Column("message", sa.Text(), nullable=True),
            sa.Column("detail_json", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(
                ["plugin_id"], ["plugin_registry.plugin_id"], ondelete="RESTRICT"
            ),
            sa.PrimaryKeyConstraint("log_id"),
        ],
        {"ix_plugin_log_plugin_ts": ["plugin_id", "ts"]},
    ),
    (
        "plugin_finding",
        [
            sa.Column("finding_id", sa.Integer(), nullable=False),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("gateway_id", sa.Integer(), nullable=True),
            sa.Column("node_id", sa.Integer(), nullable=True),
            sa.Column("slot", sa.Integer(), nullable=True),
            sa.Column("plugin_id", sa.String(length=64), nullable=False),
            sa.Column("producer", sa.String(length=80), nullable=False),
            sa.Column("kind", sa.String(length=32), nullable=False),
            sa.Column("metric", sa.String(length=16), nullable=True),
            sa.Column("value", sa.Float(), nullable=True),
            sa.Column("threshold", sa.Float(), nullable=True),
            sa.Column("source", sa.String(length=8), nullable=False),
            sa.Column("evidence_json", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(["site_id"], ["site.site_id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(
                ["gateway_id"], ["gateway.gateway_id"], ondelete="RESTRICT"
            ),
            sa.ForeignKeyConstraint(["node_id"], ["node.node_id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(
                ["plugin_id"], ["plugin_registry.plugin_id"], ondelete="RESTRICT"
            ),
            sa.PrimaryKeyConstraint("finding_id"),
        ],
        {"ix_finding_site_ts": ["site_id", "ts"]},
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
