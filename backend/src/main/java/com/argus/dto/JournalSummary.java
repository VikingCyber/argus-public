package com.argus.dto;

import java.time.LocalDate;
import java.util.List;
import java.util.Map;

/**
 * Сводка за период: счётчики, разбивка по типам тревог и динамика по дням.
 *
 * Раньше в шапке были только всеобщие итоги с момента установки, из которых
 * нельзя было понять ни что за день, ни сколько машин пришло именно сегодня.
 */
public record JournalSummary(
        long platesTotal,
        long platesUnique,
        long platesDenied,
        long alertsTotal,
        /** Сколько тревог каждого типа за период. */
        Map<String, Long> alertsByType,
        /** Ряд по дням без пропусков: ноль, если событий не было. */
        List<DayBucket> daily,
        /** Машин в реестре (не событий за период). */
        long vehiclesTotal
) {
    public record DayBucket(LocalDate day, long plates, long alerts) {
    }
}
