"""site version numbers unique per business

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-17 09:00:00.000000

Two publishes at the same moment can never both save version N for one business.
"""
from collections.abc import Sequence

from alembic import op


revision: str = '0014'
down_revision: str | None = '0013'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('siteversion', schema=None) as batch_op:
        batch_op.create_unique_constraint(
            'uq_siteversion_business_number', ['business_id', 'number']
        )


def downgrade() -> None:
    with op.batch_alter_table('siteversion', schema=None) as batch_op:
        batch_op.drop_constraint('uq_siteversion_business_number', type_='unique')
