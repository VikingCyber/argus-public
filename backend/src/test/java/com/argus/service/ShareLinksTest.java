package com.argus.service;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;

/**
 * Ссылка на снимок в письме: из веб-пути БД и базы шары должны получаться
 * и кликабельный file-URL, и UNC-путь для отображения.
 */
class ShareLinksTest {

    private static final String WEB_PATH =
            "images/2026-10-05/cam-01-alert-20261005-162013-521.jpg";

    @Test
    void stripsImagesPrefix() {
        assertEquals("2026-10-05/cam-01-alert-20261005-162013-521.jpg",
                ShareLinks.relativePath(WEB_PATH));
        assertEquals("2026-10-05/x.jpg", ShareLinks.relativePath("/images/2026-10-05/x.jpg"));
        assertEquals("2026-10-05/x.jpg", ShareLinks.relativePath("images\\2026-10-05\\x.jpg"));
        assertNull(ShareLinks.relativePath(null));
        assertNull(ShareLinks.relativePath("  "));
        assertNull(ShareLinks.relativePath("images/"));
    }

    @Test
    void buildsFileUrlFromUncBase() {
        assertEquals(
                "file://192.168.1.10/images$/2026-10-05/cam-01-alert-20261005-162013-521.jpg",
                ShareLinks.fileUrl("\\\\192.168.1.10\\images$", ShareLinks.relativePath(WEB_PATH)));
    }

    @Test
    void buildsUncPathForDisplay() {
        assertEquals(
                "\\\\192.168.1.10\\images$\\2026-10-05\\cam-01-alert-20261005-162013-521.jpg",
                ShareLinks.uncPath("\\\\192.168.1.10\\images$", ShareLinks.relativePath(WEB_PATH)));
    }

    @Test
    void acceptsFileSchemeAndTrailingSlash() {
        String expected =
                "file://192.168.1.10/images$/2026-10-05/cam-01-alert-20261005-162013-521.jpg";
        assertEquals(expected, ShareLinks.fileUrl(
                "file://192.168.1.10/images$/", ShareLinks.relativePath(WEB_PATH)));
        assertEquals(expected, ShareLinks.fileUrl(
                "\\\\192.168.1.10\\images$\\", ShareLinks.relativePath(WEB_PATH)));
    }

    @Test
    void encodesSpacesAndCyrillicInHref() {
        String href = ShareLinks.fileUrl("\\\\srv\\share",
                ShareLinks.relativePath("images/2026-10-05/фото 1.jpg"));
        assertEquals("file://srv/share/2026-10-05/%D1%84%D0%BE%D1%82%D0%BE%201.jpg", href);
    }

    @Test
    void returnsNullWhenBaseIsNotConfigured() {
        assertNull(ShareLinks.fileUrl("", ShareLinks.relativePath(WEB_PATH)));
        assertNull(ShareLinks.fileUrl(null, ShareLinks.relativePath(WEB_PATH)));
        assertNull(ShareLinks.uncPath("  ", ShareLinks.relativePath(WEB_PATH)));
    }
}
