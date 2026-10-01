"""Caption style presets and helpers shared by the API and the renderer."""

from typing import Any, Optional

from models.enums import CaptionStylePreset
from schemas.clip import CaptionStyle, FramingOptions

PRESET_LABELS: dict[CaptionStylePreset, str] = {
    CaptionStylePreset.TIKTOK: "TikTok Pop",
    CaptionStylePreset.KARAOKE: "Karaoke Highlight",
    CaptionStylePreset.MINIMAL: "Minimal Clean",
    CaptionStylePreset.NEON: "Neon Glow",
    CaptionStylePreset.BOLD_YELLOW: "Bold Yellow",
}

PRESETS: dict[CaptionStylePreset, CaptionStyle] = {
    CaptionStylePreset.TIKTOK: CaptionStyle(
        preset=CaptionStylePreset.TIKTOK,
        font_size=72,
        text_color="#FFFFFF",
        highlight_color="#FF2D55",
        outline_color="#000000",
        outline_width=4,
        position="center",
        uppercase=True,
        words_per_line=3,
        pop_scale=1.2,
    ),
    CaptionStylePreset.KARAOKE: CaptionStyle(
        preset=CaptionStylePreset.KARAOKE,
        font_size=68,
        text_color="#FFFFFF",
        highlight_color="#00E5A0",
        outline_color="#101010",
        outline_width=4,
        position="bottom",
        uppercase=False,
        words_per_line=4,
        bottom_margin=300,
        pop_scale=1.12,
    ),
    CaptionStylePreset.MINIMAL: CaptionStyle(
        preset=CaptionStylePreset.MINIMAL,
        font_size=54,
        font_family="DejaVuSans",
        text_color="#F5F5F5",
        highlight_color="#F5F5F5",
        outline_color="#000000",
        outline_width=1,
        background_color="#000000",
        background_opacity=0.45,
        position="bottom",
        uppercase=False,
        words_per_line=4,
        shadow=False,
        pop_scale=1.05,
        bottom_margin=260,
    ),
    CaptionStylePreset.NEON: CaptionStyle(
        preset=CaptionStylePreset.NEON,
        font_size=66,
        text_color="#00F0FF",
        highlight_color="#FF00E5",
        outline_color="#001B2E",
        outline_width=3,
        position="center",
        uppercase=True,
        words_per_line=3,
        shadow=True,
        pop_scale=1.18,
    ),
    CaptionStylePreset.BOLD_YELLOW: CaptionStyle(
        preset=CaptionStylePreset.BOLD_YELLOW,
        font_size=76,
        text_color="#FFF200",
        highlight_color="#FF6A00",
        outline_color="#000000",
        outline_width=6,
        position="bottom",
        uppercase=True,
        words_per_line=3,
        pop_scale=1.25,
        bottom_margin=340,
    ),
}

DEFAULT_CAPTION_STYLE = PRESETS[CaptionStylePreset.TIKTOK]


def get_default_caption_style() -> CaptionStyle:
    """Return a copy of the default caption style."""
    return DEFAULT_CAPTION_STYLE.model_copy(deep=True)


def get_default_framing(width: int = 1080, height: int = 1920, fps: int = 30) -> FramingOptions:
    """Return default 9:16 framing options matching the target resolution."""
    return FramingOptions(output_width=width, output_height=height, fps=fps)


def resolve_preset(name: Optional[str]) -> CaptionStyle:
    """Resolve a preset by name, falling back to the default style."""
    if not name:
        return get_default_caption_style()
    try:
        preset = CaptionStylePreset(str(name).lower())
    except ValueError:
        return get_default_caption_style()
    return PRESETS[preset].model_copy(deep=True)


def caption_style_to_dict(style: CaptionStyle | dict[str, Any] | None) -> dict[str, Any]:
    """Coerce a caption style (schema or raw dict) into a JSON-safe dict."""
    if style is None:
        return get_default_caption_style().model_dump(mode="json")
    if isinstance(style, CaptionStyle):
        return style.model_dump(mode="json")
    merged = get_default_caption_style().model_dump(mode="json")
    merged.update({key: value for key, value in style.items() if value is not None})
    return CaptionStyle(**merged).model_dump(mode="json")


def framing_to_dict(framing: FramingOptions | dict[str, Any] | None) -> dict[str, Any]:
    """Coerce framing options into a JSON-safe dict."""
    if framing is None:
        return get_default_framing().model_dump(mode="json")
    if isinstance(framing, FramingOptions):
        return framing.model_dump(mode="json")
    merged = get_default_framing().model_dump(mode="json")
    merged.update({key: value for key, value in framing.items() if value is not None})
    return FramingOptions(**merged).model_dump(mode="json")


def apply_style_overrides(
    base: CaptionStyle | dict[str, Any] | None,
    overrides: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Merge user supplied caption overrides onto a base style."""
    style_dict = caption_style_to_dict(base)
    if overrides:
        style_dict.update({key: value for key, value in overrides.items() if value is not None})
    return caption_style_to_dict(style_dict)


def list_presets() -> list[dict[str, Any]]:
    """Return every preset with a friendly label, for the frontend gallery."""
    return [
        {
            "name": preset,
            "label": PRESET_LABELS[preset],
            "style": style.model_dump(mode="json"),
        }
        for preset, style in PRESETS.items()
    ]
