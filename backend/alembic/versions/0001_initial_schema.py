"""Initial schema: users, episodes, transcripts, clips and render jobs.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-01-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_superuser", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_id", "users", ["id"])
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "episodes",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            "owner_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source_url", sa.String(length=2048), nullable=True),
        sa.Column("original_filename", sa.String(length=512), nullable=True),
        sa.Column("stored_filename", sa.String(length=512), nullable=False),
        sa.Column("media_path", sa.String(length=1024), nullable=False),
        sa.Column("media_type", sa.String(length=32), nullable=False, server_default="video"),
        sa.Column("mime_type", sa.String(length=128), nullable=True),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("width", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("height", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fps", sa.Float(), nullable=False, server_default="0"),
        sa.Column("has_video_stream", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="uploaded"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_episodes_id", "episodes", ["id"])
    op.create_index("ix_episodes_owner_id", "episodes", ["owner_id"])
    op.create_index("ix_episodes_status", "episodes", ["status"])

    op.create_table(
        "transcripts",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            "episode_id",
            sa.Integer(),
            sa.ForeignKey("episodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("language", sa.String(length=16), nullable=False, server_default="en"),
        sa.Column("model_name", sa.String(length=255), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("word_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("raw_payload", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("episode_id", name="uq_transcripts_episode_id"),
    )
    op.create_index("ix_transcripts_id", "transcripts", ["id"])
    op.create_index("ix_transcripts_episode_id", "transcripts", ["episode_id"])


    op.create_table(
        "transcript_segments",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            "transcript_id",
            sa.Integer(),
            sa.ForeignKey("transcripts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("start_time", sa.Float(), nullable=False, server_default="0"),
        sa.Column("end_time", sa.Float(), nullable=False, server_default="0"),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("words", sa.JSON(), nullable=True),
    )
    op.create_index("ix_transcript_segments_id", "transcript_segments", ["id"])
    op.create_index(
        "ix_transcript_segments_transcript_id", "transcript_segments", ["transcript_id"]
    )

    op.create_table(
        "clips",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            "episode_id",
            sa.Integer(),
            sa.ForeignKey("episodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("hook", sa.Text(), nullable=False, server_default=""),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("start_time", sa.Float(), nullable=False),
        sa.Column("end_time", sa.Float(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("transcript_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("words", sa.JSON(), nullable=True),
        sa.Column("virality_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("hook_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("emotion_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("quotability_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("keywords", sa.JSON(), nullable=True),
        sa.Column("caption_style", sa.JSON(), nullable=False),
        sa.Column("framing", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="draft"),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_path", sa.String(length=1024), nullable=True),
        sa.Column("thumbnail_path", sa.String(length=1024), nullable=True),
        sa.Column("output_size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_clips_id", "clips", ["id"])
    op.create_index("ix_clips_episode_id", "clips", ["episode_id"])
    op.create_index("ix_clips_owner_id", "clips", ["owner_id"])
    op.create_index("ix_clips_status", "clips", ["status"])

    op.create_table(
        "render_jobs",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            "episode_id",
            sa.Integer(),
            sa.ForeignKey("episodes.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "clip_id",
            sa.Integer(),
            sa.ForeignKey("clips.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("job_type", sa.String(length=32), nullable=False, server_default="render"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("task_id", sa.String(length=128), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_render_jobs_id", "render_jobs", ["id"])
    op.create_index("ix_render_jobs_episode_id", "render_jobs", ["episode_id"])
    op.create_index("ix_render_jobs_clip_id", "render_jobs", ["clip_id"])
    op.create_index("ix_render_jobs_job_type", "render_jobs", ["job_type"])
    op.create_index("ix_render_jobs_status", "render_jobs", ["status"])
    op.create_index("ix_render_jobs_task_id", "render_jobs", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_render_jobs_task_id", table_name="render_jobs")
    op.drop_index("ix_render_jobs_status", table_name="render_jobs")
    op.drop_index("ix_render_jobs_job_type", table_name="render_jobs")
    op.drop_index("ix_render_jobs_clip_id", table_name="render_jobs")
    op.drop_index("ix_render_jobs_episode_id", table_name="render_jobs")
    op.drop_index("ix_render_jobs_id", table_name="render_jobs")
    op.drop_table("render_jobs")

    op.drop_index("ix_clips_status", table_name="clips")
    op.drop_index("ix_clips_owner_id", table_name="clips")
    op.drop_index("ix_clips_episode_id", table_name="clips")
    op.drop_index("ix_clips_id", table_name="clips")
    op.drop_table("clips")

    op.drop_index("ix_transcript_segments_transcript_id", table_name="transcript_segments")
    op.drop_index("ix_transcript_segments_id", table_name="transcript_segments")
    op.drop_table("transcript_segments")

    op.drop_index("ix_transcripts_episode_id", table_name="transcripts")
    op.drop_index("ix_transcripts_id", table_name="transcripts")
    op.drop_table("transcripts")

    op.drop_index("ix_episodes_status", table_name="episodes")
    op.drop_index("ix_episodes_owner_id", table_name="episodes")
    op.drop_index("ix_episodes_id", table_name="episodes")
    op.drop_table("episodes")

    op.drop_index("ix_users_email", table_name="users")
    op.drop_index("ix_users_id", table_name="users")
    op.drop_table("users")
