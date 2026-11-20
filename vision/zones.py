"""Геометрия зон кадра: игнор по перекрытию площади вместо центра bbox.

Раньше бокс считался «в зоне», если в ней лежал его центр: машина, стоящая
наполовину в игнор-зоне, детектировалась и будила тревоги. Теперь зона
игнорирует бокс, когда не меньше overlap_frac его площади попадает в неё.
"""
from collections.abc import Sequence

Rect = Sequence[float]


def overlap_frac(bbox: Sequence[float], rect: Sequence[float], shape) -> float:
    """Доля (0..1) площади bbox, попадающая в нормированный прямоугольник (0..1)."""
    h, w = shape[:2]
    x1, y1, x2, y2 = bbox[0], bbox[1], bbox[2], bbox[3]
    rx1, ry1 = rect[0] * w, rect[1] * h
    rx2, ry2 = rect[2] * w, rect[3] * h
    iw = max(0.0, min(x2, rx2) - max(x1, rx1))
    ih = max(0.0, min(y2, ry2) - max(y1, ry1))
    area = (x2 - x1) * (y2 - y1)
    if area <= 0:
        return 0.0
    return (iw * ih) / area


def bbox_ignored(bbox: Sequence[float], ignore_rois: Sequence[Rect], shape,
                 overlap_frac_min: float = 0.5) -> bool:
    """Бокс в игнор-зоне, если >= overlap_frac_min его площади там находится."""
    return any(overlap_frac(bbox, rect, shape) >= overlap_frac_min
               for rect in ignore_rois)


def in_detection_area(bbox: Sequence[float], ignore_rois: Sequence[Rect], shape,
                      overlap_frac_min: float = 0.5,
                      roi: Sequence[float] | None = None,
                      extra_ignore: Sequence[Rect] | None = None) -> bool:
    """Рабочая зона (по центру) поверх игнор-зон (по перекрытию площади).

    extra_ignore — дополнительные игнор-зоны конкретного фильтра (например,
    полная ширина верхней полосы для чтений номеров).
    """
    if bbox_ignored(bbox, ignore_rois, shape, overlap_frac_min):
        return False
    if extra_ignore and bbox_ignored(bbox, extra_ignore, shape, overlap_frac_min):
        return False
    if roi is None:
        return True
    h, w = shape[:2]
    cx = (bbox[0] + bbox[2]) / 2 / w
    cy = (bbox[1] + bbox[3]) / 2 / h
    return roi[0] <= cx <= roi[2] and roi[1] <= cy <= roi[3]
