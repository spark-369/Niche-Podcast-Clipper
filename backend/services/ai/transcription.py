"""Local Whisper-based transcription service (Hugging Face Transformers)."""

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from core.config import settings
from services.ai import audio_utils
from services.ai.model_registry import get_asr_pipeline

logger = logging.getLogger(__name__)

# transformers 4.47 emits a stream of deprecation/noise warnings from *inside*
# the Whisper generation loop. None of them are actionable from application
# code (they originate in the library's own call paths), so they are filtered
# for the duration of an ASR call instead of drowning the useful timing logs.
_ASR_IGNORABLE_WARNINGS = (
    # pipelines/automatic_speech_recognition.py itself calls `generate(inputs=..)`.
    r"The input name `inputs` is deprecated",
    # Whisper long-form generation shuttles caches between windows as legacy
    # tuples internally.
    r"Passing a tuple of `past_key_values` is deprecated",
    r"From v4\.47 onwards, when a model cache is to be returned",
    # Chunked windows are always padded to full 30s frames by the feature
    # extractor, so the missing attention mask would be all ones anyway.
    r"The attention mask is not set and cannot be inferred",
    # The final chunk ends where the file ends (often mid-word), so its last
    # timestamp is estimated — expected and harmless.
    r"Whisper did not predict an ending timestamp",
    # Cleared at load time in model_registry; kept here as belt and braces.
    r"You have passed task=transcribe, but also have set `forced_decoder_ids`",
)


@contextmanager
def _suppress_asr_noise():
    """Silence known-benign transformers deprecation noise during an ASR call."""
    import warnings

    with warnings.catch_warnings():
        for pattern in _ASR_IGNORABLE_WARNINGS:
            warnings.filterwarnings("ignore", message=pattern)
        yield


# Robust-decoding knobs passed to Whisper on every call. The converted hub
# checkpoints ship a *minimal* generation config — no quality thresholds — so
# these values replace the library defaults.
#
# transformers 4.47 constraints that shaped this dict:
# - ``temperature`` MUST be a number (``0.0`` == greedy): ``_retrieve_avg_logprobs``
#   evaluates ``temperature > 0.0``, which crashes on ``None``.
# - The temperature *ladder* (a tuple) must NOT be used: its fallback re-decode
#   drops cross-attention weights, crashing word-timestamp extraction
#   (``IndexError … axis 0 with size 0``). A single ``0.0`` means greedy decoding
#   re-uses the same logits, so timestamps stay intact.
# - ``no_speech_threshold`` is left disabled (``None``). When set, Whisper's
#   `should_skip` path marks whole silent segments as ``<|nospeech|>`` and leaves
#   their ``token_timestamps`` empty, which crashes the word-level post-processing
#   ``torch.cat`` step — triggering a chunk-level fallback that drops word-level
#   (karaoke) alignment. Disabling the skip keeps every segment's word timestamps
#   populated so captions align properly; ``compression_ratio_threshold`` is
#   retained as the only fallback trigger (it re-decodes but never skips).
ROBUST_DECODE_KWARGS: dict[str, Any] = {
    "temperature": 0.0,
    "no_speech_threshold": None,
    "logprob_threshold": None,
    "compression_ratio_threshold": 2.4,
}


@dataclass
class TranscribedWord:
    """A single word with start/end timestamps."""

    word: str
    start: float
    end: float
    probability: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "word": self.word,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "probability": self.probability,
        }


@dataclass
class TranscribedSegment:
    """A timestamped chunk of transcript plus optional word alignment."""

    index: int
    start: float
    end: float
    text: str
    words: list[TranscribedWord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start_time": round(self.start, 3),
            "end_time": round(self.end, 3),
            "text": self.text,
            "words": [word.to_dict() for word in self.words],
        }


@dataclass
class TranscriptionResult:
    """Everything the API needs to persist a transcript."""

    text: str
    language: str
    model_name: str
    duration_seconds: float
    word_count: int
    segments: list[TranscribedSegment] = field(default_factory=list)
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "language": self.language,
            "model_name": self.model_name,
            "duration_seconds": self.duration_seconds,
            "word_count": self.word_count,
            "segments": [segment.to_dict() for segment in self.segments],
        }



def _segments_from_word_chunks(
    words: list[dict[str, Any]], max_chars: int = 84, max_gap: float = 0.9
) -> list[TranscribedSegment]:
    """Group word-level chunks into caption friendly segments."""
    segments: list[TranscribedSegment] = []
    buffer: list[TranscribedWord] = []
    buffer_chars = 0
    index = 0

    def flush() -> None:
        nonlocal buffer, buffer_chars, index
        if not buffer:
            return
        segments.append(
            TranscribedSegment(
                index=index,
                start=buffer[0].start,
                end=buffer[-1].end,
                text=" ".join(word.word.strip() for word in buffer).strip(),
                words=list(buffer),
            )
        )
        index += 1
        buffer = []
        buffer_chars = 0

    previous_end: Optional[float] = None
    for item in words:
        start = float(item.get("start") or 0.0)
        end = float(item.get("end") or start)
        word = TranscribedWord(
            word=str(item.get("word") or "").strip(),
            start=start,
            end=max(end, start),
            probability=item.get("probability"),
        )
        if not word.word:
            continue
        if previous_end is not None and word.start - previous_end > max_gap:
            flush()
        buffer.append(word)
        buffer_chars += len(word.word) + 1
        previous_end = word.end
        if buffer_chars >= max_chars:
            flush()
    flush()
    return segments


def _looks_like_word_chunks(chunks: list[dict[str, Any]], full_text: str) -> bool:
    """Heuristically detect word-level Whisper output."""
    if len(chunks) < 2 or len(full_text.split()) <= len(chunks):
        return False
    return all(len(str(chunk.get("text", "")).split()) <= 2 for chunk in chunks)




def transcribe_file(
    media_path: str | Path,
    language: Optional[str] = None,
    word_timestamps: bool = True,
    normalize: bool = True,
) -> TranscriptionResult:
    """Transcribe an audio or video file entirely on-premise.

    Args:
        media_path: Path to a media file whose audio track can be decoded.
        language: Optional ISO language hint; ``None`` lets Whisper detect it.
        word_timestamps: When True, request word level alignment (needed for
            word-by-word karaoke captions).
        normalize: Peak-normalize the waveform before inference.

    Raises:
        FileNotFoundError: if the media file does not exist.
        RuntimeError: if the ASR pipeline fails.
    """
    path = Path(media_path)
    if not path.exists():
        raise FileNotFoundError(f"Media file not found: {path}")

    decode_started = time.perf_counter()
    waveform, sample_rate = audio_utils.load_audio(path)
    if normalize:
        waveform = audio_utils.normalize_waveform(waveform)

    duration = audio_utils.waveform_duration(waveform, sample_rate)
    logger.info(
        "Transcribing %s (%.1fs) with local model %s (audio decoded in %.2fs)",
        path.name,
        duration,
        settings.ASR_MODEL_NAME,
        time.perf_counter() - decode_started,
    )

    asr = get_asr_pipeline()
    generate_kwargs: dict[str, Any] = {"task": "transcribe", **ROBUST_DECODE_KWARGS}
    if language:
        generate_kwargs["language"] = language
    # Pass the model's own (cleaned) generation config explicitly: the pipeline
    # otherwise injects its private copy, which may still carry the
    # checkpoint's stale `forced_decoder_ids` and trip the task/forced-ids
    # conflict warning on every call.
    generate_kwargs["generation_config"] = asr.model.generation_config

    inference_started = time.perf_counter()
    try:
        with _suppress_asr_noise():
            output = asr(
                waveform,
                return_timestamps="word" if word_timestamps else True,
                generate_kwargs=generate_kwargs,
                batch_size=16,
            )
    except Exception as exc:  # pragma: no cover - runtime dependent
        if word_timestamps and _is_empty_token_timestamps_error(exc):
            # Defensive safety net. The no-speech skip that empties
            # ``token_timestamps`` is already disabled via
            # ``ROBUST_DECODE_KWARGS`` (``no_speech_threshold=None``), so this
            # branch should not normally be reached. If an unrelated Whisper
            # edge case still yields an empty timestamp list, retry once at
            # chunk level: captions stay functional, only word-level karaoke
            # alignment is dropped for this episode.
            logger.warning(
                "Word-timestamp inference hit an empty token_timestamps segment; "
                "retrying with chunk-level timestamps only: %s",
                exc,
            )
            try:
                with _suppress_asr_noise():
                    output = asr(
                        waveform,
                        return_timestamps=True,
                        generate_kwargs=generate_kwargs,
                        batch_size=16,
                    )
            except Exception as retry_exc:  # pragma: no cover - runtime dependent
                logger.exception("ASR inference failed (chunk-level retry)")
                raise RuntimeError(
                    f"Local transcription failed: {retry_exc}"
                ) from retry_exc
        else:
            logger.exception("ASR inference failed")
            raise RuntimeError(f"Local transcription failed: {exc}") from exc
    inference_seconds = time.perf_counter() - inference_started
    logger.info(
        "ASR inference for %s took %.1fs (%.2fx realtime)",
        path.name,
        inference_seconds,
        inference_seconds / max(duration, 0.001),
    )

    if isinstance(output, list):
        output = output[0] if output else {}

    full_text = (output.get("text") or "").strip()
    chunks = output.get("chunks") or []
    detected_language = language or output.get("language") or "en"

    segments: list[TranscribedSegment] = []
    if chunks:
        if _looks_like_word_chunks(chunks, full_text):
            words: list[dict[str, Any]] = []
            for chunk in chunks:
                start_raw, end_raw = _chunk_pair(chunk)
                start = _coerce_timestamp(start_raw, 0.0)
                words.append(
                    {
                        "word": str(chunk.get("text", "")).strip(),
                        "start": start,
                        "end": _coerce_timestamp(end_raw, start),
                    }
                )
            segments = _segments_from_word_chunks(words)
        else:
            for position, chunk in enumerate(chunks):
                start_raw, end_raw = _chunk_pair(chunk)
                start = _coerce_timestamp(start_raw, 0.0)
                end = _coerce_timestamp(end_raw, start)
                text = str(chunk.get("text", "")).strip()
                segments.append(
                    TranscribedSegment(
                        index=position,
                        start=start,
                        end=max(end, start),
                        text=text,
                        words=[
                            TranscribedWord(word=token, start=start, end=max(end, start))
                            for token in text.split()
                        ],
                    )
                )

    if not segments and full_text:
        end_of_episode = duration or 0.0
        segments = [
            TranscribedSegment(
                index=0,
                start=0.0,
                end=end_of_episode,
                text=full_text,
                words=[
                    TranscribedWord(word=token, start=0.0, end=end_of_episode)
                    for token in full_text.split()
                ],
            )
        ]

    if not full_text and segments:
        full_text = " ".join(segment.text for segment in segments).strip()

    word_count = len(full_text.split()) or sum(
        len(segment.words) for segment in segments
    )

    return TranscriptionResult(
        text=full_text,
        language=detected_language,
        model_name=settings.ASR_MODEL_NAME,
        duration_seconds=duration,
        word_count=word_count,
        segments=segments,
        raw_payload={
            "text": full_text,
            "chunks": chunks,
            "language": detected_language,
        },
    )


def transcribe_preview(
    media_path: str | Path, start_seconds: float, end_seconds: float
) -> str:
    """Transcribe a slice of media; handy for re-checking a clip boundary."""
    waveform, sample_rate = audio_utils.load_audio(media_path)
    slice_audio = audio_utils.slice_waveform(
        waveform, sample_rate, start_seconds, end_seconds
    )
    asr = get_asr_pipeline()
    with _suppress_asr_noise():
        output = asr(
            slice_audio,
            return_timestamps=True,
            generate_kwargs={"task": "transcribe", **ROBUST_DECODE_KWARGS},
        )
    if isinstance(output, list):
        output = output[0] if output else {}
    return (output.get("text") or "").strip()


def _is_empty_token_timestamps_error(exc: BaseException) -> bool:
    """Detect the word-timestamp crash caused by an empty ``token_timestamps``.

    ``torch.cat([...])`` on an empty list raises ``RuntimeError: torch.cat():
    expected a non-empty list of Tensors``. This is the exact failure the
    transformers word-level post-processing step produces when the
    robust-decoding thresholds cause Whisper to skip a segment entirely,
    leaving its ``token_timestamps`` empty. (Checking the message instead of
    importing torch keeps this dependency-free and robust to message
    formatting changes across versions.)
    """
    while exc is not None:
        message = str(exc).strip()
        if "expected a non-empty list of Tensors" in message or (
            "torch.cat" in message and "non-empty" in message
        ):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _coerce_timestamp(value: Any, fallback: float) -> float:
    """Whisper may return ``(start, end)`` tuples or single floats."""
    if isinstance(value, (tuple, list)):
        value = value[0] if value else None
    if value is None:
        return fallback
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _chunk_pair(chunk: dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    """Normalise the ``timestamp`` field of a Whisper chunk."""
    timestamp = chunk.get("timestamp") or (None, None)
    if isinstance(timestamp, (tuple, list)):
        start = timestamp[0] if len(timestamp) > 0 else None
        end = timestamp[1] if len(timestamp) > 1 else None
        return start, end
    return timestamp, None

