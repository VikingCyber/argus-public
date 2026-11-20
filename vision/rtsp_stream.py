import threading
import time

import cv2
import numpy as np


class CameraStream:
    """Поток RTSP-камеры: читает кадры в отдельном потоке, всегда хранит последний кадр.

    Умеет переподключаться при обрыве соединения.
    """

    def __init__(self, cam_id: str, rtsp_url: str, name: str = "", reconnect_delay: float = 3.0):
        self.cam_id = cam_id
        self.rtsp_url = rtsp_url
        self.name = name
        self.reconnect_delay = reconnect_delay

        self._cap: cv2.VideoCapture | None = None
        self._latest_frame: np.ndarray | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False
        self._frame_id = 0
        self._frame_event = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"cam-{self.cam_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        self._release()

    def _release(self) -> None:
        with self._lock:
            if self._cap is not None:
                self._cap.release()
                self._cap = None
            self._connected = False

    def _open(self) -> bool:
        with self._lock:
            self._release()
            cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                self._cap = cap
                self._connected = True
                return True
            cap.release()
            self._cap = None
            return False

    def _run(self) -> None:
        while not self._stop.is_set():
            cap = self._cap
            if cap is None or not self._connected:
                if not self._open():
                    self._connected = False
                    time.sleep(self.reconnect_delay)
                    continue
                cap = self._cap
            if cap is None:
                time.sleep(self.reconnect_delay)
                continue
            ret, frame = cap.read()
            if not ret or frame is None:
                self._connected = False
                self._release()
                time.sleep(self.reconnect_delay)
                continue
            with self._lock:
                self._latest_frame = frame
                self._connected = True
                self._frame_id += 1
            self._frame_event.set()

    def read(self) -> np.ndarray | None:
        """Последний принятый кадр (неблокирующий)."""
        with self._lock:
            return self._latest_frame.copy() if self._latest_frame is not None else None

    def read_seq(self) -> int:
        """Порядковый номер последнего принятого кадра (для read_new)."""
        with self._lock:
            return self._frame_id

    def stats(self) -> dict:
        """Диагностика источника кадров (единый интерфейс с HttpFrameSource).

        Для прямого RTSP нет HTTP-опросов, поэтому poll-счётчики
        нулевые: main.py использует этот метод для периодического отчёта
        «новых/повторов» кадров, а повторы тут невозможны в принципе.
        """
        with self._lock:
            return {
                "polls": 0,
                "dups": 0,
                "new": self._frame_id,
                "decode_fail": 0,
                "seq": self._frame_id,
            }

    def read_new(self, prev_id: int, timeout: float = 1.0):
        """Блокирующий вызов: возвращает (кадр, новый_номер) как только камера
        приняла кадр новее prev_id. Возвращает (None, prev_id) по таймауту.

        Вместо грубого time.sleep — ждём события: поток просыпается сразу,
        когда _run принял новый кадр, и не тратит CPU на холостой опрос.
        """
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                fid = self._frame_id
                frame = self._latest_frame
            if fid != prev_id and frame is not None:
                return frame.copy(), fid
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, prev_id
            self._frame_event.wait(remaining)

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected and self._cap is not None