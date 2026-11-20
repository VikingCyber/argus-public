from stream_server import DetectionCache


def test_cache_empty_initially():
    dc = DetectionCache()
    assert dc.snapshot() == []


def test_cache_snapshot_returns_shallow_copy():
    dc = DetectionCache()
    objects = [{"bbox": [10, 20, 100, 200], "name": "car", "confidence": 0.9}]
    dc.update(objects)
    snap = dc.snapshot()
    assert len(snap) == 1
    assert snap == objects
    # snapshot — это новый список (list(self._objects)), dict-ы те же ссылки
    assert snap is not objects
    assert snap[0] is objects[0]


def test_cache_snapshot_expires_after_max_age():
    dc = DetectionCache()
    dc.update([{"bbox": [1, 2, 3, 4]}])
    assert dc.snapshot(max_age=0.0) == []
    assert len(dc.snapshot(max_age=999.0)) == 1


def test_cache_update_with_empty_list():
    dc = DetectionCache()
    dc.update([{"bbox": [1, 2, 3, 4]}])
    assert len(dc.snapshot()) == 1
    dc.update([])
    assert dc.snapshot() == []


def test_cache_default_max_age_two_seconds():
    dc = DetectionCache()
    dc.update([{"bbox": [1, 2, 3, 4]}])
    snap = dc.snapshot()
    assert len(snap) == 1
    snap_default = dc.snapshot(max_age=2.0)
    assert len(snap_default) == 1