import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import yaml

from event_client import EventClient
from frame_source import open_source
from object_detector import ObjectDetector
from zones import bbox_ignored

VEHICLE_CLASS_IDS = {1, 2, 3, 5, 7}  # bicycle, car, motorcycle, bus, truck (COCO)


def frame_signature(frame: np.ndarray, size=(160, 90)) -> np.ndarray:
    small = cv2.resize(frame, size)
    return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.int16)


def signature_diff(a, b) -> float:
    if a is None or b is None:
        return 255.0
    return float(np.mean(np.abs(a - b)))


class DataCollector:
    """Фоновый сбор кадров с техникой для A/B тестов и будущего дообучения.

    Лёгкий детектор (yolov8n @ небольшой imgsz) работает только триггером.
    Кадр сохраняется, когда: есть ТС + прошло >= min_interval_sec +
    сцена заметно изменилась с прошлого события (diff_threshold).
    По событию отправляет в backend информационный алерт VEHICLE_SEEN с фото.
    """

    def __init__(self, cfg: dict, cam_cfg: dict):
        self.cam_id = cam_cfg["id"]
        collect = cfg.get("collect", {})
        root = Path(__file__).resolve().parent
        self.dataset_dir = root / collect.get("dataset_dir", "dataset")
        self.check_interval = collect.get("check_interval_sec", 0.5)
        self.min_interval = collect.get("min_interval_sec", 15)
        self.diff_threshold = collect.get("diff_threshold", 9.0)
        self._ignore_rois = [tuple(float(v) for v in rect) for rect in
                             collect.get("ignore_rois", []) if len(rect) == 4]
        # Как в main.py: игнор по перекрытию площади, а не по центру bbox
        self._ignore_overlap = collect.get("ignore_overlap_frac", 0.5)

        backend = cfg.get("backend", {})
        self.client = EventClient(
            backend.get("url", "http://localhost:8080"),
            backend.get("api_key", ""),
        )
        self.notify_enabled = collect.get("notify_enabled", True)
        self.notify_cooldown = collect.get("notify_cooldown_sec", 300)

        # Только транспорт: из конфига классов детектора объектов, иначе все COCO
        object_classes = cfg.get("object_classes")
        vehicle_classes = sorted(set(object_classes) & VEHICLE_CLASS_IDS) if object_classes else None
        self.detector = ObjectDetector(
            model_path=collect.get("trigger_model", "yolov8n.pt"),
            min_confidence=collect.get("trigger_min_conf", 0.35),
            classes=vehicle_classes,
            imgsz=collect.get("trigger_imgsz", 640),
        )
        self.stream = open_source(f"collect-{self.cam_id}",
                                  cam_cfg.get("relay_url") or cam_cfg["rtsp_url"],
                                  cam_cfg.get("name", ""),
                                  fps=float(cfg.get("relay_fps", 12)))

        self._last_event_ts = 0.0
        self._last_notify_ts = 0.0
        self._last_sig: np.ndarray | None = None
        self._saved_total = 0
        # Серия: пока ТС едет мимо, сохраняем несколько кадров подряд
        self.burst_max = collect.get("burst_max_frames", 6)
        self.burst_diff = collect.get("burst_diff_threshold", 4.0)
        self.burst_interval = collect.get("burst_interval_sec", 2.0)
        self._burst_left = 0
        self._burst_sig: np.ndarray | None = None
        self._last_burst_ts = 0.0

    def _in_collection_area(self, bbox: list, shape) -> bool:
        if not self._ignore_rois:
            return True
        return not bbox_ignored(bbox, self._ignore_rois, shape, self._ignore_overlap)

    def _save(self, frame: np.ndarray, vehicles: list) -> Path:
        day_dir = self.dataset_dir / datetime.now().strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now()
        name = ts.strftime("%H%M%S") + f"-{self.cam_id}"
        img_path = day_dir / f"{name}.jpg"
        cv2.imwrite(str(img_path), frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        meta = {
            "camera_id": self.cam_id,
            "captured_at": ts.isoformat(timespec="seconds"),
            "vehicles": [
                {"name": v["name"], "confidence": round(v["confidence"], 3),
                 "bbox": v["bbox"]}
                for v in vehicles
            ],
        }
        (day_dir / f"{name}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return img_path

    def _notify(self, frame: np.ndarray) -> bool:
        sent = self.client.send_alert(
            camera_id=self.cam_id,
            alert_type="VEHICLE_SEEN",
            detected_at=datetime.now().isoformat(timespec="seconds"),
            image=frame,
        )
        if not sent:
            print("[collect] backend недоступен — кадр сохранён, уведомление не ушло")
        return sent

    def run(self) -> None:
        self.detector.detect(np.zeros((64, 64, 3), dtype=np.uint8))  # прогрев
        self.stream.start()
        print(f"[collect] камера {self.cam_id}, датасет: {self.dataset_dir}")
        print("[collect] Ctrl+C — остановка")
        last_stats = time.monotonic()

        while True:
            time.sleep(self.check_interval)
            frame = self.stream.read()
            if frame is None:
                continue

            objects = self.detector.detect(frame)
            vehicles = [o for o in objects if o["class_id"] in VEHICLE_CLASS_IDS
                        and self._in_collection_area(o["bbox"], frame.shape)]
            if not vehicles:
                continue

            now = time.monotonic()
            sig = frame_signature(frame)

            if self._burst_left > 0:
                # Серия: ТС ещё в кадре — сохраняем, пока сцена меняется
                if (now - self._last_burst_ts >= self.burst_interval
                        and signature_diff(sig, self._burst_sig) >= self.burst_diff):
                    best = max(vehicles, key=lambda v: v["confidence"])
                    path = self._save(frame, vehicles)
                    self._saved_total += 1
                    self._burst_left -= 1
                    self._last_burst_ts = now
                    self._burst_sig = sig
                    print(f"[collect] серия {self.burst_max - self._burst_left}/"
                          f"{self.burst_max}: {path.name} "
                          f"({best['name']} conf={best['confidence']:.2f})")
                continue

            diff = signature_diff(sig, self._last_sig)
            if now - self._last_event_ts < self.min_interval or diff < self.diff_threshold:
                continue

            # Новое событие: ТС приехало
            best = max(vehicles, key=lambda v: v["confidence"])
            path = self._save(frame, vehicles)
            self._saved_total += 1
            self._last_sig = sig
            self._last_event_ts = now
            print(f"[collect] сохранён кадр #{self._saved_total}: {path.name} "
                  f"({best['name']} conf={best['confidence']:.2f})")

            if self.burst_max > 1:
                self._burst_left = self.burst_max - 1
                self._burst_sig = sig
                self._last_burst_ts = now

            if self.notify_enabled and now - self._last_notify_ts >= self.notify_cooldown and self._notify(frame):
                self._last_notify_ts = now
                print("[collect] уведомление VEHICLE_SEEN отправлено")

            if now - last_stats >= 30.0:
                print(f"[collect] всего кадров: {self._saved_total}")
                last_stats = now


def main() -> None:
    parser = argparse.ArgumentParser(description="Argus: фоновый сбор кадров с ТС")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parent / "config.yaml"))
    parser.add_argument("--camera", help="id камеры из config.yaml (по умолчанию первая)")
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cameras = cfg.get("cameras", [])
    if not cameras:
        print("ОШИБКА: в config.yaml нет камер")
        return
    cam_cfg = next((c for c in cameras if c["id"] == args.camera), cameras[0])

    try:
        DataCollector(cfg, cam_cfg).run()
    except KeyboardInterrupt:
        print("\n[collect] остановка")


if __name__ == "__main__":
    main()
