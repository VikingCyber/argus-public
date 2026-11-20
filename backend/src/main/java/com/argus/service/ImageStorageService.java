package com.argus.service;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.web.multipart.MultipartFile;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.time.LocalDate;
import java.time.format.DateTimeFormatter;

@Service
public class ImageStorageService {

    private static final DateTimeFormatter DAY = DateTimeFormatter.ofPattern("yyyy-MM-dd");
    private static final DateTimeFormatter TS = DateTimeFormatter.ofPattern("yyyyMMdd-HHmmss-SSS");

    private final Path root;

    public ImageStorageService(@Value("${argus.images-dir:data/images}") String imagesDir) {
        this.root = Paths.get(imagesDir).toAbsolutePath().normalize();
    }

    /**
     * Сохраняет файл в подпапку по дате. Возвращает относительный путь для веба: images/2026-08-19/cam-01-plate-....jpg
     */
    public String save(MultipartFile file, String cameraId, String prefix) throws IOException {
        Files.createDirectories(root);
        Path dayDir = root.resolve(LocalDate.now().format(DAY));
        Files.createDirectories(dayDir);
        String filename = cameraId + "-" + prefix + "-" + java.time.LocalDateTime.now().format(TS) + ".jpg";
        Path target = dayDir.resolve(filename);
        file.transferTo(target.toAbsolutePath());
        return "images/" + dayDir.getFileName() + "/" + filename;
    }

    /**
     * Абсолютный путь файла на диске по веб-относительному пути (images/2026-10-26/x.jpg).
     * Нужен для письма: вкладывать фото не будем, отдаём путь — по клику открывается.
     */
    public String localPath(String webPath) {
        if (webPath == null || webPath.isBlank()) {
            return null;
        }
        if (webPath.startsWith("images/")) {
            webPath = webPath.substring("images/".length());
        }
        return root.resolve(webPath).normalize().toString();
    }
}