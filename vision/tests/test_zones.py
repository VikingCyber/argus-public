"""Тесты геометрии зон: чистый Python, без torch — быстрые в CI."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from zones import bbox_ignored, in_detection_area, overlap_frac

SHAPE = (720, 1280, 3)  # h, w, channels
# Правая верхняя полоса (дорога за забором) в нормализованных координатах
STRIP = [0.7617, 0.0, 1.0, 0.4194]


def test_overlap_frac_full_and_partial():
    # бокс целиком в полосе (x внутри 975..1280, y внутри 0..302)
    assert overlap_frac([1000, 100, 1200, 200], STRIP, SHAPE) == pytest.approx(1.0)
    # ровно половина площади в полосе: x 800..1150 -> пересечение 175 из 350
    # (левый край полосы 0.7617*1280 = 974.976, поэтому сравниваем с допуском)
    assert overlap_frac([800, 100, 1150, 200], STRIP, SHAPE) == pytest.approx(0.5, abs=0.001)
    # бокс полностью вне полосы
    assert overlap_frac([100, 400, 400, 600], STRIP, SHAPE) == 0.0


def test_overlap_frac_zero_area_bbox():
    assert overlap_frac([500, 300, 500, 300], STRIP, SHAPE) == 0.0


def test_bbox_ignored_threshold_is_50_percent():
    # ~40% площади в полосе -> ещё не игнор
    assert not bbox_ignored([800, 100, 1092, 200], [STRIP], SHAPE, 0.5)
    # ровно 50% в полосе -> игнор
    assert bbox_ignored([800, 100, 1150, 200], [STRIP], SHAPE, 0.5)


def test_extra_ignore_strips_plate_reads():
    full_top = [0.0, 0.0, 1.0, 0.3]
    bbox = [100, 100, 300, 250]  # целиком в верхней трети кадра
    # без extra_ignore бокс проходит
    assert in_detection_area(bbox, [], SHAPE)
    # с полной верхней полосой как extra_ignore — отсекается
    assert not in_detection_area(bbox, [], SHAPE, extra_ignore=[full_top])


def test_roi_is_checked_by_center():
    roi = [0.1, 0.1, 0.6, 0.6]
    inside = [200, 200, 400, 400]    # центр ~ (300/1280, 300/720) в roi
    outside = [900, 500, 1100, 700]  # центр вне roi
    assert in_detection_area(inside, [], SHAPE, roi=roi)
    assert not in_detection_area(outside, [], SHAPE, roi=roi)
