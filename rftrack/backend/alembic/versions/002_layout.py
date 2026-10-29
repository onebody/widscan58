"""布局与实例局部配置：`workbench_layout` + `layout_widget`，外加两处"全局/场地覆写"列。

与 `001_baseline` 同一套防御式写法：`create_all + stamp` 那条路已经把结构建好了，本脚本
在那台机器上一次都不会执行；直接 `alembic upgrade head` 那条路才需要它自己建。两条路
产出的结构必须逐 PRAGMA 事实相等（`tests/test_migrations.py` 钉这条），所以列序与类型
要和 `app/models.py` 的声明顺序一模一样。

Revision ID: 002_layout
Revises: 001_baseline
Create Date: 2026-10-29
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "002_layout"
down_revision = "001_baseline"
branch_labels = None
depends_on = None

#: 追加到已有表末尾的可空覆写列。顺序即 `app/models.py` 里的声明顺序——列序变了，
#: 两条建库路径的 `PRAGMA table_info` 就会不一致，而那是对拍测试的判据。
ADDED_COLUMNS: dict[str, list[tuple[str, sa.types.TypeEngine]]] = {
    "app_config": [
        ("retention_events_days", sa.Integer()),
        ("retention_summary_days", sa.Integer()),
        ("summary_bucket_s", sa.Integer()),
        ("rawiq_persist", sa.Boolean()),
        ("current_site_id", sa.Integer()),
        ("settings_json", sa.Text()),
    ],
    "site": [("settings_json", sa.Text())],
}

TABLES: list[tuple[str, list]] = [
    (
        "workbench_layout",
        [
            sa.Column("layout_id", sa.Integer(), nullable=False),
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("relay_id", sa.Integer(), nullable=True),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("defaults_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["site_id"], ["site.site_id"], ondelete="RESTRICT"
            ),
            sa.ForeignKeyConstraint(
                ["relay_id"], ["relay.relay_id"], ondelete="RESTRICT"
            ),
            sa.PrimaryKeyConstraint("layout_id"),
            sa.UniqueConstraint("site_id", "name", name="uq_layout_site_name"),
        ],
    ),
    (
        "layout_widget",
        [
            sa.Column("instance_id", sa.String(length=120), nullable=False),
            sa.Column("layout_id", sa.Integer(), nullable=False),
            sa.Column("plugin_id", sa.String(length=120), nullable=False),
            sa.Column("pos_x", sa.Integer(), nullable=False),
            sa.Column("pos_y", sa.Integer(), nullable=False),
            sa.Column("pos_w", sa.Integer(), nullable=False),
            sa.Column("pos_h", sa.Integer(), nullable=False),
            sa.Column("config_json", sa.Text(), nullable=True),
            sa.Column("sort", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["layout_id"], ["workbench_layout.layout_id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("instance_id", "layout_id"),
        ],
    ),
]


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _column_names(table: str) -> set[str]:
    bind = op.get_bind()
    if table not in _table_names():
        return set()
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    for table, columns in ADDED_COLUMNS.items():
        have = _column_names(table)
        for name, column_type in columns:
            if name in have:
                continue
            # 只加可空列：有数据的库上任何"带默认值的非空列"都要回填，那是另一码事。
            op.add_column(table, sa.Column(name, column_type, nullable=True))

    existing = _table_names()
    for name, columns in TABLES:
        if name in existing:
            continue
        op.create_table(name, *columns)

    # 每场地只有一个"当前布局"：部分唯一索引把这条规则放进库里，而不是放进应用层。
    indexes = {
        i["name"] for i in sa.inspect(op.get_bind()).get_indexes("workbench_layout")
    }
    if "uq_layout_active_site" not in indexes:
        op.create_index(
            "uq_layout_active_site",
            "workbench_layout",
            ["site_id"],
            unique=True,
            sqlite_where=sa.text("is_active = 1"),
        )


def downgrade() -> None:
    indexes = {
        i["name"]
        for i in sa.inspect(op.get_bind()).get_indexes("workbench_layout")
        if "workbench_layout" in _table_names()
    }
    if "uq_layout_active_site" in indexes:
        op.drop_index("uq_layout_active_site", table_name="workbench_layout")
    for name, _columns in reversed(TABLES):
        if name in _table_names():
            op.drop_table(name)
    for table, columns in ADDED_COLUMNS.items():
        have = _column_names(table)
        for name, _column_type in reversed(columns):
            if name in have:
                op.drop_column(table, name)
