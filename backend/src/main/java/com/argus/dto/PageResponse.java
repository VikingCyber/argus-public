package com.argus.dto;

import java.util.List;

/**
 * Страница данных для журнала: содержимое плюс метаданные пагинации.
 *
 * @param items      строки текущей страницы
 * @param page       номер страницы с нуля, как его просил клиент
 * @param size       фактический размер страницы (после обрезки лимитом)
 * @param total      всего строк под текущими фильтрами
 * @param totalPages сколько страниц при таком размере
 * @param hasNext    есть ли следующая страница
 */
public record PageResponse<T>(
        List<T> items,
        int page,
        int size,
        long total,
        int totalPages,
        boolean hasNext
) {
    public static <T> PageResponse<T> of(List<T> items, int page, int size, long total) {
        int totalPages = size <= 0 ? 0 : (int) Math.ceil((double) total / size);
        return new PageResponse<>(items, page, size, total, totalPages, page + 1 < totalPages);
    }

    public static <T> PageResponse<T> empty(int page, int size) {
        return new PageResponse<>(List.of(), page, size, 0, 0, false);
    }
}
