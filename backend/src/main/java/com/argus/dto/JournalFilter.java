package com.argus.dto;

import java.time.LocalDateTime;

/**
 * Фильтры журнала. Любое поле может быть null — тогда фильтр не применяется.
 * Пустая строка тоже считается «не заданным», чтобы UI не присылал мусор.
 */
public record JournalFilter(
        String cameraId,
        /** PLATE, ALERT или null (оба вида). */
        String kind,
        /** Тип тревоги — применим только к ALERT. */
        String alertType,
        /** Поиск по номеру и по типу тревоги, без учёта регистра. */
        String query,
        /** ALLOWED / ALLOWED_FUZZY / DENIED — применим только к PLATE. */
        String decision,
        LocalDateTime from,
        LocalDateTime to
) {
    public JournalFilter {
        cameraId = blankToNull(cameraId);
        kind = blankToNull(kind);
        alertType = blankToNull(alertType);
        query = blankToNull(query);
        decision = blankToNull(decision);
    }

    private static String blankToNull(String value) {
        return value == null || value.isBlank() ? null : value.trim();
    }

    /** Есть ли хоть один непустой фильтр — тогда счётчики надо пересчитывать. */
    public boolean isFiltered() {
        return cameraId != null || kind != null || alertType != null
                || query != null || decision != null || from != null || to != null;
    }

    /** Аналогично, но с принудительно заданным интервалом (для статистики). */
    public JournalFilter withRange(LocalDateTime newFrom, LocalDateTime newTo) {
        return new JournalFilter(cameraId, kind, alertType, query, decision, newFrom, newTo);
    }

    /**
     * Поисковый запрос в виде номера: латиница, верхний регистр, без точек
     * и пробелов. Сравнивается с translate() в SQL, поэтому не зависит от
     * раскладки клавиатуры. Сам query при этом не меняется — по нему же
     * ищутся английские названия типов тревог.
     */
    public String plateQuery() {
        return com.argus.util.PlateText.normalizeSearch(query);
    }
}
