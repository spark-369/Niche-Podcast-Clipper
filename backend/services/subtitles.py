"""Subtitle export (SRT / WebVTT) built from the same word timings the
caption burner uses.

Because :func:`services.video.captions.words_from_payload` is the single
source of truth for word timing resolution, exported subtitles always match
the burned-in captions: edited transcripts, missing word payloads and
clip-boundary rebasing are handled identically.
"""

from __future__ import annotations

from typing import Sequence

from services.video.captions import (
    CaptionWord,
    group_caption_lines,
    words_from_payload,
)

SUBTITLE_WORDS_PER_LINE = 5
SUBTITLE_MAX_LINES = 2


def build_subtitle_entries(
    words: Sequence[dict],
    clip_start: float,
    clip_end: float,
    fallback_text: str = "",
) -> list[tuple[float, float, str]]:
    """Return ``(start, end, text)`` caption cues in clip-relative seconds.

    Words are grouped into cues with the same chunking the on-screen captions
    use, so an SRT file mirrors the burned captions line for line.
    """
    caption_words: list[CaptionWord] = words_from_payload(
        list(words or []),
        clip_start,
        clip_end,
        fallback_text,
    )
    entries: list[tuple[float, float, str]] = []
    for group in group_caption_lines(
        caption_words, SUBTITLE_WORDS_PER_LINE, SUBTITLE_MAX_LINES
    ):
        if not group:
            continue
        text = " ".join(word.text for word in group).strip()
        if not text:
            continue
        entries.append(
            (max(group[0].start, 0.0), max(group[-1].end, group[0].start + 0.05), text)
        )
    return entries


def _format_timestamp(seconds: float, separator: str, milliseconds: bool) -> str:
    """Render seconds as an ``HH:MM:SS`` timestamp with the requested precision."""
    total = max(float(seconds or 0.0), 0.0)
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    secs = int(total % 60)
    if milliseconds:
        millis = int(round((total - int(total)) * 1000))
        if millis >= 1000:  # rounding can carry over into the next second
            millis = 999
        return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.000"


def to_srt(entries: Sequence[tuple[float, float, str]]) -> str:
    """Format subtitle cues as an SRT document (SubRip)."""
    blocks: list[str] = []
    for index, (start, end, text) in enumerate(entries, start=1):
        blocks.append(
            f"{index}\n"
            f"{_format_timestamp(start, ',', True)} --> "
            f"{_format_timestamp(end, ',', True)}\n"
            f"{text}\n"
        )
    return "\n".join(blocks)


def to_vtt(entries: Sequence[tuple[float, float, str]]) -> str:
    """Format subtitle cues as a WebVTT document."""
    lines = ["WEBVTT", ""]
    for index, (start, end, text) in enumerate(entries, start=1):
        lines.append(
            f"{index}\n"
            f"{_format_timestamp(start, '.', True)} --> "
            f"{_format_timestamp(end, '.', True)}\n"
            f"{text}\n"
        )
    return "\n".join(lines)


def render_subtitles(
    fmt: str,
    words: Sequence[dict],
    clip_start: float,
    clip_end: float,
    fallback_text: str = "",
) -> tuple[str, str]:
    """Build a subtitle document.

    Returns:
        ``(document, media_type)`` where ``fmt`` is ``"srt"`` or ``"vtt"``.

    Raises:
        ValueError: when ``fmt`` is not a supported subtitle format.
    """
    normalised = (fmt or "").strip().lower()
    entries = build_subtitle_entries(words, clip_start, clip_end, fallback_text)
    if normalised == "srt":
        return to_srt(entries), "application/x-subrip"
    if normalised in {"vtt", "webvtt"}:
        return to_vtt(entries), "text/vtt"
    raise ValueError(f"Unsupported subtitle format '{fmt}'. Use 'srt' or 'vtt'.")