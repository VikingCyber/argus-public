package com.argus.controller;

import com.argus.dto.JournalEntry;
import com.argus.dto.JournalFilter;
import com.argus.dto.JournalSummary;
import com.argus.dto.PageResponse;
import com.argus.service.JournalService;
import org.springframework.format.annotation.DateTimeFormat;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.time.LocalDateTime;
import java.util.List;

/**
 * Журнал одним списком: номера и тревоги, пагинация, фильтры, выгрузка CSV.
 *
 * Раньше здесь были /plates и /alerts с одним только limit — дальше первой
 * сотни записей добраться было нельзя, и интерфейс показывал ровно то,
 * что эти два метода вернули.
 */
@RestController
@RequestMapping("/api/v1")
public class JournalController {

    private final JournalService journalService;

    public JournalController(JournalService journalService) {
        this.journalService = journalService;
    }

    @GetMapping("/journal")
    public PageResponse<JournalEntry> journal(
            @RequestParam(defaultValue = "0") int page,
            @RequestParam(required = false) Integer size,
            @RequestParam(required = false) String cameraId,
            /** PLATE или ALERT; пусто — оба вида. */
            @RequestParam(required = false) String kind,
            /** Тип тревоги (например NIGHT_PERSON). */
            @RequestParam(required = false) String type,
            /** Поиск по номеру и типу тревоги. */
            @RequestParam(required = false) String q,
            /** ALLOWED / ALLOWED_FUZZY / DENIED. */
            @RequestParam(required = false) String decision,
            @RequestParam(required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) LocalDateTime from,
            @RequestParam(required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) LocalDateTime to) {
        JournalFilter filter = new JournalFilter(cameraId, kind, type, q, decision, from, to);
        int effectiveSize = size == null ? JournalService.DEFAULT_PAGE_SIZE : size;
        return journalService.page(filter, page, effectiveSize);
    }

    @GetMapping("/journal/export.csv")
    public ResponseEntity<byte[]> exportCsv(
            @RequestParam(required = false) String cameraId,
            @RequestParam(required = false) String kind,
            @RequestParam(required = false) String type,
            @RequestParam(required = false) String q,
            @RequestParam(required = false) String decision,
            @RequestParam(required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) LocalDateTime from,
            @RequestParam(required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) LocalDateTime to) {
        JournalFilter filter = new JournalFilter(cameraId, kind, type, q, decision, from, to);
        List<JournalEntry> rows = journalService.forExport(filter);
        String csv = toCsv(rows);

        // Имя файла с датой выгрузки — иначе браузер предложит «journal.csv»
        // и следующая выгрузка перезапишет предыдущую.
        String fileName = "argus-journal-" + java.time.LocalDate.now() + ".csv";
        return ResponseEntity.ok()
                .header(HttpHeaders.CONTENT_DISPOSITION, "attachment; filename=\"" + fileName + "\"")
                .contentType(new MediaType("text", "csv", java.nio.charset.StandardCharsets.UTF_8))
                .body(("\uFEFF" + csv).getBytes(java.nio.charset.StandardCharsets.UTF_8));
    }

    @GetMapping("/stats/summary")
    public JournalSummary summary(
            @RequestParam(required = false) String cameraId,
            @RequestParam(required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) LocalDateTime from,
            @RequestParam(required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) LocalDateTime to,
            /** Сколько дней показывать в ряду, если период не задан явно. */
            @RequestParam(defaultValue = "14") int days) {
        JournalFilter filter = new JournalFilter(cameraId, null, null, null, null, from, to);
        return journalService.summary(filter, from, to, days);
    }

    private String toCsv(List<JournalEntry> rows) {
        StringBuilder sb = new StringBuilder();
        sb.append("время;вид;камера;номер;статус;тип тревоги;уверенность;проезд;фото\n");
        for (JournalEntry e : rows) {
            sb.append(csv(e.detectedAt() == null ? "" : e.detectedAt().toString().replace('T', ' ')))
                    .append(';')
                    .append("PLATE".equals(e.kind()) ? "номер" : "тревога")
                    .append(';')
                    .append(csv(e.cameraId()))
                    .append(';')
                    .append(csv(e.plateNumber()))
                    .append(';')
                    .append(csv(e.decision()))
                    .append(';')
                    .append(csv(e.alertType()))
                    .append(';')
                    .append(e.confidence() == null ? "" : String.format(java.util.Locale.ROOT, "%.3f", e.confidence()))
                    .append(';')
                    .append(e.trackId() == null ? "" : "#" + e.trackId())
                    .append(';')
                    .append(csv(e.imagePath()))
                    .append('\n');
        }
        return sb.toString();
    }

    /** В CSV попадает содержимое полей: кавычками и переводами строк не заменить. */
    private String csv(String value) {
        if (value == null || value.isEmpty()) {
            return "";
        }
        return "\"" + value.replace("\"", "\"\"").replace("\r", " ").replace("\n", " ") + "\"";
    }
}
