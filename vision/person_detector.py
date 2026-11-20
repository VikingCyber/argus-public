from datetime import datetime

import numpy as np
from ultralytics import YOLO

PERSON_CLASS_ID = 0


class NightWindow:
    """Окно ночного времени (по умолчанию 20:00 - 8:00), корректно обрабатывает переход через полночь."""

    def __init__(self, start_hour: int = 20, end_hour: int = 8):
        if not 0 <= start_hour <= 23 or not 0 <= end_hour <= 23:
            raise ValueError("Часы должны быть в диапазоне 0-23")
        self.start_hour = start_hour
        self.end_hour = end_hour

    def is_active(self, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        hour = now.hour
        if self.start_hour == self.end_hour:
            return True
        if self.start_hour < self.end_hour:
            return self.start_hour <= hour < self.end_hour
        return hour >= self.start_hour or hour < self.end_hour


class PersonDetector:
    """Детекция людей через YOLO (COCO класс person)."""

    def __init__(self, model_path: str = "yolov8n.pt", min_confidence: float = 0.5, device: str = "cpu"):
        self.model = YOLO(model_path)
        self.min_confidence = min_confidence
        self.device = device

    def detect(self, frame: np.ndarray) -> list[dict]:
        results = self.model.predict(frame, verbose=False, device=self.device, classes=[PERSON_CLASS_ID])
        persons = []
        for result in results:
            boxes = getattr(result, "boxes", None)
            data = boxes.data.cpu().numpy() if boxes is not None else None
            if data is None:
                continue
            for det in data:
                x1, y1, x2, y2, conf, _ = det
                if conf >= self.min_confidence:
                    persons.append({
                        "bbox": [int(x1), int(y1), int(x2), int(y2)],
                        "confidence": float(conf),
                    })
        return persons