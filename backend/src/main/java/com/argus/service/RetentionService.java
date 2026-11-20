package com.argus.service;

import com.argus.dto.RetentionResult;
import com.argus.repository.AlertEventRepository;
import com.argus.repository.PlateEventRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;

import java.io.IOException;
import java.nio.file.FileVisitResult;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.SimpleFileVisitor;
import java.nio.file.attribute.BasicFileAttributes;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.List;
import java.util.function.IntSupplier;
import java.util.stream.Stream;

/**
 * Ночная очистка старых данных: строки журнала и файлы снимков.
 *
 * <p>Файлы удаляются по каталогам-датам ({@code images/YYYY-MM-DD}), которые
 * создаёт {@link ImageStorageService}. Это надёжнее, чем удалять по списку
 * путей из БД: не остаётся сирот, если строка потерялась, и не нужно держать
 * в памяти список имён. Отсечка одна и та же для БД и файлов — начало суток
 * {@code today - days}: строки за этот день и его каталог остаются.
 *
 * <p>Порядок: сначала файлы, потом строки. Если процесс упадёт между шагами,
 * в журнале будут записи со ссылками на уже удалённые файлы (битая картинка),
 * зато диск не забьётся. Обратный порядок оставлял бы файлы-сироты навсегда.
 */
@Service
public class RetentionService {

    private static final Logger log = LoggerFactory.getLogger(RetentionService.class);

    private static final long GB = 1024L * 1024L * 1024L;

    /** Предел числа пакетов за прогон: страховка от бесконечного цикла удаления. */
    private static final int MAX_BATCHES = 1000;

    private final PlateEventRepository plateEvents;
    private final AlertEventRepository alertEvents;
    private final EmailNotificationService email;
    private final Path imagesRoot;
    private final boolean enabled;
    private final int days;
    private final long minFreeGb;
    private final int batchSize;

    /** Дата последнего письма о нехватке места: не чаще одного раза в сутки. */
    private LocalDate lastDiskAlertDay;

    public RetentionService(PlateEventRepository plateEvents,
                            AlertEventRepository alertEvents,
                            EmailNotificationService email,
                            @Value("${argus.images-dir:data/images}") String imagesDir,
                            @Value("${argus.retention.enabled:true}") boolean enabled,
                            @Value("${argus.retention.days:90}") int days,
                            @Value("${argus.retention.min-free-gb:5}") long minFreeGb,
                            @Value("${argus.retention.batch-size:500}") int batchSize) {
        this.plateEvents = plateEvents;
        this.alertEvents = alertEvents;
        this.email = email;
        this.imagesRoot = Paths.get(imagesDir).toAbsolutePath().normalize();
        this.enabled = enabled;
        this.days = days;
        this.minFreeGb = minFreeGb;
        this.batchSize = Math.max(batchSize, 1);
    }

    /** Ночной запуск (по умолчанию 03:30 по времени сервера). */
    @Scheduled(cron = "${argus.retention.cron:0 30 3 * * *}")
    public void nightly() {
        if (!enabled) {
            log.info("Ретеншн выключен (argus.retention.enabled=false)");
            return;
        }
        try {
            RetentionResult result = run(false);
            log.info("Ретеншн: удалено {} строк и {} файлов, освобождено {} МБ; "
                            + "свободно на диске {} ГБ",
                    result.deletedRows(), result.deletedFiles(),
                    result.freedBytes() / (1024 * 1024), freeGb(result.freeBytesAfter()));
        } catch (Exception e) {
            log.error("Ретеншн провалился: {}", e.getMessage(), e);
            email.sendNotice("Argus: сбой очистки старых данных",
                    "<p>Ночная очистка старых событий и снимков завершилась ошибкой.</p>"
                            + "<pre>" + escape(e.toString()) + "</pre>");
        }
    }

    /**
     * Прогон очистки. {@code dryRun=true} — только посчитать, ничего не удаляя
     * (для проверки политики и оценки объёма через admin-endpoint).
     */
    public RetentionResult run(boolean dryRun) {
        int retentionDays = Math.max(days, 1);
        LocalDate cutoffDate = LocalDate.now().minusDays(retentionDays);
        LocalDateTime cutoffTs = cutoffDate.atStartOfDay();

        List<Path> dirs = oldDayDirs(cutoffDate);
        int candidateFiles = dirs.stream().mapToInt(this::countFiles).sum();
        long candidateBytes = dirs.stream().mapToLong(this::sizeOf).sum();
        long candidateRows = plateEvents.countByDetectedAtBefore(cutoffTs)
                + alertEvents.countByDetectedAtBefore(cutoffTs);
        long freeBefore = freeBytes();

        if (dryRun) {
            log.info("Ретеншн (dry-run): под удаление {} строк, {} файлов в {} каталогах",
                    candidateRows, candidateFiles, dirs.size());
            return new RetentionResult(true, retentionDays, cutoffDate, candidateRows,
                    0, candidateFiles, candidateBytes, freeBefore, freeBefore, names(dirs));
        }

        int deletedFiles = 0;
        long freedBytes = 0;
        for (Path dir : dirs) {
            long size = sizeOf(dir);
            int files = countFiles(dir);
            if (deleteTree(dir)) {
                deletedFiles += files;
                freedBytes += size;
            } else {
                log.warn("Ретеншн: не удалось удалить каталог {}", dir);
            }
        }

        int deletedRows = deleteInBatches("plate_events",
                () -> plateEvents.deleteBatchOlderThan(cutoffTs, batchSize))
                + deleteInBatches("alert_events",
                () -> alertEvents.deleteBatchOlderThan(cutoffTs, batchSize));

        long freeAfter = freeBytes();
        maybeAlertLowDisk(freeAfter);
        return new RetentionResult(false, retentionDays, cutoffDate, candidateRows,
                deletedRows, deletedFiles, freedBytes, freeBefore, freeAfter, names(dirs));
    }

    private int deleteInBatches(String table, IntSupplier deleteBatch) {
        int total = 0;
        for (int i = 0; i < MAX_BATCHES; i++) {
            int deleted = deleteBatch.getAsInt();
            if (deleted <= 0) {
                break;
            }
            total += deleted;
        }
        if (total >= MAX_BATCHES * batchSize) {
            log.warn("Ретеншн: {} — достигнут предел {} пакетов за прогон, остаток удалится завтра",
                    table, MAX_BATCHES);
        }
        return total;
    }

    /** Каталоги снимков с датой в имени строго раньше отсечки. */
    private List<Path> oldDayDirs(LocalDate cutoff) {
        if (!Files.isDirectory(imagesRoot)) {
            log.warn("Ретеншн: каталог снимков не найден: {}", imagesRoot);
            return List.of();
        }
        List<Path> result = new ArrayList<>();
        try (Stream<Path> children = Files.list(imagesRoot)) {
            children.filter(Files::isDirectory)
                    .filter(path -> {
                        LocalDate day = parseDay(path.getFileName().toString());
                        return day != null && day.isBefore(cutoff);
                    })
                    .sorted()
                    .forEach(result::add);
        } catch (IOException e) {
            log.warn("Ретеншн: не удалось прочитать {}: {}", imagesRoot, e.getMessage());
        }
        return result;
    }

    /** Имя каталога — дата (YYYY-MM-DD). Посторонние каталоги не трогаем. */
    private static LocalDate parseDay(String name) {
        try {
            return LocalDate.parse(name);
        } catch (DateTimeParseException e) {
            return null;
        }
    }

    private boolean deleteTree(Path root) {
        try {
            Files.walkFileTree(root, new SimpleFileVisitor<>() {
                @Override
                public FileVisitResult visitFile(Path file, BasicFileAttributes attrs)
                        throws IOException {
                    Files.deleteIfExists(file);
                    return FileVisitResult.CONTINUE;
                }

                @Override
                public FileVisitResult postVisitDirectory(Path dir, IOException exc)
                        throws IOException {
                    if (exc != null) {
                        throw exc;
                    }
                    Files.deleteIfExists(dir);
                    return FileVisitResult.CONTINUE;
                }
            });
            return true;
        } catch (IOException e) {
            log.warn("Ретеншн: ошибка удаления {}: {}", root, e.getMessage());
            return false;
        }
    }

    private int countFiles(Path dir) {
        try (Stream<Path> walk = Files.walk(dir)) {
            return (int) walk.filter(Files::isRegularFile).count();
        } catch (IOException e) {
            return 0;
        }
    }

    private long sizeOf(Path dir) {
        try (Stream<Path> walk = Files.walk(dir)) {
            return walk.filter(Files::isRegularFile).mapToLong(path -> {
                try {
                    return Files.size(path);
                } catch (IOException e) {
                    return 0L;
                }
            }).sum();
        } catch (IOException e) {
            return 0L;
        }
    }

    /** Свободное место на диске с каталогом снимков; -1 — определить не удалось. */
    private long freeBytes() {
        Path probe = imagesRoot;
        while (probe != null && !Files.exists(probe)) {
            probe = probe.getParent();
        }
        if (probe == null) {
            return -1;
        }
        try {
            return Files.getFileStore(probe).getUsableSpace();
        } catch (IOException e) {
            log.warn("Ретеншн: не удалось узнать свободное место: {}", e.getMessage());
            return -1;
        }
    }

    private void maybeAlertLowDisk(long freeBytes) {
        if (minFreeGb <= 0 || freeBytes < 0 || freeBytes >= minFreeGb * GB) {
            return;
        }
        LocalDate today = LocalDate.now();
        synchronized (this) {
            if (today.equals(lastDiskAlertDay)) {
                return;
            }
            lastDiskAlertDay = today;
        }
        log.warn("Мало места на диске: свободно {} ГБ, порог {} ГБ",
                freeGb(freeBytes), minFreeGb);
        email.sendNotice("Argus: мало места на диске",
                "<p>Свободно <b>" + freeGb(freeBytes) + " ГБ</b> из порога "
                        + minFreeGb + " ГБ на диске со снимками.</p>"
                        + "<p>Каталог: <code>" + escape(imagesRoot.toString()) + "</code></p>"
                        + "<p>Проверь, что ночная очистка отрабатывает, и при необходимости "
                        + "уменьши <code>argus.retention.days</code>.</p>");
    }

    private static long freeGb(long bytes) {
        return bytes < 0 ? -1 : bytes / GB;
    }

    private static List<String> names(List<Path> dirs) {
        return dirs.stream().map(path -> path.getFileName().toString()).toList();
    }

    private static String escape(String value) {
        return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;");
    }
}
