
import cv2
import numpy as np
import requests


class EventClient:
    """Отправляет события (номера и тревоги) в Java-бэкенд."""

    def __init__(self, base_url: str, api_key: str = "", timeout: float = 10.0,
                 jpeg_quality: int = 95):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        # Качество сохраняемого снимка. Раньше было жёстко 85, поверх кадра,
        # который уже прошёл JPEG в релее (q92) — двойное сжатие мылило номер.
        # 95 — компромисс: заметно чётче, файл больше примерно вдвое.
        self._jpeg_quality = max(1, min(100, int(jpeg_quality)))

    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def _jpeg_bytes(self, frame: np.ndarray | None) -> bytes | None:
        if frame is None:
            return None
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self._jpeg_quality])
        return buf.tobytes() if ok else None

    def send_plate(self, camera_id: str, plate: str, confidence: float,
                   detected_at: str, track_id: int | None = None,
                   image: np.ndarray | None = None) -> bool:
        data: dict[str, object] = {
            "cameraId": camera_id,
            "plateNumber": plate,
            "confidence": confidence,
            "detectedAt": detected_at,
        }
        if track_id is not None:
            data["trackId"] = track_id
        return self._post_multipart("/api/v1/events/plates", data, image)

    def send_alert(self, camera_id: str, alert_type: str, detected_at: str,
                   track_id: int | None = None,
                   image: np.ndarray | None = None) -> bool:
        data: dict[str, object] = {
            "cameraId": camera_id,
            "alertType": alert_type,
            "detectedAt": detected_at,
        }
        if track_id is not None:
            data["trackId"] = track_id
        return self._post_multipart("/api/v1/events/alerts", data, image)

    def _post_multipart(self, path: str, fields: dict, image: np.ndarray | None) -> bool:
        url = self.base_url + path
        files = None
        img_bytes = self._jpeg_bytes(image)
        if img_bytes:
            files = {"image": ("event.jpg", img_bytes, "image/jpeg")}
        try:
            resp = requests.post(url, data=fields, files=files,
                                 headers=self._headers(), timeout=self.timeout)
            return resp.status_code in (200, 201, 204)
        except requests.RequestException as exc:
            print(f"[event_client] Ошибка отправки {path}: {exc}")
            return False