from pathlib import Path

import numpy as np
from ultralytics import YOLO

_TRACKER_CFG = Path(__file__).resolve().parent / "trackers"


class ObjectDetector:
    """Универсальная детекция объектов (COCO-классы): person, car, truck, bus и т.д.

    classes — список COCO-id, которые ищем. Передаётся прямо в модель
    (ultralytics отсекает остальные классы на входе): без него в обзор
    попадают растения, животные, мебель и прочий уличный шум.
    """

    def __init__(self, model_path: str = "yolov8s.pt", min_confidence: float = 0.5,
                 classes: list[int] | None = None, device: str = "cpu", imgsz: int = 640,
                 tracker: str = "bytetrack_argus.yaml"):
        self.model = YOLO(model_path)
        self.min_confidence = min_confidence
        self.classes = sorted(set(classes)) if classes else None
        self.device = device
        self.imgsz = imgsz
        self.tracker = str(_TRACKER_CFG / tracker) if not Path(tracker).is_absolute() else tracker
        self.names = self.model.names

    def detect(self, frame: np.ndarray) -> list[dict]:
        results = self.model.predict(frame, verbose=False, device=self.device,
                                     classes=self.classes, imgsz=self.imgsz)
        objects = []
        for result in results:
            boxes = getattr(result, "boxes", None)
            if boxes is None or boxes.xyxy is None:
                continue
            xyxy = boxes.xyxy.cpu().numpy()
            confs = boxes.conf.cpu().numpy() if boxes.conf is not None else []
            clss = boxes.cls.cpu().numpy() if boxes.cls is not None else []
            for (x1, y1, x2, y2), conf, cls in zip(xyxy, confs, clss):
                if conf < self.min_confidence:
                    continue
                cls_id = int(cls)
                objects.append({
                    "bbox": [int(x1), int(y1), int(x2), int(y2)],
                    "confidence": float(conf),
                    "class_id": cls_id,
                    "name": self.names[cls_id],
                })
        return objects

    def detect_persons(self, frame: np.ndarray) -> list[dict]:
        """Только люди (class_id=0)."""
        return [o for o in self.detect(frame) if o["class_id"] == 0]

    def track_objects(self, frame: np.ndarray) -> list[dict]:
        """Детекция с устойчивым ID объекта (ByteTrack внутри ultralytics).

        persist=True держит состояние трекера между вызовами: без него каждый кадр
        начинает трекинг заново и все ID каждый раз новые. Состояние трекера общее
        на инстанс модели, поэтому персистентность держится на уровне объекта.

        Каждое чтение возвращает track_id — это и есть идентичность машины на всём
        проезде. Свой IoU-матчер поверх этого не нужен и только вредил: он терял
        треки при сдвиге на десяток пикселей и ронял ID при перекрытиях.

        Настройки трекера (track_buffer и т.д.) задаются только YAML-файлом в
        ultralytics: аргумент `tracker_args` в track() не читается.
        Поэтому конфиг по умолчанию — трекеры/bytetrack_argus.yaml, никаких
        переопределений через kwargs.
        """
        results = self.model.track(
            frame, verbose=False, device=self.device, classes=self.classes,
            imgsz=self.imgsz, conf=self.min_confidence, tracker=self.tracker,
            persist=True)
        objects: list[dict] = []
        for result in results:
            boxes = getattr(result, "boxes", None)
            if boxes is None or boxes.xyxy is None:
                continue
            xyxy = boxes.xyxy.cpu().numpy()
            confs = boxes.conf.cpu().numpy() if boxes.conf is not None else []
            clss = boxes.cls.cpu().numpy() if boxes.cls is not None else []
            ids = boxes.id.cpu().numpy() if boxes.id is not None else []
            for (x1, y1, x2, y2), conf, cls, track_id in zip(xyxy, confs, clss, ids):
                if conf < self.min_confidence:
                    continue
                cls_id = int(cls)
                objects.append({
                    "bbox": [int(x1), int(y1), int(x2), int(y2)],
                    "confidence": float(conf),
                    "class_id": cls_id,
                    "name": self.names[cls_id],
                    "track_id": int(track_id),
                })
        return objects