package com.argus.dto;

import java.time.LocalDate;
import java.util.List;

/**
 * Итог прогона очистки старых данных (ночной задачи или ручного запуска).
 *
 * <p>{@code candidateRows} — сколько строк подлежало удалению на момент старта,
 * {@code deletedRows} — сколько реально удалено (в режиме {@code dryRun} — 0).
 * {@code directories} — какие каталоги снимков попали под очистку (в dry-run
 * это и есть ответ на вопрос «что будет удалено»).
 */
public record RetentionResult(
        boolean dryRun,
        int days,
        LocalDate cutoff,
        long candidateRows,
        int deletedRows,
        int deletedFiles,
        long freedBytes,
        long freeBytesBefore,
        long freeBytesAfter,
        List<String> directories) {
}
