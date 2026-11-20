"""Обзорный коллаж: что видят нейросети на кадрах + кропы номеров и их апскейлы.

Для каждого изображения:
- аннотированный кадр: боксы номеров (зелёные) и ТС (оранжевые), игнор-полоса
  (красная) и band-зона чтения номеров (жёлтая) из config.yaml;
- строка кропов: оригинал / x4 / x8 (кубическая интерполяция) с результатом
  OCR на каждом масштабе — наглядно показывает потолок разрешения 720p.

Выход:
- images/review/crops_review.jpg  — весь обзор одним файлом;
- images/review/crops/*.jpg       — отдельные кропы (для показа по одному).

Запуск: .venv\\Scripts\\python.exe tools\\make_crops_review.py
"""
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent.parent  # C:\dev\argus
sys.path.insert(0, str(ROOT / "vision"))

from object_detector import ObjectDetector
from plate_reader import PlateReader
from plate_text import normalize_plate

VEHICLE_CLASS_IDS = {1, 2, 3, 5, 7}  # bicycle, car, motorcycle, bus, truck (COCO)

FRAME_W = 1000          # ширина аннотированного кадра в коллаже
CROP_MAX_W = 300        # максимальная ширина варианта кропа
CROP_H = 88             # базовая высота варианта кропа
DET_CONF = 0.35         # показывать боксы номеров с этой уверенностью
OCR_CONF = 0.5          # OCR-попытки для боксов с этой уверенностью
OBJ_CONF = 0.4

FONTS = {}


def font(size: int):
    if size not in FONTS:
        try:
            FONTS[size] = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", size)
        except (OSError, ValueError):
            FONTS[size] = ImageFont.load_default()
    return FONTS[size]


def load_zones():
    import yaml
    cfg = yaml.safe_load((ROOT / "vision" / "config.yaml").read_text(encoding="utf-8"))
    unread = cfg.get("unread_plate", {})
    strip = [tuple(float(v) for v in r) for r in unread.get("ignore_rois", []) if len(r) == 4]
    band = [tuple(float(v) for v in r) for r in unread.get("plate_ignore_rois", []) if len(r) == 4]
    return strip, band


def ocr_variant(reader: PlateReader, roi: np.ndarray) -> str:
    img = reader.pipeline._preprocess_plate(roi)
    if img.size == 0:
        return ""
    return normalize_plate(reader.recognizer.recognize(img))


def build_panel(reader, obj_det, path: Path, strip, band, labels, crops_dir: Path):
    frame = cv2.imread(str(path))
    if frame is None:
        return None
    h, w = frame.shape[:2]

    dets = reader.detector.detect(frame)
    dets = [d for d in dets if d["confidence"] >= DET_CONF]
    dets.sort(key=lambda d: d["confidence"], reverse=True)
    vehicles = [o for o in obj_det.detect(frame) if o["class_id"] in VEHICLE_CLASS_IDS]

    # Кропы и апскейлы (x4/x8 кубический), OCR на каждом масштабе
    plate_infos = []
    for det in dets[:2]:
        x1, y1, x2, y2 = det["bbox"]
        roi = frame[max(0, y1):y2, max(0, x1):x2]
        if roi.size == 0:
            continue
        rh, rw = roi.shape[:2]
        variants = []
        for name, scale in (("оригинал", 1), ("x4 cubic", 4), ("x8 cubic", 8)):
            var = roi if scale == 1 else cv2.resize(roi, (rw * scale, rh * scale),
                                                    interpolation=cv2.INTER_CUBIC)
            text = ocr_variant(reader, var) if det["confidence"] >= OCR_CONF else ""
            variants.append((var, name, text))
            stem = path.stem
            cv2.imwrite(str(crops_dir / f"{stem}_p{len(plate_infos)}_{scale}x.jpg"), var,
                        [cv2.IMWRITE_JPEG_QUALITY, 92])
        plate_infos.append({"det": det, "w": rw, "h": rh, "variants": variants})

    # Аннотированный кадр (боксы рисуем в масштабе дисплея)
    scale_f = FRAME_W / w
    dh = round(h * scale_f)
    disp = cv2.resize(frame, (FRAME_W, dh), interpolation=cv2.INTER_AREA)
    for o in vehicles:
        x1, y1, x2, y2 = [round(v * scale_f) for v in o["bbox"]]
        cv2.rectangle(disp, (x1, y1), (x2, y2), (0, 140, 255), 2)
        labels.append((x1 + 4, max(2, y1 - 20), f"{o['name']} {o['confidence']:.2f}", 14,
                       (255, 190, 110)))
    for info in plate_infos:
        x1, y1, x2, y2 = [round(v * scale_f) for v in info["det"]["bbox"]]
        cv2.rectangle(disp, (x1, y1), (x2, y2), (0, 200, 0), 2)
        best = next((v[2] for v in info["variants"] if v[2]), "")
        labels.append((x1 + 4, max(2, y1 - 22),
                       f"{best or '?'} · {info['det']['confidence']:.2f} · {info['w']}x{info['h']} px",
                       15, (110, 255, 110)))
    for rect, color in ((strip, (0, 0, 230)), (band, (0, 220, 220))):
        for rx1, ry1, rx2, ry2 in rect:
            cv2.rectangle(disp, (round(rx1 * FRAME_W), round(ry1 * dh)),
                          (round(rx2 * FRAME_W), round(ry2 * dh)), color, 1)

    # Сборка панели: заголовок + кадр + строки кропов
    header = np.full((30, FRAME_W, 3), (34, 34, 34), np.uint8)
    rows = [header, disp]
    labels.append((10, 6, f"{path.name}  ·  {w}x{h}px  ·  номеров: {len(dets)}, ТС: {len(vehicles)}",
                   15, (240, 240, 240)))
    if not plate_infos:
        empty = np.full((40, FRAME_W, 3), (22, 22, 22), np.uint8)
        rows.append(empty)
        labels.append((10, 34 + 10, "номеров не найдено", 15, (170, 170, 170)))
    for idx, info in enumerate(plate_infos):
        row_h = max(CROP_H, min(CROP_MAX_W, round(CROP_H * info["w"] / info["h"])) + 2)
        block = np.full((row_h + 46, FRAME_W, 3), (20, 20, 20), np.uint8)
        x = 10
        for var, name, text in info["variants"]:
            vh, vw = var.shape[:2]
            dw = min(CROP_MAX_W, round(row_h * vw / vh))
            dhh = round(dw * vh / vw)
            tile = cv2.resize(var, (dw, dhh), interpolation=cv2.INTER_NEAREST)
            block[0:dhh, x:x + dw] = tile
            cv2.rectangle(block, (x, 0), (x + dw, dhh), (90, 90, 90), 1)
            labels.append((x, row_h + 6, name, 13, (190, 190, 190)))
            labels.append((x, row_h + 26, text or "—", 15, (110, 255, 110) if text else (150, 150, 150)))
            x += dw + 16
        rows.append(block)
    panel = np.vstack(rows)
    return panel


def draw_labels(panel: np.ndarray, labels) -> np.ndarray:
    pil = Image.fromarray(cv2.cvtColor(panel, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil)
    for x, y, text, size, rgb in labels:
        draw.text((x, y), text, font=font(size), fill=rgb)
    return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)


def main() -> None:
    out_dir = ROOT / "images" / "review"
    crops_dir = out_dir / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)

    defaults = [ROOT / "images" / "easy.jpg", ROOT / "images" / "light.jpg",
                ROOT / "images" / "niva.jpg", ROOT / "images" / "Сложный.png",
                ROOT / "vision" / "dataset" / "2026-08-26" / "070843-cam-01.jpg",
                ROOT / "vision" / "dataset" / "2026-08-26" / "070924-cam-01.jpg"]
    paths = [p for p in defaults if p.exists()]
    if len(sys.argv) > 1:
        paths = [Path(a) for a in sys.argv[1:] if Path(a).exists()]

    print("Загрузка моделей...")
    reader = PlateReader(min_confidence=DET_CONF, imgsz=1280)
    obj_det = ObjectDetector(model_path="yolov8s.pt", min_confidence=OBJ_CONF, imgsz=1280)
    obj_det.detect(np.zeros((640, 640, 3), dtype=np.uint8))  # прогрев
    strip, band = load_zones()
    print("Модели готовы.")

    panels = []
    labels = []
    for path in paths:
        labels.clear()
        panel = build_panel(reader, obj_det, path, strip, band, labels, crops_dir)
        if panel is None:
            print(f"{path.name}: не прочитан, пропущен")
            continue
        panel = draw_labels(panel, labels)
        panels.append(panel)
        print(f"{path.name}: панель готова")

    sep = np.full((12, FRAME_W, 3), (50, 50, 50), np.uint8)
    collage_parts = []
    for i, panel in enumerate(panels):
        if i:
            collage_parts.append(sep)
        collage_parts.append(panel)
    collage = np.vstack(collage_parts)
    out_path = out_dir / "crops_review.jpg"
    cv2.imwrite(str(out_path), collage, [cv2.IMWRITE_JPEG_QUALITY, 88])
    print(f"\nКоллаж: {out_path} ({collage.shape[1]}x{collage.shape[0]})")
    print(f"Кропы: {crops_dir} ({len(list(crops_dir.glob('*.jpg')))} файлов)")


if __name__ == "__main__":
    main()
