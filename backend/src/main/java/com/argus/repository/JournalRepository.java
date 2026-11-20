package com.argus.repository;

import com.argus.dto.JournalEntry;
import com.argus.dto.JournalFilter;
import jakarta.persistence.EntityManager;
import jakarta.persistence.PersistenceContext;
import org.springframework.stereotype.Repository;
import org.springframework.transaction.annotation.Transactional;

import java.sql.Date;
import java.sql.Timestamp;
import java.time.Instant;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Единый журнал: распознанные номера и тревоги в одной хронологической ленте.
 *
 * Сделано нативным UNION ALL, а не двумя запросами в Java: пагинация, счётчик
 * и поиск должны считаться по всей выборке сразу, иначе «всего 4000, показано
 * последние 100» получится неверным.
 *
 * Сортировка всегда по (detected_at, id) — по одному detected_at строки с
 * одинаковой секундой «перепрыгивали» бы между страницами, и при перезагрузке
 * страницы дубликаты терялись.
 */
@Repository
@Transactional(readOnly = true)
public class JournalRepository {

    /** Тело выборки без ORDER BY/LIMIT — используется и для страниц, и для счётчика. */
    private static final String SELECT_BODY = """
            SELECT p.id AS id,
                   'PLATE' AS kind,
                   p.plate_number AS plate_number,
                   p.decision AS decision,
                   NULL::text AS alert_type,
                   p.track_id AS track_id,
                   p.camera_id AS camera_id,
                   p.detected_at AS detected_at,
                   p.image_path AS image_path,
                   p.confidence AS confidence
              FROM plate_events p
             WHERE (CAST(:cameraId AS text) IS NULL OR p.camera_id = :cameraId)
               AND (CAST(:kind AS text) IS NULL OR :kind = 'PLATE')
               AND (CAST(:plateQuery AS text) IS NULL
                    OR translate(upper(p.plate_number), 'АВЕКМНОРСТУХ', 'ABEKMHOPCTYX')
                       ILIKE '%' || :plateQuery || '%')
               AND (CAST(:decision AS text) IS NULL OR p.decision = :decision)
               AND (CAST(:alertType AS text) IS NULL OR FALSE)
               AND (CAST(:fromTs AS timestamp) IS NULL OR p.detected_at >= CAST(:fromTs AS timestamp))
               AND (CAST(:toTs AS timestamp) IS NULL OR p.detected_at <= CAST(:toTs AS timestamp))
            UNION ALL
            SELECT a.id,
                   'ALERT',
                   a.plate_number,
                   NULL::text,
                   a.alert_type,
                   a.track_id,
                   a.camera_id,
                   a.detected_at,
                   a.image_path,
                   NULL::double precision
              FROM alert_events a
             WHERE (CAST(:cameraId AS text) IS NULL OR a.camera_id = :cameraId)
               AND (CAST(:kind AS text) IS NULL OR :kind = 'ALERT')
               AND (CAST(:plateQuery AS text) IS NULL
                    OR translate(upper(a.plate_number), 'АВЕКМНОРСТУХ', 'ABEKMHOPCTYX')
                       ILIKE '%' || :plateQuery || '%')
               AND (CAST(:query AS text) IS NULL OR a.alert_type ILIKE '%' || :query || '%')
               AND (CAST(:alertType AS text) IS NULL OR a.alert_type = :alertType)
               AND (CAST(:decision AS text) IS NULL OR FALSE)
               AND (CAST(:fromTs AS timestamp) IS NULL OR a.detected_at >= CAST(:fromTs AS timestamp))
               AND (CAST(:toTs AS timestamp) IS NULL OR a.detected_at <= CAST(:toTs AS timestamp))
            """;

    private static final String ORDER_BY = " ORDER BY detected_at DESC, id DESC ";

    @PersistenceContext
    private EntityManager em;

    /**
     * Страница журнала. size и offset подставляются как приведённые к int,
     * иначе PostgreSQL не может определить тип параметра в LIMIT.
     */
    public List<JournalEntry> findPage(JournalFilter filter, int offset, int size) {
        String sql = SELECT_BODY + ORDER_BY
                + " LIMIT CAST(:size AS int) OFFSET CAST(:offset AS int)";
        List<?> rows = em.createNativeQuery(sql)
                .setParameter("cameraId", filter.cameraId())
                .setParameter("kind", filter.kind())
                .setParameter("query", filter.query())
                .setParameter("plateQuery", filter.plateQuery())
                .setParameter("decision", filter.decision())
                .setParameter("alertType", filter.alertType())
                .setParameter("fromTs", filter.from())
                .setParameter("toTs", filter.to())
                .setParameter("size", size)
                .setParameter("offset", offset)
                .getResultList();
        return mapRows(rows);
    }

    /** Сколько всего строк под фильтром — для «показано 1–50 из 4321». */
    public long count(JournalFilter filter) {
        String sql = "SELECT COUNT(*) FROM (" + SELECT_BODY + ") AS j";
        Object result = em.createNativeQuery(sql)
                .setParameter("cameraId", filter.cameraId())
                .setParameter("kind", filter.kind())
                .setParameter("query", filter.query())
                .setParameter("plateQuery", filter.plateQuery())
                .setParameter("decision", filter.decision())
                .setParameter("alertType", filter.alertType())
                .setParameter("fromTs", filter.from())
                .setParameter("toTs", filter.to())
                .getSingleResult();
        return result instanceof Number number ? number.longValue() : 0L;
    }

    /**
     * Выборка для экспорта CSV: та же лента, но без пагинации.
     * Ограничение сверху обязательно — иначе один запрос выгрузит весь журнал
     * в память и положит бэкенд.
     */
    public List<JournalEntry> findForExport(JournalFilter filter, int maxRows) {
        String sql = SELECT_BODY + ORDER_BY + " LIMIT CAST(:size AS int)";
        List<?> rows = em.createNativeQuery(sql)
                .setParameter("cameraId", filter.cameraId())
                .setParameter("kind", filter.kind())
                .setParameter("query", filter.query())
                .setParameter("plateQuery", filter.plateQuery())
                .setParameter("decision", filter.decision())
                .setParameter("alertType", filter.alertType())
                .setParameter("fromTs", filter.from())
                .setParameter("toTs", filter.to())
                .setParameter("size", maxRows)
                .getResultList();
        return mapRows(rows);
    }

    /** Сколько номеров пришло за период. */
    public long countPlates(JournalFilter filter) {
        return countScalar("""
                SELECT COUNT(*) FROM plate_events p
                 WHERE (CAST(:cameraId AS text) IS NULL OR p.camera_id = :cameraId)
                   AND (CAST(:fromTs AS timestamp) IS NULL OR p.detected_at >= CAST(:fromTs AS timestamp))
                   AND (CAST(:toTs AS timestamp) IS NULL OR p.detected_at <= CAST(:toTs AS timestamp))
                """, filter);
    }

    /** Сколько разных номеров за период. */
    public long countDistinctPlates(JournalFilter filter) {
        return countScalar("""
                SELECT COUNT(DISTINCT p.plate_number) FROM plate_events p
                 WHERE (CAST(:cameraId AS text) IS NULL OR p.camera_id = :cameraId)
                   AND (CAST(:fromTs AS timestamp) IS NULL OR p.detected_at >= CAST(:fromTs AS timestamp))
                   AND (CAST(:toTs AS timestamp) IS NULL OR p.detected_at <= CAST(:toTs AS timestamp))
                """, filter);
    }

    /** Сколько номеров из чужого (DENIED) за период. */
    public long countDeniedPlates(JournalFilter filter) {
        return countScalar("""
                SELECT COUNT(*) FROM plate_events p
                 WHERE p.decision = 'DENIED'
                   AND (CAST(:cameraId AS text) IS NULL OR p.camera_id = :cameraId)
                   AND (CAST(:fromTs AS timestamp) IS NULL OR p.detected_at >= CAST(:fromTs AS timestamp))
                   AND (CAST(:toTs AS timestamp) IS NULL OR p.detected_at <= CAST(:toTs AS timestamp))
                """, filter);
    }

    /** Сколько тревог за период, с разбивкой по типам. */
    public long countAlerts(JournalFilter filter) {
        return countScalar("""
                SELECT COUNT(*) FROM alert_events a
                 WHERE (CAST(:cameraId AS text) IS NULL OR a.camera_id = :cameraId)
                   AND (CAST(:fromTs AS timestamp) IS NULL OR a.detected_at >= CAST(:fromTs AS timestamp))
                   AND (CAST(:toTs AS timestamp) IS NULL OR a.detected_at <= CAST(:toTs AS timestamp))
                """, filter);
    }

    public Map<String, Long> alertsByType(JournalFilter filter) {
        String sql = """
                SELECT a.alert_type, COUNT(*) FROM alert_events a
                 WHERE (CAST(:cameraId AS text) IS NULL OR a.camera_id = :cameraId)
                   AND (CAST(:fromTs AS timestamp) IS NULL OR a.detected_at >= CAST(:fromTs AS timestamp))
                   AND (CAST(:toTs AS timestamp) IS NULL OR a.detected_at <= CAST(:toTs AS timestamp))
                 GROUP BY a.alert_type
                 ORDER BY COUNT(*) DESC
                """;
        List<?> rows = em.createNativeQuery(sql)
                .setParameter("cameraId", filter.cameraId())
                .setParameter("fromTs", filter.from())
                .setParameter("toTs", filter.to())
                .getResultList();
        Map<String, Long> result = new LinkedHashMap<>();
        for (Object row : rows) {
            Object[] cells = (Object[]) row;
            result.put(String.valueOf(cells[0]), ((Number) cells[1]).longValue());
        }
        return result;
    }

    /**
     * Ряд по дням: сколько номеров и сколько тревог было в каждый день периода.
     * Дни без событий тоже возвращаются (с нулём) — иначе на графике были бы
     * разрывы, которые читаются как «камера не работала».
     */
    public Map<LocalDate, long[]> dailyCounts(JournalFilter filter, LocalDate fromDay, LocalDate toDay) {
        Map<LocalDate, long[]> result = new LinkedHashMap<>();
        for (LocalDate day = fromDay; !day.isAfter(toDay); day = day.plusDays(1)) {
            result.put(day, new long[2]);
        }
        fillDaily(result, """
                SELECT CAST(date_trunc('day', p.detected_at) AS date) AS day, COUNT(*)
                  FROM plate_events p
                 WHERE (CAST(:cameraId AS text) IS NULL OR p.camera_id = :cameraId)
                   AND (CAST(:fromTs AS timestamp) IS NULL OR p.detected_at >= CAST(:fromTs AS timestamp))
                   AND (CAST(:toTs AS timestamp) IS NULL OR p.detected_at <= CAST(:toTs AS timestamp))
                 GROUP BY 1
                """, filter, 0);
        fillDaily(result, """
                SELECT CAST(date_trunc('day', a.detected_at) AS date) AS day, COUNT(*)
                  FROM alert_events a
                 WHERE (CAST(:cameraId AS text) IS NULL OR a.camera_id = :cameraId)
                   AND (CAST(:fromTs AS timestamp) IS NULL OR a.detected_at >= CAST(:fromTs AS timestamp))
                   AND (CAST(:toTs AS timestamp) IS NULL OR a.detected_at <= CAST(:toTs AS timestamp))
                 GROUP BY 1
                """, filter, 1);
        return result;
    }

    private void fillDaily(Map<LocalDate, long[]> target, String sql, JournalFilter filter, int slot) {
        List<?> rows = em.createNativeQuery(sql)
                .setParameter("cameraId", filter.cameraId())
                .setParameter("fromTs", filter.from())
                .setParameter("toTs", filter.to())
                .getResultList();
        for (Object row : rows) {
            Object[] cells = (Object[]) row;
            LocalDate day = toLocalDate(cells[0]);
            long[] bucket = day == null ? null : target.get(day);
            if (bucket != null) {
                bucket[slot] = ((Number) cells[1]).longValue();
            }
        }
    }

    private long countScalar(String sql, JournalFilter filter) {
        Object result = em.createNativeQuery(sql)
                .setParameter("cameraId", filter.cameraId())
                .setParameter("fromTs", filter.from())
                .setParameter("toTs", filter.to())
                .getSingleResult();
        return result instanceof Number number ? number.longValue() : 0L;
    }

    private List<JournalEntry> mapRows(List<?> rows) {
        List<JournalEntry> entries = new ArrayList<>(rows.size());
        for (Object row : rows) {
            Object[] c = (Object[]) row;
            entries.add(new JournalEntry(
                    str(c[1]),
                    num(c[0]),
                    str(c[2]),
                    str(c[3]),
                    str(c[4]),
                    num(c[5]),
                    str(c[6]),
                    toLocalDateTime(c[7]),
                    str(c[8]),
                    c[9] instanceof Number n ? n.doubleValue() : null));
        }
        return entries;
    }

    private static String str(Object value) {
        return value == null ? null : String.valueOf(value);
    }

    private static Long num(Object value) {
        return value instanceof Number n ? n.longValue() : null;
    }

    /**
     * Время из нативной выборки приходит как java.sql.Timestamp — в зависимости
     * от версии драйвера и типа JDBC. Раньше здесь стояла проверка
     * instanceof LocalDateTime, из-за которой поле молча становилось null,
     * и в журнале не было ни даты, ни сортировки по дате на экране.
     */
    private static LocalDateTime toLocalDateTime(Object value) {
        return switch (value) {
            case LocalDateTime dt -> dt;
            case Timestamp ts -> ts.toLocalDateTime();
            case OffsetDateTime odt -> odt.toLocalDateTime();
            case ZonedDateTime zdt -> zdt.toLocalDateTime();
            case Instant i -> LocalDateTime.ofInstant(i, ZoneId.systemDefault());
            default -> null;
        };
    }

    private static LocalDate toLocalDate(Object value) {
        return switch (value) {
            case LocalDate ld -> ld;
            case Date d -> d.toLocalDate();
            case Timestamp ts -> ts.toLocalDateTime().toLocalDate();
            default -> null;
        };
    }
}
