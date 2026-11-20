package com.argus.service;

import jakarta.mail.internet.MimeMessage;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.mail.javamail.JavaMailSender;
import org.springframework.mail.javamail.MimeMessageHelper;
import org.springframework.scheduling.annotation.Async;
import org.springframework.stereotype.Service;

import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayDeque;
import java.util.Deque;
import java.util.Map;
import java.util.Set;

/**
 * Отправка уведомлений о тревогах на email. Отключена, пока не настроен SMTP.
 */
@Service
public class EmailNotificationService {

    private static final Logger log = LoggerFactory.getLogger(EmailNotificationService.class);
    private static final DateTimeFormatter TS = DateTimeFormatter.ofPattern("dd.MM.yyyy HH:mm:ss");

    /** Человекочитаемые названия типов событий для писем. */
    private static final Map<String, String> TITLES = Map.of(
            "NIGHT_PERSON", "Человек на территории ночью",
            "YELLOW_VEST", "Человек в сигнальном жилете",
            "VEHICLE_SEEN", "Обнаружено транспортное средство",
            "UNKNOWN_PLATE", "Проезд незнакомого транспортного средства",
            "UNREAD_PLATE", "Транспортное средство без распознанного номера");

    /** Информационные типы (не тревоги): другой заголовок письма. */
    private static final Set<String> INFO_TYPES = Set.of("VEHICLE_SEEN");

    private final JavaMailSender mailSender;

    @Value("${argus.notifications.email.enabled:false}")
    private boolean enabled;

    @Value("${argus.notifications.email.from:}")
    private String from;

    @Value("${argus.notifications.email.to:}")
    private String to;

    @Value("${argus.notifications.email.subject-prefix:Argus}")
    private String subjectPrefix;

    /** Базовый адрес, по которому доступны фото (argus.public-base-url / ARGUS_PUBLIC_BASE_URL).
     *  Если заполнен (напр. http://192.168.1.10:8080), в письме формируется кликабельная
     *  ссылка на снимок. Если пуст — веб-ссылки в письме нет. */
    @Value("${argus.public-base-url:}")
    private String publicBaseUrl;

    /** База файловой шары с каталогом снимков (argus.file-base-url / ARGUS_FILE_BASE_URL),
     *  напр. {@code \\192.168.1.10\images$} или {@code file://192.168.1.10/images$}.
     *  Если заполнена, в письме будет кликабельный UNC-путь (file:-ссылка). */
    @Value("${argus.file-base-url:}")
    private String fileBaseUrl;

    /** Максимум писем в час (страховка от шторма). 0 или меньше = без лимита. */
    @Value("${argus.notifications.email.rate-limit-per-hour:10}")
    private int rateLimitPerHour;

    /** Время отправки последних писем (для часового лимита), защищено synchronized. */
    private final Deque<LocalDateTime> sentWindow = new ArrayDeque<>();

    public EmailNotificationService(JavaMailSender mailSender) {
        this.mailSender = mailSender;
    }

    @Async
    public void sendAlert(String alertType, String cameraId, String cameraName,
                          LocalDateTime detectedAt, String imageWebPath, String imageLocalPath) {
        if (!enabled) {
            log.warn("Email-уведомления выключены (MAIL_ENABLED != true). Тревога {} по камере {} не отправлена",
                    alertType, cameraId);
            return;
        }
        if (from.isBlank() || to.isBlank()) {
            log.warn("Email-уведомления включены, но не задан отправитель/получатель (MAIL_FROM/MAIL_TO). "
                    + "Тревога {} по камере {} не отправлена", alertType, cameraId);
            return;
        }
        if (!tryAcquireRateLimit()) {
            log.warn("Достигнут лимит писем ({} в час) — тревога {} по камере {} не отправлена",
                    rateLimitPerHour, alertType, cameraId);
            return;
        }
        try {
            MimeMessage mime = mailSender.createMimeMessage();
            MimeMessageHelper helper = new MimeMessageHelper(mime, false, "UTF-8");
            helper.setFrom(from);
            // MAIL_TO может содержать несколько адресов через запятую/точку с запятой;
            // обрезаем пробелы вокруг каждого — "a@x.ru, b@x.ru" сильнее, чем кажется.
            String[] recipients = java.util.Arrays.stream(to.split("[,;]"))
                    .map(String::trim)
                    .filter(s -> !s.isEmpty())
                    .toArray(String[]::new);
            helper.setTo(recipients);
            helper.setSubject(String.format("%s: %s — камера %s",
                    subjectPrefix, title(alertType), cameraId));
            helper.setText(buildHtml(alertType, cameraId, cameraName, detectedAt,
                    imageWebPath, imageLocalPath), true);
            mailSender.send(mime);
            log.info("Email-уведомление о тревоге {} отправлено на {}", alertType, to);
        } catch (Exception e) {
            log.warn("Не удалось отправить email о тревоге {}: {}", alertType, e.getMessage());
        }
    }

    /**
     * Служебное письмо (не тревога): отчёт ретеншна, мало места на диске.
     * Часовой лимит тревог не расходует — иначе ночной отчёт «съел» бы слот,
     * а в пиковый час тревога не ушла бы.
     */
    @Async
    public void sendNotice(String subject, String htmlBody) {
        if (!enabled) {
            log.warn("Email-уведомления выключены (MAIL_ENABLED != true) — служебное письмо "
                    + "\"{}\" не отправлено", subject);
            return;
        }
        if (from.isBlank() || to.isBlank()) {
            log.warn("Не задан MAIL_FROM/MAIL_TO — служебное письмо \"{}\" не отправлено", subject);
            return;
        }
        try {
            MimeMessage mime = mailSender.createMimeMessage();
            MimeMessageHelper helper = new MimeMessageHelper(mime, false, "UTF-8");
            helper.setFrom(from);
            helper.setTo(recipients());
            helper.setSubject(subjectPrefix + ": " + subject);
            helper.setText("<html><body style=\"font-family: Arial, sans-serif;\">"
                    + htmlBody + "</body></html>", true);
            mailSender.send(mime);
            log.info("Служебное письмо \"{}\" отправлено на {}", subject, to);
        } catch (Exception e) {
            log.warn("Не удалось отправить служебное письмо \"{}\": {}", subject, e.getMessage());
        }
    }

    /** Получатели из MAIL_TO: несколько адресов через запятую или точку с запятой. */
    private String[] recipients() {
        return java.util.Arrays.stream(to.split("[,;]"))
                .map(String::trim)
                .filter(s -> !s.isEmpty())
                .toArray(String[]::new);
    }

    /** Резервирует слот часового лимита. @Async: вызовы из разных потоков. */
    private synchronized boolean tryAcquireRateLimit() {
        if (rateLimitPerHour <= 0) {
            return true;
        }
        LocalDateTime hourAgo = LocalDateTime.now().minusHours(1);
        while (!sentWindow.isEmpty() && sentWindow.peekFirst().isBefore(hourAgo)) {
            sentWindow.pollFirst();
        }
        if (sentWindow.size() >= rateLimitPerHour) {
            return false;
        }
        sentWindow.addLast(LocalDateTime.now());
        return true;
    }

    private static String title(String alertType) {
        return TITLES.getOrDefault(alertType, alertType);
    }

    private String buildHtml(String alertType, String cameraId, String cameraName,
                             LocalDateTime detectedAt, String imageWebPath, String imageLocalPath) {
        String heading = INFO_TYPES.contains(alertType)
                ? "Argus: наблюдение"
                : "Argus: тревога";
        // Снимок не вкладываем (забили бы почту). Вместо вложения — ссылка по клику
        // (если задан ARGUS_PUBLIC_BASE_URL) и/или локальный путь к файлу на сервере.
        // Снимок не вкладываем (забили бы почту). Вместо вложения — ссылки:
        // на файл в шаре (кликабельный UNC) и/или веб-ссылка, плюс локальный путь.
        String rel = ShareLinks.relativePath(imageWebPath);
        String shareHref = ShareLinks.fileUrl(fileBaseUrl, rel);
        String shareShown = ShareLinks.uncPath(fileBaseUrl, rel);
        String webHref = webLink(imageWebPath);

        StringBuilder photo = new StringBuilder();
        if (shareHref != null) {
            // href = file://host/share/..., текст = \\host\share\... — в Outlook
            // и Thunderbird открывается по клику, в веб-почте путь остаётся
            // видимым и его можно скопировать.
            photo.append("""
                    <tr><td valign="top"><b>Снимок:</b></td>
                    <td><a href="%s" style="font-family:Consolas,monospace;font-size:80%%">%s</a></td></tr>
                    """.formatted(escapeHtml(shareHref), escapeHtml(shareShown)));
        }
        if (webHref != null) {
            photo.append("""
                    <tr><td valign="top"><b>Снимок (веб):</b></td>
                    <td><a href="%s">открыть по ссылке</a></td></tr>
                    """.formatted(escapeHtml(webHref)));
        }
        if (imageLocalPath != null && !imageLocalPath.isBlank()) {
            photo.append("""
                    <tr><td valign="top"><b>Файл на сервере:</b></td>
                    <td style="font-family:Consolas,monospace;font-size:80%%;color:#666">%s</td></tr>
                    """.formatted(escapeHtml(imageLocalPath)));
        }
        String photoHtml = photo.toString();
        return """
                <html><body style="font-family: Arial, sans-serif;">
                <h2>%s</h2>
                <table>
                <tr><td><b>Тип:</b></td><td>%s (%s)</td></tr>
                <tr><td><b>Камера:</b></td><td>%s (%s)</td></tr>
                <tr><td><b>Время:</b></td><td>%s</td></tr>
                %s
                </table>
                </body></html>
                """.formatted(
                heading,
                title(alertType),
                alertType,
                cameraId,
                cameraName == null ? "—" : cameraName,
                detectedAt.format(TS),
                photoHtml);
    }

    /** Публичная ссылка на снимок либо null, если базовый адрес не настроен. */
    private String webLink(String imageWebPath) {
        if (publicBaseUrl.isBlank() || imageWebPath == null || imageWebPath.isBlank()) {
            return null;
        }
        String base = publicBaseUrl.endsWith("/")
                ? publicBaseUrl.substring(0, publicBaseUrl.length() - 1)
                : publicBaseUrl;
        return base + "/" + imageWebPath;
    }

    private static String escapeHtml(String value) {
        return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("\"", "&quot;");
    }
}