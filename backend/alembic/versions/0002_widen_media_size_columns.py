"""Widen byte-size columns to BIGINT.

``MAX_UPLOAD_SIZE_MB`` defaults to 2048, i.e. 2147483648 bytes — one byte past
the 32-bit ``INTEGER`` maximum — so ``episodes.file_size_bytes`` and
``clips.output_size_bytes`` must be ``BIGINT`` to avoid an overflow error on
large uploads and renders.

Revision ID: 0002_widen_media_size_columns
Revises: 0001_initial_schema
Create Date: 2026-01-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_widen_media_size_columns"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "episodes",
        "file_size_bytes",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False,
        existing_server_default="0",
    )
    op.alter_column(
        "clips",
        "output_size_bytes",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False,
        existing_server_default="0",
    )


def downgrade() -> None:
    op.alter_column(
        "clips",
        "output_size_bytes",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False,
        existing_server_default="0",
    )
    op.alter_column(
        "episodes",
        "file_size_bytes",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False,
        existing_server_default="0",
    )
