"""Add clip favourites and public share links.

- ``clips.is_favorite``: boolean star flag used to shortlist the best
  highlights (default ``false`` for every existing row).
- ``clip_shares``: tokenised public share links with optional expiry,
  revocation and a view counter, consumed by the ``/public`` router.

Revision ID: 0003_favorites_share_links
Revises: 0002_widen_media_size_columns
Create Date: 2026-01-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_favorites_share_links"
down_revision: Union[str, None] = "0002_widen_media_size_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "clips",
        sa.Column(
            "is_favorite",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.create_table(
        "clip_shares",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            "clip_id",
            sa.Integer(),
            sa.ForeignKey("clips.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("view_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_clip_shares_id", "clip_shares", ["id"])
    op.create_index("ix_clip_shares_clip_id", "clip_shares", ["clip_id"])
    op.create_index("ix_clip_shares_owner_id", "clip_shares", ["owner_id"])
    op.create_index("ix_clip_shares_token", "clip_shares", ["token"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_clip_shares_token", table_name="clip_shares")
    op.drop_index("ix_clip_shares_owner_id", table_name="clip_shares")
    op.drop_index("ix_clip_shares_clip_id", table_name="clip_shares")
    op.drop_index("ix_clip_shares_id", table_name="clip_shares")
    op.drop_table("clip_shares")
    op.drop_column("clips", "is_favorite")