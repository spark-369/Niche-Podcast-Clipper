"""Vertical clip renderer: crop, auto-frame, burn captions and encode.

The frame pipeline is OpenCV only (``VideoCapture`` -> frame loop ->
``VideoWriter``). When OpenCV has no H.264 encoder available, the rendered
``mp4v`` file is transparently transcoded to browser-playable H.264 with the
FFmpeg binary bundled in the ``imageio-ffmpeg`` package.
"""

import contextlib
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import cv2
import numpy as np

from services.media_probe import probe_media
from services.video import captions as caption_utils
from services.video import framing as framing_utils

logger = logging.getLogger(__name__)

# Maps the FFmpeg binary path to the H.264 encoder that last worked with it, so
# a render that already proved ``libx264`` usable does not re-probe (and fail
# through) the other candidates on every subsequent clip.
_working_h264_encoder: dict[str, str] = {}
# One-time ``-encoders`` probe results per FFmpeg binary, so the pipe writer is
# only used when the bundled build actually ships libx264.
_libx264_probe: dict[str, bool] = {}

ProgressCallback = Callable[[int], None]


class _FfmpegPipeWriter:
    """Stream raw BGR frames into a live FFmpeg libx264 encode via stdin.

    OpenCV's bundled codecs usually lack an H.264 encoder, so the previous flow
    encoded every frame twice (``mp4v`` via OpenCV, then an FFmpeg transcode).
    Piping frames straight into the bundled FFmpeg binary encodes **once**, at
    browser-playable H.264, with a faster preset — roughly halving the encode
    stage while producing the same bytes.
    """

    def __init__(self, process: subprocess.Popen) -> None:
        self._process = process

    def write(self, frame: np.ndarray) -> None:
        if self._process.poll() is not None:
            raise RuntimeError("FFmpeg pipe encoder exited before the render finished")
        self._process.stdin.write(np.ascontiguousarray(frame).tobytes())

    def release(self) -> None:
        try:
            self._process.stdin.close()
        except OSError:  # pragma: no cover - already closed
            pass
        try:
            self._process.wait(timeout=180)
        except subprocess.TimeoutExpired:  # pragma: no cover - stuck encoder
            self._process.kill()
            self._process.wait(timeout=10)


def _ffmpeg_supports_libx264(ffmpeg: str) -> bool:
    """One-time ``-encoders`` probe so the pipe path is only used when usable."""
    cached = _libx264_probe.get(ffmpeg)
    if cached is not None:
        return cached
    try:
        probe = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True,
            timeout=15,
            check=False,
        )
        supported = b"libx264" in probe.stdout
    except Exception:  # pragma: no cover - probe is best effort
        supported = False
    _libx264_probe[ffmpeg] = supported
    return supported


def _open_ffmpeg_pipe_writer(
    path: Path, fps: float, size: tuple[int, int]
) -> tuple[Optional[_FfmpegPipeWriter], str]:
    """Encode frames by piping them into the bundled FFmpeg (single pass).

    Returns ``(writer, codec)`` or ``(None, "")`` when the pipe path is not
    usable, letting the caller fall back to the OpenCV writer.
    """
    try:
        import imageio_ffmpeg  # type: ignore

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None, ""
    if not _ffmpeg_supports_libx264(ffmpeg):
        return None, ""

    width, height = size
    command = [
        ffmpeg,
        "-y",
        "-loglevel", "error",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}",
        "-r", f"{fps:.6f}",
        "-i", "pipe:0",
        "-an",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        # Tolerate odd dimensions instead of failing the encode.
        "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
        "-movflags", "+faststart",
        str(path),
    ]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None, ""
    logger.debug("FFmpeg pipe writer started for %s", path)
    return _FfmpegPipeWriter(process), "libx264-pipe"


@dataclass
class RenderResult:
    """Outcome of a clip render."""

    output_path: str
    thumbnail_path: Optional[str] = None
    # Caption-free companion render written for the editor preview. It shares the
    # deliverable's frames *and* audio but has no burned captions, so the editor's
    # draft overlay is the only caption track.
    preview_path: Optional[str] = None
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration_seconds: float = 0.0
    frame_count: int = 0
    size_bytes: int = 0
    captions_burned: int = 0
    codec: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_path": self.output_path,
            "thumbnail_path": self.thumbnail_path,
            "preview_path": self.preview_path,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "duration_seconds": self.duration_seconds,
            "frame_count": self.frame_count,
            "size_bytes": self.size_bytes,
            "captions_burned": self.captions_burned,
            "codec": self.codec,
            "warnings": self.warnings,
        }


@contextlib.contextmanager
def _quiet_stderr():
    """Silence C-level stderr noise (fd 2) from OpenCV's codec probing.

    Celery replaces ``sys.stderr`` with a ``LoggingProxy`` that has no
    ``fileno()``, so we duplicate the raw file descriptor 2 directly rather
    than touching ``sys.stderr``. If the redirect cannot be established the
    caller simply continues with stderr as-is.
    """
    try:
        saved_fd = os.dup(2)
        devnull = os.open(os.devnull, os.O_WRONLY)
    except (OSError, ValueError):  # pragma: no cover - e.g. closed stderr
        yield
        return
    try:
        os.dup2(devnull, 2)
        yield
    finally:
        os.dup2(saved_fd, 2)
        os.close(saved_fd)
        os.close(devnull)


def _open_writer(
    path: Path, fps: float, size: tuple[int, int]
) -> tuple[Any, str]:
    """Create a video writer, preferring a single-pass H.264 encode.

    The fastest path pipes raw frames straight into the bundled FFmpeg with
    libx264: one encode, browser-playable, and no transcode stage afterwards.
    When that is unavailable, OpenCV codecs are attempted (``avc1``/``H264``
    first; ``mp4v`` is a last resort that ``_finalize_encoded_video``
    transcodes to H.264 afterwards).

    OpenCV emits noisy C++ stderr while probing for codecs it does not have;
    that output is suppressed here since the fallback is expected behaviour.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    pipe_writer, pipe_codec = _open_ffmpeg_pipe_writer(path, fps, size)
    if pipe_writer is not None:
        return pipe_writer, pipe_codec

    fallback: Optional[cv2.VideoWriter] = None
    for fourcc_name in ("avc1", "H264", "mp4v"):
        writer: Any = None
        try:
            with _quiet_stderr():
                writer = cv2.VideoWriter(
                    str(path),
                    cv2.VideoWriter_fourcc(*fourcc_name),  # type: ignore[attr-defined]
                    fps,
                    size,
                )
            if writer.isOpened():
                logger.debug("OpenCV writer opened with codec %s", fourcc_name)
                if fourcc_name == "mp4v":
                    # Remember mp4v but keep trying for a browser-playable codec.
                    fallback = writer
                    continue
                return writer, fourcc_name
        finally:
            # A rejected writer is never returned, so it has to be released
            # here. It still holds an open handle on ``path``; leaking it left
            # the file locked and broke the later ``temp_path.replace(path)``
            # of the H.264 transcode.
            if writer is not None and writer is not fallback:
                with _quiet_stderr():
                    writer.release()

    with _quiet_stderr():
        if fallback is not None:
            return fallback, "mp4v"

        writer = cv2.VideoWriter(
            str(path),
            cv2.VideoWriter_fourcc(*"mp4v"),  # type: ignore[attr-defined]
            fps,
            size,
        )
        if not writer.isOpened():  # pragma: no cover - environment dependent
            writer.release()
            raise RuntimeError(
                "Could not open an OpenCV VideoWriter. Install a codec-enabled "
                "'opencv-python-headless' build (avc1/mp4v)."
            )
    return writer, "mp4v"


def _extract_source_audio(
    source: Path, start_time: float, end_time: float, output: Path
) -> bool:
    """Extract the clip's audio segment from the source media to AAC.

    Returns ``True`` when a usable audio file was written. Source audio
    timestamps are preserved as-is, so callers muxing it under re-timed video
    must account for the offset themselves.
    """
    try:
        import imageio_ffmpeg  # type: ignore
    except ImportError:
        return False

    duration = max(end_time - start_time, 0.1)
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-y",
        "-ss", f"{max(start_time, 0.0):.3f}",
        "-t", f"{duration:.3f}",
        "-i", str(source),
        "-vn",
        "-ac", "2",
        "-ar", "44100",
        "-c:a", "aac",
        "-b:a", "128k",
        str(output),
    ]
    try:
        proc = subprocess.run(command, capture_output=True, timeout=600, check=False)
    except Exception as exc:  # pragma: no cover - environment dependent
        logger.warning("Audio extraction failed: %s", exc)
        return False
    if proc.returncode != 0 or not output.exists() or output.stat().st_size < 1024:
        return False
    return True


def _reencode_to_h264(
    path: Path,
    audio_path: Optional[Path] = None,
) -> bool:
    """Re-encode an mp4v file to H.264 in place using imageio-ffmpeg's binary.

    ``mp4v`` (MPEG-4 Part 2) does not play in web browsers, so when no H.264
    encoder is available to OpenCV we transcode with the FFmpeg binary bundled
    inside the ``imageio-ffmpeg`` wheel (no system FFmpeg required). When
    ``audio_path`` is provided its track is muxed into the output.

    The bundled ``imageio-ffmpeg`` FFmpeg build is minimal and frequently ships
    **without** ``libx264``, so a list of H.264 encoders is probed in turn. If
    none is usable, FFmpeg's real stderr is logged rather than failing silently,
    so the cause of an ``mp4v`` fallback is visible instead of opaque.
    """
    try:
        import imageio_ffmpeg  # type: ignore
    except ImportError:
        return False

    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    temp_path = path.with_name(path.stem + ".h264.mp4")
    # Try the encoder that already worked first; probing the others costs a
    # failed encode of the whole clip on every render otherwise.
    candidates = (
        "libx264",
        "libopenh264",
        "h264_videotoolbox",
        "h264_nvenc",
    )
    cached = _working_h264_encoder.get(ffmpeg_exe)
    h264_encoders = (
        (cached, *(name for name in candidates if name != cached))
        if cached
        else candidates
    )
    last_error = ""
    for encoder in h264_encoders:
        command = [
            ffmpeg_exe,
            "-y",
            "-i", str(path),
        ]
        if audio_path is not None:
            command += ["-i", str(audio_path)]
        command += [
            # Explicitly select only the video from the source, so any placeholder
            # audio track OpenCV left in the mp4v container is dropped rather than
            # carried through alongside the replacement track.
            "-map", "0:v:0",
            "-c:v", encoder,
            "-preset", "veryfast",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
        ]
        if audio_path is not None:
            command += [
                "-map", "1:a:0",
                "-c:a", "aac",
                "-b:a", "128k",
                "-ac", "2",
                "-shortest",
            ]
        command += [
            "-map_metadata", "-1",
            "-movflags", "+faststart",
            str(temp_path),
        ]
        try:
            proc = subprocess.run(
                command, capture_output=True, timeout=1800, check=False
            )
        except Exception as exc:  # pragma: no cover - environment dependent
            last_error = f"{encoder}: {exc}"
            temp_path.unlink(missing_ok=True)
            continue

        if proc.returncode == 0 and temp_path.exists() and _verify_output(temp_path):
            # Same invariant as `_mux_audio`: exactly one audio track, otherwise the
            # original and the replacement would play together.
            if audio_path is not None:
                streams = _count_audio_streams(temp_path)
                if streams is not None and streams != 1:
                    logger.warning(
                        "H.264 re-encode produced %s audio streams in %s; "
                        "discarding the transcode and trying the next encoder",
                        streams,
                        temp_path,
                    )
                    temp_path.unlink(missing_ok=True)
                    # Try the next candidate rather than giving up outright: a
                    # malformed audio map from one encoder should not force the
                    # unplayable mp4v original on the user.
                    last_error = f"{encoder}: re-encode contained {streams} audio streams"
                    continue
            temp_path.replace(path)
            # Remember the winner so later renders skip the failed probes.
            _working_h264_encoder[ffmpeg_exe] = encoder
            return True

        # This encoder was rejected (e.g. "Unknown encoder 'libx264'"); capture
        # the real reason and try the next candidate instead of failing silently.
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
        last_error = f"{encoder}: {detail[-400:]}"
        temp_path.unlink(missing_ok=True)
        logger.warning(
            "H.264 encoder %r unavailable (%s); trying next candidate",
            encoder,
            detail[-200:],
        )

    # Re-encoding is possible but no candidate produced a usable file. The
    # destination still holds the caller's original mp4v render, which is what
    # the warning tells the user about.
    logger.warning(
        "H.264 encoder unavailable; wrote MPEG-4 Part 2 (mp4v), which does not "
        "play in most web browsers. No usable H.264 encoder was found. "
        "Last error: %s. Install a full FFmpeg with libx264 (or run "
        "OpenCV with an H.264 encoder) so clips render browser-playable.",
        last_error,
    )
    return False


def _mux_audio(video_path: Path, audio_path: Path) -> bool:
    """Add an AAC audio track to an already H.264-encoded MP4 in place.

    The output is guaranteed to carry exactly one audio stream. OpenCV's
    ``avc1`` writer can leave a placeholder audio track inside the container, so
    the mapping must be explicit and exclusive: only ``0:v:0`` and ``1:a:0`` are
    selected. A permissive map such as ``1:a:0?`` combined with FFmpeg's default
    stream selection can carry the pre-existing track into the output *as well*,
    and players then mix two audio tracks — which is exactly the "both tracks are
    running" symptom.
    """
    try:
        import imageio_ffmpeg  # type: ignore
    except ImportError:
        return False

    temp_path = video_path.with_name(video_path.stem + ".mux.mp4")
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-y",
        "-i", str(video_path),
        "-i", str(audio_path),
        # Map only the video from input 0 and only the replacement audio from
        # input 1. Because the mapping is explicit and nothing else is selected,
        # any audio track already inside the rendered MP4 is excluded from the
        # output — the container ends up with exactly one audio stream.
        "-map", "0:v:0",
        "-map", "1:a:0",
        # Re-encode the replacement audio and copy the video untouched.
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ac", "2",
        "-shortest",
        # Start from a clean container so no stale track metadata survives.
        "-map_metadata", "-1",
        "-movflags", "+faststart",
        str(temp_path),
    ]
    try:
        proc = subprocess.run(command, capture_output=True, timeout=1800, check=False)
    except Exception as exc:  # pragma: no cover - environment dependent
        logger.warning("Audio mux failed: %s", exc)
        return False
    if proc.returncode != 0 or not temp_path.exists() or not _verify_output(temp_path):
        logger.warning("Audio mux failed: %s", proc.stderr[-500:] if proc.stderr else "")
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        return False

    # Guard the invariant that was violated: exactly one audio stream in the
    # container. Two would play simultaneously, which is the "both tracks are
    # running" symptom.
    streams = _count_audio_streams(temp_path)
    if streams is not None and streams != 1:
        logger.warning("Audio mux produced %s audio streams in %s", streams, temp_path)
        temp_path.unlink(missing_ok=True)
        return False

    temp_path.replace(video_path)
    return True


def _finalize_encoded_video(
    path: Path, codec: str, audio_path: Optional[Path], warnings: list[str]
) -> str:
    """Make a freshly encoded MP4 browser-playable with exactly one audio track.

    ``mp4v`` (MPEG-4 Part 2) does not play in browsers, so it is transcoded to
    H.264 with FFmpeg; an H.264 file only needs the audio muxed in. Shared by the
    deliverable and its caption-free preview companion so both end up with the
    same codec and the same single audio stream.
    """
    if codec == "mp4v":
        if _reencode_to_h264(path, audio_path=audio_path):
            return "avc1"
        warnings.append(
            "H.264 encoder unavailable; wrote MPEG-4 Part 2 (mp4v), which "
            "does not play in most web browsers"
        )
        return codec
    if audio_path is not None:
        _mux_audio(path, audio_path)
    return codec


def _count_audio_streams(path: Path) -> Optional[int]:
    """Return the number of audio streams in ``path``, or None if unknown.

    Used to assert a rendered container carries exactly one audio track; a
    second one would mean the source audio survived alongside the replacement
    and the two would be mixed on playback.

    ``ffprobe`` is preferred because it reads the *container* and reports each
    stream exactly once. The old implementation parsed the ``Stream #0:1:
    Audio:`` lines from an ``ffmpeg -f null -`` run, but that log prints every
    stream **twice** — once for the demuxed input and once for the mapped
    output — so a perfectly good single-track file was counted as two. The
    guard then rejected the freshly encoded H.264 file, discarded it, and the
    caller fell back to the unplayable ``mp4v`` original. ``ffmpeg``-only
    parsing is kept as a fallback, with the doubled output block removed.
    """
    try:
        import imageio_ffmpeg  # type: ignore
    except ImportError:
        return None

    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    # Preferred path: ffprobe ships next to ffmpeg in most distributions and
    # lists container streams unambiguously.
    ffprobe = Path(ffmpeg_exe).with_name("ffprobe" + Path(ffmpeg_exe).suffix)
    if ffprobe.exists():
        command = [
            str(ffprobe),
            "-v", "error",
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0",
            str(path),
        ]
        try:
            proc = subprocess.run(
                command, capture_output=True, timeout=120, check=False
            )
        except Exception:  # pragma: no cover - environment dependent
            proc = None
        if proc is not None and proc.returncode == 0:
            text = (proc.stdout or b"").decode("utf-8", "replace")
            kinds = [line.strip().lower() for line in text.splitlines() if line.strip()]
            if kinds:
                return sum(1 for kind in kinds if kind == "audio")

    # Fallback: parse ffmpeg's log, ignoring the output section so each stream
    # is counted once.
    command = [
        ffmpeg_exe,
        "-i", str(path),
        "-hide_banner",
        "-f", "null",
        "-",
    ]
    try:
        proc = subprocess.run(command, capture_output=True, timeout=120, check=False)
    except Exception:  # pragma: no cover - environment dependent
        return None
    text = (proc.stderr or b"").decode("utf-8", "replace")
    # Keep only the header lines before the output section. Those lines carry
    # a "[0x1]" style stream id, whereas the mapped output lines look like
    # "Stream #0:1: Audio: pcm_s16le, ..." and would otherwise be counted too.
    header = re.split(r"^(?:\s*)Output #0", text, maxsplit=1, flags=re.MULTILINE)[0]
    matches = re.findall(r"Stream #\d+:\d+\[\w+\][^:]*: Audio:", header)
    if matches:
        return len(matches)
    # Older builds omit the stream id brackets; fall back to a looser pattern
    # while still ignoring anything inside the output section.
    return len(re.findall(r"Stream #\d+:\d+[^:]*: Audio:", header))


def _verify_output(path: Path) -> bool:
    """Re-open a rendered file and confirm at least one frame decodes."""
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            return False
        success, frame = capture.read()
        return bool(success and frame is not None and frame.size > 0)
    finally:
        capture.release()


def _write_thumbnail(video_path: Path, width: int, height: int) -> Optional[str]:
    """Grab a representative frame from a rendered clip as a JPEG thumbnail."""
    capture = cv2.VideoCapture(str(video_path))
    try:
        if not capture.isOpened():
            return None
        total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total > 10:
            capture.set(cv2.CAP_PROP_POS_FRAMES, max(min(int(total * 0.15), total - 1), 0))
        else:
            capture.set(cv2.CAP_PROP_POS_MSEC, 500.0)
        success, frame = capture.read()
        if not success or frame is None:
            return None
        thumb = cv2.resize(frame, (max(width // 3, 240), max(height // 3, 426)))
        thumb_path = video_path.with_suffix(".thumb.jpg")
        if cv2.imwrite(str(thumb_path), thumb, [int(cv2.IMWRITE_JPEG_QUALITY), 85]):
            return str(thumb_path)
        return None
    finally:
        capture.release()


def resolve_render_path(
    directory: str | Path, clip_id: int | str, suffix: str = ".mp4"
) -> Path:
    """Build an output path inside a clip directory."""
    base = Path(directory)
    base.mkdir(parents=True, exist_ok=True)
    return base / f"clip_{clip_id}_vertical{suffix}"


def resolve_preview_path(directory: str | Path, clip_id: int | str) -> Path:
    """Build the caption-free companion render used by the editor preview.

    The editor draws the *draft* captions over this file on a canvas, so the
    moving picture must not carry captions of its own — otherwise changing the
    style showed the old burned-in captions underneath the new draft ones.
    Playing the episode's source media instead lost the render's own framing and
    audio, so the companion is written by the same render job and keeps both.
    """
    base = Path(directory)
    base.mkdir(parents=True, exist_ok=True)
    return base / f"clip_{clip_id}_preview.mp4"


def hex_bgr_tuple(value: str) -> tuple[int, int, int]:
    """Convert a hex colour into a BGR tuple (re-exported for audio renders)."""
    return caption_utils.hex_to_bgr(value)



def _resolve_source_fps(
    capture: cv2.VideoCapture, source: Path, fallback: float
) -> tuple[float, list[str]]:
    """Determine a usable source FPS, warning when the container is unclear."""
    warnings: list[str] = []
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    if fps <= 1.0:
        probe = probe_media(source)
        fps = probe.fps if probe.fps > 1.0 else fallback
        warnings.append("Source declared no usable FPS; assuming 30 fps")
    return fps, warnings


def _build_caption_groups(
    words: Optional[Sequence[dict[str, Any]]],
    transcript_text: str,
    start_time: float,
    end_time: float,
    caption_style: dict[str, Any],
) -> list[list[caption_utils.CaptionWord]]:
    """Prepare caption groups relative to the clip start."""
    caption_words = caption_utils.words_from_payload(
        words or [], start_time, end_time, transcript_text
    )
    return caption_utils.group_caption_lines(
        caption_words,
        int(caption_style.get("words_per_line", 3)),
        int(caption_style.get("max_lines", 2)),
    )


def _compose_canvas(
    frame: np.ndarray,
    focus_x: float,
    focus_y: float,
    framing: dict[str, Any],
    state: Optional[framing_utils.FramingState],
    output_width: int,
    output_height: int,
) -> tuple[np.ndarray, Optional[framing_utils.FramingState]]:
    """Crop/scale a frame to the requested output geometry.

    The editor exposes an output size, a zoom level and a blurred-background
    fill; all three are applied here. Previously this returned the frame
    untouched, which silently made every one of those controls inert — a user
    asking for 1080x1920 with a blurred background still got the raw source
    geometry.

    When the requested output size already equals the source geometry *and* no
    zoom is requested, the frame is returned unchanged so the common
    "preserve the source" path stays a zero-copy operation.
    """
    frame_height, frame_width = frame.shape[:2]
    zoom = float(framing.get("zoom", 1.0) or 1.0)
    blur_background = bool(framing.get("blur_background", True))
    blur_strength = int(framing.get("background_blur_strength", 41) or 41)

    unchanged = (
        output_width == frame_width
        and output_height == frame_height
        and abs(zoom - 1.0) < 1e-6
    )
    if unchanged:
        return frame, state

    if blur_background:
        canvas = framing_utils.fit_with_blur_fill(
            frame,
            output_width,
            output_height,
            focus_x=focus_x,
            focus_y=focus_y,
            zoom=zoom,
            blur_strength=blur_strength,
        )
    else:
        canvas = framing_utils.crop_and_fit(
            frame,
            output_width,
            output_height,
            focus_x=focus_x,
            focus_y=focus_y,
            zoom=zoom,
            blur_background=False,
            blur_strength=blur_strength,
        )
    return canvas, state


def _next_focus(
    frame: np.ndarray,
    framing: dict[str, Any],
    focus_track: tuple[float, float],
    detect: bool = True,
) -> tuple[float, float]:
    """Compute the framing focus point for the current frame."""
    base_focus_x = float(framing.get("focus_x", 0.5))
    base_focus_y = float(framing.get("focus_y", 0.4))
    smooth = bool(framing.get("smooth_tracking", True))

    mode = framing.get("mode", "auto_face")
    if mode == "center":
        # "Centered" must mean the geometric centre. Returning base_focus_x/y
        # made it inherit whatever the manual focus sliders were set to, so the
        # Centered option produced an off-centre crop.
        return 0.5, 0.5
    if mode != "auto_face":
        # "manual": use the explicit focus point the editor exposes.
        return base_focus_x, base_focus_y

    if not detect:
        # Between detection frames: hold the current smoothed focus instead of
        # re-running the (expensive) Haar scan on every single frame.
        return focus_track

    detected = framing_utils.detect_focus_point(frame)
    if detected is not None:
        target_x = min(max(detected[0], 0.15), 0.85)
        target_y = min(max(detected[1], 0.12), 0.7)
        if smooth:
            alpha = 0.12
            return (
                focus_track[0] + (target_x - focus_track[0]) * alpha,
                focus_track[1] + (target_y - focus_track[1]) * alpha,
            )
        return target_x, target_y

    if smooth:
        return (
            focus_track[0] + (base_focus_x - focus_track[0]) * 0.05,
            focus_track[1] + (base_focus_y - focus_track[1]) * 0.05,
        )
    return base_focus_x, base_focus_y


def render_clip(
    source_path: str | Path,
    output_path: str | Path,
    start_time: float,
    end_time: float,
    caption_style: dict[str, Any],
    framing: dict[str, Any],
    words: Optional[Sequence[dict[str, Any]]] = None,
    transcript_text: str = "",
    title: str = "",
    progress_callback: Optional[ProgressCallback] = None,
) -> RenderResult:
    """Render a clip identical to the source video with bottom captions.

    The output preserves the source's native width, height and frame rate —
    no crop, zoom, resize or aspect-ratio change — with word-by-word captions
    burned in at the bottom of the frame and the source audio restored.

    Args:
        source_path: Uploaded episode media.
        output_path: Destination MP4 path.
        start_time: Clip start in seconds within the source.
        end_time: Clip end in seconds within the source.
        caption_style: Styling dictionary (see ``services.caption_styles``).
        framing: Framing dictionary (see `FramingOptions`).
        words: Word-level timings in the **source media's** timeline. They are
            rebased onto ``start_time`` by ``words_from_payload``, so callers
            pass the stored timings as-is.
        transcript_text: Fallback text when no word timings exist.
        title: Optional title card text shown at the start of the clip.
        progress_callback: Called with a 0-100 integer percentage.

    Raises:
        FileNotFoundError: if the source media is missing.
        RuntimeError: if no frames could be decoded from the requested range.
    """
    source = Path(source_path)
    destination = Path(output_path)
    if not source.exists():
        raise FileNotFoundError(f"Source media not found: {source}")

    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise RuntimeError(f"OpenCV cannot open source media: {source}")

    try:
        # Honour the requested output geometry. These used to be hardcoded to the
        # source dimensions, which silently ignored the editor's "Output size"
        # control; the source values remain the default when it is not set.
        source_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        source_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if not source_width or not source_height:
            raise RuntimeError(
                "Source has no video stream; use the audio-only renderer instead"
            )
        output_width = int(framing.get("output_width") or source_width)
        output_height = int(framing.get("output_height") or source_height)
        # H.264 requires even dimensions; pad up if the source is odd.
        output_width += output_width % 2
        output_height += output_height % 2
        target_fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0) or 30.0

        source_fps, warnings = _resolve_source_fps(capture, source, target_fps)
        # One fps everywhere. The writer's container rate must match the rate
        # frames are consumed at: `frames_to_process` counts frames with
        # `source_fps`, so writing at a different `target_fps` changed the
        # encoded duration (`count / target_fps`) away from the muxed audio's
        # duration (`clip_duration`) and the burned captions slipped against
        # the sound.
        writer_fps = source_fps or target_fps
        frames_total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if frames_total <= 0:
            warnings.append("Frame count unknown; progress reporting is approximate")

        clip_duration = max(end_time - start_time, 0.1)
        frames_to_process = max(int(round(clip_duration * writer_fps)), 1)
        # Re-running the (expensive) Haar face detection on every frame is the
        # single largest per-frame cost of the renderer. A talking head does
        # not move fast enough to need frame-rate detection: scan a couple of
        # times per second and let the smoothed focus track carry the crop
        # between scans.
        detect_interval = max(int(round(writer_fps / 2.0)), 1)
        capture.set(cv2.CAP_PROP_POS_MSEC, max(start_time, 0.0) * 1000.0)

        writer, used_codec = _open_writer(
            destination, writer_fps, (output_width, output_height)
        )
        burn_captions = bool(framing.get("burn_captions", True))
        caption_groups = (
            _build_caption_groups(
                words, transcript_text, start_time, end_time, caption_style
            )
            if burn_captions
            else []
        )

        written = 0
        captions_drawn = 0
        decoded = 0
        focus_track = (
            float(framing.get("focus_x", 0.5)),
            float(framing.get("focus_y", 0.4)),
        )
        framing_state: Optional[framing_utils.FramingState] = None

        while True:
            success, frame = capture.read()
            if not success or frame is None:
                break
            decoded += 1

            # Anchor caption timing to the frame's *actual* position in the
            # source, not to arithmetic. `CAP_PROP_POS_MSEC` after `read()` is
            # the decoded frame's own timestamp: it absorbs OpenCV seek slop
            # (a seek can land on an earlier keyframe, which shifted every
            # burned caption against the muxed audio — the audio always starts
            # exactly at `start_time`, the picture did not) and variable
            # frame-rate pacing (container FPS is an average there, so
            # `frame_index / fps` drifted over time). Fall back to frame
            # arithmetic only when the backend cannot report a position.
            reported_ms = float(capture.get(cv2.CAP_PROP_POS_MSEC) or 0.0)
            if reported_ms > 0:
                clip_time = reported_ms / 1000.0 - start_time
                if clip_time < -0.001:
                    # Pre-roll frame from a keyframe-anchored seek: not part of
                    # the clip. Skip it so picture and audio start together.
                    continue
            else:
                clip_time = (decoded - 1) / writer_fps
            if clip_time >= clip_duration or written >= frames_to_process:
                break

            # Apply the requested output size, zoom and background fill. Without
            # this the framing controls (output size, zoom, blur fill) were inert.
            # Face detection runs on the first frame and every `detect_interval`
            # frames after that; in between the smoothed focus holds steady.
            focus_track = _next_focus(
                frame, framing, focus_track, detect=decoded % detect_interval == 1
            )
            canvas, framing_state = _compose_canvas(
                frame,
                focus_track[0],
                focus_track[1],
                framing,
                framing_state,
                output_width,
                output_height,
            )

            # Captions and the title card are burned onto `canvas` from here on:
            # each clip ships exactly one video (this deliverable) plus its
            # thumbnail; the editor composites its draft overlay client-side.
            if caption_groups:
                # Draw in place: the clean caption-free canvas for the preview
                # writer was already saved above, so the per-frame `copy()` plus
                # full-array `np.array_equal` scan (a 1080x1920 copy and a
                # 6M-element comparison on every frame) served only a counter.
                # Counting via the active-group window is equivalent and free.
                group_active = any(
                    group and group[0].start <= clip_time <= group[-1].end
                    for group in caption_groups
                )
                if group_active:
                    # Honour the style's own `position`. This used to be forced
                    # to "bottom", which silently ignored the editor's Position
                    # control (Center/Top) and burned every caption at the
                    # bottom.
                    caption_utils.draw_captions(
                        canvas, clip_time, caption_groups, caption_style
                    )
                    captions_drawn += 1

            if title and clip_time <= 1.6:
                caption_utils.draw_title_card(
                    canvas, title, caption_style, min(clip_time / 0.4, 1.0)
                )

            writer.write(canvas)
            written += 1

            if progress_callback and written % 15 == 0:
                progress_callback(min(int(written / frames_to_process * 100), 99))

        writer.release()
    finally:
        capture.release()

    if written == 0:
        raise RuntimeError(
            "No frames were decoded for the requested range; check the clip timestamps"
        )

    # Restore the audio for the clip segment from the source media.
    audio_path: Optional[Path] = None
    temp_audio = destination.with_suffix(".m4a")
    if _extract_source_audio(source, start_time, end_time, temp_audio):
        audio_path = temp_audio
    if audio_path is None:
        temp_audio.unlink(missing_ok=True)

    used_codec = _finalize_encoded_video(destination, used_codec, audio_path, warnings)

    if audio_path is not None and temp_audio.exists():
        temp_audio.unlink(missing_ok=True)

    if not _verify_output(destination):
        raise RuntimeError(
            "Rendered file is not decodable; the encoder produced a corrupt MP4"
        )

    thumbnail_path = _write_thumbnail(destination, output_width, output_height)
    result = RenderResult(
        output_path=str(destination),
        thumbnail_path=thumbnail_path,
        width=output_width,
        height=output_height,
        fps=writer_fps,
        duration_seconds=round(written / writer_fps, 3),
        frame_count=written,
        size_bytes=destination.stat().st_size if destination.exists() else 0,
        captions_burned=captions_drawn,
        codec=used_codec,
        warnings=warnings,
    )
    if progress_callback:
        progress_callback(100)
    return result



def render_audio_only(
    output_path: str | Path,
    width: int,
    height: int,
    fps: float,
    caption_style: dict[str, Any],
    groups: Sequence[Sequence[caption_utils.CaptionWord]],
    duration: float,
    title: str = "",
    progress_callback: Optional[ProgressCallback] = None,
) -> RenderResult:
    """Produce a caption-only vertical video for audio episodes.

    The visual track is a subtle animated gradient so the clip still works on
    TikTok/Reels even when the source has no video stream.
    """
    destination = Path(output_path)
    writer, used_codec = _open_writer(destination, fps, (width, height))
    total_frames = max(int(duration * fps), 1)

    top = np.array(hex_bgr_tuple(caption_style.get("highlight_color", "#FFD400")), dtype=np.float32)
    bottom = np.array(
        hex_bgr_tuple(caption_style.get("background_color") or "#101828"), dtype=np.float32
    )

    try:
        for frame_index in range(total_frames):
            timestamp = frame_index / fps
            mix = (timestamp / max(duration, 0.1)) % 1.0
            color = (top * (1 - mix) + bottom * mix).astype(np.uint8)
            canvas = np.tile(color.reshape(1, 1, 3), (height, width, 1))
            if groups:
                # Honour the style's own `position` (see the note above about the
                # Position control being ignored).
                caption_utils.draw_captions(canvas, timestamp, groups, caption_style)
            if title and timestamp <= 1.6:
                caption_utils.draw_title_card(
                    canvas, title, caption_style, min(timestamp / 0.4, 1.0)
                )
            writer.write(canvas)
            if progress_callback and frame_index % 30 == 0:
                progress_callback(min(int(frame_index / total_frames * 100), 99))
    finally:
        writer.release()

    warnings: list[str] = []
    used_codec = _finalize_encoded_video(destination, used_codec, None, warnings)
    if not _verify_output(destination):
        raise RuntimeError(
            "Rendered file is not decodable; the encoder produced a corrupt MP4"
        )

    thumbnail_path = _write_thumbnail(destination, width, height)
    if progress_callback:
        progress_callback(100)
    return RenderResult(
        output_path=str(destination),
        thumbnail_path=thumbnail_path,
        width=width,
        height=height,
        fps=fps,
        duration_seconds=round(duration, 3),
        frame_count=total_frames,
        size_bytes=destination.stat().st_size if destination.exists() else 0,
        captions_burned=total_frames if groups else 0,
        codec=used_codec,
        warnings=warnings,
    )


def preview_frame(
    source_path: str | Path,
    timestamp: float,
    caption_style: dict[str, Any],
    framing: dict[str, Any],
    words: Optional[Sequence[dict[str, Any]]] = None,
    clip_start: float = 0.0,
    clip_end: float = 0.0,
    title: str = "",
    transcript_text: str = "",
) -> Optional[np.ndarray]:
    """Render a single composited frame; used by the frontend preview."""
    capture = cv2.VideoCapture(str(source_path))
    try:
        if not capture.isOpened():
            return None
        capture.set(cv2.CAP_PROP_POS_MSEC, max(timestamp, 0.0) * 1000.0)
        success, frame = capture.read()
        if not success or frame is None:
            return None

        source_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        source_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if not source_width or not source_height:
            return None
        # Match the render exactly: the preview must show the same output size,
        # zoom and background fill the renderer will produce, otherwise the
        # editor's framing controls appear to do nothing.
        output_width = int(framing.get("output_width") or source_width)
        output_height = int(framing.get("output_height") or source_height)

        # Resolve the focus point the same way the renderer does, so `auto_face`
        # and `manual` produce the identical crop in the preview and the render.
        # Passing the raw focus_x/focus_y here meant an auto-face clip previewed
        # from the frame centre while the render tracked the face.
        focus_x, focus_y = _next_focus(frame, framing, (0.5, 0.4))

        frame, _ = _compose_canvas(
            frame,
            focus_x,
            focus_y,
            framing,
            None,
            output_width,
            output_height,
        )

        if framing.get("burn_captions", True):
            groups = _build_caption_groups(
                words,
                transcript_text,
                clip_start,
                clip_end or clip_start + 30.0,
                caption_style,
            )
            # Honour the style's own `position` so the editor's Position control works.
            caption_utils.draw_captions(
                frame, max(timestamp - clip_start, 0.0), groups, caption_style
            )
        if title and timestamp - clip_start <= 1.6:
            caption_utils.draw_title_card(
                frame, title, caption_style, min((timestamp - clip_start) / 0.4, 1.0)
            )
        return frame
    finally:
        capture.release()
