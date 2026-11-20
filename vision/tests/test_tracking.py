import numpy as np

from tracking import EpisodeTracker


def _tracker(**overrides):
    cfg = {"plate_min_votes": 3, "track_exit_grace_sec": 1.0,
           "plate_cluster_tolerance": 0.25, "plate_attach_margin_frac": 0.5}
    cfg.update(overrides)
    return EpisodeTracker(cfg)


def _car(track_id, x1=10, y1=10, x2=60, y2=40):
    return {"track_id": track_id, "bbox": [x1, y1, x2, y2]}


def _feed(tracker, track_id, plate_box, text, frames=6, step=0.1, dx=3):
    """Прогоняет эпизод: кузов и номер едут вместе кадр за кадром."""
    for i in range(frames):
        now = i * step
        shift = i * dx
        tracker.observe([_car(track_id, x1=10 + shift, x2=60 + shift)], now)
        tracker.attach_plate([plate_box[0] + shift, plate_box[1],
                              plate_box[2] + shift, plate_box[3]], text, 0.9, None)
    return tracker.episodes[track_id]


def test_small_shift_keeps_same_episode_and_single_plate():
    """Сдвиг кузова на десяток пикселей не рвёт эпизод и не даёт второго номера."""
    tracker = _tracker()
    episode = _feed(tracker, 7, [48, 20, 63, 30], "A111AA")

    assert list(tracker.episodes) == [7]
    pending = tracker.pending_plate(episode)
    assert pending is not None
    assert pending[0] == "A111AA"
    assert len(episode.clusters) == 1


def test_two_cars_do_not_swap_ids_or_merge_plates():
    """Две машины рядом: каждый эпизод получает свой номер, чтения не смешиваются."""
    tracker = _tracker()
    for i in range(6):
        now = i * 0.1
        shift = i * 3
        tracker.observe([_car(1, x1=10 + shift, x2=60 + shift),
                         _car(2, x1=200 + shift, x2=250 + shift)], now)
        tracker.attach_plate([48 + shift, 20, 63 + shift, 30], "A111AA", 0.9, None)
        tracker.attach_plate([238 + shift, 20, 253 + shift, 30], "B222BB", 0.9, None)

    assert sorted(tracker.episodes) == [1, 2]
    plate1 = tracker.pending_plate(tracker.episodes[1])
    plate2 = tracker.pending_plate(tracker.episodes[2])
    assert plate1 is not None and plate1[0] == "A111AA"
    assert plate2 is not None and plate2[0] == "B222BB"


def test_three_plates_on_one_truck_are_separate_clusters():
    """У грузовика три физических номера — три кластера, не один счётчик."""
    tracker = _tracker()
    truck = {"x1": 100, "y1": 100, "x2": 700, "y2": 500}
    for i in range(6):
        tracker.observe([_car(9, **truck)], i * 0.1)
        tracker.attach_plate([120, 120, 200, 150], "A111AA", 0.9, None)
        tracker.attach_plate([500, 120, 580, 150], "B222BB", 0.9, None)
        tracker.attach_plate([380, 430, 460, 460], "C333CC", 0.9, None)

    episode = tracker.episodes[9]
    assert len(episode.clusters) == 3
    # событие всё равно одно: лучший кластер отдаёт один номер
    best = tracker.pending_plate(episode)
    assert best is not None and best[0] == "A111AA"


def test_no_duplicate_after_notified():
    """После отправки номера эпизод больше не выдаёт событие."""
    tracker = _tracker()
    for i in range(6):
        now = i * 0.1
        tracker.observe([_car(3)], now)
        tracker.attach_plate([48, 20, 63, 30], "A111AA", 0.9, None)

    episode = tracker.episodes[3]
    assert tracker.pending_plate(episode) is not None
    episode.notified = True
    assert tracker.pending_plate(episode) is None


def test_episode_survives_gap_shorter_than_grace():
    """Краткое перекрытие не закрывает эпизод: тот же track_id продолжит проезд."""
    tracker = _tracker()
    for i in range(3):
        tracker.observe([_car(4)], i * 0.1)
        tracker.attach_plate([48, 20, 63, 30], "A111AA", 0.9, None)
    # пропадает на 0.5с (< grace 1.0с)
    assert tracker.observe([], 0.5) == []
    assert 4 in tracker.episodes
    tracker.observe([_car(4)], 0.6)
    pending = tracker.pending_plate(tracker.episodes[4])
    assert pending is not None and pending[0] == "A111AA"


def test_episode_closes_after_grace():
    """Уехал надолго — эпизод возвращается как «gone» и его можно закрыть."""
    tracker = _tracker()
    tracker.observe([_car(5)], 0.0)
    assert tracker.observe([], 2.0) == [5]
    assert tracker.close(5) is not None
    assert tracker.episodes == {}


def test_no_plate_never_blocks_unread_timer():
    """Без номеров readable_since ставится по площади — таймер тревоги может сработать."""
    tracker = _tracker()
    tracker.observe([_car(6, x1=100, y1=100, x2=600, y2=500)], 0.0)
    episode = tracker.episodes[6]
    episode.mark_readable(0.0, 0.02, (720, 1280, 3))
    assert episode.readable_since is not None
    assert tracker.pending_plate(episode) is None


def test_plate_outside_any_body_is_ignored():
    """Номер без кузова не создаёт эпизод — иначе каждое чтение роняло машину."""
    tracker = _tracker()
    tracker.observe([_car(8, x1=10, y1=10, x2=60, y2=40)], 0.0)
    assert tracker.attach_plate([600, 500, 700, 560], "A111AA", 0.9, None) is None
    assert list(tracker.episodes) == [8]


def test_garbage_readings_never_reach_min_votes():
    """Три разных номера по одному голосу — это каша, а не результат."""
    tracker = _tracker()
    for i, text in enumerate(["A111AA", "B222BB", "C333CC"]):
        tracker.observe([_car(11)], i * 0.1)
        tracker.attach_plate([48, 20, 63, 30], text, 0.6, None)

    assert tracker.pending_plate(tracker.episodes[11]) is None


def test_best_frame_prefers_vehicle_frame():
    tracker = _tracker()
    tracker.observe([_car(12)], 0.0)
    episode = tracker.episodes[12]
    assert episode.best_frame("fallback") == "fallback"
    episode.best_vehicle_frame = np.zeros((4, 4, 3), dtype=np.uint8)
    assert episode.best_frame("fallback") is not None


def test_new_track_id_near_old_episode_does_not_duplicate_event():
    """У ворот ByteTrack сменил ID — та же машина не должна дать второе событие."""
    tracker = _tracker()
    tracker.observe([_car(1)], 0.0)
    episode = tracker.episodes[1]
    episode.notified = True
    # трек пропал на 0.3с и вернулся уже под другим ID, рядом по координатам
    tracker.observe([_car(2, x1=14, y1=11, x2=64, y2=41)], 0.3)

    assert list(tracker.episodes) == [2]
    inherited = tracker.episodes[2]
    assert inherited is episode
    assert inherited.notified is True
    assert tracker.pending_plate(inherited) is None


def test_inherited_episode_keeps_collected_votes():
    """Проезд не теряется: собранные до разрыва голоса номера переживают смену ID."""
    tracker = _tracker(plate_min_votes=5)
    for i in range(4):
        tracker.observe([_car(1, x1=10 + i * 3, x2=60 + i * 3)], i * 0.1)
        tracker.attach_plate([48 + i * 3, 20, 63 + i * 3, 30], "A111AA", 0.9, None)
    assert tracker.pending_plate(tracker.episodes[1]) is None  # 4 голоса из 5

    tracker.observe([_car(5, x1=24, x2=74)], 0.4)  # разрыв трека, новый ID
    episode = tracker.episodes[5]
    assert tracker.pending_plate(episode) is None
    tracker.attach_plate([62, 20, 77, 30], "A111AA", 0.9, None)

    pending = tracker.pending_plate(episode)
    assert pending is not None and pending[0] == "A111AA"


def test_distant_vehicle_does_not_inherit_episode():
    """Вторая машина вдалеке — отдельный эпизод, наследования быть не должно."""
    tracker = _tracker()
    tracker.observe([_car(1, x1=10, y1=10, x2=60, y2=40)], 0.0)
    episode = tracker.episodes[1]
    episode.notified = True

    tracker.observe([_car(2, x1=600, y1=500, x2=900, y2=700)], 0.3)

    assert sorted(tracker.episodes) == [1, 2]
    assert tracker.episodes[2] is not episode
    assert tracker.episodes[2].notified is False
    assert tracker.reassoc_count == 0


def test_live_episode_is_never_stolen_by_new_track():
    """Живой в этом кадре эпизод не отдаётся другому ID — это другая ТС рядом."""
    tracker = _tracker()
    tracker.observe([_car(1), _car(2, x1=14, y1=10, x2=64, y2=40)], 0.0)
    first = tracker.episodes[1]
    second = tracker.episodes[2]

    assert second is not first
    assert tracker.episodes[1] is first


def test_reassoc_window_expires():
    """Эпизод пропавший слишком давно новым ID не наследуется."""
    tracker = _tracker(track_reassoc_sec=2.0, track_exit_grace_sec=60.0)
    tracker.observe([_car(1)], 0.0)
    first = tracker.episodes[1]
    first.notified = True

    tracker.observe([_car(2, x1=12, y1=10, x2=62, y2=40)], 5.0)

    assert sorted(tracker.episodes) == [1, 2]
    assert tracker.episodes[2].notified is False
    assert first.notified is True


def test_inherited_episode_is_not_immediately_gone():
    """Наследованный эпизод считается только что виденным, даже если окно шире grace."""
    tracker = _tracker(track_reassoc_sec=30.0, track_exit_grace_sec=1.0)
    tracker.observe([_car(1)], 0.0)
    gone = tracker.observe([_car(2, x1=12, y1=10, x2=62, y2=40)], 5.0)

    assert gone == []
    assert 2 in tracker.episodes


def test_same_parked_vehicle_after_track_restart_is_suppressed():
    """Стоящая машина: трек оборвался, эпизод закрыт — новое событие не шлём."""
    tracker = _tracker(vehicle_seen_dedup_sec=600)
    tracker.observe([_car(1)], 0.0)
    tracker.mark_notified(tracker.episodes[1], 0.0)

    assert tracker.observe([], 2.0) == [1]
    tracker.close(1)

    # та же машина, но ByteTrack выдал новый ID на том же месте
    tracker.observe([_car(2)], 2.5)
    assert tracker.episodes[2].notified is True


def test_notified_vehicle_memory_expires():
    """Машина уехала и окно памяти истекло — новую в этой точке снова шлём."""
    tracker = _tracker(vehicle_seen_dedup_sec=5.0)
    tracker.observe([_car(1)], 0.0)
    tracker.mark_notified(tracker.episodes[1], 0.0)
    tracker.observe([], 2.0)
    tracker.close(1)

    tracker.observe([_car(2)], 10.0)
    assert tracker.episodes[2].notified is False


def test_notified_vehicle_elsewhere_does_not_suppress():
    """Память о машине в одной точке не подавляет другую вдалеке."""
    tracker = _tracker(vehicle_seen_dedup_sec=600)
    tracker.observe([_car(1, x1=10, y1=10, x2=60, y2=40)], 0.0)
    tracker.mark_notified(tracker.episodes[1], 0.0)

    tracker.observe([_car(2, x1=600, y1=500, x2=900, y2=700)], 0.5)
    assert tracker.episodes[2].notified is False


def test_notified_vehicle_memory_follows_movement():
    """Запись следует за машиной: сдвинулась и снова «нашлась» — не шлём."""
    tracker = _tracker(vehicle_seen_dedup_sec=600)
    tracker.observe([_car(1, x1=10, y1=10, x2=60, y2=40)], 0.0)
    tracker.mark_notified(tracker.episodes[1], 0.0)

    # машина проехала вперёд, запись должна обновиться на новую позицию
    tracker.observe([_car(1, x1=40, y1=20, x2=120, y2=70)], 0.5)
    tracker.observe([], 2.0)
    tracker.close(1)

    tracker.observe([_car(2, x1=45, y1=22, x2=125, y2=72)], 2.5)
    assert tracker.episodes[2].notified is True


def test_trailer_next_to_notified_truck_is_a_neighbor():
    """Сцеп: прицеп рядом с только что отправленным тягачом — второе письмо не шлём.

    Координаты из реального кейса 11.11.2026 (кадр 1920x1080): тягач
    (162,408)-(567,816), прицеп (408,384)-(1058,755).
    """
    tracker = _tracker(vehicle_seen_neighbor_sec=20)
    tracker.observe([_car(1, 162, 408, 567, 816)], 0.0)
    tracker.mark_notified(tracker.episodes[1], 0.0)

    assert tracker.notified_neighbor([408, 384, 1058, 755], 7.0) is not None


def test_vehicle_far_from_notified_one_is_not_a_neighbor():
    """Вторая машина вдалеке — обычное событие, сосед её не глушит."""
    tracker = _tracker(vehicle_seen_neighbor_sec=20)
    tracker.observe([_car(1, 10, 10, 60, 40)], 0.0)
    tracker.mark_notified(tracker.episodes[1], 0.0)

    assert tracker.notified_neighbor([900, 600, 1200, 800], 1.0) is None


def test_stale_notified_record_is_not_a_neighbor():
    """Запись десятиминутной давности — это другая машина, а не сосед по сцепу."""
    tracker = _tracker(vehicle_seen_neighbor_sec=20)
    tracker.observe([_car(1, 162, 408, 567, 816)], 0.0)
    tracker.mark_notified(tracker.episodes[1], 0.0)

    assert tracker.notified_neighbor([408, 384, 1058, 755], 30.0) is None


def test_suppressed_neighbor_does_not_drag_scene_memory():
    """Подавленный прицеп не должен утаскивать память за собой по кадру.

    Иначе он становится «якорем» и цепочкой глушит следующие машины в очереди.
    """
    truck = [162, 408, 567, 816]
    tracker = _tracker(vehicle_seen_neighbor_sec=20)
    tracker.observe([_car(1, *truck)], 0.0)
    tracker.episodes[1].sent = True
    tracker.mark_notified(tracker.episodes[1], 0.0)

    tracker.observe([_car(1, *truck), _car(2, 408, 384, 1058, 755)], 0.1)
    trailer = tracker.episodes[2]
    trailer.notified = True          # подавлен как часть сцепа, письма не было
    tracker.observe([_car(1, *truck), _car(2, 1200, 380, 1900, 760)], 0.2)

    assert tracker._notified_vehicles[0]["bbox"] == truck


def test_vehicle_leaving_through_edge_is_forgotten_quickly():
    """Уехавшая за край машина не должна глушить следующую в этой точке."""
    tracker = _tracker(vehicle_seen_dedup_sec=600, vehicle_seen_edge_ttl_sec=30)
    tracker.observe([_car(1, 1180, 400, 1279, 700)], 0.0)
    tracker.mark_notified(tracker.episodes[1], 0.0)
    tracker.observe([], 1.5)
    tracker.note_exit(tracker.episodes[1], (720, 1280), 2.0)
    tracker.close(1)

    # через 40 с в этой точке новая машина — событие должно уйти
    tracker.observe([_car(2, 1180, 400, 1279, 700)], 40.0)
    assert tracker.episodes[2].notified is False


def test_vehicle_lost_mid_scene_keeps_long_memory():
    """Пропавшая в середине сцены машина может стоять за кустом — помним долго."""
    tracker = _tracker(vehicle_seen_dedup_sec=600, vehicle_seen_edge_ttl_sec=30)
    tracker.observe([_car(1, 400, 300, 800, 600)], 0.0)
    tracker.mark_notified(tracker.episodes[1], 0.0)
    tracker.observe([], 1.5)
    tracker.note_exit(tracker.episodes[1], (720, 1280), 2.0)
    tracker.close(1)

    tracker.observe([_car(2, 400, 300, 800, 600)], 100.0)
    assert tracker.episodes[2].notified is True


def test_passing_vehicle_near_standing_truck_is_not_suppressed():
    """Трактор проезжает мимо стоящей на весах фуры — событие должно уйти.

    Фура видна прямо сейчас, значит новый трек рядом — другая машина.
    """
    truck = {"x1": 400, "y1": 300, "x2": 800, "y2": 700}
    tracker = _tracker(vehicle_seen_visible_sec=2.0)
    for i in range(5):
        tracker.observe([_car(1, **truck)], i * 0.5)
    tracker.episodes[1].sent = True
    tracker.mark_notified(tracker.episodes[1], 2.0)

    # через 0.5 с рядом с фурой рождается второй трек — трактор
    tracker.observe([_car(1, **truck), _car(2, 500, 350, 900, 650)], 2.5)

    assert tracker.episodes[2].notified is False


def test_standing_truck_redetected_after_long_gap_is_suppressed():
    """Фура стояла, трек потеряли, новый id на том же месте — это та же фура."""
    truck = {"x1": 400, "y1": 300, "x2": 800, "y2": 700}
    tracker = _tracker(vehicle_seen_visible_sec=2.0, track_reassoc_sec=2.0)
    for i in range(5):
        tracker.observe([_car(1, **truck)], i * 0.5)
    tracker.episodes[1].sent = True
    tracker.mark_notified(tracker.episodes[1], 2.0)

    tracker.observe([], 3.0)                      # трек пропал
    tracker.observe([_car(2, **truck)], 8.0)      # новый id там же

    assert tracker.episodes[2].notified is True


def test_trailer_moving_with_truck_is_suppressed_at_emission():
    """Тягач и прицеп едут с одной скоростью — второе письмо по сцепу не шлём."""
    tracker = _tracker(vehicle_seen_neighbor_sec=20)
    for i in range(6):
        shift = i * 40
        tracker.observe([_car(1, 162 + shift, 408, 567 + shift, 816),
                         _car(2, 408 + shift, 384, 1058 + shift, 755)], i * 0.5)
    truck = tracker.episodes[1]
    truck.sent = True
    tracker.mark_notified(truck, 2.5)
    trailer = tracker.episodes[2]

    assert trailer.speed() > 40
    assert tracker.notified_neighbor(trailer.bbox, 2.5, trailer.speed()) is not None


def test_passing_tractor_is_not_an_emission_neighbor():
    """У стоящей фуры и проезжающего трактора разные скорости — не соседи."""
    tracker = _tracker(vehicle_seen_neighbor_sec=20)
    for i in range(5):
        tracker.observe([_car(1, 400, 300, 800, 700)], i * 0.5)
    truck = tracker.episodes[1]
    truck.sent = True
    tracker.mark_notified(truck, 2.0)

    assert tracker.notified_neighbor([500, 350, 900, 650], 2.5, 300.0) is None