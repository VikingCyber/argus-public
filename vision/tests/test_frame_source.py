import time
from unittest.mock import patch

import cv2
import numpy as np

from frame_source import HttpFrameSource

# JPEG > 1000 байт (требование HttpFrameSource._run)
_SMALL_IMG = np.zeros((320, 240, 3), dtype=np.uint8)
_ok, _JPEG_BYTES = cv2.imencode(".jpg", _SMALL_IMG, [cv2.IMWRITE_JPEG_QUALITY, 70])
assert _ok
assert len(_JPEG_BYTES) > 1000, f"jpeg too small: {len(_JPEG_BYTES)}"


def _mock_response(jpeg=None):
    data = jpeg if jpeg is not None else _JPEG_BYTES.tobytes()
    return type("R", (), {"status_code": 200, "content": data})


def test_read_seq_starts_at_zero():
    src = HttpFrameSource("test", "http://x/frame", fps=120)
    assert src.read_seq() == 0


def test_dedup_skips_identical_jpeg():
    src = HttpFrameSource("test", "http://x/frame", fps=120)
    with patch("requests.get", return_value=_mock_response()):
        src.start()
        time.sleep(0.15)
        src.stop()
    stats = src.stats()
    assert stats["new"] == 1
    assert stats["polls"] > 1
    assert stats["dups"] + stats["new"] == stats["polls"]
    assert src.read_seq() == 1


def test_two_unique_frames_advance_seq():
    src = HttpFrameSource("test", "http://x/frame", fps=120)
    call_count = 0
    jpeg1 = _JPEG_BYTES.tobytes()

    def two_frames(url, timeout=3.0):
        nonlocal call_count
        call_count += 1
        if call_count > 5:
            white = np.ones(_SMALL_IMG.shape, dtype=np.uint8) * 255
            ok, buf = cv2.imencode(".jpg", white, [cv2.IMWRITE_JPEG_QUALITY, 70])
            assert ok
            assert len(buf) > 1000, f"white jpeg too small: {len(buf)}"
            return type("R", (), {"status_code": 200, "content": buf.tobytes()})
        return type("R", (), {"status_code": 200, "content": jpeg1})

    with patch("requests.get", side_effect=two_frames):
        src.start()
        time.sleep(0.15)
        src.stop()
    stats = src.stats()
    assert stats["new"] >= 2
    assert src.read_seq() >= 2


def test_read_returns_frame_after_dedup():
    src = HttpFrameSource("test", "http://x/frame", fps=120)
    with patch("requests.get", return_value=_mock_response()):
        src.start()
        time.sleep(0.1)
        src.stop()
    frame = src.read()
    assert isinstance(frame, np.ndarray)
    assert frame.shape[2] == 3


def test_is_connected_updates_on_success():
    src = HttpFrameSource("test", "http://x/frame", fps=120)
    assert not src.is_connected
    with patch("requests.get", return_value=_mock_response()):
        src.start()
        time.sleep(0.1)
        src.stop()
    assert src.is_connected


def test_stats_returns_all_counters():
    src = HttpFrameSource("test", "http://x/frame", fps=120)
    with patch("requests.get", return_value=_mock_response()):
        src.start()
        time.sleep(0.15)
        src.stop()
    s = src.stats()
    for k in ("polls", "dups", "new", "decode_fail", "seq"):
        assert k in s
    assert s["seq"] == src.read_seq()