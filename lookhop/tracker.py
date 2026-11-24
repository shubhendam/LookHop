"""Camera capture and face -> feature extraction (MediaPipe Face Landmarker).

Features per frame: [yaw, pitch, iris_h, iris_v]
  yaw/pitch : head rotation in degrees, from MediaPipe's face transform matrix
  iris_h/v  : iris position inside the eye, relative to the eye corners
              (roll-invariant; averaged over both eyes)
"""
from __future__ import annotations

import logging
import os
import shutil
import ssl
import sys
import time
import urllib.request
from pathlib import Path

import cv2
import numpy as np

from lookhop.cameras import camera_index  # noqa: F401 (re-exported for callers)
from lookhop.config import MODELS_DIR, RESOURCE_DIR

os.environ.setdefault("GLOG_minloglevel", "2")  # silence MediaPipe's C++ info logs

log = logging.getLogger(__name__)

MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/"
             "face_landmarker/float16/1/face_landmarker.task")
MODEL_NAME = "face_landmarker.task"
MODEL_PATH = MODELS_DIR / MODEL_NAME                 # per-user download location
BUNDLED_MODEL = RESOURCE_DIR / "models" / MODEL_NAME  # shipped in builds / repo models/

if sys.platform == "win32":
    # DirectShow keeps only the newest frame, so slow polling never sees stale images.
    CAMERA_BACKENDS = (cv2.CAP_DSHOW, cv2.CAP_MSMF)
elif sys.platform == "darwin":
    CAMERA_BACKENDS = (cv2.CAP_AVFOUNDATION, cv2.CAP_ANY)
else:
    CAMERA_BACKENDS = (cv2.CAP_V4L2, cv2.CAP_ANY)

# Landmark indices (478-point mesh). Each eye: corner A, corner B, iris centre.
# Both axes run from the subject's right toward their left, so the two eyes
# agree on direction whether or not the image is mirrored.
EYE_R = (33, 133, 468)   # right eye: outer corner -> inner corner
EYE_L = (362, 263, 473)  # left eye: inner corner -> outer corner


def ensure_model() -> Path:
    """Path of the face model, downloading it on first use if it isn't bundled."""
    for path in (BUNDLED_MODEL, MODEL_PATH):
        if path.exists() and path.stat().st_size > 1_000_000:
            return path
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MODEL_PATH.with_suffix(".part")
    log.info("Downloading face model to %s", MODEL_PATH)
    req = urllib.request.Request(MODEL_URL, headers={"User-Agent": "python-urllib"})
    try:  # python.org builds on macOS ship without root certificates; certifi comes with mediapipe
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = None
    with urllib.request.urlopen(req, timeout=60, context=ctx) as resp, open(tmp, "wb") as f:
        shutil.copyfileobj(resp, f)
    tmp.replace(MODEL_PATH)
    return MODEL_PATH


class Camera:
    """Webcam capture: AVFoundation on macOS, OpenCV elsewhere."""
    def __init__(self, index: int, width: int, height: int, fps: float | None = None):
        """Remember the camera index, requested size and frame rate."""
        self.index, self.width, self.height, self.fps = index, width, height, fps
        self.cap: cv2.VideoCapture | None = None

    def open(self) -> bool:
        """Open the camera; True if frames can be read."""
        if sys.platform == "darwin":  # exact device via AVFoundation (OpenCV's numbering is unreliable)
            from lookhop.platform.macos.avcamera import AVCamera
            av_cam = AVCamera(self.index, self.width, self.height)
            if av_cam.open():
                self.cap = av_cam
                return True
            return False
        for backend in CAMERA_BACKENDS:
            cap = cv2.VideoCapture(self.index, backend)
            if not cap.isOpened():
                cap.release()
                continue
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            if self.fps:
                cap.set(cv2.CAP_PROP_FPS, self.fps)
            ok, frame = cap.read()
            if ok and frame is not None:
                self.cap = cap
                log.info("Camera %d open (%dx%d, backend %d)", self.index,
                         frame.shape[1], frame.shape[0], backend)
                return True
            cap.release()
        return False

    def read(self) -> np.ndarray | None:
        """The latest frame as a BGR array, or None if unavailable."""
        if self.cap is None:
            return None
        if not isinstance(self.cap, cv2.VideoCapture):  # AVCamera: returns the frame or None
            return self.cap.read()
        ok, frame = self.cap.read()
        return frame if ok else None

    def close(self) -> None:
        """Release the camera."""
        if self.cap is not None:
            if isinstance(self.cap, cv2.VideoCapture):
                self.cap.release()
            else:
                self.cap.close()
            self.cap = None


def head_angles(matrix: np.ndarray) -> tuple[float, float]:
    """Yaw and pitch in degrees from MediaPipe's face transform matrix."""
    R = np.asarray(matrix, float)[:3, :3]
    R = R / np.linalg.norm(R, axis=0, keepdims=True)  # strip any scale
    yaw = np.degrees(np.arcsin(np.clip(-R[2, 0], -1.0, 1.0)))
    pitch = np.degrees(np.arctan2(R[2, 1], R[2, 2]))
    if pitch > 90:  # keep pitch continuous around "facing the camera"
        pitch -= 180
    elif pitch < -90:
        pitch += 180
    return float(yaw), float(pitch)


def _eye_ratio(pts: np.ndarray, eye: tuple[int, int, int]) -> tuple[float, float]:
    """Iris position inside one eye relative to its corners (horizontal, vertical)."""
    a, b, iris = pts[eye[0]], pts[eye[1]], pts[eye[2]]
    axis = b - a
    length = float(np.hypot(*axis))
    if length < 1e-6:
        return 0.5, 0.0
    u = axis / length
    n = np.array([-u[1], u[0]])
    h = float(np.dot(iris - a, u) / length)
    v = float(np.dot(iris - (a + b) / 2, n) / length)
    return h, v


def iris_ratios(pts: np.ndarray) -> tuple[float, float]:
    """Iris position averaged over both eyes."""
    h1, v1 = _eye_ratio(pts, EYE_R)
    h2, v2 = _eye_ratio(pts, EYE_L)
    return (h1 + h2) / 2, (v1 + v2) / 2


class FaceFeatures:
    """MediaPipe Face Landmarker wrapper: frame -> [yaw, pitch, iris_h, iris_v]."""
    def __init__(self):
        """Load the face model and create the landmarker."""
        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision

        self._mp = mp
        options = vision.FaceLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_buffer=ensure_model().read_bytes()),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=1,
            output_face_blendshapes=False,
            output_facial_transformation_matrixes=True,
            min_face_detection_confidence=0.5,
            min_face_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self._landmarker = vision.FaceLandmarker.create_from_options(options)
        self._last_ts = -1
        self.last_points: np.ndarray | None = None  # pixel coords, for the preview

    def process(self, frame_bgr: np.ndarray) -> np.ndarray | None:
        """Features for one BGR frame, or None when no face is found."""
        ts = int(time.monotonic() * 1000)
        if ts <= self._last_ts:
            ts = self._last_ts + 1
        self._last_ts = ts

        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        result = self._landmarker.detect_for_video(image, ts)
        if not result.face_landmarks or not result.facial_transformation_matrixes:
            self.last_points = None
            return None
        landmarks = result.face_landmarks[0]
        if len(landmarks) < 478:  # model without iris points
            self.last_points = None
            return None

        h, w = frame_bgr.shape[:2]
        pts = np.array([(p.x * w, p.y * h) for p in landmarks], dtype=np.float64)
        self.last_points = pts
        yaw, pitch = head_angles(np.asarray(result.facial_transformation_matrixes[0]))
        iris_h, iris_v = iris_ratios(pts)
        return np.array([yaw, pitch, iris_h, iris_v])

    def close(self) -> None:
        """Free the landmarker."""
        self._landmarker.close()
