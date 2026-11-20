"""Оффлайн-бенчмарк распознавания номеров по датасету.

Прогоняет ANPR-пайплайн (детекция YOLO + CRNN OCR) по кадрам из dataset/
и считает базовые метрики: долю кадров с детекцией номера и с прочитанным
текстом, средние conf и время OCR. Используется как baseline (Фаза 0)
и для A/B моделей OCR INT8 vs FP32 (Фаза 4).

Запуск из vision/:
    .venv\\Scripts\\python.exe tools\\bench_dataset.py                # INT8
    .venv\\Scripts\\python.exe tools\\bench_dataset.py --ocr fp32    # FP32
"""
import argparse
import sys
import time
from collections import defaultdict
from pathlib import Path

import cv2
import torch
from torchvision import transforms

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plate_reader import _load_anpr_module
from plate_text import normalize_plate

ANPR_DIR = ROOT.parent / "anpr"
OCR_MODELS = {
    "int8": ANPR_DIR / "models" / "ocr_crnn" / "quant" / "crnn_ocr_model_int8_fx.pth",
    "fp32": ANPR_DIR / "models" / "ocr_crnn" / "model" / "crnn_ocr_model_best.pth",
}


def make_fp32_recognizer(anpr, model_path: Path, device):
    """CRNNRecognizer без FX-квантизации: FP32-веса грузятся напрямую в скелет CRNN."""
    from torch import nn  # noqa: F401  (нужен anpr.CRNN)

    class FP32Recognizer(anpr.CRNNRecognizer):
        def __init__(self, path: str, dev):
            self.device = dev
            self.transform = transforms.Compose([
                transforms.ToPILImage(), transforms.Grayscale(),
                transforms.Resize((anpr.Config.OCR_IMG_HEIGHT, anpr.Config.OCR_IMG_WIDTH)),
                transforms.ToTensor(), transforms.Normalize(mean=[0.5], std=[0.5]),
            ])
            self.int_to_char = {i + 1: c for i, c in enumerate(anpr.Config.OCR_ALPHABET)}
            self.int_to_char[0] = ""
            model = anpr.CRNN(len(anpr.Config.OCR_ALPHABET) + 1).eval()
            model.load_state_dict(torch.load(path, map_location=dev))
            self.model = model
            print("FP32 OCR загружен (без квантизации).")

    return FP32Recognizer(str(model_path), device)


def main() -> None:
    parser = argparse.ArgumentParser(description="Бенчмарк ANPR по датасету")
    parser.add_argument("--dataset", default=str(ROOT / "dataset"))
    parser.add_argument("--ocr", choices=sorted(OCR_MODELS), default="int8")
    parser.add_argument("--det-conf", type=float, default=0.5,
                        help="порог детекции номера (как в live: plate_min_conf)")
    parser.add_argument("--det-imgsz", type=int, default=640,
                        help="imgsz детектора номеров (штатный anpr = 640)")
    parser.add_argument("--limit", type=int, default=0, help="максимум кадров (0 = все)")
    args = parser.parse_args()

    anpr = _load_anpr_module()
    anpr.Config.YOLO_MODEL_PATH = str(ANPR_DIR / anpr.Config.YOLO_MODEL_PATH)
    anpr.Config.DETECTION_CONFIDENCE_THRESHOLD = args.det_conf
    detector = anpr.YOLODetector(anpr.Config.YOLO_MODEL_PATH, anpr.Config.DEVICE)
    # Переопределяем imgsz предсказания из обёртки (anpr не изменяем)
    detector.model.overrides["imgsz"] = args.det_imgsz

    ocr_path = OCR_MODELS[args.ocr]
    if args.ocr == "fp32":
        recognizer = make_fp32_recognizer(anpr, ocr_path, anpr.Config.DEVICE)
    else:
        anpr.Config.OCR_MODEL_PATH = str(ocr_path)
        recognizer = anpr.CRNNRecognizer(str(ocr_path), anpr.Config.DEVICE)

    images = sorted(Path(args.dataset).rglob("*.jpg"))
    if args.limit > 0:
        images = images[: args.limit]
    if not images:
        print(f"Нет кадров в {args.dataset}")
        return
    print(f"Кадров: {len(images)} | OCR: {args.ocr} ({ocr_path.name}) | "
          f"det-conf: {args.det_conf} | det-imgsz: {args.det_imgsz}")

    per_day = defaultdict(lambda: {"frames": 0, "det": 0, "read": 0})
    total = det_frames = read_frames = 0
    plate_dets = 0
    det_conf_sum = 0.0
    ocr_ms_sum = 0.0
    det_ms_sum = 0.0
    bbox_heights: list[int] = []
    samples: list[tuple[str, str]] = []

    for path in images:
        frame = cv2.imread(str(path))
        if frame is None:
            continue
        total += 1
        day = path.parent.name
        t0 = time.perf_counter()
        dets = detector.detect(frame)
        det_ms_sum += (time.perf_counter() - t0) * 1000
        if dets:
            det_frames += 1
            per_day[day]["det"] += 1
        texts = []
        for det in dets:
            plate_dets += 1
            det_conf_sum += det["confidence"]
            x1, y1, x2, y2 = det["bbox"]
            bbox_heights.append(max(1, y2 - y1))
            roi = frame[y1:y2, x1:x2]
            if roi.size == 0:
                continue
            t0 = time.perf_counter()
            raw = recognizer.recognize(roi)
            ocr_ms_sum += (time.perf_counter() - t0) * 1000
            text = normalize_plate(raw)
            if text:
                texts.append(text)
        per_day[day]["frames"] += 1
        if texts:
            read_frames += 1
            per_day[day]["read"] += 1
            if len(samples) < 25:
                samples.append((f"{day}/{path.name}", "; ".join(texts)))

    if total == 0:
        print("Ни один кадр не прочитан")
        return

    pct = lambda n: f"{100.0 * n / total:.1f}%"
    avg_det_conf = det_conf_sum / plate_dets if plate_dets else 0.0
    avg_ocr_ms = ocr_ms_sum / plate_dets if plate_dets else 0.0
    avg_det_ms = det_ms_sum / total if total else 0.0

    print("\n===== РЕЗУЛЬТАТ =====")
    print(f"Кадров всего:              {total}")
    print(f"Кадров с детекцией номера: {det_frames} ({pct(det_frames)})")
    print(f"Кадров с прочитанным:      {read_frames} ({pct(read_frames)})")
    print(f"Детекций номера:           {plate_dets} (avg conf {avg_det_conf:.3f})")
    print(f"Среднее время детекции:    {avg_det_ms:.0f} мс/кадр")
    print(f"Среднее время OCR:         {avg_ocr_ms:.1f} мс/номер")
    if bbox_heights:
        hs = sorted(bbox_heights)
        med = hs[len(hs) // 2]
        under = lambda t: sum(1 for h in hs if h < t)
        print(f"Высота bbox номера:        min {hs[0]} / медиана {med} / max {hs[-1]} px")
        print(f"  ниже 32 px: {under(32)}, ниже 48 px: {under(48)}, ниже 64 px: {under(64)} из {len(hs)}")
    print("\nПо дням:")
    for day in sorted(per_day):
        d = per_day[day]
        print(f"  {day}: кадров {d['frames']}, детекция {d['det']}, "
              f"прочитано {d['read']}")
    if samples:
        print("\nПримеры чтений:")
        for name, text in samples:
            print(f"  {name}: {text}")


if __name__ == "__main__":
    main()
