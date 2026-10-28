"""基线：层级四表（site/relay/gateway/node）+ 实例设置 + 频道方案。

防御式写法：每张表先查存在性，已存在就跳过——首次部署走的是
migrate.py 的 create_all + stamp 场景，本脚本是"直接 alembic upgrade head"
那条路的等价物，两条路都可能在同一台机器上先后执行过。

Revision ID: 001_baseline
Revises:
Create Date: 2026-10-29
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


TABLES: list[tuple[str, list]] = [
    # app_config：实例级全局设置，约定只用 id=1 这一行。instance_role 为空时按配置兜底为 site。
    (
        "app_config",
        [
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("instance_role", sa.String(length=16), nullable=True),
            sa.Column("theme", sa.String(length=16), nullable=True),
            sa.Column("lang", sa.String(length=8), nullable=True),
            sa.Column("tz", sa.String(length=64), nullable=True),
            sa.Column("units", sa.String(length=16), nullable=True),
            sa.Column("default_refresh_ms", sa.Integer(), nullable=True),
            sa.Column("alert_defaults_json", sa.Text(), nullable=True),
            sa.Column("updated_by", sa.String(length=64), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        ],
    ),
    # site：场地。origin=aggregated 时 last_sync_at/lag_s 必须有值（R19 的判据）。
    (
        "site",
        [
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("slug", sa.String(length=64), nullable=False),
            sa.Column("timezone", sa.String(length=64), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("retention_events_days", sa.Integer(), nullable=True),
            sa.Column("retention_summary_days", sa.Integer(), nullable=True),
            sa.Column("origin", sa.String(length=16), nullable=False),
            sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("lag_s", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("site_id"),
            sa.UniqueConstraint("slug", name="uq_site_slug"),
        ],
    ),
    # relay：物理位置；同名只在同场地内互斥，跨场地同名是正常情况。
    (
        "relay",
        [
            sa.Column("relay_id", sa.Integer(), nullable=False),
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["site_id"], ["site.site_id"], ondelete="RESTRICT"
            ),
            sa.PrimaryKeyConstraint("relay_id"),
            sa.UniqueConstraint("site_id", "name", name="uq_relay_site_name"),
        ],
    ),
    # gateway：一台跑 serve.mjs 的机器；dial_state 为空表示从未拨过。
    (
        "gateway",
        [
            sa.Column("gateway_id", sa.Integer(), nullable=False),
            sa.Column("relay_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column("base_url", sa.String(length=255), nullable=True),
            sa.Column("serial_prefix", sa.String(length=64), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("dial_state", sa.String(length=16), nullable=True),
            sa.Column("dial_reason", sa.String(length=32), nullable=True),
            sa.Column("dial_failures", sa.Integer(), nullable=False),
            sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(
                ["relay_id"], ["relay.relay_id"], ondelete="RESTRICT"
            ),
            sa.PrimaryKeyConstraint("gateway_id"),
            sa.UniqueConstraint("relay_id", "name", name="uq_gateway_relay_name"),
        ],
    ),
    # node：节点唯一键是 (gateway_id, transport_addr)：同型号两块板在两个场地都叫 widscan58-2 是正常的。
    (
        "node",
        [
            sa.Column("node_id", sa.Integer(), nullable=False),
            sa.Column("gateway_id", sa.Integer(), nullable=False),
            sa.Column("display_name", sa.String(length=120), nullable=False),
            sa.Column("transport_kind", sa.String(length=24), nullable=False),
            sa.Column("transport_addr", sa.String(length=191), nullable=False),
            sa.Column("capabilities_json", sa.Text(), nullable=True),
            sa.Column("fw_version", sa.String(length=64), nullable=True),
            sa.Column("protocol_version", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
            sa.Column("stops_rh", sa.Boolean(), nullable=False),
            sa.ForeignKeyConstraint(
                ["gateway_id"], ["gateway.gateway_id"], ondelete="RESTRICT"
            ),
            sa.PrimaryKeyConstraint("node_id"),
            sa.UniqueConstraint(
                "gateway_id", "transport_addr", name="uq_node_gateway_addr"
            ),
        ],
    ),
    # channel_plan：频道方案；relay_id 为空 = 全场共享。
    (
        "channel_plan",
        [
            sa.Column("plan_id", sa.Integer(), nullable=False),
            sa.Column("relay_id", sa.Integer(), nullable=True),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("band", sa.String(length=16), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["relay_id"], ["relay.relay_id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("plan_id"),
        ],
    ),
    # channel_plan_item：freq_khz 统一 kHz 整数，消掉 web 侧 MHz / 固件侧 kHz 的口径分裂。
    (
        "channel_plan_item",
        [
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("plan_id", sa.Integer(), nullable=False),
            sa.Column("slot_index", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("freq_khz", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(
                ["plan_id"], ["channel_plan.plan_id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("item_id"),
            sa.UniqueConstraint("plan_id", "slot_index", name="uq_plan_slot"),
        ],
    ),
]


def upgrade() -> None:
    existing = _tables()
    for name, columns in TABLES:
        if name in existing:
            continue
        op.create_table(name, *columns)


def downgrade() -> None:
    existing = _tables()
    for name, _columns in reversed(TABLES):
        if name in existing:
            continue
        op.drop_table(name)

