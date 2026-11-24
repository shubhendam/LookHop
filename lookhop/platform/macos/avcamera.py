"""macOS: webcam capture straight from AVFoundation.

OpenCV numbers cameras in an order that doesn't match the system's device list
(a nearby iPhone's Continuity Camera can come first), so on macOS LookHop opens
the exact AVCaptureDevice instead and hands frames back as BGR numpy arrays.
"""
from __future__ import annotations

import logging
import threading

import cv2
import numpy as np

log = logging.getLogger(__name__)


def devices():
    """Video capture devices in a stable order (sorted by unique ID).

    AVFoundation's own order changes while cameras are in use, so positions in it can't be
    trusted between calls. Every index in LookHop (config, picker, capture) refers to this order.
    """
    import AVFoundation as av
    return sorted(av.AVCaptureDevice.devicesWithMediaType_(av.AVMediaTypeVideo),
                  key=lambda d: str(d.uniqueID()))


# Requested frame size -> AVCaptureSession preset constant name.
_PRESETS = {(352, 288): "AVCaptureSessionPreset352x288", (640, 480): "AVCaptureSessionPreset640x480",
            (1280, 720): "AVCaptureSessionPreset1280x720",
            (1920, 1080): "AVCaptureSessionPreset1920x1080"}

_sink_cls = None


def _sink_class():
    """The sample-buffer delegate class. Objective-C classes can only be registered once
    per process, so it is created lazily on first use and shared by every camera."""
    global _sink_cls
    if _sink_cls is None:
        import CoreMedia  # noqa: F401 - registers the CMSampleBuffer type for the delegate
        from Foundation import NSObject

        class LookHopFrameSink(NSObject):
            """Receives frames from AVFoundation and forwards them to its `owner` AVCamera."""

            def captureOutput_didOutputSampleBuffer_fromConnection_(self, _out, sample, _conn):
                """AVCaptureVideoDataOutput delegate method, called on the capture queue."""
                owner = getattr(self, "owner", None)
                if owner is None:
                    return
                try:
                    owner._on_frame(sample)
                except Exception:  # noqa: BLE001
                    if not owner._warned:
                        owner._warned = True
                        log.warning("frame conversion failed", exc_info=True)

        _sink_cls = LookHopFrameSink
    return _sink_cls


class AVCamera:
    """Same small interface as tracker.Camera: open() / read() / close()."""

    def __init__(self, index: int, width: int = 640, height: int = 480):
        """Remember which camera (index in list_cameras) and the size to ask for."""
        self.index = index  # position in list_cameras()
        self.width, self.height = width, height  # requested size (picks a session preset)
        self._session = None
        self._sink = None
        self._cond = threading.Condition()
        self._frame: np.ndarray | None = None
        self._seq = 0
        self._seen = 0
        self._warned = False

    def _on_frame(self, sample_buffer) -> None:
        """Delegate callback: convert one BGRA pixel buffer into a BGR numpy frame and store it."""
        import CoreMedia
        import Quartz
        buf = CoreMedia.CMSampleBufferGetImageBuffer(sample_buffer)
        if buf is None:
            return
        Quartz.CVPixelBufferLockBaseAddress(buf, 1)  # read-only
        try:
            w, h = Quartz.CVPixelBufferGetWidth(buf), Quartz.CVPixelBufferGetHeight(buf)
            stride = Quartz.CVPixelBufferGetBytesPerRow(buf)
            raw = Quartz.CVPixelBufferGetBaseAddress(buf).as_buffer(stride * h)
            bgra = np.frombuffer(raw, np.uint8).reshape(h, stride)[:, :w * 4].reshape(h, w, 4)
            frame = np.ascontiguousarray(bgra[:, :, :3])  # copy: the buffer is reused
            if self.width and w > self.width * 1.25:  # preset ignored: shrink for the tracker
                frame = cv2.resize(frame, (self.width, round(h * self.width / w)),
                                   interpolation=cv2.INTER_AREA)
        finally:
            Quartz.CVPixelBufferUnlockBaseAddress(buf, 1)
        with self._cond:
            self._frame, self._seq = frame, self._seq + 1
            self._cond.notify_all()

    def open(self, timeout: float = 8.0) -> bool:
        """Start capturing; True once the first frame arrives within `timeout` seconds."""
        try:
            return self._open(timeout)
        except Exception:  # noqa: BLE001 - a broken camera must not kill the calling thread
            log.exception("AVFoundation camera failed to open")
            self.close()
            return False

    def _open(self, timeout: float) -> bool:
        """Build the AVCaptureSession (input, BGRA output, preset) and wait for a frame."""
        import AVFoundation as av
        import dispatch
        import Quartz

        devs = devices()
        if not 0 <= self.index < len(devs):
            return False
        device = devs[self.index]
        inp, err = av.AVCaptureDeviceInput.deviceInputWithDevice_error_(device, None)
        if inp is None:
            log.warning("Cannot open camera %s: %s", device.localizedName(), err)
            return False

        session = av.AVCaptureSession.alloc().init()
        if not session.canAddInput_(inp):
            return False
        session.addInput_(inp)
        out = av.AVCaptureVideoDataOutput.alloc().init()
        out.setVideoSettings_({"PixelFormatType": Quartz.kCVPixelFormatType_32BGRA})
        out.setAlwaysDiscardsLateVideoFrames_(True)
        self._sink = _sink_class().alloc().init()
        self._sink.owner = self
        self._queue = dispatch.dispatch_queue_create(b"lookhop.camera", None)
        out.setSampleBufferDelegate_queue_(self._sink, self._queue)
        if not session.canAddOutput_(out):
            return False
        session.addOutput_(out)
        # The preset must be set AFTER the input is attached: attaching an input resets it to
        # the camera's full resolution (1920x1080 on a FaceTime HD camera).
        preset = _PRESETS.get((self.width, self.height))
        if preset and session.canSetSessionPreset_(getattr(av, preset)):
            session.setSessionPreset_(getattr(av, preset))
        else:
            log.info("No %dx%d preset for '%s'; using its default size",
                     self.width, self.height, device.localizedName())
        session.startRunning()
        log.info("Session preset '%s', requested %dx%d", session.sessionPreset(),
                 self.width, self.height)
        self._session = session
        with self._cond:
            ok = self._cond.wait_for(lambda: self._frame is not None, timeout)
        if not ok:
            self.close()
            return False
        log.info("Camera '%s' open via AVFoundation (%dx%d)", device.localizedName(),
                 self._frame.shape[1], self._frame.shape[0])
        return True

    def read(self, timeout: float = 1.0) -> np.ndarray | None:
        """The next new frame (waits up to `timeout`), or None if the camera stalled."""
        with self._cond:
            if not self._cond.wait_for(lambda: self._seq != self._seen, timeout):
                return None
            self._seen = self._seq
            return self._frame

    def close(self) -> None:
        """Stop the session and drop the delegate."""
        if self._session is not None:
            self._session.stopRunning()
            self._session = None
        if self._sink is not None:
            self._sink.owner = None
        self._sink = None
        with self._cond:
            self._frame = None
