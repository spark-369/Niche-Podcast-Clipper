"""Audio decoding helpers that feed the Whisper pipeline.

``soundfile`` reads plain WAV/FLAC inputs directly. Compressed or video
containers (``.mp4``, ``.m4a``, ``.mp3``, ...) cannot be demuxed by libsndfile,
so their audio track is first decoded with the prebuilt FFmpeg binary shipped
with ``imageio-ffmpeg`` (the same binary the video renderer uses) into a
temporary 16 kHz mono WAV. ``librosa``/``audioread`` stays as a last-resort
fallback for exotic inputs.
"""

import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

TARGET_SAMPLE_RATE = 16000


def _bundled_ffmpeg() -> Optional[str]:
    """Return the prebuilt FFmpeg binary shipped with imageio-ffmpeg, if any."""
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # pragma: no cover - package optional at runtime
        logger.debug("imageio-ffmpeg unavailable; cannot decode media containers")
        return None


def _decode_with_ffmpeg(path: Path, sample_rate: int) -> Tuple[np.ndarray, int]:
    """Decode any media container to mono PCM via the bundled FFmpeg binary.

    The track is resampled to ``sample_rate`` and mixed down to mono during the
    decode, so no further resampling is needed afterwards.
    """
    import soundfile as sf

    ffmpeg = _bundled_ffmpeg()
    if ffmpeg is None:
        raise RuntimeError("No FFmpeg binary available for audio decoding")

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        wav_path = Path(handle.name)
    try:
        command = [
            ffmpeg,
            "-y",
            "-i", str(path),
            "-vn",  # drop any video stream
            "-ac", "1",  # mono
            "-ar", str(sample_rate),  # resample to the Whisper rate
            "-f", "wav",
            str(wav_path),
        ]
        completed = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"FFmpeg could not decode {path.name}")
        samples, native_rate = sf.read(str(wav_path), dtype="float32", always_2d=True)
        return samples.mean(axis=1), int(native_rate)
    finally:
        wav_path.unlink(missing_ok=True)


def load_audio(
    path: str | Path, sample_rate: int = TARGET_SAMPLE_RATE
) -> Tuple[np.ndarray, int]:
    """Load a mono float32 waveform resampled to ``sample_rate`` (16 kHz by default).

    Returns:
        (waveform, sample_rate) where waveform is a 1-D float32 numpy array.
    """
    audio_path = Path(path)
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio source not found: {audio_path}")

    try:
        import soundfile as sf

        samples, native_rate = sf.read(str(audio_path), dtype="float32", always_2d=True)
        waveform = samples.mean(axis=1)
    except Exception as exc:
        logger.debug(
            "soundfile could not read %s (%s); decoding the audio track with FFmpeg",
            audio_path,
            exc,
        )
        try:
            waveform, native_rate = _decode_with_ffmpeg(audio_path, sample_rate)
        except Exception as ffmpeg_exc:
            logger.debug(
                "FFmpeg decode failed (%s); falling back to librosa", ffmpeg_exc
            )
            import librosa

            waveform, native_rate = librosa.load(
                str(audio_path), sr=None, mono=True, dtype="float32"
            )

    if native_rate != sample_rate:
        import librosa

        waveform = librosa.resample(
            np.asarray(waveform, dtype=np.float32),
            orig_sr=native_rate,
            target_sr=sample_rate,
        )
        native_rate = sample_rate

    waveform = np.ascontiguousarray(waveform, dtype=np.float32)
    return waveform, native_rate


def waveform_duration(waveform: np.ndarray, sample_rate: int) -> float:
    """Duration in seconds of a waveform array."""
    if sample_rate <= 0:
        return 0.0
    return round(len(waveform) / float(sample_rate), 3)


def slice_waveform(
    waveform: np.ndarray,
    sample_rate: int,
    start_seconds: float,
    end_seconds: Optional[float] = None,
) -> np.ndarray:
    """Return the portion of a waveform between two timestamps."""
    start = max(int(start_seconds * sample_rate), 0)
    end = len(waveform) if end_seconds is None else int(end_seconds * sample_rate)
    return waveform[start:end]


def normalize_waveform(waveform: np.ndarray, target_peak: float = 0.95) -> np.ndarray:
    """Peak-normalize a waveform so quiet podcast audio transcribes better."""
    if waveform.size == 0:
        return waveform
    peak = float(np.max(np.abs(waveform)))
    if peak <= 1e-6:
        return waveform
    return np.clip(waveform * (target_peak / peak), -1.0, 1.0).astype(np.float32)
