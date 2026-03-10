"""add rag metrics table

Revision ID: 8f4c9e3b7a2a
Revises: d02740717c5a
Create Date: 2026-03-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "8f4c9e3b7a2a"
down_revision: Union[str, None] = "d02740717c5a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rag_metrics",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=True),
        sa.Column("metric_type", sa.String(length=50), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_rag_metrics_conversation_id"), "rag_metrics", ["conversation_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_rag_metrics_conversation_id"), table_name="rag_metrics")
    op.drop_table("rag_metrics")
