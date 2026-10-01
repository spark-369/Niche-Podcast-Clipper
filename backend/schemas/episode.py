"""Pydantic schemas for episodes, transcripts and media metadata."""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from models.enums import EpisodeStatus


class MediaMetadata(BaseModel):
    """Technical metadata probed from the uploaded media file."""

    duration_seconds: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_video_stream: bool = True
    media_type: str = "video"
    mime_type: Optional[str] = None
    file_size_bytes: int = 0


class EpisodeUpdate(BaseModel):
    """Editable episode fields."""

    title: Optional[str] = Field(default=None, max_length=512)
    description: Optional[str] = None


class EpisodeRead(BaseModel):
    """Episode representation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: int
    title: str
    description: Optional[str] = None
    source_url: Optional[str] = None
    original_filename: Optional[str] = None
    stored_filename: str
    media_type: str
    mime_type: Optional[str] = None
    file_size_bytes: int
    duration_seconds: float
    width: int
    height: int
    fps: float
    has_video_stream: bool
    language: Optional[str] = None
    status: EpisodeStatus
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    has_transcript: bool = False
    clip_count: int = 0


class EpisodeUploadResponse(BaseModel):
    """Response returned right after a successful upload."""

    episode: EpisodeRead
    message: str = "Episode uploaded successfully"


class EpisodeUrlRequest(BaseModel):
    """Queue an episode from a remote URL into local storage."""

    url: HttpUrl
    title: Optional[str] = Field(default=None, max_length=512)
    description: Optional[str] = None


class TranscriptSegmentRead(BaseModel):
    """A timestamped transcript chunk."""

    model_config = ConfigDict(from_attributes=True)

    index: int
    start_time: float
    end_time: float
    text: str
    words: Optional[list[dict[str, Any]]] = None


class TranscriptRead(BaseModel):
    """Full transcript payload."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    episode_id: int
    text: str
    language: str
    model_name: str
    duration_seconds: float
    word_count: int
    created_at: datetime
    segments: list[TranscriptSegmentRead] = []


class TranscribeRequest(BaseModel):
    """Options for the local ASR pipeline."""

    language: Optional[str] = Field(
        default=None, description="Optional ISO language hint, e.g. 'en'"
    )
    word_timestamps: bool = Field(
        default=True, description="Produce word level timestamps for karaoke captions"
    )
    force: bool = Field(
        default=False, description="Re-run transcription even if a transcript exists"
    )


class TranscribeResponse(BaseModel):
    """Acknowledgement that transcription was dispatched."""

    episode_id: int
    job_id: Optional[int] = None
    task_id: Optional[str] = None
    status: EpisodeStatus
    cached: bool = False
    message: str
