import threading
import time
from typing import Union

import cv2
import numpy as np
import requests

from rtsp_stream import CameraStream


class HttpFrameSource:
    """Источник кадров по HTTP (релей stream_server /frame).

    Тот же интерфейс, что у CameraStream: start/stop/read/is_connected.
    Позволяет нескольким сервисам работать с одной камерой без
    исчерпания лимита RTSP-сессий.
    """

    def __init__(self, cam_id: str, url: str, name: str = "", fps: float = 12.0):
        self.cam_id = cam_id
        # stream_server слушает только IPv4 (0.0.0.0). "localhost" резолвится в
        # ::1 первым и соединение вечно висит в SYN_SENT — кадры не идут.
        # Принудительно берём 127.0.0.1, чтобы релей работал независимо от
        # порядка адресов в hosts.
        url = url.replace("://localhost:", "://127.0.0.1:")
        self.url = url
        self.name = name
        self._interval = 1.0 / max(fps, 0.5)

        self._latest_frame: np.ndarray | None = None
        self._connected = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_raw: bytes | None = None
        self._frame_seq = 0
        # Счётчики диагностики дублей (периодический отчёт из main.py):
        # polls — сколько HTTP-опросов /frame сделано; dups — сколько раз сервер
        # вернул ТОТ ЖЕ кадр (байт-в-байт); new — сколько уникальных кадров
        # декодировано; decode_fail — битые JPEG.
        self._poll_count = 0
        self._dup_count = 0
        self._new_count = 0
        self._decode_fail_count = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"http-{self.cam_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                resp = requests.get(self.url, timeout=3.0)
                if resp.status_code == 200 and len(resp.content) > 1000:
                    raw = resp.content
                    self._poll_count += 1
                    # relay_fps опрашивает /frame чаще, чем камера отдаёт кадры:
                    # поток получает один и тот же JPEG несколько раз подряд.
                    # Не продвигаем счётчик (и не декодируем заново) пока кадр
                    # камеры не изменился — трекер ByteTrack считает буфер в КАДРАХ,
                    # и кормление его повторами тот же кадр «съедает» track_buffer
                    # в секунды, рвёт track_id и дублирует фото проезда.
                    if raw == self._last_raw:
                        self._dup_count += 1
                        self._set_connected(True)
                        time.sleep(self._interval)
                        continue
                    arr = np.frombuffer(raw, dtype=np.uint8)
                    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if frame is not None:
                        self._new_count += 1
                        with self._lock:
                            self._latest_frame = frame
                            self._connected = True
                            self._frame_seq += 1
                        self._last_raw = raw
                        time.sleep(self._interval)
                        continue
                    self._decode_fail_count += 1
                self._set_connected(False)
            except requests.RequestException:
                self._set_connected(False)
            time.sleep(1.0)

    def _set_connected(self, value: bool) -> None:
        with self._lock:
            self._connected = value

    def read(self) -> np.ndarray | None:
        """Последний кадр (неблокирующе), как CameraStream.read()."""
        with self._lock:
            return self._latest_frame.copy() if self._latest_frame is not None else None

    def read_seq(self) -> int:
        """Порядковый номер последнего принятого кадра (как CameraStream.read_seq)."""
        with self._lock:
            return self._frame_seq

    def stats(self) -> dict:
        """Диагностика дублей: сколько опросов, повторов и уникальных кадров."""
        with self._lock:
            return {
                "polls": self._poll_count,
                "dups": self._dup_count,
                "new": self._new_count,
                "decode_fail": self._decode_fail_count,
                "seq": self._frame_seq,
            }

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected


def open_source(cam_id: str, source: str, name: str = "",
                fps: float = 12.0) -> Union[CameraStream, "HttpFrameSource"]:
    """RTSP-URL → CameraStream; http(s)-URL → HttpFrameSource (релей)."""
    if source.lower().startswith(("http://", "https://")):
        return HttpFrameSource(cam_id, source, name, fps=fps)
    return CameraStream(cam_id, source, name)
