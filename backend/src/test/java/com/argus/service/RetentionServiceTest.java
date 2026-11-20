package com.argus.service;

import com.argus.dto.RetentionResult;
import com.argus.repository.AlertEventRepository;
import com.argus.repository.PlateEventRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.LocalDate;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class RetentionServiceTest {

    private static final int DAYS = 90;

    @TempDir
    Path images;

    private Path oldDir;
    private Path freshDir;
    private Path strayDir;

    private PlateEventRepository plateEvents;
    private AlertEventRepository alertEvents;
    private EmailNotificationService email;

    @BeforeEach
    void setUp() throws IOException {
        oldDir = dayDir(LocalDate.now().minusDays(DAYS + 1));
        freshDir = dayDir(LocalDate.now().minusDays(1));
        strayDir = images.resolve("not-a-date");
        Files.createDirectories(strayDir);

        plateEvents = mock(PlateEventRepository.class);
        alertEvents = mock(AlertEventRepository.class);
        email = mock(EmailNotificationService.class);
        when(plateEvents.countByDetectedAtBefore(any())).thenReturn(5L);
        when(alertEvents.countByDetectedAtBefore(any())).thenReturn(2L);
        // Первый пакет что-то удаляет, второй — уже пусто: цикл останавливается.
        when(plateEvents.deleteBatchOlderThan(any(), anyInt())).thenReturn(3, 0);
        when(alertEvents.deleteBatchOlderThan(any(), anyInt())).thenReturn(2, 0);
    }

    @Test
    void deletesOnlyDaysOlderThanCutoff() {
        RetentionResult result = service().run(false);

        assertFalse(result.dryRun());
        assertFalse(Files.exists(oldDir), "старый каталог должен быть удалён");
        assertTrue(Files.exists(freshDir), "свежий каталог трогать нельзя");
        assertTrue(Files.exists(strayDir), "каталоги не-даты трогать нельзя");
        assertEquals(1, result.deletedFiles());
        assertEquals(5, result.deletedRows());
        assertEquals(LocalDate.now().minusDays(DAYS), result.cutoff());
        verify(plateEvents, times(2)).deleteBatchOlderThan(any(), anyInt());
        verify(alertEvents, times(2)).deleteBatchOlderThan(any(), anyInt());
    }

    @Test
    void dryRunChangesNothing() {
        RetentionResult result = service().run(true);

        assertTrue(result.dryRun());
        assertTrue(Files.exists(oldDir));
        assertTrue(result.directories().contains(oldDir.getFileName().toString()));
        assertEquals(0, result.deletedRows());
        assertEquals(1, result.deletedFiles());
        verify(plateEvents, never()).deleteBatchOlderThan(any(), anyInt());
        verify(alertEvents, never()).deleteBatchOlderThan(any(), anyInt());
    }

    @Test
    void keepsDirectoryOfCutoffDay() throws IOException {
        Path cutoffDay = dayDir(LocalDate.now().minusDays(DAYS));
        Path dayBefore = dayDir(LocalDate.now().minusDays(DAYS + 2));

        service().run(false);

        assertTrue(Files.exists(cutoffDay), "день отсечки ещё хранится");
        assertFalse(Files.exists(dayBefore));
    }

    private RetentionService service() {
        // minFreeGb=0: в тесте не хотим зависеть от свободного места на машине.
        return new RetentionService(plateEvents, alertEvents, email,
                images.toString(), true, DAYS, 0, 500);
    }

    private Path dayDir(LocalDate day) throws IOException {
        Path dir = images.resolve(day.toString());
        Files.createDirectories(dir);
        Files.writeString(dir.resolve("cam-01-vehicle-120000-000.jpg"), "jpeg");
        return dir;
    }
}
