import importlib.util
import sys
from pathlib import Path

import cv2
import numpy as np

from plate_text import normalize_plate

ANPR_DIR = Path(__file__).resolve().parent.parent / "anpr"
ANPR_MODULE_PATH = ANPR_DIR / "inference.py"


def _load_anpr_module():
    """Загружает inference.py из склонированного репозитория как модуль (без изменения репозитория)."""
    spec = importlib.util.spec_from_file_location("anpr_inference", ANPR_MODULE_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Не удалось загрузить модуль ANPR: {ANPR_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class PlateReader:
    """Обёртка над ANPR-пайплайном: детекция номера YOLO -> коррекция перспективы -> CRNN OCR.

    Отличия от «штатного» anpr-пайплайна (сам anpr не изменяется):
    - imgsz детектора номеров поднимается до 1280 (на обзорной камере находит
      на порядок больше номеров, чем штатные 640);
    - используется detect() вместо track(): ByteTrack отбрасывает новые
      треки с conf < new_track_thresh (0.6) и душит мелкие номера; привязка
      номера к машине и консенсус делаются в main.py (IoU-треки + голосование);
    - мелкий кроп номера апскейлится перед OCR.
    """

    def __init__(self, min_confidence: float = 0.5, imgsz: int = 1280,
                 upscale_min_height: int = 48, upscale_max_factor: int = 5):
        self.min_confidence = min_confidence
        self.upscale_min_height = upscale_min_height
        self.upscale_max_factor = max(1, upscale_max_factor)
        anpr = _load_anpr_module()
        anpr.Config.YOLO_MODEL_PATH = str(ANPR_DIR / anpr.Config.YOLO_MODEL_PATH)
        anpr.Config.OCR_MODEL_PATH = str(ANPR_DIR / anpr.Config.OCR_MODEL_PATH)
        anpr.Config.DETECTION_CONFIDENCE_THRESHOLD = min_confidence
        self.detector = anpr.YOLODetector(anpr.Config.YOLO_MODEL_PATH, anpr.Config.DEVICE)
        # imgsz предсказания переопределяем из обёртки (переопределение аргументов predict)
        self.detector.model.overrides["imgsz"] = imgsz
        self.recognizer = anpr.CRNNRecognizer(anpr.Config.OCR_MODEL_PATH, anpr.Config.DEVICE)
        self.pipeline = anpr.ANPR_Pipeline(self.recognizer)

    def _maybe_upscale(self, roi: np.ndarray) -> np.ndarray:
        """Апскейл кропа номера, если он ниже upscale_min_height (помогает CRNN)."""
        h, w = roi.shape[:2]
        if h <= 0 or h >= self.upscale_min_height:
            return roi
        factor = min(self.upscale_max_factor, -(-self.upscale_min_height // h))
        if factor <= 1:
            return roi
        return cv2.resize(roi, (w * factor, h * factor), interpolation=cv2.INTER_CUBIC)

    def process_frame(self, frame: np.ndarray) -> list[dict]:
        """Возвращает распознанные номера: [{bbox, confidence, text, track_id}, ...].

        text — сырой OCR-результат кадра (без anpr-стабилизации): консенсус
        по серии кадров считается в main.py посимвольным голосованием.
        """
        detections = self.detector.detect(frame)
        plates = []
        for det in detections:
            x1, y1, x2, y2 = det["bbox"]
            roi = frame[y1:y2, x1:x2]
            if roi.size == 0:
                continue
            roi = self._maybe_upscale(roi)
            plate_img = self.pipeline._preprocess_plate(roi)
            if plate_img.size == 0:
                continue
            text = normalize_plate(self.recognizer.recognize(plate_img))
            if not text or det["confidence"] < self.min_confidence:
                continue
            plates.append({
                "bbox": [int(x1), int(y1), int(x2), int(y2)],
                "confidence": round(det["confidence"], 4),
                "text": text,
                "track_id": det.get("track_id"),
            })
        return plates


def crop_roi(frame: np.ndarray, bbox: list[int]) -> np.ndarray | None:
    """Вырезает регион по bbox [x1, y1, x2, y2]."""
    x1, y1, x2, y2 = bbox
    roi = frame[y1:y2, x1:x2]
    return roi if roi.size > 0 else None
