"""Lazy, process-wide singletons for the locally hosted Hugging Face models.

Models are downloaded once into ``HF_HOME`` (``./models``) and then reused by
both the FastAPI process and Celery workers. Nothing is sent to the cloud.
"""

import logging
import threading
from typing import Any, Optional

from core.config import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_asr_pipeline: Optional[Any] = None
_seq2seq_pipeline: Optional[Any] = None
_transformers_log_quieted = False


def _quiet_transformers_log_noise() -> None:
    """Drop known-benign transformers *log* warnings, once per process.

    Unlike the warnings-module filters used around ASR calls, transformers
    emits these notices through its own logging system (``logger.warning(_once)
    in generation/model code), so a logging filter attached to the emitting
    module loggers is the right tool. None are actionable from application
    code on the pinned transformers version:

    - the chunked pipeline passes ``task`` on every generate call and whisper
      generation re-derives decoder ids itself, so the stale checkpoint
      ``forced_decoder_ids`` notice fires per run with freshly computed ids;
    - whisper long-form passes legacy tuple caches between internal calls;
    - the ``Cache`` instance notice is a future output-format change;
    - the attention-mask notice is moot: every 30s window is padded to full
      length, so the mask would be all ones;
    - the missing ending timestamp is emitted by the tokenizer's chunk-merging
      step (``tokenization_whisper._decode_asr``) when the file ends mid-word;
      the final chunk is still resolved normally afterwards.
    """
    global _transformers_log_quieted
    if _transformers_log_quieted:
        return
    _transformers_log_quieted = True

    class _NoiseFilter(logging.Filter):
        _PATTERNS = (
            "You have passed task=",
            "Passing a tuple of `past_key_values` is deprecated",
            "From v4.47 onwards, when a model cache is to be returned",
            "The attention mask is not set and cannot be inferred",
            "Whisper did not predict an ending timestamp",
            # transformers announces the resolved device (cpu here) at WARNING
            # level on every from_pretrained/pipeline load; purely informational.
            "Device set to use",
        )

        def filter(self, record: logging.LogRecord) -> bool:
            message = record.getMessage()
            return not any(pattern in message for pattern in self._PATTERNS)

    noise_filter = _NoiseFilter()
    for name in (
        "transformers.generation.utils",
        "transformers.models.whisper.generation_whisper",
        "transformers.models.whisper.modeling_whisper",
        "transformers.models.whisper.tokenization_whisper",
        "transformers.models.whisper.tokenization_whisper_fast",
        "transformers.cache_utils",
        "transformers.modeling_utils",
    ):
        logging.getLogger(name).addFilter(noise_filter)


def _resolve_device(device: Optional[str] = None) -> int:
    """Translate a device string into the integer expected by transformers."""
    requested = (device or settings.AI_DEVICE or "cpu").lower()
    if requested.startswith("cuda"):
        try:
            import torch  # imported lazily to keep startup fast

            if torch.cuda.is_available():
                if ":" in requested:
                    return int(requested.split(":", 1)[1])
                return 0
            logger.warning("CUDA requested but unavailable; falling back to CPU")
        except Exception as exc:  # pragma: no cover - torch optional at runtime
            logger.warning("Torch CUDA check failed (%s); using CPU", exc)
    return -1  # -1 == CPU for Hugging Face pipelines


def _cuda_toolkit_available() -> bool:
    """Return True only if a CUDA runtime library is actually loadable.

    A ``cu130``-linked wheel (e.g. torch 2.11) on a host with no matching CUDA
    runtime fails at *import* time with
    ``libcudart.so.13: cannot open shared object file``. Checking for the
    library up front lets callers downgrade to CPU instead of crashing.
    """
    import ctypes.util

    for name in ("cudart", "cuda"):
        try:
            if ctypes.util.find_library(name):
                return True
        except Exception:  # pragma: no cover - platform dependent
            continue
    return False


def _apply_offline_env() -> None:
    """Point transformers at the local cache and decide online/offline mode.

    With ``TRANSFORMERS_OFFLINE`` the configured models must already be cached;
    a freshly switched ``ASR_MODEL_NAME``/``HIGHLIGHT_MODEL_NAME`` would then be
    unable to download at all. Offline mode is therefore only applied when both
    configured models are actually present in the local hub cache — a new model
    name transparently gets one online download, after which offline mode kicks
    back in on the next start.
    """
    import os
    from pathlib import Path

    os.environ.setdefault("HF_HOME", settings.HF_HOME)

    if not settings.TRANSFORMERS_OFFLINE:
        return

    hub = Path(os.environ["HF_HOME"]).expanduser() / "hub"
    cached = all(
        (hub / ("models--" + name.replace("/", "--"))).is_dir()
        for name in (settings.ASR_MODEL_NAME, settings.HIGHLIGHT_MODEL_NAME)
    )
    if not cached:
        logger.warning(
            "TRANSFORMERS_OFFLINE is enabled but '%s' / '%s' is not in the local "
            "cache yet; allowing online access for the one-time download",
            settings.ASR_MODEL_NAME,
            settings.HIGHLIGHT_MODEL_NAME,
        )
        return

    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_OFFLINE"] = "1"


def get_asr_pipeline(device: Optional[str] = None) -> Any:
    """Return the cached automatic-speech-recognition pipeline.

    Uses ``openai/whisper-base`` by default: small, fast on CPU, and accurate
    enough for spoken-word podcast content.
    """
    global _asr_pipeline
    if _asr_pipeline is not None:
        return _asr_pipeline

    with _lock:
        if _asr_pipeline is not None:
            return _asr_pipeline
        _apply_offline_env()

        from transformers import (
            AutoModelForSpeechSeq2Seq,
            AutoProcessor,
            pipeline,
        )

        logger.info("Loading local ASR model '%s'...", settings.ASR_MODEL_NAME)
        _quiet_transformers_log_noise()
        # ``eager`` attention keeps Whisper's cross-attention weights available
        # (needed for word-level timestamps) and avoids the SDPA fallback
        # warning at inference time.
        model = AutoModelForSpeechSeq2Seq.from_pretrained(
            settings.ASR_MODEL_NAME,
            attn_implementation="eager",
        )
        processor = AutoProcessor.from_pretrained(settings.ASR_MODEL_NAME)
        _asr_pipeline = pipeline(
            task="automatic-speech-recognition",
            model=model,
            tokenizer=processor.tokenizer,
            feature_extractor=processor.feature_extractor,
            device=_resolve_device(device),
            chunk_length_s=settings.ASR_CHUNK_LENGTH_S,
            # The chunker strides on both sides (step = chunk - 2 * stride),
            # so keep the stride short or large parts of the audio are decoded
            # multiple times (see ASR_STRIDE_S comment in core.config).
            stride_length_s=min(settings.ASR_STRIDE_S, settings.ASR_CHUNK_LENGTH_S // 4),
            return_timestamps="word",
        )
        # Whisper checkpoints converted for the hub ship ``forced_decoder_ids``
        # in their generation config, which conflicts with the
        # ``task=transcribe`` argument used at inference time; the pipeline
        # derives the correct decoder ids itself, so drop the stale ones. The
        # pipeline keeps its own (possibly copied) generation config and
        # injects it at generate time, so clear both objects.
        generation_config = getattr(model, "generation_config", None)
        if (
            generation_config is not None
            and getattr(generation_config, "forced_decoder_ids", None) is not None
        ):
            generation_config.forced_decoder_ids = None
        pipeline_generation_config = getattr(_asr_pipeline, "generation_config", None)
        if (
            pipeline_generation_config is not None
            and getattr(pipeline_generation_config, "forced_decoder_ids", None) is not None
        ):
            pipeline_generation_config.forced_decoder_ids = None
        logger.info("Local ASR model ready")
        return _asr_pipeline


def get_seq2seq_pipeline(device: Optional[str] = None) -> Any:
    """Return the cached text2text-generation pipeline (Flan-T5 by default)."""
    global _seq2seq_pipeline
    if _seq2seq_pipeline is not None:
        return _seq2seq_pipeline

    with _lock:
        if _seq2seq_pipeline is not None:
            return _seq2seq_pipeline
        _apply_offline_env()

        from transformers import pipeline

        logger.info(
            "Loading local highlight model '%s'...", settings.HIGHLIGHT_MODEL_NAME
        )
        _seq2seq_pipeline = pipeline(
            task="text2text-generation",
            model=settings.HIGHLIGHT_MODEL_NAME,
            device=_resolve_device(device),
        )
        logger.info("Local highlight model ready")
        return _seq2seq_pipeline


def warmup_models(which: str = "both") -> dict[str, bool]:
    """Optionally preload models at startup; failures are reported, not raised."""
    status = {"asr": False, "highlight": False}
    if which in ("asr", "both"):
        try:
            get_asr_pipeline()
            status["asr"] = True
        except Exception as exc:  # pragma: no cover - network/disk dependent
            logger.error("ASR warmup failed: %s", exc)
    if which in ("highlight", "both"):
        try:
            get_seq2seq_pipeline()
            status["highlight"] = True
        except Exception as exc:  # pragma: no cover
            logger.error("Highlight model warmup failed: %s", exc)
    return status


def release_models() -> None:
    """Drop cached pipelines and free memory (used by long-running workers)."""
    global _asr_pipeline, _seq2seq_pipeline
    _asr_pipeline = None
    _seq2seq_pipeline = None
    try:
        import gc

        import torch

        gc.collect()
        if torch.cuda.is_available() and _cuda_toolkit_available():  # pragma: no cover - GPU only
            torch.cuda.empty_cache()
    except Exception:  # pragma: no cover
        pass
