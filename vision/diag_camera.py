"""Диагностика RTSP-канала камеры: реальный FPS, разрешение, кодек.

Отдельная программа — запускать ВМЕСТО нашей системы (после stop_all.ps1),
чтобы единственной RTSP-сессией был этот скрипт. Замеряет, что камера
реально отдаёт, без влияния нашего кода.

Пример:
    python -u diag_camera.py --seconds 15
    python -u diag_camera.py --source rtsp://... --seconds 10 --json
"""

import argparse
import json
import time
from pathlib import Path

import cv2
import numpy as np
import yaml


def _codec_name(cap) -> str:
    fourcc = int(cap.get(cv2.CAP_PROP_FOURCC) or 0)
    code = "".join(chr((fourcc >> (8 * i)) & 0xFF) for i in range(4))
    return code


def _decode_ctime(cap) -> int:
    return int(cap.get(cv2.CAP_PROP_POS_MSEC) or 0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Сканирование RTSP-канала камеры")
    parser.add_argument("--source", help="rtsp:// URL (по умолчанию первая камера из config.yaml)")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parent / "config.yaml"))
    parser.add_argument("--seconds", type=float, default=15.0, help="длительность замера, сек")
    parser.add_argument("--json", action="store_true", help="вывод JSON-отчётом")
    args = parser.parse_args()

    source = args.source
    if not source:
        with open(args.config, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        cameras = cfg.get("cameras", [])
        if not cameras:
            print("ОШИБКА: нет камер в config.yaml, укажите --source")
            return
        source = cameras[0]["rtsp_url"]

    cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        print(json.dumps({"ok": False, "error": "не удалось открыть RTSP"}, ensure_ascii=False))
        return
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    reported_fps = cap.get(cv2.CAP_PROP_FPS)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    codec = _codec_name(cap)

    # Первый кадр до старта таймера: открытие RTSP может тянуться.
    frame = cap.read()[1]
    if frame is None:
        cap.release()
        print(json.dumps({"ok": False, "error": "первый кадр не получен"}, ensure_ascii=False))
        return

    start = time.monotonic()
    count = 0
    dup = 0
    decode_ms_total = 0.0
    last = None
    while time.monotonic() - start < args.seconds:
        t0 = time.perf_counter()
        ret, frame = cap.read()
        dt = (time.perf_counter() - t0) * 1000.0
        if not ret or frame is None:
            continue
        if last is not None and (frame.shape == last.shape) and np.array_equal(frame, last):
            dup += 1
        last = frame
        count += 1
        decode_ms_total += dt
    wall = time.monotonic() - start
    cap.release()

    fps = count / wall if wall > 0 else 0.0
    real_fps = (count - dup) / wall if wall > 0 else 0.0
    report = {
        "ok": True,
        "source": source,
        "reported_fps": round(reported_fps, 1),
        "real_fps_получено_новых": round(real_fps, 1),
        "fps_чтение_read": round(fps, 1),
        "width": width,
        "height": height,
        "codec": codec,
        "frames_read": count,
        "duplicates": dup,
        "avg_decode_ms": round(decode_ms_total / max(1, count), 1),
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"\n=== RTSP-диагностика {source} ===")
        print(f"Заявленный FPS камеры:      {report['reported_fps']}")
        print(f"Реально новых кадров в сек:  {report['real_fps_получено_новых']}")
        print(f"Чтений cap.read() в сек:     {report['fps_чтение_read']}")
        print(f"Разрешение:                  {width}x{height}")
        print(f"Кодек:                       {codec}")
        print(f"Кадров принято:              {count}, повторов: {dup}")
        print(f"Среднее время декодирования: {report['avg_decode_ms']} мс")
        print(f"\nВывод: канал реально даёт до {real_fps or fps:.0f} fps.")


if __name__ == "__main__":
    main()