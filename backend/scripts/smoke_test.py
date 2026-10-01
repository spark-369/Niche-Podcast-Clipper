"""Smoke tests for the local AI and capture-heavy helpers (no models needed).

Run with::

    python -m scripts.smoke_test
"""

import sys
from pathlib import Path

# Allow running as a plain script from the backend directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import caption_styles  # noqa: E402
from services.ai.highlights import (  # noqa: E402
    build_candidate_windows,
    generate_highlights,
    score_heuristics,
)
from services.video import captions as caption_utils  # noqa: E402
from services.video import framing as framing_utils  # noqa: E402


def _sample_segments() -> list[dict]:
    lines = [
        "Most people think growth is about doing more, but the truth is it is about doing less.",
        "I used to think I needed a bigger audience until I realised the problem was my hook.",
        "Here is why ninety percent of creators quit in the first six months, and it is brutal.",
        "Let me tell you the secret that changed everything for my podcast last year.",
        "You will not believe how many downloads we lost because of a single thumbnail mistake.",
    ]
    segments = []
    for index, line in enumerate(lines):
        start = index * 12.0
        words = []
        tokens = line.split()
        for word_index, token in enumerate(tokens):
            word_start = start + word_index * (11.5 / max(len(tokens), 1))
            words.append(
                {
                    "word": token,
                    "start": round(word_start, 3),
                    "end": round(word_start + 0.4, 3),
                }
            )
        segments.append(
            {
                "index": index,
                "start_time": start,
                "end_time": start + 11.5,
                "text": line,
                "words": words,
            }
        )
    return segments


def check_candidate_windows() -> None:
    windows = build_candidate_windows(_sample_segments(), min_duration=15, max_duration=40)
    assert windows, "expected at least one candidate window"
    assert all(window.end > window.start for window in windows)
    print(f"[ok] candidate windows: {len(windows)} built")


def check_heuristics() -> None:
    windows = build_candidate_windows(_sample_segments(), min_duration=15, max_duration=40)
    score = score_heuristics(windows[0])
    assert 0.0 <= score.virality_score <= 1.0
    assert 0.0 <= score.hook_score <= 1.0
    print(f"[ok] heuristics: virality={score.virality_score:.3f} ({score.rationale})")


def check_highlights_without_model() -> None:
    """Force the heuristic path by monkeypatching the model prompt runner."""
    from services.ai import highlights as highlight_module

    original = highlight_module._run_prompt
    highlight_module._run_prompt = lambda *args, **kwargs: ""
    try:
        results = generate_highlights(
            _sample_segments(), clip_count=3, min_duration=15, max_duration=40
        )
    finally:
        highlight_module._run_prompt = original

    assert results, "expected highlights via the heuristic fallback"
    assert len(results) <= 3
    starts = [item.start for item in results]
    assert starts == sorted(starts), "highlights must be ordered by start time"
    print(f"[ok] highlight selection: {len(results)} clips, top score {results[0].virality_score:.3f}")


def check_caption_rendering() -> None:
    import numpy as np

    style = caption_styles.caption_style_to_dict(None)
    words = caption_utils.words_from_payload(
        [
            {"word": "welcome", "start": 0.0, "end": 0.4},
            {"word": "to", "start": 0.4, "end": 0.6},
            {"word": "the", "start": 0.6, "end": 0.9},
            {"word": "show", "start": 0.9, "end": 1.3},
        ],
        clip_start=0.0,
        clip_end=2.0,
        fallback_text="welcome to the show",
    )
    assert len(words) == 4

    groups = caption_utils.group_caption_lines(words, words_per_line=3, max_lines=2)
    assert groups

    frame = np.zeros((1920, 1080, 3), dtype=np.uint8)
    rendered = caption_utils.draw_captions(frame, 1.0, groups, style)
    assert rendered.shape == (1920, 1080, 3)
    assert rendered.any(), "captions should have been drawn onto the frame"
    print(f"[ok] caption rendering: {len(groups)} groups, {int(rendered.sum())} pixel energy")


def check_framing_math() -> None:
    box = framing_utils.compute_crop_box(1920, 1080, 1080, 1920, 0.5, 0.4, 1.0)
    assert len(box) == 4
    width = box[2]
    assert abs(width / box[3] - 1080 / 1920) < 0.02, "crop must match the 9:16 ratio"
    print(f"[ok] framing: crop box x={box[0]} y={box[1]} w={width} h={box[3]}")


def check_storage_paths() -> None:
    from services import storage

    root = storage.ensure_storage()
    assert root.exists()
    name = storage.build_stored_filename("my episode.mp4")
    assert name.endswith(".mp4")
    try:
        storage.build_stored_filename("notes.pdf")
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("unsupported extensions must be rejected")
    print(f"[ok] storage: root={root} sample_filename={name}")


def check_subtitle_export() -> None:
    from services.subtitles import build_subtitle_entries, to_srt, to_vtt

    entries = build_subtitle_entries(
        [
            {"word": token, "start": index * 0.4, "end": index * 0.4 + 0.38}
            for index, token in enumerate("welcome to the show folks".split())
        ],
        clip_start=0.0,
        clip_end=2.0,
        fallback_text="welcome to the show folks",
    )
    assert entries, "expected at least one subtitle cue"
    starts = [entry[0] for entry in entries]
    assert starts == sorted(starts), "cues must be time-ordered"

    srt = to_srt(entries)
    assert srt.splitlines()[1].count("-->") == 1
    assert "," in srt.splitlines()[1], "SRT timestamps use a comma"
    vtt = to_vtt(entries)
    assert vtt.startswith("WEBVTT")
    vtt_timing = next(line for line in vtt.splitlines() if "-->" in line)
    assert "." in vtt_timing, "WebVTT timestamps use a dot"
    print(f"[ok] subtitles: {len(entries)} cues, srt+vtt formatted")


def check_share_link_model() -> None:
    from datetime import datetime, timedelta, timezone

    from models.clip import ClipShare

    now = datetime.now(timezone.utc)
    active = ClipShare(clip_id=1, owner_id=1, token="abc123")
    assert active.is_active, "a fresh share is active"

    expired = ClipShare(
        clip_id=1, owner_id=1, token="def456", expires_at=now - timedelta(days=1)
    )
    assert not expired.is_active, "an expired share must be inactive"

    revoked = ClipShare(clip_id=1, owner_id=1, token="ghi789", revoked_at=now)
    assert not revoked.is_active, "a revoked share must be inactive"

    future = ClipShare(
        clip_id=1, owner_id=1, token="jkl000", expires_at=now + timedelta(days=7)
    )
    assert future.is_active, "a future expiry stays active"
    print("[ok] share links: expiry and revocation gates behave")


def main() -> int:
    checks = [
        check_storage_paths,
        check_candidate_windows,
        check_heuristics,
        check_highlights_without_model,
        check_caption_rendering,
        check_framing_math,
        check_subtitle_export,
        check_share_link_model,
    ]
    failures = 0
    for check in checks:
        try:
            check()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[FAIL] {check.__name__}: {exc}")

    print("\nSmoke test summary:", "ALL PASSED" if failures == 0 else f"{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
