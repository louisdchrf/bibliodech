"""add default_room_id to users

Revision ID: c3d4e5f6a7b8
Revises: b1c2d3e4f5a6
Create Date: 2026-06-24

"""
from alembic import op
import sqlalchemy as sa

revision = 'c3d4e5f6a7b8'
down_revision = 'b1c2d3e4f5a6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("default_room_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_users_default_room_id", "rooms", ["default_room_id"], ["id"]
        )


def downgrade():
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("fk_users_default_room_id", type_="foreignkey")
        batch_op.drop_column("default_room_id")
