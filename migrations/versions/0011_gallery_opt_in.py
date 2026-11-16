"""gallery opt in

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-17 01:59:03.963354
"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op


revision: str = '0011'
down_revision: str | None = '0010'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A plain ALTER TABLE: batch mode would rebuild "business", which fails on a database with
    # rows because other tables reference it.
    op.add_column(
        "business",
        sa.Column("gallery_opt_in", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("business", "gallery_opt_in")
