"""`local_settings_import`：原 `web/` SPA 的 localStorage 一次性导入留痕。

方案 §3 原本给它排的是 `006_import`，但 Alembic 的链是线性的：`003/004/005` 属 S6/S7/S8，
今天还不存在，此时建 `006` 就等于让未来的迁移往链中间插，只会多出分支与多头。
所以实建序号是 `003_import`，语义名不变，§3 表已同步回写。

判据（方案 §3 / §7 S4 回滚列）：**导入可回滚**——原始 payload 与每个落点的新旧值都留在
这一行里，`reverted_at` 只记时刻、不删行；把留痕改没了，下一次就无从对照。

Revision ID: 003_import
Revises: 002_layout
Create Date: 2026-10-29
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "003_import"
down_revision = "002_layout"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "local_settings_import" in existing:
        return
    op.create_table(
        "local_settings_import",
        sa.Column("import_id", sa.Integer(), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("changes_json", sa.Text(), nullable=True),
        sa.Column("unmapped_json", sa.Text(), nullable=True),
        sa.Column("reverted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("import_id"),
    )


def downgrade() -> None:
    if "local_settings_import" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("local_settings_import")
