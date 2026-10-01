"""On-premise highlight extraction with a compact local seq2seq model.

Flow:
1. Split the transcript into overlapping, caption-sized candidate windows.
2. Ask the local Flan-T5 checkpoint to rate each window for virality and to
   produce a hook, a summary and a list of keywords.
3. Blend the model signal with deterministic heuristics (hook phrases, numbers,
   sentiment words, question density) so scores stay stable on CPU-only boxes.
"""

import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from core.config import settings
from services.ai.model_registry import get_seq2seq_pipeline

logger = logging.getLogger(__name__)

HOOK_PHRASES = (
    "the truth is",
    "here's why",
    "nobody talks about",
    "most people",
    "what nobody tells you",
    "the biggest mistake",
    "i used to think",
    "let me tell you",
    "the secret",
    "believe it or not",
    "this changed everything",
    "you won't believe",
    "the problem is",
    "stop doing",
    "never do",
    "listen",
    "literally",
    "million",
    "hundred thousand",
)

EMOTION_WORDS = (
    "amazing",
    "insane",
    "crazy",
    "terrible",
    "awful",
    "love",
    "hate",
    "fear",
    "scared",
    "exciting",
    "shocking",
    "surprising",
    "hilarious",
    "frustrating",
    "painful",
    "incredible",
    "unbelievable",
    "heartbreaking",
    "powerful",
    "wild",
)

FILLER_WORDS = ("um", "uh", "you know", "like i said", "kind of", "sort of")

NUMBER_PATTERN = re.compile(r"\b\d+([.,]\d+)?%?\b")


@dataclass
class CandidateWindow:
    """A transcript window considered for clipping."""

    index: int
    start: float
    end: float
    text: str
    words: list[dict[str, Any]] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(self.end - self.start, 0.0)


@dataclass
class HighlightScore:
    """Model + heuristic scores for a candidate window."""

    hook: str = ""
    summary: str = ""
    title: str = ""
    keywords: list[str] = field(default_factory=list)
    model_score: float = 0.5
    hook_score: float = 0.0
    emotion_score: float = 0.0
    quotability_score: float = 0.0
    virality_score: float = 0.0
    rationale: str = ""


@dataclass
class Highlight:
    """A scored highlight ready to become a clip."""

    start: float
    end: float
    title: str
    hook: str
    summary: str
    text: str
    words: list[dict[str, Any]]
    virality_score: float
    hook_score: float
    emotion_score: float
    quotability_score: float
    keywords: list[str]
    model_name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "title": self.title,
            "hook": self.hook,
            "summary": self.summary,
            "text": self.text,
            "words": self.words,
            "virality_score": round(self.virality_score, 3),
            "hook_score": round(self.hook_score, 3),
            "emotion_score": round(self.emotion_score, 3),
            "quotability_score": round(self.quotability_score, 3),
            "keywords": self.keywords,
            "model_name": self.model_name,
        }


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return float(max(low, min(high, value)))


def build_candidate_windows(
    segments: Sequence[dict[str, Any]],
    min_duration: float = 15.0,
    max_duration: float = 60.0,
    stride: Optional[float] = None,
) -> list[CandidateWindow]:
    """Merge consecutive transcript segments into overlapping clip windows."""
    usable = [
        segment
        for segment in segments
        if (segment.get("text") or "").strip()
        and float(segment.get("end_time") or 0.0) > float(segment.get("start_time") or 0.0)
    ]
    if not usable:
        return []

    stride = stride or max(min_duration * 0.5, 5.0)
    windows: list[CandidateWindow] = []
    index = 0

    for start_position in range(len(usable)):
        window_start = float(usable[start_position]["start_time"])
        collected: list[dict[str, Any]] = []
        words: list[dict[str, Any]] = []
        window_end = window_start

        for segment in usable[start_position:]:
            segment_end = float(segment["end_time"])
            segment_duration = segment_end - window_start
            if segment_duration > max_duration and collected:
                break
            collected.append(segment)
            words.extend(segment.get("words") or [])
            window_end = segment_end
            if window_end - window_start >= min_duration:
                break

        if not collected:
            continue
        duration = window_end - window_start
        if duration < min_duration * 0.6:
            continue

        text = " ".join((segment.get("text") or "").strip() for segment in collected)
        windows.append(
            CandidateWindow(
                index=index,
                start=window_start,
                end=window_end,
                text=text.strip(),
                words=words,
            )
        )
        index += 1

        # Stop once the remaining audio cannot fill a minimum window.
        if len(usable) - start_position < 2:
            break
        if window_start + stride >= float(usable[-1]["end_time"]):
            break

    return _deduplicate_windows(windows, stride)


def _deduplicate_windows(
    windows: list[CandidateWindow], stride: float
) -> list[CandidateWindow]:
    """Advance by ``stride`` to keep overlap but drop near-identical windows."""
    if not windows:
        return []
    kept: list[CandidateWindow] = []
    last_start = -math.inf
    for window in windows:
        if window.start - last_start < stride * 0.9:
            continue
        kept.append(window)
        last_start = window.start
    return kept


def score_heuristics(window: CandidateWindow) -> HighlightScore:
    """Deterministic, model-independent scoring used to stabilise T5 output."""
    text = window.text.strip()
    lowered = text.lower()
    word_count = max(len(text.split()), 1)
    duration = max(window.duration, 1.0)
    words_per_minute = word_count / duration * 60.0

    hook_hits = sum(1 for phrase in HOOK_PHRASES if phrase in lowered)
    emotion_hits = sum(1 for word in EMOTION_WORDS if word in lowered)
    filler_hits = sum(1 for filler in FILLER_WORDS if filler in lowered)
    question_hits = text.count("?")
    number_hits = len(NUMBER_PATTERN.findall(text))
    exclamation_hits = text.count("!")
    sentence_count = max(len([s for s in re.split(r"[.!?]+", text) if s.strip()]), 1)

    hook_score = _clamp(0.25 + hook_hits * 0.16 + question_hits * 0.1)
    emotion_score = _clamp(0.2 + emotion_hits * 0.12 + exclamation_hits * 0.05)
    quotability_score = _clamp(
        0.3
        + number_hits * 0.12
        + min(sentence_count / max(word_count / 12.0, 1.0), 1.0) * 0.25
    )

    # Penalise filler-heavy, overly long or near-silent windows.
    filler_penalty = min(filler_hits * 0.05, 0.2)
    density_bonus = 0.1 if 110 <= words_per_minute <= 200 else -0.08
    length_penalty = 0.0
    if duration < 12:
        length_penalty = 0.15
    elif duration > 75:
        length_penalty = 0.1

    model_score = _clamp(
        0.35 + hook_score * 0.3 + emotion_score * 0.2 + quotability_score * 0.15
    )
    virality = _clamp(
        model_score
        + density_bonus
        - filler_penalty
        - length_penalty
        + min(hook_hits, 3) * 0.03
    )

    return HighlightScore(
        model_score=model_score,
        hook_score=hook_score,
        emotion_score=emotion_score,
        quotability_score=quotability_score,
        virality_score=virality,
        rationale=(
            f"hook_hits={hook_hits} emotion_hits={emotion_hits} "
            f"questions={question_hits} numbers={number_hits} wpm={words_per_minute:.0f}"
        ),
    )


TITLE_PROMPT = (
    "You write viral short-form video titles. "
    "Write one punchy title under 60 characters for this podcast excerpt. "
    "Title: {text}"
)

HOOK_PROMPT = (
    "Extract the single most attention-grabbing sentence from this podcast excerpt. "
    "Return only that sentence: {text}"
)

SUMMARY_PROMPT = (
    "Summarise this podcast excerpt in one short sentence for a video caption: {text}"
)

VIRALITY_PROMPT = (
    "Rate from 0 to 100 how likely this podcast excerpt is to go viral as a short clip. "
    "Answer with only a number.\nExcerpt: {text}\nScore:"
)

KEYWORD_PROMPT = "List the 5 most important keywords for this excerpt, separated by commas: {text}"

NUMBER_IN_TEXT = re.compile(r"\b\d{1,3}(?:\.\d+)?\b")


def _truncate(text: str, max_chars: int = 1000) -> str:
    """Flan-T5 has a 512 token window; keep prompts comfortably inside it."""
    cleaned = " ".join(text.split())
    return cleaned if len(cleaned) <= max_chars else cleaned[:max_chars].rsplit(" ", 1)[0]


def _run_prompt(prompt: str, temperature: float, max_new_tokens: int) -> str:
    """Invoke the local seq2seq pipeline with a single prompt."""
    generator = get_seq2seq_pipeline()
    do_sample = temperature > 0.05
    output = generator(
        prompt,
        max_new_tokens=max_new_tokens,
        do_sample=do_sample,
        temperature=max(temperature, 0.01) if do_sample else None,
        num_beams=1 if do_sample else 4,
        truncation=True,
    )
    if isinstance(output, list):
        output = output[0] if output else {}
    return str(output.get("generated_text", "")).strip()


def _parse_model_score(raw: str) -> Optional[float]:
    """Pull a 0-100 (or 0-1) virality rating out of free-form model output."""
    cleaned = raw.strip().lower()
    if not cleaned:
        return None
    lowered_hints = {
        "very high": 0.92,
        "high": 0.8,
        "medium": 0.55,
        "moderate": 0.5,
        "low": 0.28,
        "very low": 0.15,
    }
    for hint, value in lowered_hints.items():
        if hint in cleaned:
            return value
    match = NUMBER_IN_TEXT.search(cleaned)
    if not match:
        return None
    try:
        value = float(match.group())
    except ValueError:
        return None
    if value > 1.0:
        value = value / 100.0
    return _clamp(value)


def _parse_keywords(raw: str) -> list[str]:
    parts = [part.strip(" .;:-").lower() for part in re.split(r"[,\n]", raw or "")]
    keywords: list[str] = []
    for part in parts:
        if 1 < len(part) <= 40 and part not in keywords:
            keywords.append(part)
    return keywords[:5]


def _fallback_title(window: CandidateWindow) -> str:
    """Build a title from the highest-signal sentence when T5 output is empty."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", window.text) if s.strip()]
    if not sentences:
        return f"Highlight at {int(window.start)}s"
    best = max(sentences, key=lambda sentence: len(sentence.split()))
    return best[:70].rstrip("., ") or f"Highlight at {int(window.start)}s"


def analyze_window(
    window: CandidateWindow,
    temperature: float = 0.4,
    focus: Optional[str] = None,
    heuristics: Optional[HighlightScore] = None,
) -> HighlightScore:
    """Score one window with the local seq2seq model plus heuristics.

    ``heuristics`` may be supplied by the caller so a window scored during
    candidate pre-selection is not scored twice.
    """
    heuristics = heuristics or score_heuristics(window)
    excerpt = _truncate(window.text)
    focus_line = f" Focus on: {focus}." if focus else ""

    try:
        title = _run_prompt(
            TITLE_PROMPT.format(text=excerpt) + focus_line,
            temperature=temperature,
            max_new_tokens=32,
        )
        hook = _run_prompt(
            HOOK_PROMPT.format(text=excerpt) + focus_line,
            temperature=temperature,
            max_new_tokens=48,
        )
        summary = _run_prompt(
            SUMMARY_PROMPT.format(text=excerpt) + focus_line,
            temperature=temperature,
            max_new_tokens=48,
        )
        rating = _run_prompt(
            VIRALITY_PROMPT.format(text=excerpt),
            temperature=0.0,
            max_new_tokens=6,
        )
        keywords_raw = _run_prompt(
            KEYWORD_PROMPT.format(text=excerpt),
            temperature=temperature,
            max_new_tokens=32,
        )
        model_rating = _parse_model_score(rating)
        keywords = _parse_keywords(keywords_raw)
    except Exception as exc:  # pragma: no cover - runtime dependent
        logger.warning("Local highlight model failed on window %s: %s", window.index, exc)
        model_rating, keywords = None, []
        title = hook = summary = ""

    title = title.strip('"')[:80] or _fallback_title(window)
    hook = hook.strip('"') or _fallback_title(window)
    summary = summary.strip('"') or hook

    model_score = heuristics.model_score if model_rating is None else _clamp(model_rating)
    return HighlightScore(
        title=title,
        hook=hook,
        summary=summary,
        keywords=keywords,
        model_score=model_score,
        hook_score=heuristics.hook_score,
        emotion_score=heuristics.emotion_score,
        quotability_score=heuristics.quotability_score,
        virality_score=heuristics.virality_score,
        rationale=heuristics.rationale,
    )


def analyze_windows(
    windows: Sequence[CandidateWindow],
    temperature: float = 0.4,
    focus: Optional[str] = None,
    heuristic_scores: Optional[Sequence[HighlightScore]] = None,
    with_ratings: bool = True,
) -> list[HighlightScore]:
    """Score many windows with *batched* model calls.

    The naive per-window loop made five sequential pipeline invocations per
    window, re-encoding the same excerpt each time and leaving the CPU idle
    between decodes. Grouping the prompts by type and handing the pipeline a
    list lets transformers batch them internally, which cuts the scoring stage
    by several times on CPU. A failed batch degrades to the heuristic
    fallbacks instead of failing the whole run.

    ``with_ratings`` disables the virality-rating pass: the selection ranking
    is heuristic-driven and the persisted clips store the heuristic score, so
    the rating never reaches the output in the generation pipeline — skipping
    it removes a fifth of the generation work for free.
    """
    generator = get_seq2seq_pipeline()
    focus_line = f" Focus on: {focus}." if focus else ""
    excerpts = [_truncate(window.text) for window in windows]
    sampled = temperature > 0.05

    def _batch(prompts: list[str], max_new_tokens: int) -> list[str]:
        try:
            outputs = generator(
                prompts,
                max_new_tokens=max_new_tokens,
                do_sample=sampled,
                temperature=max(temperature, 0.01) if sampled else None,
                # The outputs are parsed programmatically (a rating number, a
                # comma list); greedy decoding is plenty and skips the 4-way
                # beam search the single-window path used.
                num_beams=1,
                truncation=True,
                batch_size=8,
            )
        except Exception as exc:  # pragma: no cover - runtime dependent
            logger.warning("Batched highlight prompt failed: %s", exc)
            return [""] * len(prompts)
        if not isinstance(outputs, list):
            outputs = [outputs]
        return [str(item.get("generated_text", "")).strip() for item in outputs]

    titles = _batch([TITLE_PROMPT.format(text=e) + focus_line for e in excerpts], 24)
    hooks = _batch([HOOK_PROMPT.format(text=e) + focus_line for e in excerpts], 40)
    summaries = _batch([SUMMARY_PROMPT.format(text=e) + focus_line for e in excerpts], 40)
    if with_ratings:
        ratings = _batch([VIRALITY_PROMPT.format(text=e) for e in excerpts], 6)
    else:
        ratings = [""] * len(excerpts)
    keywords_raw = _batch([KEYWORD_PROMPT.format(text=e) for e in excerpts], 24)

    results: list[HighlightScore] = []
    for position, window in enumerate(windows):
        heuristics = (
            heuristic_scores[position] if heuristic_scores is not None else None
        ) or score_heuristics(window)
        model_rating = _parse_model_score(ratings[position])
        keywords = _parse_keywords(keywords_raw[position])
        title = titles[position].strip('"')[:80] or _fallback_title(window)
        hook = hooks[position].strip('"') or _fallback_title(window)
        summary = summaries[position].strip('"') or hook
        model_score = heuristics.model_score if model_rating is None else _clamp(model_rating)
        results.append(
            HighlightScore(
                title=title,
                hook=hook,
                summary=summary,
                keywords=keywords,
                model_score=model_score,
                hook_score=heuristics.hook_score,
                emotion_score=heuristics.emotion_score,
                quotability_score=heuristics.quotability_score,
                virality_score=heuristics.virality_score,
                rationale=heuristics.rationale,
            )
        )
    return results


def _overlap_ratio(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Intersection over union for two time ranges."""
    intersection = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    union = max(a[1], b[1]) - min(a[0], b[0])
    if union <= 0:
        return 0.0
    return intersection / union


def select_highlights(
    scored: Iterable[tuple[CandidateWindow, HighlightScore]],
    clip_count: int,
    max_overlap: float = 0.35,
) -> list[tuple[CandidateWindow, HighlightScore]]:
    """Greedily pick the highest scoring, non-redundant windows."""
    ranked = sorted(scored, key=lambda pair: pair[1].virality_score, reverse=True)
    chosen: list[tuple[CandidateWindow, HighlightScore]] = []
    for window, score in ranked:
        if len(chosen) >= clip_count:
            break
        if any(
            _overlap_ratio(
                (window.start, window.end), (other.start, other.end)
            )
            > max_overlap
            for other, _ in chosen
        ):
            continue
        chosen.append((window, score))
    return sorted(chosen, key=lambda pair: pair[0].start)


def generate_highlights(
    segments: Sequence[dict[str, Any]],
    clip_count: int = 5,
    min_duration: float = 15.0,
    max_duration: float = 60.0,
    temperature: float = 0.4,
    focus: Optional[str] = None,
) -> list[Highlight]:
    """Run the full local highlight extraction pipeline.

    Args:
        segments: Transcript segments with ``start_time``/``end_time``/``text``.
        clip_count: Maximum number of highlights to return.
        min_duration: Minimum clip length in seconds.
        max_duration: Maximum clip length in seconds.
        temperature: Sampling temperature for the local seq2seq model.
        focus: Optional creative direction injected into the prompts.

    Returns:
        A start-time ordered list of `Highlight` objects.
    """
    windows = build_candidate_windows(segments, min_duration, max_duration)
    if not windows:
        logger.warning("No candidate windows could be built from the transcript")
        return []

    # Stage 1: cheap deterministic scoring for every window (no model inference).
    heuristic_scores = [score_heuristics(window) for window in windows]

    # Stage 2: the local seq2seq model costs ~5 generations per window, and the
    # ranking is driven entirely by the heuristic virality score (the model only
    # supplies title/hook/summary/keywords). A 90-minute episode yields hundreds
    # of windows, so scoring them all took minutes; restrict the model to a
    # shortlist and keep the rest of the pipeline unchanged.
    budget = max(clip_count * 4, 15)
    if len(windows) > budget:
        shortlist = sorted(
            range(len(windows)),
            key=lambda index: heuristic_scores[index].virality_score,
            reverse=True,
        )[:budget]
    else:
        shortlist = list(range(len(windows)))

    logger.info(
        "Scoring %d of %d candidate windows with %s",
        len(shortlist),
        len(windows),
        settings.HIGHLIGHT_MODEL_NAME,
    )

    scored: list[tuple[CandidateWindow, HighlightScore]] = []
    shortlist_windows = [windows[index] for index in shortlist]
    shortlist_scores = [heuristic_scores[index] for index in shortlist]
    scored = list(
        zip(
            shortlist_windows,
            # ``with_ratings=False``: the selection ranking is heuristic-driven
            # and the persisted clips store the heuristic virality score, so
            # the model rating never reaches the output of this pipeline.
            analyze_windows(
                shortlist_windows,
                temperature,
                focus,
                shortlist_scores,
                with_ratings=False,
            ),
        )
    )

    selected = select_highlights(scored, clip_count)

    highlights: list[Highlight] = []
    for window, score in selected:
        words = _trim_words(window.words, window.start, window.end)
        highlights.append(
            Highlight(
                start=window.start,
                end=window.end,
                title=score.title,
                hook=score.hook,
                summary=score.summary,
                text=window.text,
                words=words,
                virality_score=score.virality_score,
                hook_score=score.hook_score,
                emotion_score=score.emotion_score,
                quotability_score=score.quotability_score,
                keywords=score.keywords,
                model_name=settings.HIGHLIGHT_MODEL_NAME,
            )
        )

    logger.info("Selected %d highlights", len(highlights))
    return highlights


def _trim_words(
    words: Sequence[dict[str, Any]], start: float, end: float
) -> list[dict[str, Any]]:
    """Normalise the window's words for persistence.

    ``words`` are already exactly the words of the selected segments (built by
    extending every collected segment's ``words``), so they correspond 1:1 with
    ``window.text``. We must NOT drop any here: Whisper word boundaries do not
    line up precisely with segment boundaries, so filtering by ``start``/``end``
    shaved off the first/last words — which then made the stored word list
    disagree with the transcript and forced ``words_from_payload`` to abandon the
    real timings in favour of even distribution, breaking audio sync.

    Timestamps are kept **absolute** (the source media's own timeline); every
    consumer rebases them onto the clip start via ``words_from_payload``.
    """
    trimmed: list[dict[str, Any]] = []
    for word in words or []:
        text = str(word.get("word") or word.get("text") or "").strip()
        if not text:
            continue
        try:
            word_start = float(word.get("start") or 0.0)
            word_end = float(word.get("end") or word_start)
        except (TypeError, ValueError):
            continue
        if word_end <= word_start:
            word_end = word_start + 0.05
        trimmed.append(
            {
                "word": text,
                "start": round(word_start, 3),
                "end": round(max(min(word_end, end), word_start + 0.001), 3),
            }
        )
    return trimmed


def describe_model() -> dict[str, Any]:
    """Metadata about the locally hosted highlight model."""
    return {
        "highlight_model": settings.HIGHLIGHT_MODEL_NAME,
        "asr_model": settings.ASR_MODEL_NAME,
        "device": settings.AI_DEVICE,
        "prompt_temperature_default": 0.4,
        "hook_phrases": len(HOOK_PHRASES),
        "emotion_words": len(EMOTION_WORDS),
    }


def score_to_json(score: HighlightScore) -> str:  # pragma: no cover - debug helper
    """Serialize a score for logging."""
    return json.dumps(
        {
            "title": score.title,
            "hook": score.hook,
            "virality": round(score.virality_score, 3),
            "rationale": score.rationale,
        }
    )
