"""Pydantic schemas for clips, caption styling and framing."""

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.enums import CaptionStylePreset, ClipStatus, JobStatus, JobType

HEX_COLOR_PATTERN = r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$"


class CaptionStyle(BaseModel):
    """Caption styling consumed by the OpenCV caption burner."""

    preset: CaptionStylePreset = CaptionStylePreset.TIKTOK
    font_size: int = Field(default=64, ge=16, le=200)
    font_family: str = Field(default="DejaVuSans-Bold", max_length=128)
    text_color: str = Field(default="#FFFFFF", pattern=HEX_COLOR_PATTERN)
    highlight_color: str = Field(default="#FFD400", pattern=HEX_COLOR_PATTERN)
    outline_color: str = Field(default="#000000", pattern=HEX_COLOR_PATTERN)
    background_color: Optional[str] = Field(
        default=None, pattern=HEX_COLOR_PATTERN
    )
    background_opacity: float = Field(default=0.0, ge=0.0, le=1.0)
    position: Literal["top", "center", "bottom"] = "bottom"
    uppercase: bool = True
    words_per_line: int = Field(default=3, ge=1, le=8)
    max_lines: int = Field(default=2, ge=1, le=4)
    outline_width: int = Field(default=3, ge=0, le=12)
    shadow: bool = True
    pop_scale: float = Field(default=1.15, ge=1.0, le=1.6)
    bottom_margin: int = Field(default=320, ge=0, le=1400)


class FramingOptions(BaseModel):
    """Auto-framing options for the 9:16 vertical conversion."""

    mode: Literal["auto_face", "center", "manual"] = "auto_face"
    focus_x: float = Field(
        default=0.5, ge=0.0, le=1.0, description="Horizontal focus point (0-1)"
    )
    focus_y: float = Field(
        default=0.4, ge=0.0, le=1.0, description="Vertical focus point (0-1)"
    )
    zoom: float = Field(default=1.0, ge=1.0, le=2.5)
    smooth_tracking: bool = True
    blur_background: bool = True
    background_blur_strength: int = Field(default=41, ge=1, le=151)
    output_width: int = Field(default=1080, ge=360, le=2160)
    output_height: int = Field(default=1920, ge=640, le=3840)
    fps: int = Field(default=30, ge=15, le=60)
    burn_captions: bool = True
    crf: int = Field(default=23, ge=0, le=51)


class ClipGenerateRequest(BaseModel):
    """Parameters for the local highlight extraction job."""

    clip_count: int = Field(default=5, ge=1, le=20)
    min_duration_seconds: float = Field(default=15.0, ge=5.0, le=120.0)
    max_duration_seconds: float = Field(default=60.0, ge=10.0, le=180.0)
    temperature: float = Field(default=0.4, ge=0.0, le=1.5)
    focus: Optional[str] = Field(
        default=None,
        max_length=512,
        description="Optional creative direction, e.g. 'funniest moments'",
    )
    use_cache: bool = True
    caption_style: CaptionStyle = Field(default_factory=CaptionStyle)
    framing: FramingOptions = Field(default_factory=FramingOptions)

    @model_validator(mode="after")
    def _check_duration_window(self) -> "ClipGenerateRequest":
        if self.max_duration_seconds <= self.min_duration_seconds:
            raise ValueError(
                "max_duration_seconds must be greater than min_duration_seconds"
            )
        return self


class ClipUpdate(BaseModel):
    """Editable clip fields (including caption + framing tweaks)."""

    title: Optional[str] = Field(default=None, max_length=512)
    hook: Optional[str] = None
    summary: Optional[str] = None
    # The caption text shown when no word timings exist. Persisted here (and by
    # the render endpoint) so the stored text always describes the burned
    # captions.
    transcript_text: Optional[str] = Field(default=None, max_length=4000)
    start_time: Optional[float] = Field(default=None, ge=0.0)
    end_time: Optional[float] = Field(default=None, ge=0.0)
    caption_style: Optional[CaptionStyle] = None
    framing: Optional[FramingOptions] = None
    is_favorite: Optional[bool] = None


class BatchRenderRequest(BaseModel):
    """Queue renders for several clips in one request."""

    clip_ids: list[int] = Field(min_length=1, max_length=50)
    priority: int = Field(default=5, ge=0, le=9)


class BatchRenderResponse(BaseModel):
    """Per-clip acknowledgement for a batch render submission."""

    queued: list["ClipRenderResponse"] = Field(default_factory=list)
    # clip_id -> reason it could not be queued (already rendering, missing file…)
    skipped: dict[int, str] = Field(default_factory=dict)


class ClipShareCreate(BaseModel):
    """Payload for creating a public share link."""

    # None = the link never expires.
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=365)


class ClipShareRead(BaseModel):
    """A share link as seen by its owner (includes management metadata)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    clip_id: int
    token: str
    # Public URL is composed by the router (validated with an empty default, then set).
    url: str = ""
    view_count: int
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    is_active: bool
    created_at: datetime


class PublicClipRead(BaseModel):
    """Clip metadata exposed through a share link (no owner information)."""

    title: str
    hook: str
    summary: str
    duration_seconds: float
    virality_score: float
    keywords: Optional[list[str]] = None
    thumbnail_url: Optional[str] = None
    download_url: Optional[str] = None
    view_count: int = 0
    expires_at: Optional[datetime] = None
    created_at: datetime


class ClipRead(BaseModel):
    """Clip representation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    episode_id: int
    owner_id: int
    title: str
    hook: str
    summary: str
    start_time: float
    end_time: float
    duration_seconds: float
    transcript_text: str
    # Word-level timings (absolute source-media seconds) used to draw the
    # editor preview caption overlay in lockstep with the audio. Exposed here so
    # the client can composite captions without a dedicated timeline request.
    words: Optional[list[dict[str, Any]]] = None
    virality_score: float
    hook_score: float
    emotion_score: float
    quotability_score: float
    keywords: Optional[list[str]] = None
    caption_style: dict[str, Any]
    framing: dict[str, Any]
    status: ClipStatus
    progress: int
    output_path: Optional[str] = None
    thumbnail_path: Optional[str] = None
    output_size_bytes: int
    error_message: Optional[str] = None
    download_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    # Set when a caption-free companion render exists. The editor plays this file
    # under its draft caption canvas: it has the render's frames and audio but
    # none of its burned captions, so the draft styling is the only caption
    # track on screen.
    preview_url: Optional[str] = None
    is_favorite: bool = False
    created_at: datetime
    updated_at: datetime


class ClipGenerateResponse(BaseModel):
    """Acknowledgement that highlight extraction was dispatched."""

    episode_id: int
    job_id: Optional[int] = None
    task_id: Optional[str] = None
    status: JobStatus
    cached: bool = False
    message: str = "Highlight extraction queued on the local worker"


class ClipRenderRequest(BaseModel):
    """Options for dispatching an OpenCV render job."""

    caption_style: Optional[CaptionStyle] = None
    framing: Optional[FramingOptions] = None
    # The edited caption text. Persisted by the render endpoint so the burned
    # captions and the stored transcript can never drift apart when no word
    # timings are available (the renderer then distributes this text evenly).
    transcript: Optional[str] = Field(default=None, min_length=1, max_length=4000)
    priority: int = Field(default=5, ge=0, le=9)


class CaptionTimelineWord(BaseModel):
    """One word with its clip-relative on-screen window, in seconds."""

    text: str
    start: float
    end: float


class CaptionTimelineGroup(BaseModel):
    """A caption block (the words shown together) and when it is on screen."""

    words: list[CaptionTimelineWord]
    start: float
    end: float


class CaptionTimelineRead(BaseModel):
    """Every caption group for a clip, for client-side overlay rendering.

    Returned in one small payload so the editor can composite captions locally
    on each animation frame instead of requesting a re-encoded JPEG several
    times a second. The times are clip-relative and already rebased, so the
    client only has to compare them against `video.currentTime`.
    """

    clip_id: int
    duration_seconds: float
    words_per_line: int
    max_lines: int
    groups: list[CaptionTimelineGroup]


class ClipRenderResponse(BaseModel):
    """Acknowledgement that a render job was queued."""

    clip_id: int
    job_id: int
    task_id: Optional[str] = None
    status: JobStatus
    message: str = "Render job dispatched"


class RenderJobRead(BaseModel):
    """Background job state exposed to the client."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    episode_id: Optional[int] = None
    clip_id: Optional[int] = None
    job_type: JobType
    status: JobStatus
    progress: int
    task_id: Optional[str] = None
    message: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class CaptionStylePresetRead(BaseModel):
    """A ready to use caption theme for the frontend gallery."""

    name: CaptionStylePreset
    label: str
    style: CaptionStyle


# `BatchRenderRequest`/`BatchRenderResponse` are defined before
# `ClipRenderResponse` in this module, so their forward reference must be
# resolved once that class exists (same pattern as `schemas.common`).
BatchRenderResponse.model_rebuild()
