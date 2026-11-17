"""business ids never reused, owner session epoch

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-17 08:00:00.000000

Rebuilds the business table with AUTOINCREMENT, so a deleted business's id is never given to a
new one, and adds session_epoch (a random value per business, checked on every owner request).
Every existing business gets its own epoch; owners simply sign in again once.
"""
import secrets
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op


revision: str = '0013'
down_revision: str | None = '0012'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('business', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('session_epoch', sqlmodel.sql.sqltypes.AutoString(length=32), nullable=True)
        )
    connection = op.get_bind()
    business = sa.table('business', sa.column('id', sa.Integer), sa.column('session_epoch'))
    for (business_id,) in connection.execute(sa.select(business.c.id)).all():
        connection.execute(
            business.update()
            .where(business.c.id == business_id)
            .values(session_epoch=secrets.token_hex(16))
        )
    with op.batch_alter_table(
        'business', schema=None, recreate='always', table_kwargs={'sqlite_autoincrement': True}
    ) as batch_op:
        batch_op.alter_column(
            'session_epoch',
            existing_type=sqlmodel.sql.sqltypes.AutoString(length=32),
            nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table(
        'business', schema=None, recreate='always', table_kwargs={'sqlite_autoincrement': False}
    ) as batch_op:
        batch_op.drop_column('session_epoch')
