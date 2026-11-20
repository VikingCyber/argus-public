"""Тесты конфигов: примеры из репозитория должны оставаться валидными и согласованными.

Спрашиваем модель о карте классов, а не верим комментариям: имена классов COCO
берутся из yolov8*.pt, и ошибка в комментарии (2=car vs 2=motorcycle) ровно так же
опасна, как ошибка в самом списке.
"""
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

VISION = Path(__file__).resolve().parent.parent
EXAMPLES = [VISION / "config.example.yaml", VISION / "config.prod.example.yaml"]
# 0=person, 1=bicycle, 2=car, 3=motorcycle, 5=bus, 7=truck
EXPECTED_NAMES = {0: "person", 1: "bicycle", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_configs_parse(path: Path):
    cfg = _load(path)
    for key in ("cameras", "anpr", "thresholds"):
        assert key in cfg, f"{path.name}: нет секции {key}"
    assert cfg["cameras"][0]["rtsp_url"].startswith("rtsp://")


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_object_classes_are_real_coco_ids(path: Path):
    classes = _load(path)["object_classes"]
    assert 0 in classes, "человек должен оставаться в классах (ночные тревоги)"
    for cid in classes:
        assert 0 <= cid < 80, f"{path.name}: {cid} вне диапазона COCO (0..79)"


def test_class_id_comments_match_reality():
    """ID из комментариев конфига должны совпадать с реальной картой COCO."""
    try:
        from ultralytics import YOLO
        names = YOLO("yolov8n.pt").names  # веса скачиваются автоматически, если их нет
    except Exception as exc:  # noqa: BLE001 — нет сети/весов в CI — не блокируем сборку
        pytest.skip(f"модель недоступна: {exc}")
    for cid, expected in EXPECTED_NAMES.items():
        assert names[cid] == expected, f"COCO {cid} = {names[cid]}, а в коде ждём {expected}"
