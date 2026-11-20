package com.argus.service;

import com.argus.dto.JournalEntry;
import com.argus.dto.JournalFilter;
import com.argus.dto.JournalSummary;
import com.argus.dto.PageResponse;
import com.argus.repository.JournalRepository;
import com.argus.repository.VehicleRepository;
import org.springframework.stereotype.Service;

import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.LocalTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * Сборка журнала: пагинация с защитой от выхода за край и сводка за период.
 */
@Service
public class JournalService {

    /** Больше такой страницы брать бессмысленно: журнал на год не влезает в экран. */
    public static final int MAX_PAGE_SIZE = 500;
    public static final int DEFAULT_PAGE_SIZE = 50;
    /** Потолок длины ряда по дням, чтобы старый период не генерировал тысячи точек. */
    public static final int MAX_DAILY_BUCKETS = 92;
    /** Ограничение выгрузки CSV. */
    public static final int MAX_EXPORT_ROWS = 10_000;

    private final JournalRepository journalRepository;
    private final VehicleRepository vehicleRepository;

    public JournalService(JournalRepository journalRepository, VehicleRepository vehicleRepository) {
        this.journalRepository = journalRepository;
        this.vehicleRepository = vehicleRepository;
    }

    /**
     * Страница журнала под фильтром.
     *
     * Страница за пределами выборки не возвращается пустой: клиент ставит её
     * сам (например, фильтр сузился, а номер страницы остался), и пустая таблица
     * выглядит как «событий нет». Вместо этого отдаём последнюю заполненную
     * страницу, а в ответе — её номер, чтобы интерфейс синхронизировался.
     */
    public PageResponse<JournalEntry> page(JournalFilter filter, int page, int size) {
        int safeSize = Math.min(Math.max(size, 1), MAX_PAGE_SIZE);
        int safePage = Math.max(page, 0);
        long total = journalRepository.count(filter);
        if (total == 0) {
            return PageResponse.empty(safePage, safeSize);
        }
        int totalPages = (int) Math.ceil((double) total / safeSize);
        if (safePage >= totalPages) {
            safePage = totalPages - 1;
        }
        List<JournalEntry> items = journalRepository.findPage(filter, safePage * safeSize, safeSize);
        return PageResponse.of(items, safePage, safeSize, total);
    }

    /** Все строки под фильтром, но не больше лимита (для CSV). */
    public List<JournalEntry> forExport(JournalFilter filter) {
        return journalRepository.findForExport(filter, MAX_EXPORT_ROWS);
    }

    /**
     * Сводка за период. Если период не задан — последние 14 дней: «всего с
     * момента установки» бесполезно, когда системе уже несколько месяцев.
     */
    public JournalSummary summary(JournalFilter baseFilter, LocalDateTime from, LocalDateTime to, int days) {
        int bucketCount = Math.min(Math.max(days, 1), MAX_DAILY_BUCKETS);
        LocalDateTime effectiveTo = to != null ? to : LocalDateTime.now();
        LocalDateTime effectiveFrom = from != null ? from
                : effectiveTo.toLocalDate().atStartOfDay().minusDays(bucketCount - 1L);

        JournalFilter filter = baseFilter.withRange(effectiveFrom, effectiveTo);

        LocalDate toDay = effectiveTo.toLocalDate();
        LocalDate fromDay = effectiveFrom.toLocalDate();
        // Длинный период режем с начала: в шапке интересуют последние дни,
        // а не вся история за год в виде ряда из 365 точек.
        if (fromDay.isBefore(toDay.minusDays(bucketCount - 1L))) {
            fromDay = toDay.minusDays(bucketCount - 1L);
        }
        JournalFilter dayFilter = baseFilter.withRange(fromDay.atStartOfDay(), toDay.atTime(LocalTime.MAX));

        Map<LocalDate, long[]> daily = journalRepository.dailyCounts(dayFilter, fromDay, toDay);
        List<JournalSummary.DayBucket> buckets = new ArrayList<>(daily.size());
        daily.forEach((day, counts) -> buckets.add(new JournalSummary.DayBucket(day, counts[0], counts[1])));

        return new JournalSummary(
                journalRepository.countPlates(filter),
                journalRepository.countDistinctPlates(filter),
                journalRepository.countDeniedPlates(filter),
                journalRepository.countAlerts(filter),
                journalRepository.alertsByType(filter),
                buckets,
                vehicleRepository.count());
    }
}
