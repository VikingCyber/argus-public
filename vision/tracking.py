"""Трекинг ТС и жизненный цикл эпизода проезда.

Модуль чистый: без torch, cv2 и ultralytics, поэтому вся логика эпизода
тестируется обычными тестами и не тянет модель в каждый прогон.

Идентичность машины задаёт трекер ultralytics (ByteTrack), а не наш код:
track_id приходит готовым и не меняется при сдвиге машины в кадре. Здесь мы
храним состояние эпизода и решаем, когда проезд считать завершённым.

Одно правило на проезд: событие отправляется один раз, дальше эпизод закрыт.
Прежде здесь жили четыре независимых механизма (cooldown по тексту, дедуп по
позиции, episode_emitted, episode_alarmed), и они конфликтовали между собой —
одна машина давала и тревогу, и номер.
"""
from collections import Counter, defaultdict
from collections.abc import Sequence
import logging
import time

log = logging.getLogger(__name__)

Rect = list[int]

# Окно истории центров для оценки скорости ТС.
_MOTION_WINDOW_SEC = 2.0


def _center(box: Sequence[float]) -> tuple[float, float]:
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def _diagonal(box: Sequence[float]) -> float:
    return ((box[2] - box[0]) ** 2 + (box[3] - box[1]) ** 2) ** 0.5


def _iou(box_a: Sequence[float], box_b: Sequence[float]) -> float:
    """IoU двух прямоугольников [x1, y1, x2, y2]."""
    ax1, ay1, ax2, ay2 = box_a[0], box_a[1], box_a[2], box_a[3]
    bx1, by1, bx2, by2 = box_b[0], box_b[1], box_b[2], box_b[3]
    iw = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    ih = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def relative_position(plate_box: Sequence[float], body_box: Sequence[float]) -> tuple[float, float]:
    """Положение номера относительно кузова, нормированное на его размеры.

    Абсолютные координаты бесполезны: машина едет, и один и тот же физический
    номер даёт разные координаты каждый кадр. Относительное положение внутри
    кузова стабильно, поэтому номера разных бортов разделяются, а дрожание
    одного номера остаётся в одном кластере.
    """
    pcx, pcy = _center(plate_box)
    bcx, bcy = _center(body_box)
    bw = max(1.0, body_box[2] - body_box[0])
    bh = max(1.0, body_box[3] - body_box[1])
    return (pcx - bcx) / bw, (pcy - bcy) / bh


class PlateCluster:
    """Голоса за один физический номер внутри одного эпизода."""

    def __init__(self, rel: tuple[float, float]):
        self.rel = rel
        self.votes: Counter = Counter()
        self.confidence: dict[str, list[float]] = defaultdict(list)
        self.best_area = 0.0
        self.best_frame = None

    def add(self, text: str, confidence: float, area: float, frame) -> None:
        self.votes[text] += 1
        self.confidence[text].append(confidence)
        if area >= self.best_area:
            self.best_area = area
            self.best_frame = frame

    @property
    def total_votes(self) -> int:
        return sum(self.votes.values())

    def resolve(self) -> tuple[str, float, float] | None:
        """Итоговый номер + уверенность + согласие голосов.

        Возвращает (text, confidence, agreement): согласие нужно вызывающему
        коду, чтобы не принять кашу из двух разных номеров за один.
        """
        if not self.votes:
            return None
        length = Counter(len(t) for t in self.votes).most_common(1)[0][0]
        chars: list[str] = []
        for index in range(length):
            column = Counter(t[index] for t in self.votes if len(t) > index)
            if not column:
                return None
            chars.append(column.most_common(1)[0][0])
        text = "".join(chars)
        agree = self.votes.most_common(1)[0][1]
        confidence = 0.0
        for values in self.confidence.values():
            for v in values:
                confidence = max(confidence, v)
        return text, float(confidence), agree / max(1, self.total_votes)


class VehicleEpisode:
    """Один проезд одной машины: трек ultralytics + набранные номера."""

    def __init__(self, track_id: int, bbox: Rect, now: float):
        self.track_id = track_id
        self.bbox = bbox
        self.first_seen = now
        self.last_seen = now
        self.best_vehicle_area = 0.0
        self.best_vehicle_frame = None
        self.best_vehicle_bbox: Rect | None = None
        self.best_vehicle_name: str | None = None
        self.best_vehicle_conf = 0.0
        # Лучший кадр, где ТС видно ЦЕЛИКОМ (не на краю кадра) — для отправки
        # в момент обнаружения. Отдельно от best_vehicle_*: когда машина стоит
        # у камеры, крупнейший bbox может быть обрезан краем, а нужен полный вид.
        self.best_full_area = 0.0
        self.best_full_frame = None
        self.best_full_bbox: Rect | None = None
        self.best_full_name: str | None = None
        self.best_full_conf = 0.0
        # Монотонное время, с которого ТС держится ЦЕЛИКОМ в кадре без перерыва
        # (None — не целиком или только вошло). Триггер ранней отправки.
        self.full_in_frame_since: float | None = None
        self.last_name: str | None = None
        self.last_conf = 0.0
        self.clusters: list[PlateCluster] = []
        self.notified = False
        # Событие реально ушло в backend (а не подавлено как дубль сцепа).
        # Только такая машина двигает запись о «последней отправленной»: иначе
        # подавленный прицеп утащил бы её за собой и цепочкой глушил следующих.
        self.sent = False
        self.readable_since: float | None = None
        self.resolved_plate: str | None = None
        self.unread_alarmed = False
        self.born_in_entry = False
        # История центров за последние ~2 с: нужна, чтобы отличить сцеп
        # (тягач и прицеп едут с одной скоростью) от чужой машины, которая
        # проезжает мимо стоящей. Скорость по крайним точкам устойчивее, чем
        # по последнему шагу: детектор дрожит на несколько пикселей.
        self._motion: list[tuple[float, float, float]] = []

    def update(self, bbox: Rect, now: float) -> None:
        self.bbox = bbox
        self.last_seen = now
        cx, cy = _center(bbox)
        self._motion.append((now, cx, cy))
        while len(self._motion) > 2 and now - self._motion[0][0] > _MOTION_WINDOW_SEC:
            self._motion.pop(0)

    def speed(self) -> float:
        """Скорость ТС в px/с по истории центров. 0 — стоит или мало данных."""
        if len(self._motion) < 2:
            return 0.0
        t0, x0, y0 = self._motion[0]
        t1, x1, y1 = self._motion[-1]
        dt = t1 - t0
        if dt < 0.15:
            return 0.0
        return ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5 / dt

    def add_plate(self, plate_box: Sequence[float], text: str, confidence: float,
                  frame, cluster_tolerance: float) -> PlateCluster | None:
        """Относит чтение к существующему кластеру номера или создаёт новый."""
        rel = relative_position(plate_box, self.bbox)
        if self.clusters:
            nearest = min(self.clusters,
                          key=lambda c: (c.rel[0] - rel[0]) ** 2 + (c.rel[1] - rel[1]) ** 2)
            distance = ((nearest.rel[0] - rel[0]) ** 2 + (nearest.rel[1] - rel[1]) ** 2) ** 0.5
            if distance <= cluster_tolerance:
                area = (plate_box[2] - plate_box[0]) * (plate_box[3] - plate_box[1])
                nearest.add(text, confidence, area, frame)
                return nearest
        cluster = PlateCluster(rel)
        area = (plate_box[2] - plate_box[0]) * (plate_box[3] - plate_box[1])
        cluster.add(text, confidence, area, frame)
        self.clusters.append(cluster)
        return cluster

    def note_vehicle_frame(self, frame, shape, now: float | None = None) -> None:
        """Запоминает лучшие кадры ТС — для отправки с боксом.

        Две цели одновременно:
        1. best_vehicle_* — кадр с самым КРУПНЫМ кузовом (в любом положении).
        2. best_full_*    — кадр с самым крупным кузовом, видимым ЦЕЛИКОМ
           (не прижатым к краю кадра). Именно его просил отправлять пользователь:
           «кадр техники в кадре целиком в момент её обнаружения».
        while full_in_frame_since: с какого момента держится целиком (триггер
        ранней отправки, чтобы не ждать уезда машины).
        """
        x1, y1, x2, y2 = self.bbox
        area = (x2 - x1) * (y2 - y1)
        if area >= self.best_vehicle_area:
            self.best_vehicle_area = area
            self.best_vehicle_frame = frame
            self.best_vehicle_bbox = list(self.bbox)
            self.best_vehicle_name = self.last_name
            self.best_vehicle_conf = self.last_conf

        h, w = shape[:2]
        # Не прижат ли к краю кадра: бокс с 0 на границе = машина обрезана краем,
        # не «целиком». Отступ 2px — чтобы дрожание детектора не дёргало флаг.
        margin = 2
        in_frame = (x1 > margin and y1 > margin
                    and x2 < w - margin and y2 < h - margin)
        if in_frame:
            if area >= self.best_full_area:
                self.best_full_area = area
                self.best_full_frame = frame
                self.best_full_bbox = list(self.bbox)
                self.best_full_name = self.last_name
                self.best_full_conf = self.last_conf
            if self.full_in_frame_since is None and now is not None:
                self.full_in_frame_since = now
        else:
            self.full_in_frame_since = None

    def mark_readable(self, now: float, area_frac_min: float, shape) -> None:
        if self.readable_since is not None:
            return
        h, w = shape[:2]
        area_frac = ((self.bbox[2] - self.bbox[0])
                     * (self.bbox[3] - self.bbox[1]) / (w * h))
        if area_frac >= area_frac_min:
            self.readable_since = now

    def best_frame(self, fallback):
        """Кадр для отправки: приоритет — ТС целиком в кадре, иначе самый крупный.

        Фрейм отдаётся наружу (к backend уходит JPEG), поэтому копия нужна только
        если кадр ещё используется буфером: reference достаточно, пока буфер жив.
        """
        if self.best_full_frame is not None:
            return self.best_full_frame
        return self.best_vehicle_frame if self.best_vehicle_frame is not None else fallback

    def best_cluster(self) -> PlateCluster | None:
        """Кластер с самым читаемым номером: больше всего согласованных голосов."""
        if not self.clusters:
            return None
        return max(self.clusters, key=lambda c: c.total_votes)


class EpisodeTracker:
    """Реестр активных проездов. Одна машина — один эпизод — одно событие."""

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.episodes: dict[int, VehicleEpisode] = {}
        # Запас по времени закрытия эпизода должен быть >= времени, в течение
        # которого ByteTrack держит ID в буфере (track_buffer кадров, а трекер
        # кормится только новыми кадрами камеры ~4.5 fps; 180 кадров = ~40с).
        # Иначе эпизод закроется раньше, чем трекер вернёт тот же track_id,
        # и проезд разорвётся на два события.
        self._plate_min_votes = cfg.get("plate_min_votes", 5)
        self._exit_grace_sec = cfg.get("track_exit_grace_sec", 18.0)
        self._cluster_tolerance = cfg.get("plate_cluster_tolerance", 0.25)
        self._attach_margin = cfg.get("plate_attach_margin_frac", 0.5)
        # Переприсвоение трека: сколько секунд эпизод, пропустившийся из выдачи,
        # ещё может «забрать» новый track_id, и насколько близко должны быть боксы.
        # Дефолт 20 выше старого 10: на низком FPS детектор теряет машину дольше,
        # а окно меньше длины обрыва = дубль письма (одна машина -> два эпизода).
        # Прод-конфиг под весовую выставляет 30 (см. config.prod.example.yaml).
        self._reassoc_sec = cfg.get("track_reassoc_sec", 20.0)
        self._reassoc_dist_frac = cfg.get("track_reassoc_dist_frac", 0.6)
        self.reassoc_count = 0
        # Дедуп VEHICLE_SEEN по сцене, не зависящий от жизни трека.
        # ByteTrack может потерять стоящую у края машину, эпизод закроется и
        # удалится, а при следующей детекции родится новый эпизод — и второе
        # письмо по той же машине. Поэтому после отправки запоминаем машину
        # (позиция + размер) и подавляем повторные события по ней, пока запись
        # «свежая» и новое детектирование рядом. Запись обновляется на каждой
        # детекции рядом, то есть следует за машиной, пока её видно: стоящая
        # машина не даёт повторов, а уехавшая перестаёт обновляться и забывается.
        self._seen_dedup_sec = float(cfg.get("vehicle_seen_dedup_sec", 600.0))
        self._seen_dedup_iou = float(cfg.get("vehicle_seen_dedup_iou", 0.3))
        self._seen_dedup_dist_frac = float(cfg.get("vehicle_seen_dedup_dist_frac", 0.8))
        # Сосед при отправке: сцеп (тягач + прицеп) даёт два живых эпизода —
        # YOLO не знает класса «прицеп» и видит его обычным грузовиком. Второй
        # эпизод рождается раньше, чем первый отправлен, поэтому проверка при
        # рождении его не ловит; ловит только проверка в момент отправки.
        self._neighbor_sec = float(cfg.get("vehicle_seen_neighbor_sec", 20.0))
        self._neighbor_dist_frac = float(
            cfg.get("vehicle_seen_neighbor_dist_frac", 0.6))
        # Хвост памяти о машине, выехавшей за край кадра. Пока машину видно,
        # запись продлевается; но после выезда держать её 10 минут нельзя —
        # следующая машина в этой же точке получит «уже отправлено».
        self._edge_ttl_sec = float(cfg.get("vehicle_seen_edge_ttl_sec", 30.0))
        # Отличие сцепа от чужой машины рядом — по скорости.
        # Тягач и прицеп едут с одной скоростью; трактор, проезжающий мимо
        # стоящей на весах фуры, — с совсем другой. Совпадение по близости
        # (или по «машина уже была в этом месте») глушило такие проезды.
        self._visible_sec = float(cfg.get("vehicle_seen_visible_sec", 2.0))
        self._still_speed = float(cfg.get("vehicle_seen_still_speed", 40.0))
        self._speed_tolerance = float(cfg.get("vehicle_seen_speed_tolerance", 40.0))
        self._notified_vehicles: list[dict] = []
        log.info("эпизод: exit_grace=%.0fs reassoc=%.0fs dist_frac=%.2f min_votes=%d",
                 self._exit_grace_sec, self._reassoc_sec, self._reassoc_dist_frac,
                 self._plate_min_votes)
        log.info("эпизод: seen_dedup=%.0fs iou=%.2f dist_frac=%.2f",
                 self._seen_dedup_sec, self._seen_dedup_iou, self._seen_dedup_dist_frac)
        log.info("эпизод: сосед=%.0fs dist_frac=%.2f, хвост у края=%.0fs",
                 self._neighbor_sec, self._neighbor_dist_frac, self._edge_ttl_sec)
        log.info("эпизод: видимость=%.0fs стоймя<=%.0fpx/s допуск скорости=%.0fpx/s",
                 self._visible_sec, self._still_speed, self._speed_tolerance)

    def observe(self, objects: list[dict], now: float) -> list[int]:
        """Регистрирует детекции с трекерными ID. Возвращает эпизоды, ушедшие из кадра.

        Пропавший из выдачи трекер не означает, что машина уехала: ByteTrack держит
        ID в буфере (track_buffer кадров), и машина может вернуться. Поэтому эпизод
        закрывается только когда ID не появлялся дольше трек_exit_grace_sec.

        У ворот трек рвётся чаще всего: штанга/струна перекрывают кузов, соседние ТС
        сливаются, детектор теряет машину у края кадра. ByteTrack после такого выдаёт
        НОВЫЙ track_id, и без переприсвоения одна машина получала два эпизода —
        второе событие и второе письмо. Поэтому новый track_id рядом с недавно
        пропавшим эпизодом наследует его состояние: собранные голоса номера, лучшие
        кадры и флаг notified. Наследованный notified — и есть защита от дубля.
        """
        seen = set()
        self._purge_notified(now)
        for obj in objects:
            track_id = obj.get("track_id")
            if track_id is None:
                continue
            seen.add(track_id)
            bbox = obj["bbox"]
            name = obj.get("name")
            confidence = float(obj.get("confidence") or 0.0)
            episode = self.episodes.get(track_id)
            if episode is None:
                inherited = self._inherit(bbox, now, seen)
                if inherited is not None:
                    self.reassoc_count += 1
                    log.info("трек #%d переприсвоен эпизоду #%d — машина та же, id сменился",
                             track_id, inherited.track_id)
                    inherited.track_id = track_id
                    episode = inherited
                else:
                    episode = VehicleEpisode(track_id, bbox, now)
                    # Трек родился заново (старый эпизод уже закрыт), но это может
                    # быть та же СТОЯЩАЯ машина. Если по этой позиции уже слали
                    # VEHICLE_SEEN — помечаем эпизод отправленным, письма не будет.
                    # Только для стоящей и невидимой сейчас машины: иначе мимо
                    # стоящей фуры проезжает трактор и его событие глушится.
                    if self._match_notified(bbox, now, birth=True) is not None:
                        episode.notified = True
                        log.info("трек #%d: машина уже была в этом месте — VEHICLE_SEEN подавлен",
                                 track_id)
                self.episodes[track_id] = episode
            episode.update(bbox, now)
            episode.last_name = name
            episode.last_conf = confidence
            if episode.sent:
                # Запись следует за уже отправленной машиной: пока её видно,
                # позиция обновляется и запись не «протухает».
                self._refresh_notified(bbox, now, episode.speed())
        gone = [tid for tid, ep in self.episodes.items()
                if tid not in seen and now - ep.last_seen > self._exit_grace_sec]
        return gone

    def _purge_notified(self, now: float) -> None:
        """Убирает записи о машинах, которых давно не видно."""
        self._notified_vehicles = [entry for entry in self._notified_vehicles
                                   if not self._entry_expired(entry, now)]

    def _entry_expired(self, entry: dict, now: float) -> bool:
        """Запись протухла: у выехавшей за край машины срок короче.

        Пока машину видно, `_refresh_notified` продлевает запись; срок начинает
        идти только с последней детекции. Поэтому «машина уехала» и «машина
        стоит, но детектор её потерял» различаются: у выехавшей за край
        `note_exit` ставит короткий ttl, у пропавшей в середине сцены остаётся
        длинный vehicle_seen_dedup_sec.
        """
        return now - entry["ts"] > entry.get("ttl", self._seen_dedup_sec)

    def notified_neighbor(self, bbox: Rect, now: float,
                          speed: float | None = None) -> dict | None:
        """Свежая запись об уже отправленной машине ВПЛОТНУЮ к этой.

        Проверяется в момент отправки, а не при рождении эпизода: прицеп
        (YOLO видит его как обычный грузовик) получает свой эпизод раньше, чем
        тягач отправлен, и проверка при рождении его пропускает — отсюда два
        письма по одному сцепу с интервалом в секунды.

        Скорости обязаны совпадать: сцеп едет как одно целое, а трактор,
        проезжающий мимо стоящей на весах фуры, движется совсем иначе. Одна
        близость боксов глушила такие проезды — событие по трактору не уходило.

        Свежесть тоже обязательна: рядом должна быть машина, которую видно
        прямо сейчас. Запись десятиминутной давности — это другая машина,
        которая просто остановилась там же.
        """
        cx, cy = _center(bbox)
        for entry in self._notified_vehicles:
            if now - entry["ts"] > self._neighbor_sec:
                continue
            if not self._speed_compatible(entry, speed):
                continue
            if _iou(bbox, entry["bbox"]) >= self._seen_dedup_iou:
                return entry
            ex, ey = _center(entry["bbox"])
            distance = ((cx - ex) ** 2 + (cy - ey) ** 2) ** 0.5
            scale = max(_diagonal(entry["bbox"]), _diagonal(bbox), 1.0)
            if distance <= self._neighbor_dist_frac * scale:
                return entry
        return None

    def _speed_compatible(self, entry: dict, speed: float | None) -> bool:
        """Одна машина или две: сравниваем скорости.

        None — скорость неизвестна (мало кадров), тогда не мешаем совпадению
        по геометрии: лучше пропустить лишнее письмо, чем потерять событие.
        """
        if speed is None:
            return True
        entry_speed = float(entry.get("speed", 0.0))
        limit = max(self._speed_tolerance, 0.5 * max(entry_speed, speed))
        return abs(entry_speed - speed) <= limit

    def note_exit(self, episode: VehicleEpisode, shape,
                  now: float | None = None) -> None:
        """Эпизод закрылся: сократить память, если машина выехала за край кадра.

        Выехавшая за край машина действительно уехала — держать запись
        vehicle_seen_dedup_sec нельзя, иначе следующая машина, вставшая на том
        же месте, будет помечена «уже отправлена» и письма не получит.
        Пропавшая в середине сцены может стоять за штангой или кустом — её
        помним долго, иначе повторная детекция даст второе письмо.
        """
        now = time.monotonic() if now is None else now
        entry = self._match_notified(episode.bbox, now)
        if entry is None:
            return
        h, w = shape[:2]
        margin_x = 0.08 * w
        margin_y = 0.08 * h
        x1, y1, x2, y2 = episode.bbox
        at_edge = (x1 <= margin_x or x2 >= w - margin_x
                   or y1 <= margin_y or y2 >= h - margin_y)
        if not at_edge:
            return
        entry["ttl"] = self._edge_ttl_sec
        log.info("трек #%d: машина уехала за край — память о ней %.0f с",
                 episode.track_id, self._edge_ttl_sec)

    def _match_notified(self, bbox: Rect, now: float, birth: bool = False,
                        speed: float | None = None) -> dict | None:
        """Запись об уже отправленной машине в этой же точке (не истёкшая).

        Совпадение — по IoU (машина почти на месте) или по близости центров
        (машина сдвинулась/подъехала). Порог близости в долях диагонали bbox,
        чтобы он не зависел от масштаба машины в кадре.

        birth=True — вызов на рождении трека (старый эпизод уже закрыт). Тут
        одной геометрии мало: мимо стоящей на весах фуры проезжает трактор,
        боксы накладываются, и событие по трактору глушилось. Поэтому при
        рождении считаем «той же машиной» только если:
        1. запись давно не обновлялась — значит, машины, по которой она
           сделана, СЕЙЧАС не видно (проверка двигается каждым кадром, пока
           машина в кадре);
        2. та машина стояла (скорость ~0): едущая машина за секунды уходит,
           новый трек на её месте — это уже другое ТС.
        """
        best: dict | None = None
        best_score = -1.0
        cx, cy = _center(bbox)
        for entry in self._notified_vehicles:
            if self._entry_expired(entry, now):
                continue
            if birth:
                if now - entry["ts"] <= self._visible_sec:
                    continue
                if float(entry.get("speed", 0.0)) > self._still_speed:
                    continue
            elif not self._speed_compatible(entry, speed):
                continue
            iou = _iou(bbox, entry["bbox"])
            ex, ey = _center(entry["bbox"])
            distance = ((cx - ex) ** 2 + (cy - ey) ** 2) ** 0.5
            scale = max(_diagonal(entry["bbox"]), _diagonal(bbox), 1.0)
            close = distance <= self._seen_dedup_dist_frac * scale
            if iou < self._seen_dedup_iou and not close:
                continue
            score = max(iou, 1.0 - distance / scale)
            if score > best_score:
                best, best_score = entry, score
        return best

    def _refresh_notified(self, bbox: Rect, now: float,
                          speed: float | None = None) -> bool:
        """Продлевает запись, если детекция рядом с уже отправленной машиной."""
        entry = self._match_notified(bbox, now)
        if entry is None:
            return False
        entry["bbox"] = list(bbox)
        entry["ts"] = now
        if speed is not None:
            entry["speed"] = speed
        # Машину видно — память о ней снова длинная (короткий ttl ставит
        # note_exit, когда она выехала за край и больше не продлевается).
        entry["ttl"] = self._seen_dedup_sec
        return True

    def mark_notified(self, episode: VehicleEpisode, now: float | None = None) -> None:
        """Регистрирует машину как отправленную, чтобы не слать письмо повторно."""
        now = time.monotonic() if now is None else now
        bbox = list(episode.bbox)
        speed = episode.speed()
        entry = self._match_notified(bbox, now)
        if entry is None:
            self._notified_vehicles.append({"bbox": bbox, "ts": now, "speed": speed})
        else:
            entry["bbox"] = bbox
            entry["ts"] = now
            entry["speed"] = speed
            entry["ttl"] = self._seen_dedup_sec

    def _inherit(self, bbox: Rect, now: float, seen: set) -> VehicleEpisode | None:
        """Ближайший недавно пропавший эпизод, если это физически та же машина.

        Три условия, все обязательны:
        1. эпизода нет в `seen` — иначе это другая живая ТС в этом же кадре;
        2. он пропал не дольше track_reassoc_sec назад;
        3. центры боксов близки.

        Порог близости задаётся в долях диагонали бокса, а не в пикселях: у ворот
        ТС в кадре крупное, на развороте мелкое, и пиксельный порог срабатывал бы
        то на одном участке, то на другом.
        """
        cx, cy = _center(bbox)
        best: VehicleEpisode | None = None
        best_distance = float("inf")
        for episode in self.episodes.values():
            if episode.track_id in seen:
                continue
            if now - episode.last_seen > self._reassoc_sec:
                continue
            ex, ey = _center(episode.bbox)
            distance = ((cx - ex) ** 2 + (cy - ey) ** 2) ** 0.5
            scale = max(_diagonal(episode.bbox), _diagonal(bbox), 1.0)
            if distance > self._reassoc_dist_frac * scale:
                continue
            if distance < best_distance:
                best, best_distance = episode, distance
        if best is not None:
            self.episodes.pop(best.track_id, None)
        return best

    def attach_plate(self, plate_box: Rect, text: str, confidence: float,
                     frame) -> VehicleEpisode | None:
        """Привязывает чтение номера к эпизоду машины, которой он принадлежит.

        Номер не создаёт эпизод: без кузова нечего трекать, иначе каждое чтение
        роняет по машине — именно это давало 2-6 событий на один грузовик.
        Номера, не попавшие ни в один кузов (машина в кадре не видна целиком),
        игнорируются: их всё равно не с чем связать.
        """
        px, py = _center(plate_box)
        best: VehicleEpisode | None = None
        best_dist = float("inf")
        for episode in self.episodes.values():
            if episode.notified:
                continue
            x1, y1, x2, y2 = episode.bbox
            if x1 <= px <= x2 and y1 <= py <= y2:
                episode.add_plate(plate_box, text, confidence, frame, self._cluster_tolerance)
                return episode
            margin_x = (x2 - x1) * self._attach_margin
            margin_y = (y2 - y1) * self._attach_margin
            if (x1 - margin_x) <= px <= (x2 + margin_x) and (y1 - margin_y) <= py <= (y2 + margin_y):
                # Ближайший к центру номеру: у соседних машин зоны привязки
                # пересекаются, и "первый подходящий" отдавал бы номер соседу.
                dist = (px - x1) ** 2 + (py - y1) ** 2
                if dist < best_dist:
                    best, best_dist = episode, dist
        if best is None:
            return None
        best.add_plate(plate_box, text, confidence, frame, self._cluster_tolerance)
        return best

    def pending_plate(self, episode: VehicleEpisode) -> tuple[str, float] | None:
        """Номер эпизода, когда голосов уже достаточно. None — ещё рано."""
        if episode.notified:
            return None
        cluster = episode.best_cluster()
        if cluster is None or cluster.total_votes < self._plate_min_votes:
            return None
        resolved = cluster.resolve()
        if resolved is None:
            return None
        text, confidence, agreement = resolved
        # Согласие важнее числа голосов: 5 одинаковых и 2 спорных — это уверенный
        # номер, а 3 разных по 2 голоса — не номер, а каша из бортов.
        if agreement < 0.5:
            return None
        return text, confidence

    def close(self, track_id: int) -> VehicleEpisode | None:
        return self.episodes.pop(track_id, None)

    def open_episodes(self) -> list[VehicleEpisode]:
        return list(self.episodes.values())