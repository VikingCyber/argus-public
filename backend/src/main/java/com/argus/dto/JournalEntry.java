package com.argus.dto;

import java.time.LocalDateTime;

/**
 * Строка единого журнала: распознанный номер ИЛИ тревога.
 *
 * Раньше журнал был двумя отдельными лентами (таблица номеров и лента тревог),
 * поэтому в одном списке их смешать было нечем. Теперь оба вида событий
 * лежат в одной таблице и различаются полем kind, а заполненные поля
 * зависят от вида: у номера есть decision и confidence, у тревоги — alertType.
 */
public record JournalEntry(
        /** PLATE или ALERT. */
        String kind,
        Long id,
        /** Номер ТС. У тревоги может быть null, если она не привязана к номеру. */
        String plateNumber,
        /** ALLOWED / ALLOWED_FUZZY / DENIED — только для PLATE. */
        String decision,
        /** NIGHT_PERSON / UNKNOWN_PLATE / ... — только для ALERT. */
        String alertType,
        Long trackId,
        String cameraId,
        LocalDateTime detectedAt,
        String imagePath,
        /** Уверенность распознавания — только для PLATE. */
        Double confidence
) {
    public boolean isPlate() {
        return "PLATE".equals(kind);
    }

    public boolean isAlert() {
        return "ALERT".equals(kind);
    }
}
