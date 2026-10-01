"""OpenCV based media probing: duration, resolution, fps and frame extraction."""

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import cv2

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}


@dataclass
class ProbeResult:
    """Technical metadata discovered by probing a media file."""

    duration_seconds: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    frame_count: int = 0
    has_video_stream: bool = False
    media_type: str = "audio"
    aspect_ratio: float = 0.0
    codec_fourcc: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def is_audio_extension(path: str | Path) -> bool:
    return Path(path).suffix.lower() in AUDIO_EXTENSIONS


def is_video_extension(path: str | Path) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS


def probe_media(path: str | Path) -> ProbeResult:
    """Inspect a media file using OpenCV only (no FFmpeg binaries).

    Falls back to ``cv2.CAP_PROP_*`` heuristics when the container does not
    expose exact values (common for audio-only or variable frame rate files).
    """
    media_path = Path(path)
    result = ProbeResult()

    if not media_path.exists():
        raise FileNotFoundError(f"Media file not found: {media_path}")

    result.media_type = "video" if is_video_extension(media_path) else "audio"

    capture = cv2.VideoCapture(str(media_path))
    try:
        if not capture.isOpened():
            logger.warning("OpenCV could not open %s; returning defaults", media_path)
            return result

        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        fourcc_value = int(capture.get(cv2.CAP_PROP_FOURCC) or 0)

        result.fps = round(fps, 3)
        result.frame_count = frames
        result.width = width
        result.height = height
        result.has_video_stream = bool(width and height)

        if fps > 0 and frames > 0:
            result.duration_seconds = round(frames / fps, 3)
        else:
            # Some containers need a millisecond based reading.
            millis = float(capture.get(cv2.CAP_PROP_POS_MSEC) or 0.0)
            result.duration_seconds = round(millis / 1000.0, 3)

        if result.has_video_stream:
            result.media_type = "video"
            result.aspect_ratio = round(width / height, 4) if height else 0.0

        if fourcc_value:
            result.codec_fourcc = "".join(
                [chr((fourcc_value >> (8 * i)) & 0xFF) for i in range(4)]
            ).strip()
    finally:
        capture.release()

    return result


def extract_frame(
    path: str | Path, timestamp_seconds: float, save_to: Optional[str | Path] = None
):
    """Grab a single frame at ``timestamp_seconds`` and optionally write it to disk."""
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            return None
        capture.set(cv2.CAP_PROP_POS_MSEC, max(timestamp_seconds, 0.0) * 1000.0)
        success, frame = capture.read()
        if not success:
            return None
        if save_to is not None:
            out_path = Path(save_to)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(out_path), frame)
        return frame
    finally:
        capture.release()


def iter_frames(path: str | Path, start_seconds: float = 0.0, end_seconds: Optional[float] = None):
    """Yield ``(timestamp, frame)`` tuples between two timestamps."""
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            return
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 30.0) or 30.0
        capture.set(cv2.CAP_PROP_POS_MSEC, max(start_seconds, 0.0) * 1000.0)
        frame_index = int(max(start_seconds, 0.0) * fps)
        while True:
            success, frame = capture.read()
            if not success:
                break
            timestamp = frame_index / fps
            if end_seconds is not None and timestamp > end_seconds:
                break
            yield timestamp, frame
            frame_index += 1
    finally:
        capture.release()
