"""Качество JPEG снимков и живого потока.

Причина тестов: качество было зашито в коде (снимок 85, /frame 92, страница
70) и его нельзя было подкрутить без правки исходников. Теперь это ключи
конфига snapshot_jpeg_quality / view_jpeg_quality. Тест держит три обещания:
значение доходит до imencode, мусор в конфиге не валит сервис, и страница
:8090 действительно отдаёт обе панели.
"""
import numpy as np

from event_client import EventClient
from stream_server import INDEX_HTML, _jpeg_quality


def _noisy_image() -> np.ndarray:
    """Шум: на гладком кадре размер JPEG почти не зависит от качества."""
    rng = np.random.default_rng(0)
    return rng.integers(0, 255, (240, 320, 3), dtype=np.uint8)


def test_event_client_default_quality_is_95():
    # Было 85: поверх уже сжатого релеем кадра давало видимое мыло.
    assert EventClient("http://localhost:8080")._jpeg_quality == 95


def test_event_client_clamps_quality():
    assert EventClient("http://x", jpeg_quality=0)._jpeg_quality == 1
    assert EventClient("http://x", jpeg_quality=150)._jpeg_quality == 100
    assert EventClient("http://x", jpeg_quality=80)._jpeg_quality == 80


def test_event_client_quality_changes_payload_size():
    frame = _noisy_image()
    low = EventClient("http://x", jpeg_quality=30)._jpeg_bytes(frame)
    high = EventClient("http://x", jpeg_quality=95)._jpeg_bytes(frame)
    assert low and high
    assert len(high) > len(low) * 2, "качество 95 должно давать заметно больше байт"


def test_event_client_handles_empty_frame():
    assert EventClient("http://x")._jpeg_bytes(None) is None


def test_jpeg_quality_reads_config():
    assert _jpeg_quality({"snapshot_jpeg_quality": 88}, "snapshot_jpeg_quality", 95) == 88
    assert _jpeg_quality({"view_jpeg_quality": "70"}, "view_jpeg_quality", 80) == 70


def test_jpeg_quality_falls_back_on_missing_or_garbage():
    # Кривой конфиг не должен ронять релей на старте.
    assert _jpeg_quality({}, "view_jpeg_quality", 80) == 80
    assert _jpeg_quality({"view_jpeg_quality": "abc"}, "view_jpeg_quality", 80) == 80
    assert _jpeg_quality({"view_jpeg_quality": None}, "view_jpeg_quality", 80) == 80


def test_jpeg_quality_clamps_range():
    assert _jpeg_quality({"view_jpeg_quality": 0}, "view_jpeg_quality", 80) == 1
    assert _jpeg_quality({"view_jpeg_quality": 300}, "view_jpeg_quality", 80) == 100


def test_index_page_has_both_panels_and_no_scroll():
    # Обе панели на одной странице, кадр вписан целиком (contain), страница
    # не прокручивается: body{overflow:hidden}.
    assert 'data-src="/stream"' in INDEX_HTML
    assert 'data-src="/raw"' in INDEX_HTML
    assert "object-fit:contain" in INDEX_HTML
    assert "overflow:hidden" in INDEX_HTML
