"""add unique constraint on books.isbn

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-24
"""
from alembic import op
import sqlalchemy as sa

revision = 'e5f6a7b8c9d0'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("books") as batch_op:
        batch_op.create_unique_constraint("uq_books_isbn", ["isbn"])


def downgrade():
    with op.batch_alter_table("books") as batch_op:
        batch_op.drop_constraint("uq_books_isbn", type_="unique")
