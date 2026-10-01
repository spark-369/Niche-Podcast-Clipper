"""Word-by-word caption burning with OpenCV.

Everything here is pure Python + OpenCV: text is rasterised with ``cv2.putText``
(plus Pillow for precise glyph metrics when available) so no FFmpeg ``drawtext``
filter or external binary is required.
"""

import logging
from dataclasses import dataclass
from typing import Any, Optional, Sequence

import cv2
import numpy as np

logger = logging.getLogger(__name__)

FONT_MAP = {
    "DejaVuSans": cv2.FONT_HERSHEY_SIMPLEX,
    "DejaVuSans-Bold": cv2.FONT_HERSHEY_DUPLEX,
    "DejaVuSerif": cv2.FONT_HERSHEY_COMPLEX,
    "DejaVuSansMono": cv2.FONT_HERSHEY_PLAIN,
}

DEFAULT_FONT = cv2.FONT_HERSHEY_DUPLEX
FONT_SCALE_DIVISOR = 32.0  # converts px font size -> cv2 font scale
LINE_SPACING = 1.18


@dataclass
class CaptionWord:
    """A single caption word with its own on-screen timing."""

    text: str
    start: float
    end: float


def hex_to_bgr(value: str, default: tuple[int, int, int] = (255, 255, 255)) -> tuple[int, int, int]:
    """Convert ``#RRGGBB`` (or ``#AARRGGBB``/``#RGB``) into an OpenCV BGR tuple."""
    if not value:
        return default
    raw = value.strip().lstrip("#")
    try:
        if len(raw) == 3:
            raw = "".join(ch * 2 for ch in raw)
        if len(raw) == 8:  # AARRGGBB -> drop alpha
            raw = raw[2:]
        if len(raw) != 6:
            return default
        red = int(raw[0:2], 16)
        green = int(raw[2:4], 16)
        blue = int(raw[4:6], 16)
        return (blue, green, red)
    except ValueError:
        return default


def resolve_font(font_family: Optional[str]) -> int:
    """Map a font family name to an OpenCV Hershey font constant."""
    if not font_family:
        return DEFAULT_FONT
    if font_family in FONT_MAP:
        return FONT_MAP[font_family]
    lowered = font_family.lower()
    if "bold" in lowered or "black" in lowered:
        return cv2.FONT_HERSHEY_DUPLEX
    if "mono" in lowered:
        return cv2.FONT_HERSHEY_PLAIN
    if "serif" in lowered and "sans" not in lowered:
        return cv2.FONT_HERSHEY_COMPLEX
    return cv2.FONT_HERSHEY_SIMPLEX


def ease_out(t: float) -> float:
    """Ease-out easing used for the caption pop animation."""
    t = max(0.0, min(t, 1.0))
    return 1 - (1 - t) ** 3


def _normalise_token(token: str) -> str:
    """Lower-case and strip punctuation so two spellings of a word compare equal."""
    return "".join(ch for ch in (token or "").lower() if ch.isalnum())


def _matches_fallback_text(
    captions: Sequence[CaptionWord], fallback_text: str
) -> bool:
    """Check whether stored word timings still describe ``fallback_text``.

    An edited transcript must invalidate the word timings captured from the
    original transcription, otherwise the stale words keep being burned into
    the video. Comparison is punctuation/case insensitive, and a mismatch in
    word count is enough to declare the timings stale.
    """
    expected = [_normalise_token(token) for token in (fallback_text or "").split()]
    expected = [token for token in expected if token]
    if not expected:
        return True
    actual = [_normalise_token(word.text) for word in captions]
    actual = [token for token in actual if token]
    return actual == expected


def words_from_payload(
    words: Sequence[dict[str, Any]],
    clip_start: float,
    clip_end: float,
    fallback_text: str = "",
) -> list[CaptionWord]:
    """Normalise persisted word timings into clip-relative caption words.

    Persisted ``words`` carry **absolute** timestamps in the source media's own
    timeline (Whisper reports them against the uploaded file). Every consumer
    compares them against *clip-relative* time — the renderer uses
    ``frame_index / fps`` and the preview endpoint passes ``timestamp -
    clip_start`` — so the two must be rebased onto the clip start here, or no
    caption group is ever active and the captions simply never appear.

    ``fallback_text`` is the clip's current ``transcript_text``. It takes
    precedence over ``words`` whenever the two disagree: the editor stores the
    edited transcript there, so the original word timings become stale and must
    be replaced by timings derived from the new text. Without this check an
    edited transcript would be burned with the old words' timings.
    """
    captions: list[CaptionWord] = []
    for item in words or []:
        text = str(item.get("word") or item.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(item.get("start") or 0.0)
            end = float(item.get("end") or start)
        except (TypeError, ValueError):
            continue
        if end <= start:
            end = start + 0.28
        # Rebase onto the clip start. Timings at or before the clip start clamp
        # to 0 so a word that straddles the boundary still highlights at the
        # first frame instead of landing at a negative time.
        captions.append(
            CaptionWord(
                text=text,
                start=max(start - clip_start, 0.0),
                end=max(end - clip_start, 0.0),
            )
        )
    # A word clamped to zero length by the rebase would never match the
    # `start <= t <= end` test, so give it a frame's worth of duration.
    for word in captions:
        if word.end <= word.start:
            word.end = word.start + 0.28

    if captions and _matches_fallback_text(captions, fallback_text):
        return captions

    # No word timings available (or the transcript was edited): distribute the
    # text across the clip evenly. Already clip-relative by construction.
    tokens = [token for token in (fallback_text or "").split() if token]
    if not tokens:
        return []
    span = max(clip_end - clip_start, 0.5)
    per_word = span / len(tokens)
    return [
        CaptionWord(
            text=token,
            start=round(index * per_word, 3),
            end=round((index + 1) * per_word, 3),
        )
        for index, token in enumerate(tokens)
    ]


def group_caption_lines(
    captions: Sequence[CaptionWord], words_per_line: int, max_lines: int
) -> list[list[CaptionWord]]:
    """Split the word stream into fixed-size caption groups."""
    per_group = max(words_per_line * max_lines, 1)
    return [
        list(captions[index : index + per_group])
        for index in range(0, len(captions), per_group)
    ]


def _active_group(
    groups: Sequence[Sequence[CaptionWord]], timestamp: float
) -> Optional[Sequence[CaptionWord]]:
    for group in groups:
        if not group:
            continue
        if group[0].start <= timestamp <= group[-1].end:
            return group
    return None



def _measure(text: str, font: int, scale: float, thickness: int) -> tuple[int, int, int]:
    """Return ``(width, height, baseline)`` for a piece of text."""
    (width, height), baseline = cv2.getTextSize(text, font, scale, thickness)
    return width, height, baseline


def _draw_line(
    canvas: np.ndarray,
    words: Sequence[CaptionWord],
    active_index: int,
    scale: float,
    baseline_y: int,
    progress: float,
    style: dict[str, Any],
    font: int,
) -> int:
    """Draw one caption line with per-word highlighting; returns its width."""
    text_color = hex_to_bgr(style.get("text_color", "#FFFFFF"))
    highlight = hex_to_bgr(style.get("highlight_color", "#FFD400"))
    outline = hex_to_bgr(style.get("outline_color", "#000000"))
    outline_width = int(style.get("outline_width", 3))
    thickness = max(1, int(round(scale * 1.8)))
    gap = int(round(scale * 16))

    rendered = [
        word.text.upper() if style.get("uppercase") else word.text for word in words
    ]
    widths = [_measure(text, font, scale, thickness)[0] for text in rendered]
    total_width = sum(widths) + gap * max(len(rendered) - 1, 0)

    frame_width = canvas.shape[1]
    # On narrow frames a full line may exceed the width; shrink the line
    # proportionally instead of clipping words off the edges.
    max_line_width = int(frame_width * 0.92)
    if total_width > max_line_width:
        shrink = max_line_width / total_width
        scale *= shrink
        thickness = max(1, int(round(scale * 1.8)))
        gap = int(round(scale * 16))
        widths = [_measure(text, font, scale, thickness)[0] for text in rendered]
        total_width = sum(widths) + gap * max(len(rendered) - 1, 0)

    x = max(int((frame_width - total_width) / 2), 12)

    for index, text in enumerate(rendered):
        is_active = index == active_index
        pop = float(style.get("pop_scale", 1.15))
        word_scale = scale * (
            1.0 + (pop - 1.0) * ease_out(progress) if is_active else 1.0
        )
        color = highlight if is_active else text_color

        if outline_width > 0 and not is_active:
            cv2.putText(
                canvas,
                text,
                (x, baseline_y),
                font,
                word_scale,
                outline,
                thickness + outline_width,
                cv2.LINE_AA,
            )

        if style.get("shadow", True):
            cv2.putText(
                canvas,
                text,
                (x + 3, baseline_y + 3),
                font,
                word_scale,
                (0, 0, 0),
                thickness,
                cv2.LINE_AA,
            )

        cv2.putText(
            canvas, text, (x, baseline_y), font, word_scale, color, thickness, cv2.LINE_AA
        )
        x += widths[index] + gap

    return total_width



def draw_captions(
    frame: np.ndarray,
    timestamp: float,
    groups: Sequence[Sequence[CaptionWord]],
    style: dict[str, Any],
) -> np.ndarray:
    """Burn the caption group active at ``timestamp`` onto a frame.

    Args:
        frame: BGR canvas already resized to the target resolution.
        timestamp: Clip-relative time in seconds.
        groups: Caption groups produced by :func:`group_caption_lines`.
        style: Caption style dictionary (see ``services.caption_styles``).

    Returns:
        The frame with captions composited in place.
    """
    group = _active_group(groups, timestamp)
    if not group:
        return frame

    frame_height, frame_width = frame.shape[:2]

    max_lines = int(style.get("max_lines", 2))
    words_per_line = int(style.get("words_per_line", 3))
    # Font sizes are authored against a 1920px-tall canvas; scale them to the
    # actual frame so captions look identical at any resolution (full-screen
    # 1080p down to short 360p clips). A floor keeps text legible on small
    # frames instead of shrinking into unreadability.
    height_factor = frame_height / 1920.0
    font_factor = max(height_factor, 0.45)
    # Also respect the frame width for very narrow aspect ratios.
    width_factor = frame_width / 1080.0
    font_factor = min(font_factor, max(width_factor, 0.45))
    font_size = float(style.get("font_size", 64)) * font_factor
    font = resolve_font(style.get("font_family"))
    scale = max(font_size / FONT_SCALE_DIVISOR, 0.4)

    group_words = list(group)
    lines: list[list[CaptionWord]] = [
        group_words[index : index + words_per_line]
        for index in range(0, len(group_words), words_per_line)
    ][:max_lines]

    # `active_index` indexes into the *full* group, but `local_active` in the
    # draw loop is computed per line against these sliced `lines`. Cap the search
    # to the words actually on screen so a word trimmed off by `max_lines`
    # cannot be reported as active (which left no word highlighted at all).
    visible_words = [word for line in lines for word in line]
    active_index = -1
    active_word: Optional[CaptionWord] = None
    for index, word in enumerate(visible_words):
        if word.start <= timestamp <= word.end:
            active_index = index
            active_word = word
            break

    line_height = int(round(font_size * LINE_SPACING))
    position = style.get("position", "bottom")
    bottom_margin = int(style.get("bottom_margin", 320))

    if position == "top":
        first_baseline = int(frame_height * 0.08 + font_size * 1.2)
    elif position == "center":
        block_height = line_height * len(lines)
        first_baseline = int(frame_height / 2 - block_height / 2 + font_size)
    else:
        # The margin is authored against a 1920px-tall canvas; scale it to the
        # actual frame so captions stay near the bottom on any resolution
        # (320px on a 720p frame would otherwise sit near the middle).
        scaled_margin = int(bottom_margin * frame_height / 1920.0)
        scaled_margin = min(scaled_margin, int(frame_height * 0.25))
        first_baseline = max(frame_height - scaled_margin, int(font_size * 2))

    background_color = style.get("background_color")
    opacity = float(style.get("background_opacity", 0.0) or 0.0)
    if background_color and opacity > 0:
        overlay = frame.copy()
        block_top = max(first_baseline - int(font_size * 1.3), 0)
        block_bottom = min(
            first_baseline + line_height * (len(lines) - 1) + int(font_size * 0.6),
            frame_height,
        )
        cv2.rectangle(
            overlay,
            (0, block_top),
            (frame_width, block_bottom),
            hex_to_bgr(background_color),
            -1,
        )
        frame = cv2.addWeighted(overlay, opacity, frame, 1 - opacity, 0)

    for line_index, line in enumerate(lines):
        baseline_y = first_baseline + line_index * line_height
        local_active = active_index - (line_index * words_per_line)
        if local_active < 0 or local_active >= len(line):
            local_active = -1

        progress = 0.0
        if active_word is not None and local_active >= 0:
            span = max(active_word.end - active_word.start, 0.05)
            progress = max(0.0, min((timestamp - active_word.start) / span, 1.0))

        _draw_line(frame, line, local_active, scale, baseline_y, progress, style, font)

    return frame


def draw_title_card(
    frame: np.ndarray, title: str, style: dict[str, Any], progress: float = 1.0
) -> np.ndarray:
    """Overlay a short title card on the opening moments of a clip."""
    if not title:
        return frame
    frame_height, frame_width = frame.shape[:2]
    height_factor = max(frame_height / 1920.0, 0.45)

    font = resolve_font(style.get("font_family"))
    font_size = float(style.get("font_size", 64)) * 0.75 * height_factor
    scale = max(font_size / FONT_SCALE_DIVISOR, 0.4)
    thickness = max(1, int(round(scale * 1.6)))
    text = title.upper() if style.get("uppercase", True) else title

    max_width = int(frame_width * 0.86)
    while _measure(text, font, scale, thickness)[0] > max_width and len(text) > 8:
        text = text[: len(text) - 2]
        scale *= 0.97

    width, height, baseline = _measure(text, font, scale, thickness)
    x = max(int((frame_width - width) / 2), 10)
    y = int(frame_height * 0.12) + height

    alpha = max(0.0, min(progress, 1.0))
    overlay = frame.copy()
    cv2.rectangle(
        overlay,
        (x - 24, y - height - 18),
        (x + width + 24, y + baseline + 12),
        (0, 0, 0),
        -1,
    )
    cv2.addWeighted(overlay, 0.55 * alpha, frame, 1 - 0.55 * alpha, 0, frame)

    cv2.putText(frame, text, (x + 3, y + 3), font, scale, (0, 0, 0), thickness, cv2.LINE_AA)
    cv2.putText(
        frame,
        text,
        (x, y),
        font,
        scale,
        hex_to_bgr(style.get("highlight_color", "#FFD400")),
        thickness,
        cv2.LINE_AA,
    )
    return frame
