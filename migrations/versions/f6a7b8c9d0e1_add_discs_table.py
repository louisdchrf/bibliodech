"""add discs table

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-06-26
"""
from alembic import op
import sqlalchemy as sa

revision = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "discs",
        sa.Column("id", sa.Integer, primary_key=True, index=True),
        sa.Column("barcode", sa.String, nullable=True, index=True, unique=True),
        sa.Column("title", sa.String, nullable=False),
        sa.Column("artist", sa.String, nullable=True),
        sa.Column("label", sa.String, nullable=True),
        sa.Column("catalog_number", sa.String, nullable=True),
        sa.Column("year", sa.String, nullable=True),
        sa.Column("format", sa.String, nullable=True),
        sa.Column("genre", sa.String, nullable=True),
        sa.Column("track_count", sa.Integer, nullable=True),
        sa.Column("language", sa.String, nullable=True),
        sa.Column("country", sa.String, nullable=True),
        sa.Column("cover_url", sa.String, nullable=True),
        sa.Column("mbid", sa.String, nullable=True),
        sa.Column("room_id", sa.Integer, sa.ForeignKey("rooms.id"), nullable=True),
        sa.Column("enrichment_status", sa.String, nullable=False, default="ok"),
        sa.Column("source_data", sa.Text, nullable=True),
        sa.Column("added_at", sa.DateTime, nullable=True),
        sa.Column("added_by", sa.Integer, sa.ForeignKey("users.id"), nullable=True),
    )


def downgrade():
    op.drop_table("discs")
