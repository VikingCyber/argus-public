import argparse
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import requests
import yaml

from event_client import EventClient
from frame_source import open_source
from object_detector import ObjectDetector
from person_detector import NightWindow, PersonDetector
from plate_reader import PlateReader
from plate_text import normalize_plate
from tracking import EpisodeTracker
from vest_detector import VestDetector
from zones import in_detection_area as _zone_ok

VEHICLE_CLASS_IDS = {1, 2, 3, 5, 7}  # bicycle, car, motorcycle, bus, truck (COCO)

# Русские названия классов YOLO для подписей на фото — чтобы обывателю было понятно.
RU_NAMES = {
    "person": "человек",
    "bicycle": "велосипед",
    "car": "автомобиль",
    "motorcycle": "мотоцикл",
    "bus": "автобус",
    "truck": "грузовик",
}

_FONT_PATH = "C:/Windows/Fonts/arial.ttf"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("argus-vision")


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class CameraWorker:
    """Обрабатывает кадры одной камеры и отправляет события в бэкенд."""

    def __init__(self, cam_cfg: dict, cfg: dict, client: EventClient,
                 plate_reader: PlateReader | None,
                 person_detector: PersonDetector,
                 vest_detector: VestDetector,
                 vehicle_detector: ObjectDetector | None = None,
                 unread_cfg: dict | None = None):
        self.cam_cfg = cam_cfg
        self.cam_id = cam_cfg["id"]
        self.tasks = cam_cfg.get("tasks", {})
        self.thresholds = cfg.get("thresholds", {})
        self.cooldowns = cfg.get("cooldowns_sec", {})
        self.client = client

        self.plate_reader = plate_reader
        self.person_detector = person_detector
        self.vest_detector = vest_detector
        self.vehicle_detector = vehicle_detector
        self.unread_cfg = unread_cfg or {}
        self._person_tasks_enabled = bool(
            self.tasks.get("night_person") or self.tasks.get("yellow_vest"))
        self._vehicle_seen_enabled = bool(self.tasks.get("vehicle_seen"))
        self._vehicle_tasks_enabled = bool(
            self.tasks.get("anpr") or self._vehicle_seen_enabled)

        night = cfg.get("night_window", {})
        self.night_window = NightWindow(
            night.get("start_hour", 20),
            night.get("end_hour", 8),
        )
        self.person_every_n = cfg.get("person_detect_every_n_frames", 3)
        self.vehicle_every_n = self.unread_cfg.get("every_n_frames", 1)

        # Идентичность машины даёт ByteTrack внутри ultralytics: track_id приходит
        # из детекции и не меняется при сдвиге машины в кадре. Здесь живёт только
        # состояние эпизода — голоса за номер и флаги событий (см. tracking.py).
        self.tracks = EpisodeTracker(self.unread_cfg)
        # ROI (доли кадра 0..1): считать только ТС внутри зоны; None = весь кадр
        roi = self.unread_cfg.get("roi")
        self._roi = tuple(float(v) for v in roi) if roi and len(roi) == 4 else None
        self._ignore_rois = [tuple(float(v) for v in rect) for rect in
                             self.unread_cfg.get("ignore_rois", []) if len(rect) == 4]
        # Игнор по перекрытию площади: бокс «в зоне», если >= ignore_overlap_frac
        # его площади внутри (машина наполовину в зоне = игнор, а не детект).
        self._ignore_overlap = self.unread_cfg.get("ignore_overlap_frac", 0.5)
        # Дополнительные игнор-зоны только для чтений номеров: вся дальняя дорога
        # над полосой по всей ширине кадра (OCR там мусорный, транзитные машины
        # за забором не должны давать события и треки).
        self._plate_ignore_rois = [tuple(float(v) for v in rect) for rect in
                                   self.unread_cfg.get("plate_ignore_rois", []) if len(rect) == 4]
        # Слишком мелкий номер — мусорное чтение (дальние машины), не голос.
        self._plate_min_height = self.thresholds.get("plate_min_bbox_height", 10)
        # Зона въезда: UNREAD-тревога только для машин, впервые появившихся в ней
        self._entry_rois = [tuple(float(v) for v in rect) for rect in
                            self.unread_cfg.get("entry_rois", []) if len(rect) == 4]
        self._readable_min_area_frac = self.unread_cfg.get("readable_min_area_frac", 0.02)
        # Сколько секунд ТС должно держаться ЦЕЛИКОМ в кадре, прежде чем отправим
        # VEHICLE_SEEN не дожидаясь уезда (фура может стоять сутками). Если машина
        # проехала мимо не останавливаясь — сработает страховка при закрытии эпизода.
        self._vehicle_seen_ready_sec = float(
            self.unread_cfg.get("vehicle_seen_ready_sec", 3.0))

        self.stream = open_source(self.cam_id, cam_cfg.get("relay_url") or cam_cfg["rtsp_url"],
                                  cam_cfg.get("name", ""),
                                  fps=float(cfg.get("relay_fps", 12)))
        # Куда слать боксы ТС для /stream (stream_server). Из URL релея выводим
        # http://127.0.0.1:8090, /frame заменяем на /detections.
        stream_url = getattr(self.stream, "url", "")
        self._detections_url = stream_url.rsplit("/", 1)[0] + "/detections" if "/frame" in stream_url else ""
        self._annotations_url = stream_url.rsplit("/", 1)[0] + "/annotations" if "/frame" in stream_url else ""
        self._last_detections: list[dict] = []
        self._last_detections_ts = 0.0
        self._last_annotated_frame: np.ndarray | None = None
        self._detections_interval = float(cfg.get("detections_interval", 1.0))
        # Качество JPEG для /stream (боксы поверх живого кадра). Кадр смотреть
        # глазом на странице :8090, а не читать номер, поэтому 80 достаточно:
        # 70 мылило контуры, 95 в разы увеличивал поток по LAN без пользы.
        self._view_jpeg_quality = max(1, min(100, int(cfg.get("view_jpeg_quality", 80))))
        # Диагностика аннотаций: сколько кадров с боксами реально ушло в /stream.
        # Раньше ошибка POST глохла в except, и «боксы пропали» было нечем поймать.
        self._ann_sent = 0
        self._ann_failed = 0
        self._ann_last_error = ""
        self._frame_counter = 0
        self._no_frame_warned = False
        self._started_at = time.monotonic()
        self._last_sent: dict[str, float] = {}
        # Пауза между повторными чтениями одного кадра HTTP-релея (опрос /frame
        # идёт чаще, чем камера отдаёт кадры).
        self._idle_sleep_sec = 0.02
        # Диагностика дублей кадров: считаем прогоны цикла и раз в 10с выводим
        # отчёт (новые кадры / повторы / сколько ушло в трекер). 0 = выключено.
        self._frame_stats_interval = 10.0
        self._frame_stats_logs = 0

    def _send_if_cooldown_ok(self, key: str, cooldown: int, send_fn) -> None:
        now = time.monotonic()
        if now - self._last_sent.get(key, 0.0) < cooldown:
            return
        if send_fn():
            self._last_sent[key] = now

    def _process_plates(self, frame) -> list[dict]:
        """Чтения номеров за кадр: фильтр зон/размера -> привязка к эпизоду машины.

        Номер не создаёт трек: без кузова нечего трекать, иначе каждое чтение
        роняет по машине — именно это давало 2-6 событий на один грузовик.
        """
        kept: list[dict] = []
        if self.plate_reader is None:
            return kept
        now = time.monotonic()
        shape = frame.shape
        for plate in self.plate_reader.process_frame(frame):
            bbox = plate["bbox"]
            if not _zone_ok(bbox, self._ignore_rois, shape, self._ignore_overlap,
                            roi=self._roi, extra_ignore=self._plate_ignore_rois):
                continue
            if bbox[3] - bbox[1] < self._plate_min_height:
                continue
            text = normalize_plate(plate["text"])
            if not text:
                continue
            kept.append(plate)
            episode = self.tracks.attach_plate(bbox, text, plate["confidence"], frame)
            if episode is None:
                continue
            log.debug("[%s] чтение номера %s у эпизода #%s",
                      self.cam_id, text, episode.track_id)
            episode.note_vehicle_frame(frame, shape, now)
            episode.mark_readable(now, self._readable_min_area_frac, shape)
        return kept

    def _in_detection_area(self, bbox: list[int], shape) -> bool:
        """Вне игнор-зон (перекрытие площади >= ignore_overlap_frac) и в рабочей зоне."""
        return _zone_ok(bbox, self._ignore_rois, shape, self._ignore_overlap,
                        roi=self._roi)

    def _center_in_rois(self, bbox: list[int], rois: list[tuple], shape) -> bool:
        h, w = shape[:2]
        cx = (bbox[0] + bbox[2]) / 2 / w
        cy = (bbox[1] + bbox[3]) / 2 / h
        return any(x1 <= cx <= x2 and y1 <= cy <= y2 for x1, y1, x2, y2 in rois)

    def _mark_entry(self, episodes: list, shape) -> None:
        """Отмечает эпизоды, появившиеся в зоне въезда (для правила UNREAD).

        Отметка разовая и навсегда: приехавшая машина остаётся «приехавшей», даже
        если потом отъедет за пределы зоны. Иначе тревога могла бы не сработать
        потому, что к моменту таймера машина уже выехала из entry_rois.
        """
        if not self._entry_rois:
            return
        for episode in episodes:
            if episode.born_in_entry:
                continue
            if self._center_in_rois(episode.bbox, self._entry_rois, shape):
                episode.born_in_entry = True

    def _track_bodies(self, frame, shape) -> None:
        """Фаза 1 кадра: треки кузовов ByteTrack + закрытие ушедших эпизодов.

        Порядок фаз важен: сначала треки (чтобы было к чему привязывать номера),
        потом чтения, потом события — иначе consensus проверялся бы по вчерашнему кадру.
        """
        if self.vehicle_detector is None:
            log.error("[%s] трекинг включён, но детектор ТС не создан", self.cam_id)
            return
        now = time.monotonic()
        vehicles = [v for v in self.vehicle_detector.track_objects(frame)
                    if v["class_id"] in VEHICLE_CLASS_IDS
                    and v.get("track_id") is not None
                    and self._in_detection_area(v["bbox"], shape)]
        # Снимок для /stream: боксы пригодны только пока кадры актуальны.
        # Отправку делает отдельный поток (_detections_sender), здесь только память.
        self._last_detections = [
            {"bbox": list(v["bbox"]), "confidence": float(v["confidence"]),
             "class_id": int(v["class_id"]), "name": str(v["name"]),
             "track_id": int(v["track_id"])}
            for v in vehicles]
        self._last_detections_ts = time.monotonic()
        self._last_annotated_frame = frame.copy() if vehicles else None
        gone = self.tracks.observe(vehicles, now)
        self._mark_entry(list(self.tracks.episodes.values()), shape)
        for track_id in gone:
            episode = self.tracks.episodes.get(track_id)
            if episode is not None:
                self._finalize(episode, frame)

    def _finish_episodes(self, frame) -> None:
        """Фаза 3 кадра: чтения уже привязаны — проверяем consensus и таймеры."""
        now = time.monotonic()
        shape = frame.shape
        for episode in list(self.tracks.episodes.values()):
            if episode.notified:
                # Номер уже отправлен, но эпизод ещё жив (машина в кадре).
                # Держать в памяти best_vehicle_frame незачем: событие позади.
                continue
            episode.note_vehicle_frame(frame, shape, now)
            episode.mark_readable(now, self._readable_min_area_frac, shape)
            if self._vehicle_seen_enabled:
                # Упрощённый режим: событие шлём, как только ТС надёжно видно
                # целиком (не ждём уезда!), или при закрытии эпизода (_finalize).
                if (episode.full_in_frame_since is not None
                        and now - episode.full_in_frame_since >= self._vehicle_seen_ready_sec):
                    self._emit_vehicle_seen(episode, frame)
                continue
            pending = self.tracks.pending_plate(episode)
            if pending is not None:
                self._emit_plate(episode, *pending, frame)
                continue
            self._maybe_unread(episode, frame, now)

    def _emit_plate(self, episode, text: str, confidence: float, frame) -> None:
        """Одно событие на эпизод: номер consensus отправлен — эпизод закрыт."""
        if episode.notified:
            return
        image = episode.best_frame(frame)
        sent = self.client.send_plate(
            camera_id=self.cam_id,
            plate=text,
            confidence=confidence,
            track_id=episode.track_id,
            detected_at=datetime.now().isoformat(timespec="seconds"),
            image=image,
        )
        if not sent:
            # Номер не ушёл: эпизод оставляем жить, чтобы следующий кадр
            # попробовал ещё раз. Молча терять проезд нельзя.
            log.warning("[%s] трек #%d: номер %s не доставлен, будет попытка позже",
                        self.cam_id, episode.track_id, text)
            return
        episode.notified = True
        episode.sent = True
        episode.resolved_plate = text
        self.tracks.close(episode.track_id)
        log.info("[%s] трек #%d: номер %s (%.2f) отправлен",
                 self.cam_id, episode.track_id, text, confidence)

    def _maybe_unread(self, episode, frame, now: float) -> None:
        """Тревога «номер не прочитан» — только если машина стоит в зоне достаточно
        долго и ни одного consensus не набралось. Одна тревога на эпизод."""
        if episode.notified or episode.readable_since is None:
            return
        if self._entry_rois and not episode.born_in_entry:
            return
        if now - episode.readable_since < self.unread_cfg.get("timeout_sec", 10):
            return
        sent = self.client.send_alert(
            camera_id=self.cam_id,
            alert_type="UNREAD_PLATE",
            track_id=episode.track_id,
            detected_at=datetime.now().isoformat(timespec="seconds"),
            image=episode.best_frame(frame),
        )
        if not sent:
            # Не отмечаем флагом: тревога попробует отправиться на следующем кадре.
            log.warning("[%s] трек #%d: UNREAD_PLATE не доставлена, будет попытка позже",
                        self.cam_id, episode.track_id)
            return
        episode.unread_alarmed = True
        log.warning("[%s] трек #%d без номера — UNREAD_PLATE отправлена",
                    self.cam_id, episode.track_id)

    def _emit_vehicle_seen(self, episode, frame) -> None:
        """Упрощённый режим (MVP без распознавания номеров): одно информационное
        событие VEHICLE_SEEN на эпизод, с фото самого крупного кузова."""
        if episode.notified:
            return
        # Сцеп: тягач и прицеп — два эпизода, и прицеп YOLO считает обычным
        # грузовиком. Если рядом уже отправленное ТС и у него та же скорость
        # (едем вместе), второе письмо по тому же сцепу не шлём. Разные
        # скорости — это чужая машина: трактор, проезжающий мимо стоящей фуры,
        # событие получает. Эпизод помечаем, иначе проверка повторялась бы каждый кадр.
        if self.tracks.notified_neighbor(episode.bbox, time.monotonic(),
                                         episode.speed()) is not None:
            episode.notified = True
            episode.resolved_plate = ""
            log.info("[%s] трек #%d: рядом отправленное ТС с той же скоростью "
                     "(сцеп) — VEHICLE_SEEN подавлен", self.cam_id, episode.track_id)
            return
        image = self._annotated_frame(episode, frame)
        sent = self.client.send_alert(
            camera_id=self.cam_id,
            alert_type="VEHICLE_SEEN",
            track_id=episode.track_id,
            detected_at=datetime.now().isoformat(timespec="seconds"),
            image=image,
        )
        if not sent:
            log.warning("[%s] трек #%d: VEHICLE_SEEN не доставлено, будет попытка позже",
                        self.cam_id, episode.track_id)
            return
        episode.notified = True
        episode.sent = True
        episode.resolved_plate = ""
        # Запоминаем машину на уровне сцены: если ByteTrack потом разорвёт трек
        # и эпизод закроется, повторная детекция в этой точке не даст второе письмо.
        self.tracks.mark_notified(episode)
        log.info("[%s] трек #%d: VEHICLE_SEEN отправлено", self.cam_id, episode.track_id)

    def _annotated_frame(self, episode, frame):
        """Кадр для отправки с нарисованной рамкой ТС и русской подписью.

        Приоритет — кадр, где ТС видно ЦЕЛИКОМ (best_full_*): если машина долго
        стоит, не хотим слать снимок, где она уехала/обрезана краем. Иначе — самый
        крупный кузов (best_vehicle_*). Рисуем на копии: кадр живёт в эпизоде и
        не должен меняться. Если ничего нет — возвращаем текущий без разметки.
        """
        if episode.best_full_frame is not None:
            img = episode.best_full_frame.copy()
            box = episode.best_full_bbox
            name = episode.best_full_name
            conf = episode.best_full_conf
        elif episode.best_vehicle_frame is not None:
            img = episode.best_vehicle_frame.copy()
            box = episode.best_vehicle_bbox
            name = episode.best_vehicle_name
            conf = episode.best_vehicle_conf
        else:
            return frame
        if box:
            x1, y1, x2, y2 = (round(v) for v in box)
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), 2)
            label = RU_NAMES.get(name, name or "транспорт")
            tag = f"{label} {conf:.2f}"
            self._draw_label(img, tag, (x1, max(12, y1 - 8)))
        return img

    @staticmethod
    def _draw_label(img, text: str, origin) -> None:
        """Кириллическая подпись поверх кадра (cv2.putText кириллицу не умеет).

        Рисуем через PIL с системным шрифтом Arial. При неудаче шрифта/букв
        подпись пропускается — кадр всё равно остаётся с рамкой.
        """
        try:
            from PIL import Image, ImageDraw, ImageFont
            pil_img = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
            draw = ImageDraw.Draw(pil_img)
            font = ImageFont.truetype(_FONT_PATH, 22)
            tx, ty = origin
            draw.text((tx, ty), text, font=font, fill=(0, 255, 0))
            annotated = cv2.cvtColor(np.asarray(pil_img), cv2.COLOR_RGB2BGR)
            img[:] = annotated
        except (OSError, UnicodeError, ValueError):
            return

    def _finalize(self, episode, frame) -> None:
        """Эпизод ушёл из кадра: последний шанс отдать накопленный номер."""
        if episode.notified:
            pass
        elif self._vehicle_seen_enabled:
            self._emit_vehicle_seen(episode, frame)
        else:
            pending = self.tracks.pending_plate(episode)
            if pending is not None:
                self._emit_plate(episode, *pending, frame)
            elif episode.readable_since is not None and not episode.unread_alarmed:
                self._maybe_unread(episode, frame, time.monotonic())
        # Выехавшая за край машина действительно уехала — память о ней короткая,
        # иначе следующая машина в этой точке останется без письма.
        self.tracks.note_exit(episode, frame.shape)
        self.tracks.close(episode.track_id)
        log.debug("[%s] эпизод #%d закрыт", self.cam_id, episode.track_id)

    def _process_persons(self, frame) -> None:
        persons = self.person_detector.detect(frame)
        if not persons:
            return

        night_active = self.night_window.is_active()
        if self.tasks.get("night_person") and night_active:
            cooldown = self.cooldowns.get("night_person", 60)
            key = f"alert:NIGHT_PERSON:{self.cam_id}"
            self._send_if_cooldown_ok(
                key, cooldown,
                lambda: self.client.send_alert(
                    camera_id=self.cam_id,
                    alert_type="NIGHT_PERSON",
                    detected_at=datetime.now().isoformat(timespec="seconds"),
                    image=frame,
                ),
            )
            log.info("[%s] тревога: человек в ночное время", self.cam_id)

        if self.tasks.get("yellow_vest"):
            cooldown = self.cooldowns.get("yellow_vest", 30)
            flagged = self.vest_detector.flag_persons(frame, persons)
            for person in flagged:
                if person["vest"]:
                    key = f"alert:YELLOW_VEST:{self.cam_id}"
                    self._send_if_cooldown_ok(
                        key, cooldown,
                        lambda: self.client.send_alert(
                            camera_id=self.cam_id,
                            alert_type="YELLOW_VEST",
                            detected_at=datetime.now().isoformat(timespec="seconds"),
                            image=frame,
                        ),
                    )
                    log.info("[%s] тревога: человек в жёлтом жилете", self.cam_id)

    def _detections_sender(self) -> None:
        """Шлёт аннотированный JPEG в stream_server (/stream).

        Рисует боксы на том же кадре, где считалась детекция — никакого лага
        «бокс на земле». Если сервер лежит — молча ждём.
        """
        last_sent: list[dict] = []
        while True:
            time.sleep(self._detections_interval)
            if not self._annotations_url or not self._last_detections:
                continue
            if time.monotonic() - self._last_detections_ts > 2.0:
                continue
            if self._last_detections == last_sent:
                continue
            last_sent = list(self._last_detections)
            ann_frame = self._last_annotated_frame
            if ann_frame is None:
                continue
            ann = ann_frame.copy()
            for o in self._last_detections:
                x1, y1, x2, y2 = o["bbox"]
                cv2.rectangle(ann, (x1, y1), (x2, y2), (0, 255, 0), 2)
                cv2.putText(ann, f"{o.get('name', '')} {o.get('confidence', 0):.2f}", (x1, y1 - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
            ok, buf = cv2.imencode(".jpg", ann,
                                   [cv2.IMWRITE_JPEG_QUALITY, self._view_jpeg_quality])
            if not ok:
                continue
            try:
                resp = requests.post(self._annotations_url, data=buf.tobytes(), timeout=0.5,
                                     headers={"Content-Type": "image/jpeg"})
                if resp.status_code != 200:
                    self._ann_failed += 1
                    self._ann_log_error("HTTP %s от %s", resp.status_code, self._annotations_url)
                else:
                    self._ann_sent += 1
            except requests.RequestException as e:
                self._ann_failed += 1
                self._ann_log_error("%s: %s", type(e).__name__, e)

    def _ann_log_error(self, fmt: str, *args) -> None:
        """Ошибка отправки аннотаций — не чаще раза в 30 с, но обязательно в лог."""
        now = time.monotonic()
        if now - getattr(self, "_ann_err_ts", 0.0) < 30.0:
            return
        self._ann_err_ts = now
        msg = fmt % args if args else fmt
        self._ann_last_error = msg
        log.error("[%s] аннотации не уходят в /annotations: %s", self.cam_id, msg)

    def run(self) -> None:
        self.stream.start()
        url = getattr(self.stream, "rtsp_url", None) or getattr(self.stream, "url", None)
        log.info("[%s] подключение к %s", self.cam_id, url)
        if self._detections_url:
            threading.Thread(target=self._detections_sender,
                             name=f"detsend-{self.cam_id}", daemon=True).start()
        last_seq = -1
        stats = self.stream.stats()
        stats_last = dict(stats)
        stats_t0 = time.monotonic()
        n_new = 0
        n_dups = 0
        while True:
            frame = self.stream.read()
            if frame is None:
                now = time.monotonic()
                # Грация на старте: stream_server грузит модели несколько секунд,
                # кадры появляются не мгновенно — не пугаемся в первые 30с.
                if not self.stream.is_connected and (now - self._started_at > 30):
                    if not self._no_frame_warned:
                        log.error("[%s] кадры не приходят: нет соединения с источником (%s). "
                                  "Проверь stream_server (порт 8090) и доступность камеры.",
                                  self.cam_id, url)
                        self._no_frame_warned = True
                    time.sleep(2.0)
                else:
                    # Кадров ещё нет, но источник считается живым (старт, короткий
                    # провал релея). Без паузы цикл крутится вхолостую и жжёт ядро.
                    time.sleep(0.05)
                continue

            # HTTP-релей отдаёт один кадр камеры несколько раз подряд (опрос
            # идёт чаще, чем FPS камеры). ByteTrack считает буфер в кадрах —
            # повторный прогон того же кадра «съедает» track_buffer и рвёт
            # track_id. Обрабатываем только новые кадры.
            seq = self.stream.read_seq()
            if seq is not None and seq == last_seq:
                n_dups += 1
                time.sleep(self._idle_sleep_sec)
                continue
            last_seq = seq
            n_new += 1

            if self._frame_stats_interval > 0:
                now = time.monotonic()
                if now - stats_t0 >= self._frame_stats_interval:
                    stats = self.stream.stats()
                    dp = {k: st - stats_last[k] for k, st in stats.items() if k in stats_last}
                    stats_last = dict(stats)
                    stats_t0 = now
                    log.info("[%s] кадры: новых=%d повторов_пропущено=%d | "
                             "http-опрос: поллы=%d дубли_релея=%d новинок=%d сбой_декод=%d | "
                             "детектов=%d анн: отправлено=%d ошибок=%d | "
                             "эпизодов=%d переприсвоений=%d",
                             self.cam_id, n_new, n_dups,
                             dp.get("polls", 0), dp.get("dups", 0),
                             dp.get("new", 0), dp.get("decode_fail", 0),
                             len(self._last_detections), self._ann_sent, self._ann_failed,
                             len(self.tracks.episodes), self.tracks.reassoc_count)
                    self._ann_sent = 0
                    self._ann_failed = 0
                    n_new = n_dups = 0
                    self._frame_stats_logs += 1

            self._frame_counter += 1
            if self._vehicle_tasks_enabled:
                if self.vehicle_detector is None:
                    log.error("[%s] трекинг включён, но нет unread_plate.model — детектор ТС не создан",
                              self.cam_id)
                else:
                    # Треки кузовов идут каждый кадр: буфер ByteTrack считается в кадрах,
                    # и пропуск кадров рвёт эпизод.
                    if self._frame_counter % self.vehicle_every_n == 0:
                        self._track_bodies(frame, frame.shape)
                    if self.tasks.get("anpr"):
                        self._process_plates(frame)
                    self._finish_episodes(frame)

            if self._person_tasks_enabled and self._frame_counter % self.person_every_n == 0:
                self._process_persons(frame)


def main() -> None:
    parser = argparse.ArgumentParser(description="Argus vision-сервис")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parent / "config.yaml"))
    args = parser.parse_args()

    cfg = load_config(args.config)
    backend_cfg = cfg.get("backend", {})
    client = EventClient(backend_cfg.get("url", "http://localhost:8080"),
                         backend_cfg.get("api_key", ""),
                         # Качество сохраняемого снимка (в письмо/журнал). 95 вместо
                         # исторических 85: кадр уже прошёл JPEG в релее, повторное
                         # сжатие на 85 мылило номер и номерной знак.
                         jpeg_quality=cfg.get("snapshot_jpeg_quality", 95))

    anpr_cfg = cfg.get("anpr", {})
    log.info("Загрузка моделей...")
    # PlateReader (OCR номеров) — тяжёлая двойная модель (детектор + CRNN).
    # В MVD-режиме (только vehicle_seen) номера не нужны: не грузим вообще.
    anpr_needed = any(cam.get("tasks", {}).get("anpr") for cam in cfg.get("cameras", []))
    plate_reader = None
    if anpr_needed:
        plate_reader = PlateReader(
            min_confidence=cfg.get("thresholds", {}).get("plate_min_conf", 0.5),
            imgsz=anpr_cfg.get("imgsz", 1280),
            upscale_min_height=anpr_cfg.get("upscale_min_height", 48),
            upscale_max_factor=anpr_cfg.get("upscale_max_factor", 5),
        )
    person_detector = PersonDetector(
        min_confidence=cfg.get("thresholds", {}).get("person_min_conf", 0.5)
    )
    vest_detector = VestDetector(
        min_yellow_ratio=cfg.get("thresholds", {}).get("vest_yellow_min_ratio", 0.2)
    )
    unread_cfg = cfg.get("unread_plate", {})
    # Классы детектора объектов ограничены конфигом (человек + транспорт),
    # чтобы модель не тратила время и не показывала мусорные боксы.
    # Для детектора ТС в трекере оставляем только транспорт.
    object_classes = cfg.get("object_classes")
    vehicle_classes = sorted(set(object_classes) & VEHICLE_CLASS_IDS) if object_classes else None
    vehicle_detector = ObjectDetector(
        model_path=unread_cfg.get("model", "yolov8n.pt"),
        min_confidence=unread_cfg.get("min_conf", 0.4),
        classes=vehicle_classes,
        imgsz=unread_cfg.get("imgsz", 640),
        tracker="bytetrack_argus.yaml",
    ) if unread_cfg else None
    log.info("Модели загружены.")

    workers = [CameraWorker(cam, cfg, client, plate_reader, person_detector,
                            vest_detector, vehicle_detector, unread_cfg)
               for cam in cfg.get("cameras", [])]
    if not workers:
        log.error("В config.yaml нет ни одной камеры.")
        return

    for w in workers:
        threading.Thread(target=w.run, name=f"worker-{w.cam_id}", daemon=True).start()

    log.info("Argus vision запущен, камер: %d", len(workers))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Остановка...")


if __name__ == "__main__":
    main()
