"""On-premise AI services backed by local Hugging Face checkpoints."""

from services.ai.highlights import Highlight, generate_highlights, score_heuristics
from services.ai.model_registry import (
    get_asr_pipeline,
    get_seq2seq_pipeline,
    release_models,
    warmup_models,
)
from services.ai.transcription import TranscriptionResult, transcribe_file

__all__ = [
    "Highlight",
    "TranscriptionResult",
    "generate_highlights",
    "get_asr_pipeline",
    "get_seq2seq_pipeline",
    "release_models",
    "score_heuristics",
    "transcribe_file",
    "warmup_models",
]
