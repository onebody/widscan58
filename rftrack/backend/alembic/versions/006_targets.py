"""S8 探索与目标：`alert_rule` + `alert` + `candidate_target` + `track_observation`。

与 `004_events`/`005_plugins` 同一套防御式写法，列序/类型/索引照抄 `app/models.py` 的声明
顺序——`tests/test_migrations.py` 比 PRAGMA 事实，两条建库路径差一个列序就会红。

字段取舍的理由在 `models.py` 各类的 docstring 里（复核与算法分列、`uq_alert_dedup` 部分
唯一索引、目标表不设坐标列），这里不重复。`alert_rule.scope` 词表是四档
（`global/site/gateway/node`），草稿写五档含 `relay`——判它要 join 且退化无用例，与
`plugin_registry` 的 `EXECUTABLE_SCOPES` 同一结论，偏差在方案 §7 S8 行申报。

Revision ID: 006_targets
Revises: 005_plugins
Create Date: 2026-10-30
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "006_targets"
down_revision = "005_plugins"
branch_labels = None
depends_on = None

#: `(表名, 列与约束, 索引名 → (列, sqlite_where))`。`sqlite_where` 非空表示部分唯一索引——
#: `create_all` 路径由 `__table_args__` 生成它，upgrade 路径在这里显式写，两边必须一字不差。
TABLES: list[tuple[str, list, dict[str, tuple[list[str], str | None]]]] = [
    (
        "alert_rule",
        [
            sa.Column("rule_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("site_id", sa.Integer(), nullable=True),
            sa.Column("plugin_id", sa.String(length=64), nullable=False),
            sa.Column("kind", sa.String(length=32), nullable=False),
            sa.Column("scope", sa.String(length=16), nullable=False),
            sa.Column("scope_id", sa.Integer(), nullable=True),
            sa.Column("dedup_window_s", sa.Integer(), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["site_id"], ["site.site_id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(
                ["plugin_id"], ["plugin_registry.plugin_id"], ondelete="RESTRICT"
            ),
            sa.PrimaryKeyConstraint("rule_id"),
        ],
        {},
    ),
    (
        "alert",
        [
            sa.Column("alert_id", sa.Integer(), nullable=False),
            sa.Column("rule_id", sa.Integer(), nullable=False),
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("gateway_id", sa.Integer(), nullable=True),
            sa.Column("node_id", sa.Integer(), nullable=True),
            sa.Column("slot", sa.Integer(), nullable=True),
            sa.Column("kind", sa.String(length=32), nullable=False),
            sa.Column("metric", sa.String(length=16), nullable=True),
            sa.Column("value", sa.Float(), nullable=True),
            sa.Column("threshold", sa.Float(), nullable=True),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("count", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("source", sa.String(length=8), nullable=False),
            sa.Column("dedup_key", sa.String(length=160), nullable=False),
            sa.Column("evidence_json", sa.Text(), nullable=True),
            sa.Column("silenced_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("review_json", sa.Text(), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(
                ["rule_id"], ["alert_rule.rule_id"], ondelete="RESTRICT"
            ),
            sa.ForeignKeyConstraint(["site_id"], ["site.site_id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(
                ["gateway_id"], ["gateway.gateway_id"], ondelete="RESTRICT"
            ),
            sa.ForeignKeyConstraint(["node_id"], ["node.node_id"], ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("alert_id"),
        ],
        {
            "ix_alert_site_ts": (["site_id", "ts"], None),
            "uq_alert_dedup": (["dedup_key"], "status != 'resolved'"),
        },
    ),
    (
        "candidate_target",
        [
            sa.Column("target_id", sa.Integer(), nullable=False),
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("label", sa.String(length=120), nullable=True),
            sa.Column("kind", sa.String(length=32), nullable=False),
            sa.Column("producer", sa.String(length=80), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=True),
            sa.Column("confidence_basis", sa.String(length=160), nullable=True),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("review_status", sa.String(length=16), nullable=False),
            sa.Column("review_json", sa.Text(), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("coverage_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["site_id"], ["site.site_id"], ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("target_id"),
        ],
        {"ix_target_site_last": (["site_id", "last_seen_at"], None)},
    ),
    (
        "track_observation",
        [
            sa.Column("obs_id", sa.Integer(), nullable=False),
            sa.Column("target_id", sa.Integer(), nullable=False),
            sa.Column("finding_id", sa.Integer(), nullable=True),
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("gateway_id", sa.Integer(), nullable=True),
            sa.Column("node_id", sa.Integer(), nullable=True),
            sa.Column("slot", sa.Integer(), nullable=True),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("metric", sa.String(length=16), nullable=True),
            sa.Column("value", sa.Float(), nullable=True),
            sa.Column("source", sa.String(length=8), nullable=False),
            sa.Column("evidence_json", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(
                ["target_id"], ["candidate_target.target_id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["finding_id"], ["plugin_finding.finding_id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(["site_id"], ["site.site_id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(
                ["gateway_id"], ["gateway.gateway_id"], ondelete="RESTRICT"
            ),
            sa.ForeignKeyConstraint(["node_id"], ["node.node_id"], ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("obs_id"),
        ],
        {"ix_obs_target_ts": (["target_id", "ts"], None)},
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
        for index_name, (index_columns, where) in indexes.items():
            if index_name in have:
                continue
            if where is None:
                op.create_index(index_name, name, index_columns)
            else:
                op.create_index(
                    index_name, name, index_columns, unique=True, sqlite_where=sa.text(where)
                )


def downgrade() -> None:
    for name, _columns, indexes in reversed(TABLES):
        if name not in _table_names():
            continue
        have = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(name)}
        for index_name in indexes:
            if index_name in have:
                op.drop_index(index_name, table_name=name)
        op.drop_table(name)
