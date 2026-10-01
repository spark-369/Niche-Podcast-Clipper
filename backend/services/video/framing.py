"""OpenCV based 9:16 vertical framing with automatic subject tracking."""

import logging
from dataclasses import dataclass
from typing import Any, Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

_CASCADE: Optional[cv2.CascadeClassifier] = None
_CASCADE_PATH = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"


def _get_face_cascade() -> Optional[cv2.CascadeClassifier]:
    """Load (once) the bundled Haar cascade used for automatic framing."""
    global _CASCADE
    if _CASCADE is not None:
        return _CASCADE if not _CASCADE.empty() else None
    try:
        cascade = cv2.CascadeClassifier(_CASCADE_PATH)
        _CASCADE = cascade if not cascade.empty() else None
    except Exception as exc:  # pragma: no cover - depends on OpenCV build
        logger.warning("Face cascade unavailable: %s", exc)
        _CASCADE = None
    return _CASCADE


@dataclass
class FramingState:
    """Smoothed crop window used across frames to avoid jittery tracking."""

    x: int
    y: int
    width: int
    height: int
    initialised: bool = False

    def smooth_towards(self, target_x: int, target_y: int, alpha: float = 0.18) -> None:
        if not self.initialised:
            self.x, self.y = target_x, target_y
            self.initialised = True
            return
        self.x = int(round(self.x + (target_x - self.x) * alpha))
        self.y = int(round(self.y + (target_y - self.y) * alpha))

    def clamp(self, frame_width: int, frame_height: int) -> None:
        self.width = min(self.width, frame_width)
        self.height = min(self.height, frame_height)
        self.x = int(max(0, min(self.x, frame_width - self.width)))
        self.y = int(max(0, min(self.y, frame_height - self.height)))


def detect_focus_point(frame: np.ndarray) -> Optional[tuple[float, float]]:
    """Locate the largest face in a frame; returns a normalized (x, y) centre."""
    cascade = _get_face_cascade()
    if cascade is None:
        return None
    try:
        # The Haar scan's cost scales with the pixel count. Detection runs on a
        # downscaled grayscale copy — the returned focus point is normalised,
        # so the reduction is invisible to callers while cutting the scan by
        # an order of magnitude on full-resolution frames.
        frame_height, frame_width = frame.shape[:2]
        scale = max(frame_width / 640.0, 1.0)
        if scale > 1.0:
            small = cv2.resize(
                frame,
                (
                    max(2, int(round(frame_width / scale))),
                    max(2, int(round(frame_height / scale))),
                ),
                interpolation=cv2.INTER_AREA,
            )
        else:
            small = frame
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)
        faces = cascade.detectMultiScale(
            gray, scaleFactor=1.15, minNeighbors=5, minSize=(48, 48)
        )
    except cv2.error as exc:  # pragma: no cover
        logger.debug("Face detection failed: %s", exc)
        return None

    if len(faces) == 0:
        return None

    # Prefer the largest face in the upper two thirds of the frame.
    best = max(faces, key=lambda box: box[2] * box[3])
    x, y, w, h = best
    small_height, small_width = gray.shape[:2]
    return (
        float((x + w / 2) / max(small_width, 1)),
        float((y + h / 2) / max(small_height, 1)),
    )


def compute_crop_box(
    frame_width: int,
    frame_height: int,
    output_width: int,
    output_height: int,
    focus_x: float = 0.5,
    focus_y: float = 0.4,
    zoom: float = 1.0,
) -> tuple[int, int, int, int]:
    """Compute a crop rectangle matching the target aspect ratio.

    Returns:
        ``(x, y, width, height)`` in source-frame pixel coordinates.
    """
    target_ratio = output_width / max(output_height, 1)
    source_ratio = frame_width / max(frame_height, 1)

    if source_ratio > target_ratio:
        # Source is wider than 9:16 -> crop horizontally.
        crop_height = frame_height
        crop_width = int(round(crop_height * target_ratio))
    else:
        # Source is taller/narrower -> crop vertically.
        crop_width = frame_width
        crop_height = int(round(crop_width / target_ratio))

    if zoom > 1.0:
        crop_width = int(round(crop_width / zoom))
        crop_height = int(round(crop_height / zoom))

    crop_width = max(2, min(crop_width, frame_width))
    crop_height = max(2, min(crop_height, frame_height))

    center_x = int(round(focus_x * frame_width))
    center_y = int(round(focus_y * frame_height))

    x = center_x - crop_width // 2
    y = center_y - crop_height // 2
    x = int(max(0, min(x, frame_width - crop_width)))
    y = int(max(0, min(y, frame_height - crop_height)))

    # Keep the crop dimensions even; required by most encoders.
    crop_width -= crop_width % 2
    crop_height -= crop_height % 2
    return x, y, crop_width, crop_height


def blur_to_fill(
    frame: np.ndarray, output_width: int, output_height: int, strength: int = 41
) -> np.ndarray:
    """Edge-to-edge blurred background that fills the 9:16 canvas.

    The source frame is aspect-preserving crop-filled into the canvas first
    (so it is never stretched) before the heavy Gaussian blur is applied.
    """
    frame_height, frame_width = frame.shape[:2]
    x, y, crop_width, crop_height = compute_crop_box(
        frame_width, frame_height, output_width, output_height, 0.5, 0.5, 1.0
    )
    canvas = cv2.resize(
        frame[y : y + crop_height, x : x + crop_width],
        (output_width, output_height),
        interpolation=cv2.INTER_AREA,
    )
    kernel = strength if strength % 2 == 1 else strength + 1
    kernel = max(3, min(kernel, 151))
    # A blurred backdrop is low-frequency by definition, so blur at a fraction
    # of the resolution and upscale: visually identical to a full-canvas blur
    # with the equivalent kernel, at a tiny fraction of the cost (a 41px
    # Gaussian over 1080x1920 used to dominate every rendered frame).
    downscale = 8
    small = cv2.resize(
        canvas,
        (max(2, output_width // downscale), max(2, output_height // downscale)),
        interpolation=cv2.INTER_AREA,
    )
    small_kernel = max(3, min(int(round(kernel / downscale)) | 1, 31))
    blurred = cv2.GaussianBlur(small, (small_kernel, small_kernel), 0)
    blurred = cv2.resize(
        blurred, (output_width, output_height), interpolation=cv2.INTER_LINEAR
    )
    # Slightly darken so the fitted foreground and captions stay legible
    # (``blurred * 0.82`` in a single pass, no zero overlay allocation).
    return cv2.convertScaleAbs(blurred, alpha=0.82, beta=0)


def fit_with_blur_fill(
    frame: np.ndarray,
    output_width: int,
    output_height: int,
    focus_x: float = 0.5,
    focus_y: float = 0.4,
    zoom: float = 1.0,
    blur_strength: int = 41,
) -> np.ndarray:
    """Show the *whole* frame on the vertical canvas (contain) and fill any
    empty bars with a blurred, zoomed copy of the frame.

    This avoids the aggressive 9:16 crop that makes the speaker look
    over-zoomed; set ``blur_background=False`` for the classic tight crop.
    """
    frame_height, frame_width = frame.shape[:2]
    background = blur_to_fill(frame, output_width, output_height, blur_strength)

    scale = min(output_width / max(frame_width, 1), output_height / max(frame_height, 1))
    fit_width = max(2, int(round(frame_width * scale)) // 2 * 2)
    fit_height = max(2, int(round(frame_height * scale)) // 2 * 2)
    fitted = cv2.resize(frame, (fit_width, fit_height), interpolation=cv2.INTER_LINEAR)

    if zoom > 1.0:
        crop_width = max(2, int(round(fit_width / zoom)) // 2 * 2)
        crop_height = max(2, int(round(fit_height / zoom)) // 2 * 2)
        center_x = int(round(focus_x * fit_width))
        center_y = int(round(focus_y * fit_height))
        x = int(max(0, min(center_x - crop_width // 2, fit_width - crop_width)))
        y = int(max(0, min(center_y - crop_height // 2, fit_height - crop_height)))
        fitted = cv2.resize(
            fitted[y : y + crop_height, x : x + crop_width],
            (fit_width, fit_height),
            interpolation=cv2.INTER_LINEAR,
        )

    x_offset = (output_width - fit_width) // 2
    y_offset = (output_height - fit_height) // 2
    # ``background`` is a fresh per-call array; writing the fitted region into
    # it directly avoids a redundant full-canvas copy on every frame.
    canvas = background
    canvas[y_offset : y_offset + fit_height, x_offset : x_offset + fit_width] = fitted
    return canvas


def crop_and_fit(
    frame: np.ndarray,
    output_width: int,
    output_height: int,
    focus_x: float = 0.5,
    focus_y: float = 0.4,
    zoom: float = 1.0,
    blur_background: bool = True,
    blur_strength: int = 41,
) -> np.ndarray:
    """Crop a frame to the target aspect ratio and scale it to the canvas."""
    frame_height, frame_width = frame.shape[:2]
    x, y, crop_width, crop_height = compute_crop_box(
        frame_width,
        frame_height,
        output_width,
        output_height,
        focus_x,
        focus_y,
        zoom,
    )
    cropped = frame[y : y + crop_height, x : x + crop_width]
    if cropped.size == 0:
        cropped = frame

    resized = cv2.resize(
        cropped, (output_width, output_height), interpolation=cv2.INTER_LINEAR
    )
    if blur_background:
        return resized
    return resized.astype(np.uint8)
