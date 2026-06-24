"""add genre to books

Revision ID: b1c2d3e4f5a6
Revises: a764207ec4ec
Create Date: 2026-06-23 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, Sequence[str], None] = 'a764207ec4ec'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('books') as batch_op:
        batch_op.add_column(sa.Column('genre', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('books') as batch_op:
        batch_op.drop_column('genre')
