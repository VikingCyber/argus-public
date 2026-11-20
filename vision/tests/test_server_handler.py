import json
import threading
import time
from http.server import ThreadingHTTPServer

import cv2
import numpy as np
import requests

from stream_server import Broadcaster, DetectionCache, Handler


def _start_server():
    ann_bc = Broadcaster()
    nn_bc = Broadcaster()
    raw_bc = Broadcaster()
    frame_bc = Broadcaster()
    det_cache = DetectionCache()
    Handler.ann_bc = ann_bc
    Handler.nn_bc = nn_bc
    Handler.raw_bc = raw_bc
    Handler.frame_bc = frame_bc
    Handler.detection_cache = det_cache
    Handler.stream_bc = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.05)
    return server, port, ann_bc, nn_bc, det_cache


def _stop_server(server):
    server.shutdown()


def _make_jpeg():
    """JPEG > 1000 байт (требование обработчиков /annotations и HttpFrameSource)."""
    img = np.zeros((240, 320, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
    assert ok and len(buf) > 1000, f"jpeg too small: {len(buf)}"
    return buf.tobytes()


def test_annotations_post_200_updates_ann_bc():
    server, port, ann_bc, _, _ = _start_server()
    try:
        jpeg = _make_jpeg()
        url = f"http://127.0.0.1:{port}/annotations"
        r = requests.post(url, data=jpeg, headers={"Content-Type": "image/jpeg"})
        assert r.status_code == 200
        assert ann_bc.latest() is not None
    finally:
        _stop_server(server)


def test_annotations_reject_small_body():
    server, port, ann_bc, _, _ = _start_server()
    try:
        url = f"http://127.0.0.1:{port}/annotations"
        tiny = b"x" * 500
        r = requests.post(url, data=tiny, headers={"Content-Type": "image/jpeg"})
        assert r.status_code == 400
        assert ann_bc.latest() is None
    finally:
        _stop_server(server)


def test_annotations_reject_oversized_length_header():
    server, port, ann_bc, _, _ = _start_server()
    try:
        url = f"http://127.0.0.1:{port}/annotations"
        # Content-Length > 10MB → 400, тело не читается
        r = requests.post(url, data=b"",
                          headers={"Content-Type": "image/jpeg",
                                   "Content-Length": "10485761"})
        assert r.status_code == 400
        assert ann_bc.latest() is None
    finally:
        _stop_server(server)


def test_unknown_post_path_404():
    server, port, _, _, _ = _start_server()
    try:
        url = f"http://127.0.0.1:{port}/bogus"
        r = requests.post(url, data=b"", headers={"Content-Type": "image/jpeg"})
        assert r.status_code == 404
    finally:
        _stop_server(server)


def test_detections_post_200():
    server, port, _, _, _ = _start_server()
    try:
        url = f"http://127.0.0.1:{port}/detections"
        payload = json.dumps({"objects": [{"bbox": [1, 2, 3, 4], "name": "car"}]})
        r = requests.post(url, data=payload,
                          headers={"Content-Type": "application/json"})
        assert r.status_code == 200
    finally:
        _stop_server(server)


def test_detections_bad_json_400():
    server, port, _, _, _ = _start_server()
    try:
        url = f"http://127.0.0.1:{port}/detections"
        r = requests.post(url, data=b"not json",
                          headers={"Content-Type": "application/json"})
        assert r.status_code == 400
    finally:
        _stop_server(server)


def test_detections_objects_not_list_400():
    server, port, _, _, _ = _start_server()
    try:
        url = f"http://127.0.0.1:{port}/detections"
        payload = json.dumps({"objects": "garbage"})
        r = requests.post(url, data=payload,
                          headers={"Content-Type": "application/json"})
        assert r.status_code == 400
    finally:
        _stop_server(server)


def _make_handler(stream_bc, nn_bc, ann_bc):
    h = Handler.__new__(Handler)  # без __init__: только класс-атрибуты каналов
    h.stream_bc = stream_bc
    h.nn_bc = nn_bc
    h.ann_bc = ann_bc
    return h


def test_stream_prefers_explicit_stream_bc():
    h = _make_handler(stream_bc="ANN", nn_bc="NN", ann_bc="ANN-OLD")
    assert Handler._current_stream_bc(h) == "ANN"


def test_stream_falls_back_to_nn_bc_when_no_stream_bc():
    h = _make_handler(stream_bc=None, nn_bc="NN", ann_bc="ANN")
    assert Handler._current_stream_bc(h) == "NN"


def test_stream_falls_back_to_ann_bc_when_no_stream_and_no_nn():
    h = _make_handler(stream_bc=None, nn_bc=None, ann_bc="ANN")
    assert Handler._current_stream_bc(h) == "ANN"


def test_stream_returns_none_when_all_channels_empty():
    h = _make_handler(stream_bc=None, nn_bc=None, ann_bc=None)
    assert Handler._current_stream_bc(h) is None


def test_stream_does_not_pick_ann_bc_when_nn_is_the_live_channel():
    # Регрессия: `ann_bc or nn_bc` вечно выбирал ann_bc (объект always truthy),
    # даже когда main() явно указал stream_bc=nn_bc (live_nn режим). /stream
    # мёртво прилипал к пустому ann_bc.
    h = _make_handler(stream_bc="NN", nn_bc="NN", ann_bc="ANN")
    assert Handler._current_stream_bc(h) == "NN"
    assert Handler._current_stream_bc(h) != "ANN"


def test_stream_serves_frame_from_selected_channel():
    # Интеграционный: /stream реально отдаёт MJPEG из канала, выбранного
    # _current_stream_bc (ann_bc пуст — не должно блокировать показ nn_bc).
    server, port, _, nn_bc, _ = _start_server()
    try:
        Handler.stream_bc = nn_bc  # как main() при live_nn=true
        jpeg = _make_jpeg()
        nn_bc.update(jpeg)
        # ann_bc пуст намеренно — сырой регрессионный сценарий: раньше
        # `ann_bc or nn_bc` взял бы пустой ann_bc и /stream бы молчал.
        resp = requests.get(f"http://127.0.0.1:{port}/stream", stream=True,
                            timeout=(0.5, 1.0))
        assert resp.status_code == 200
        assert "multipart/x-mixed-replace" in resp.headers["Content-Type"]
        chunk = next(resp.iter_content(chunk_size=1), None)
        assert chunk, "MJPEG-поток не отдал ни байта — канал для /stream пуст"
    finally:
        Handler.stream_bc = None
        _stop_server(server)


def test_stream_503_when_no_channel_selected():
    server, port, *_ = _start_server()
    try:
        Handler.stream_bc = None  # без фолбэков тоже пусто
        Handler.nn_bc = None
        Handler.ann_bc = None
        r = requests.get(f"http://127.0.0.1:{port}/stream", timeout=1.0)
        assert r.status_code == 503
    finally:
        _stop_server(server)


def test_hold_fresh_reflects_recent_update():
    bc = Broadcaster()
    assert bc.hold_fresh() is False
    bc.update(b"jpeg-bytes")
    assert bc.hold_fresh(max_age=1.5) is True
    assert bc.hold_fresh(max_age=-1.0) is False