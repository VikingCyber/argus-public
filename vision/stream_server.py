import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np
import yaml

from object_detector import ObjectDetector
from plate_reader import PlateReader
from rtsp_stream import CameraStream
from vest_detector import VestDetector
from zones import bbox_ignored

BOUNDARY = "argusframe"
# Страница на :8090. Раньше панели шли колонкой с max-width 960 и страница
# прокручивалась: чтобы увидеть вторую камеру, приходилось листать вниз.
# Теперь сетка 2x1 на всю высоту окна с object-fit:contain — обе панели
# видны сразу, без прокрутки, кадр вписан целиком (ничего не обрезано).
# Клик по панели — на весь экран, повторный клик/Esc — назад.
INDEX_HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Argus — живой поток</title>
<style>
  html, body { height:100%; margin:0; }
  body { background:#0b0f14; color:#e6e9ef; overflow:hidden; user-select:none;
         font-family:"Segoe UI",system-ui,sans-serif; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:6px; padding:6px;
          height:100vh; height:100dvh; box-sizing:border-box; }
  @media (max-width:820px) { .grid { grid-template-columns:1fr; grid-template-rows:1fr 1fr; } }
  .panel { position:relative; min-width:0; min-height:0; background:#000;
           border:1px solid #1f2733; border-radius:8px; overflow:hidden; cursor:zoom-in; }
  .panel img { width:100%; height:100%; object-fit:contain; display:block; }
  .tag, .st { position:absolute; top:8px; padding:3px 10px; border-radius:12px;
              font-size:12px; letter-spacing:.02em; background:rgba(0,0,0,.6);
              pointer-events:none; }
  .tag { left:8px; }
  .st { right:8px; color:#ffb4b4; }
  .hint { position:absolute; left:50%; bottom:8px; transform:translateX(-50%);
          font-size:11px; color:#8b97a6; opacity:0; transition:opacity .2s;
          pointer-events:none; }
  .panel:hover .hint { opacity:1; }
  .panel:fullscreen { border:0; border-radius:0; cursor:zoom-out; }
</style></head><body>
<div class="grid">
  <div class="panel" id="p-nn">
    <img id="nn" data-src="/stream" alt="NN">
    <span class="tag">Распознавание (NN)</span>
    <span class="st" id="st-nn">нет кадра</span>
    <span class="hint">клик — на весь экран</span>
  </div>
  <div class="panel" id="p-raw">
    <img id="raw" data-src="/raw" alt="RAW">
    <span class="tag">Чистый поток</span>
    <span class="st" id="st-raw">нет кадра</span>
    <span class="hint">клик — на весь экран</span>
  </div>
</div>
<script>
(function () {
  // MJPEG-<img> живёт, пока жив HTTP-ответ. Упавший сервер браузер замечает
  // не всегда (последний кадр замирает молча), поэтому после error и при
  // возврате на вкладку поток перезапрашивается. Панель при этом не мигает
  // белым: старый кадр остаётся до первого нового.
  function connect(id) {
    var img = document.getElementById(id);
    var st = document.getElementById('st-' + id);
    var base = img.dataset.src;
    var tries = 0;
    var timer = null;
    function reconnect() {
      clearTimeout(timer);
      img.src = base + '?t=' + Date.now();
    }
    img.addEventListener('load', function () { tries = 0; });
    img.addEventListener('error', function () {
      tries = Math.min(tries + 1, 5);
      st.textContent = 'переподключение…';
      // 1, 2, 4, 8, 10, 10… с — не долбим перезапускающийся сервер.
      timer = setTimeout(reconnect, Math.min(10000, 1000 * Math.pow(2, tries - 1)));
    });
    // naturalWidth появляется с первым кадром, а complete становится true,
    // когда поток закрылся (сервер перезапускали), — у живого MJPEG он false.
    // Обе приметы проверяем при возврате на вкладку: пока она в фоне,
    // браузер может молча потерять соединение.
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden && (img.complete || !img.naturalWidth)) reconnect();
    });
    setInterval(function () {
      if (img.naturalWidth) { st.style.display = 'none'; }
    }, 1000);
    reconnect();
  }
  ['nn', 'raw'].forEach(connect);

  // Клик по панели — полноэкранный режим этой панели (а не всей страницы:
  // тогда вторая панель одна остаётся видимой и кадр крупнее).
  ['p-nn', 'p-raw'].forEach(function (id) {
    var panel = document.getElementById(id);
    panel.addEventListener('click', function () {
      if (document.fullscreenElement === panel) { document.exitFullscreen(); }
      else if (panel.requestFullscreen) { panel.requestFullscreen(); }
    });
  });
})();
</script>
</body></html>
"""


class Broadcaster:
    """Последний JPEG + событие новизны.

    Компоненты-потребители ждут на событии новые кадры вместо time.sleep:
    просыпаются ровно когда продюсер опубликовал кадр, без лишнего лага
    и без холостого опроса. Событие одно на broadcaster: одновременные
    читатели (несколько браузеров) просыпаются все вместе, каждый снимает
    последний кадр с момента своего пробуждения.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._frame: bytes | None = None
        self._generation = 0
        self._event = threading.Event()
        self._ts = 0.0

    def update(self, jpeg: bytes) -> None:
        with self._lock:
            self._frame = jpeg
            self._generation += 1
            self._ts = time.monotonic()
        self._event.set()

    def latest(self) -> bytes | None:
        with self._lock:
            return self._frame

    def hold_fresh(self, max_age: float = 1.5) -> bool:
        """True, если есть кадр, обновлённый недавно (свежее max_age сек).

        Нужно для ann_bc: кадр от main.py (с боксами) не должен затираться
        чистым кадром raw_loop через 200 мс — иначе боксы мигают и «пропадают».
        raw_loop держит паузу, пока аннотация свежая, и подливает живой
        чистый кадр, как только она протухнет или её нет вовсе.
        """
        with self._lock:
            return self._frame is not None and time.monotonic() - self._ts <= max_age

    def wait(self, since_generation: int, timeout: float = 1.0):
        """Ждёт до появления кадра новее, чем since_generation.

        Возвращает (jpeg, поколение) — читатель передаёт поколение обратно,
        чтобы не получить тот же кадр повторно. Или (None, since_generation)
        по таймауту. Событие не сбрасываем: несколько читателей будятся
        все вместе и каждый снимает последний доступный кадр.
        """
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                gen = self._generation
                frame = self._frame
            if gen > since_generation and frame is not None:
                return frame, gen
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, since_generation
            self._event.wait(remaining)


class DetectionCache:
    """Последние боксы, присланные main.py через POST /detections.

    main.py уже гоняет свою модель (yolov8n для трекинга) — рисовать боксы
    заново второй нейросетью (старый capture_loop с yolov8s+OCR) не нужно.
    Принимаем готовую разметку, рисуем её дешёвым cv2 поверх кадра в raw_loop.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._objects: list[dict] = []
        self._ts = 0.0

    def update(self, objects: list[dict]) -> None:
        with self._lock:
            self._objects = objects
            self._ts = time.monotonic()

    def snapshot(self, max_age: float = 2.0) -> list[dict]:
        """Копия детекций, если они свежие (не старше max_age секунд), иначе []."""
        with self._lock:
            if time.monotonic() - self._ts > max_age:
                return []
            return list(self._objects)


class Handler(BaseHTTPRequestHandler):
    nn_bc: Broadcaster | None = None
    raw_bc: Broadcaster | None = None
    frame_bc: Broadcaster | None = None
    ann_bc: Broadcaster | None = None
    detection_cache: DetectionCache | None = None
    # Какой канал отдаёт /stream. В live_nn-режиме — nn_bc (capture_loop),
    # иначе — ann_bc (main.py рисует боксы на своих кадрах, raw_loop подливает
    # живой чистый кадр, когда аннотаций нет). Выбирается в main().
    stream_bc: Broadcaster | None = None

    def do_POST(self) -> None:
        if self.path.startswith("/annotations") and self.ann_bc is not None:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > 10_000_000:
                self.send_response(400)
                self.end_headers()
                return
            body = self.rfile.read(length)
            if len(body) > 1000:
                self.ann_bc.update(body)
                self.send_response(200)
            else:
                self.send_response(400)
            self.end_headers()
            return
        if self.path.startswith("/detections") and self.detection_cache is not None:
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length > 0 else b""
                payload = json.loads(body.decode("utf-8"))
                objects = payload.get("objects", []) if isinstance(payload, dict) else []
            except (ValueError, UnicodeDecodeError):
                self.send_response(400)
                self.end_headers()
                return
            # Принимаем только список (dict/строка/число — мусор, сыплющий
            # исключения в raw_loop). Без этой проверки один кривой POST
            # кладёт весь релей: raw_loop падает на for o in objects...
            if not isinstance(objects, list):
                self.send_response(400)
                self.end_headers()
                return
            self.detection_cache.update(objects)
            self.send_response(200)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _current_stream_bc(self) -> Broadcaster | None:
        """Канал для /stream. Явный приоритет, а не `ann_bc or nn_bc`:

        объекты Broadcaster всегда truthy, поэтому `ann_bc or nn_bc` вечно
        возвращал ann_bc, и /stream зависал, когда аннотаций от main.py
        ещё не было (нет боксов = пустой канал). Заданный main() stream_bc
        отражает режим: live_nn → nn_bc, иначе → ann_bc. Фолбэки — на случай,
        если main() не инициализировал какой-то канал.
        """
        if self.stream_bc is not None:
            return self.stream_bc
        for bc in (self.nn_bc, self.ann_bc):
            if bc is not None:
                return bc
        return None

    def do_GET(self):
        if self.path.startswith("/stream"):
            bc = self._current_stream_bc()
            if bc is None:
                self.send_error(503, "no stream")
                return
            self._stream(bc)
        elif self.path.startswith("/raw"):
            self._stream(self.raw_bc)
        elif self.path.startswith("/frame"):
            # Последний чистый кадр одним JPEG — локальный релей для main.py/data_collector
            jpeg = self.frame_bc.latest() if self.frame_bc is not None else None
            if jpeg is None:
                self.send_error(503, "no frame yet")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpeg)))
            self.end_headers()
            try:
                self.wfile.write(jpeg)
            except OSError:
                pass
        else:
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

    def _stream(self, broadcaster: Broadcaster | None) -> None:
        if broadcaster is None:
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
            self.end_headers()
        except OSError:
            return
        seen = 0
        try:
            while True:
                jpeg, seen = broadcaster.wait(seen)
                if jpeg is None:
                    continue
                self.wfile.write(
                    b"--" + BOUNDARY.encode() +
                    b"\r\nContent-Type: image/jpeg\r\nContent-Length: " +
                    str(len(jpeg)).encode() + b"\r\n\r\n")
                self.wfile.write(jpeg)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        except OSError:
            pass


VEHICLE_CLASSES = {1, 2, 3, 5, 7}  # bicycle, car, motorcycle, bus, truck (COCO)
PERSON_CLASS = 0


def color_for(obj: dict, vest: bool) -> tuple:
    if obj["class_id"] == PERSON_CLASS:
        return (0, 0, 255) if vest else (0, 165, 255)
    if obj["class_id"] in VEHICLE_CLASSES:
        return (230, 180, 0)
    return (160, 160, 160)


def _ignore_bbox(bbox, ignore_rois, shape, overlap_frac_min: float = 0.5):
    """Бокс игнорируется, когда >= overlap_frac_min его площади в зоне (не по центру)."""
    return bbox_ignored(bbox, ignore_rois, shape, overlap_frac_min)


def draw_frame(frame, plates, objects, vest_persons):
    for p in plates:
        x1, y1, x2, y2 = p["bbox"]
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.putText(frame, p.get("text", ""), (x1, y1 - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)
    for o in objects:
        x1, y1, x2, y2 = o["bbox"]
        cv2.rectangle(frame, (x1, y1), (x2, y2), color_for(o, o["class_id"] in vest_persons), 2)
        cv2.putText(frame, f"{o['name']} {o['confidence']:.2f}", (x1, y1 - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color_for(o, o["class_id"] in vest_persons), 2)
    return frame


def _jpeg_quality(cfg: dict, key: str, default: int) -> int:
    """Качество JPEG из конфига, с зажимом в 1..100.

    Значение в конфиге может быть строкой или мусором — падать из-за этого
    на старте релея нельзя, поэтому при любой ошибке берём default.
    """
    try:
        value = int(cfg.get(key, default))
    except (TypeError, ValueError):
        return default
    return max(1, min(100, value))


def raw_loop(stream: CameraStream, cfg: dict, raw_bc: Broadcaster, frame_bc: Broadcaster,
             ann_bc: Broadcaster | None = None):
    """Публикация трёх потоков: /raw, /frame, /stream (чистый кадр в ann_bc).

    main.py присылает аннотированный JPEG (POST /annotations) и перезаписывает
    ann_bc — боксы рисуются на ТОМ ЖЕ кадре, где делалась детекция, без лага
    между кадром и разметкой.

    Качество: /frame — это кадр, из которого main.py делает снимок для письма
    (ещё один JPEG перед отправкой), поэтому он кодируется «снимковым»
    качеством. /raw смотрят глазами на странице :8090 — ему хватает view.
    """
    snapshot_q = _jpeg_quality(cfg, "snapshot_jpeg_quality", 95)
    view_q = _jpeg_quality(cfg, "view_jpeg_quality", 80)
    last_print = time.monotonic()
    frames = 0
    seen = stream.read_seq()
    while True:
        frame, seen = stream.read_new(seen, timeout=1.0)
        if frame is None:
            time.sleep(0.2)
            continue

        # copy() не нужен: imencode кадр не меняет, а копия 1080p — лишние
        # ~6 МБ памяти на каждый кадр.
        ok, raw_buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, view_q])
        if ok:
            raw_bc.update(raw_buf.tobytes())
        ok, clean_buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, snapshot_q])
        if ok:
            frame_bc.update(clean_buf.tobytes())
            # /stream: пока main.py присылает свежие аннотации (POST /annotations,
            # с боксами на том же кадре) — держим их, НЕ перетираем чистым кадром
            # каждые 200 мс (иначе боксы мигают). Нет/протухли аннотации —
            # отдаём живой чистый кадр, чтобы /stream не замирал.
            if ann_bc is not None and not ann_bc.hold_fresh():
                ann_bc.update(clean_buf.tobytes())

        frames += 1
        now = time.monotonic()
        if now - last_print >= 5.0:
            print(f"[RAW] {frames / (now - last_print):.1f} fps")
            last_print = now
            frames = 0


def capture_loop(stream: CameraStream, cfg: dict, nn_bc: Broadcaster, no_person: bool):
    anpr_cfg = cfg.get("anpr", {})
    plate_reader = PlateReader(
        min_confidence=cfg.get("thresholds", {}).get("plate_min_conf", 0.5),
        imgsz=anpr_cfg.get("imgsz", 1280),
        upscale_min_height=anpr_cfg.get("upscale_min_height", 48),
        upscale_max_factor=anpr_cfg.get("upscale_max_factor", 5),
    )
    obj_detector = ObjectDetector(
        model_path=cfg.get("object_model", "yolov8s.pt"),
        min_confidence=cfg.get("thresholds", {}).get("object_min_conf", 0.4),
        classes=cfg.get("object_classes"),
        imgsz=cfg.get("object_imgsz", 960))
    vest_detector = VestDetector(min_yellow_ratio=cfg.get("thresholds", {}).get("vest_yellow_min_ratio", 0.2))
    obj_detector.detect(np.zeros((64, 64, 3), dtype=np.uint8))  # прогрев

    ignore_rois = []
    plate_ignore_rois = []
    for group in cfg.values():
        if not isinstance(group, dict):
            continue
        ignore_rois += [tuple(float(v) for v in rect) for rect in group.get("ignore_rois", []) if len(rect) == 4]
        plate_ignore_rois += [tuple(float(v) for v in rect) for rect in
                              group.get("plate_ignore_rois", []) if len(rect) == 4]

    print("Открой http://localhost:8090 — /stream с распознаванием, /raw чистый. (Ctrl+C — остановка)")

    view_q = _jpeg_quality(cfg, "view_jpeg_quality", 80)

    object_every_n = max(1, cfg.get("object_detect_every_n_frames", 2))
    # Live-обзор не обязан читать номер каждый кадр: это отдельный heavy-пайплайн,
    # конкурирующий за CPU с main.py. Рисуем последние известные боксы.
    plate_every_n = max(1, cfg.get("plate_detect_every_n_frames", 4))
    last_print = time.monotonic()
    frames = 0
    frame_idx = 0
    objects: list = []
    plates: list = []
    while True:
        frame = stream.read()
        if frame is None:
            time.sleep(1.0)
            continue
        proc_start = time.perf_counter()
        objects = obj_detector.detect(frame) if frame_idx % object_every_n == 0 else objects
        if objects:
            objects = [o for o in objects if not _ignore_bbox(o["bbox"], ignore_rois, frame.shape)]
        persons = [o for o in objects if o["class_id"] == PERSON_CLASS]
        vest_persons = set()
        if not no_person and persons:
            vest_persons = {id(p) for p in vest_detector.flag_persons(frame, persons)}
        if frame_idx % plate_every_n == 0:
            plates = plate_reader.process_frame(frame)
            if plates:
                # Как в main.py: номера над полосой (дальняя дорога) не показываем
                plates = [p for p in plates
                          if not (_ignore_bbox(p["bbox"], ignore_rois, frame.shape)
                                  or _ignore_bbox(p["bbox"], plate_ignore_rois, frame.shape))]
        proc_ms = (time.perf_counter() - proc_start) * 1000
        frame_idx += 1

        ann = frame.copy()
        draw_frame(ann, plates, objects, vest_persons)
        ok, buf = cv2.imencode(".jpg", ann, [cv2.IMWRITE_JPEG_QUALITY, view_q])
        if ok:
            nn_bc.update(buf.tobytes())

        frames += 1
        now = time.monotonic()
        if now - last_print >= 5.0:
            print(f"[NN] {frames / (now - last_print):.1f} fps | обработка {proc_ms:.0f} мс | "
                  f"номера: {[p['text'] for p in plates]} | объекты: {[o['name'] for o in objects]}")
            last_print = now
            frames = 0

    stream.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Живой поток: /stream — с распознаванием, /raw — чистый")
    parser.add_argument("--source", help="RTSP URL (по умолчанию первая камера из config.yaml)")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parent / "config.yaml"))
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--no-person", action="store_true")
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    source = args.source
    if source is None:
        cameras = cfg.get("cameras", [])
        if not cameras:
            print("ОШИБКА: нет камер в config.yaml, укажите --source")
            return
        source = cameras[0]["rtsp_url"]

    nn_bc = Broadcaster()
    raw_bc = Broadcaster()
    frame_bc = Broadcaster()
    ann_bc = Broadcaster()
    det_cache = DetectionCache()
    Handler.nn_bc = nn_bc
    Handler.raw_bc = raw_bc
    Handler.frame_bc = frame_bc
    Handler.ann_bc = ann_bc
    Handler.detection_cache = det_cache

    stream = CameraStream("stream", source)
    stream.start()
    live_nn = bool(cfg.get("live_nn", False))
    # /stream: в live_nn-режиме — nn_bc (capture_loop, вторая нейросеть),
    # иначе — ann_bc (main.py рисует боксы на своих кадрах, raw_loop подливает
    # живой чистый кадр, пока аннотаций нет). Выбор в main(), а не в хендлере:
    # `ann_bc or nn_bc` там всегда давал ann_bc (объект = truthy), и /stream
    # мёртво прилипал к ann_bc даже когда аннотаций не было.
    Handler.stream_bc = nn_bc if live_nn else ann_bc
    raw_ann_bc = None if live_nn else ann_bc
    threading.Thread(target=raw_loop, args=(stream, cfg, raw_bc, frame_bc, raw_ann_bc),
                     daemon=True).start()
    if live_nn:
        threading.Thread(target=capture_loop, args=(stream, cfg, nn_bc, args.no_person),
                         daemon=True).start()
        print("live_nn=true: /stream — полная NN-разметка (yolov8s+OCR внутри stream_server)")
    else:
        print("live_nn=false (облегчённый): /stream — main.py рисует боксы на своих кадрах "
              "(POST /annotations), без второй YOLO")

    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановка")


if __name__ == "__main__":
    main()