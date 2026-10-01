"""Pure Python video processing built on OpenCV (no external binaries)."""

from services.video.captions import (
    CaptionWord,
    draw_captions,
    draw_title_card,
    group_caption_lines,
    hex_to_bgr,
    words_from_payload,
)
from services.video.framing import (
    FramingState,
    compute_crop_box,
    crop_and_fit,
    detect_focus_point,
)
from services.video.renderer import (
    RenderResult,
    preview_frame,
    render_audio_only,
    render_clip,
    resolve_render_path,
)

__all__ = [
    "CaptionWord",
    "FramingState",
    "RenderResult",
    "compute_crop_box",
    "crop_and_fit",
    "detect_focus_point",
    "draw_captions",
    "draw_title_card",
    "group_caption_lines",
    "hex_to_bgr",
    "preview_frame",
    "render_audio_only",
    "render_clip",
    "resolve_render_path",
    "words_from_payload",
]
