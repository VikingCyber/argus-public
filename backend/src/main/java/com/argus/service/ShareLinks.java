package com.argus.service;

import java.nio.charset.StandardCharsets;
import java.util.Locale;

/**
 * Ссылки на снимки через файловую шару (SMB).
 *
 * <p>В письме нельзя дать «кликабельный» путь {@code \\host\share\...} как есть:
 * клиент почты должен получить либо {@code file://host/share/...}, либо обычный
 * URL. Поэтому база шары задаётся в конфиге в любом из двух видов —
 * {@code \\192.168.1.10\images$} или {@code file://192.168.1.10/images$}, —
 * а здесь из неё собираются и href (file-URL), и отображаемый UNC-путь.
 *
 * <p>Базой шары считается каталог, в котором лежат подкаталоги дат, то есть
 * {@code C:\argus\data\images}. Веб-путь из БД ({@code images/<дата>/<файл>})
 * приводится к относительному {@code <дата>/<файл>}.
 */
final class ShareLinks {

    private static final String WEB_PREFIX = "images/";
    /** Символы, которые в file-URL можно оставить как есть (помимо букв/цифр). */
    private static final String KEEP = "-._~!$&'()*+,;=:@";

    private ShareLinks() {
    }

    /** {@code images/2026-10-05/x.jpg} -> {@code 2026-10-05/x.jpg}; null, если путь пуст. */
    static String relativePath(String imageWebPath) {
        if (imageWebPath == null || imageWebPath.isBlank()) {
            return null;
        }
        String path = imageWebPath.trim().replace('\\', '/');
        while (path.startsWith("/")) {
            path = path.substring(1);
        }
        if (path.startsWith(WEB_PREFIX)) {
            path = path.substring(WEB_PREFIX.length());
        }
        return path.isBlank() ? null : path;
    }

    /** {@code \\host\share} + {@code 2026-10-05/x.jpg} -> {@code file://host/share/2026-10-05/x.jpg}. */
    static String fileUrl(String shareBase, String relativePath) {
        String base = normalizeBase(shareBase);
        if (base == null || relativePath == null || relativePath.isBlank()) {
            return null;
        }
        return "file://" + base + "/" + encodePath(relativePath);
    }

    /** {@code \\host\share} + {@code 2026-10-05/x.jpg} -> {@code \\host\share\2026-10-05\x.jpg}. */
    static String uncPath(String shareBase, String relativePath) {
        String base = normalizeBase(shareBase);
        if (base == null || relativePath == null || relativePath.isBlank()) {
            return null;
        }
        return "\\\\" + base.replace('/', '\\') + "\\" + relativePath.replace('/', '\\');
    }

    /**
     * Приводит базу к виду {@code host/share} без схемы и хвостовых слэшей.
     * Понимает и {@code \\host\share}, и {@code file://host/share}.
     */
    private static String normalizeBase(String shareBase) {
        if (shareBase == null || shareBase.isBlank()) {
            return null;
        }
        String base = shareBase.trim().replace('\\', '/');
        String lower = base.toLowerCase(Locale.ROOT);
        if (lower.startsWith("file://")) {
            base = base.substring("file://".length());
        }
        while (base.startsWith("/")) {
            base = base.substring(1);
        }
        while (base.endsWith("/")) {
            base = base.substring(0, base.length() - 1);
        }
        return base.isBlank() ? null : base;
    }

    /**
     * Процентное кодирование UTF-8 для пути URL. Слэши остаются разделителями,
     * {@code $} не кодируется — иначе имя шары {@code images$} превратится в
     * {@code images%24} и SMB-путь не откроется. Пробелы и кириллица кодируются.
     */
    private static String encodePath(String path) {
        StringBuilder out = new StringBuilder(path.length() + 16);
        String[] segments = path.replace('\\', '/').split("/", -1);
        for (int i = 0; i < segments.length; i++) {
            if (i > 0) {
                out.append('/');
            }
            for (byte b : segments[i].getBytes(StandardCharsets.UTF_8)) {
                int c = b & 0xFF;
                char ch = (char) c;
                boolean plain = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z')
                        || (c >= '0' && c <= '9') || KEEP.indexOf(ch) >= 0;
                if (plain) {
                    out.append(ch);
                } else {
                    out.append('%')
                            .append(Character.toUpperCase(Character.forDigit(c >> 4, 16)))
                            .append(Character.toUpperCase(Character.forDigit(c & 0xF, 16)));
                }
            }
        }
        return out.toString();
    }
}
