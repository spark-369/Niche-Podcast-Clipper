"""Tasks package: Celery entry points for local AI and video rendering."""

from tasks.ai_tasks import (
    extract_highlights_task,
    transcribe_episode_task,
)
from tasks.video_tasks import render_clip_task

__all__ = [
    "extract_highlights_task",
    "render_clip_task",
    "transcribe_episode_task",
]
