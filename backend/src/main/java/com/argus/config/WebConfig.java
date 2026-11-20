package com.argus.config;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.servlet.config.annotation.ResourceHandlerRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;

@Configuration
public class WebConfig implements WebMvcConfigurer {

    private static final Logger log = LoggerFactory.getLogger(WebConfig.class);

    private final String imagesDir;

    public WebConfig(@Value("${argus.images-dir:data/images}") String imagesDir) {
        this.imagesDir = imagesDir;
    }

    @Override
    public void addResourceHandlers(ResourceHandlerRegistry registry) {
        Path dir = Paths.get(imagesDir).toAbsolutePath().normalize();
        String absolute = dir.toUri().toString();
        // Фото неизменяемы после записи, поэтому им можно долгий кэш: приложение
        // перерисовывает таблицу по событию, и без этого каждая перерисовка
        // заново запрашивает каждую картинку. Статика (app.js, style.css)
        // намеренно остаётся с no-cache — см. spring.web.resources в application.yml,
        // иначе браузер держит старый JS после обновления jar.
        registry.addResourceHandler("/images/**")
                .addResourceLocations(absolute)
                .setCachePeriod(86400);

        // Путь относительный, поэтому зависит от рабочего каталога процесса.
        // Если backend запущен не из backend\, он молча отдаёт 404 на все фото —
        // пишем абсолютный путь при старте, чтобы это было видно сразу.
        log.info("Каталог изображений: {}", dir);
        if (!Files.isDirectory(dir)) {
            log.warn("Каталог изображений не найден: {} — фото в журнале будут недоступны "
                    + "(запускай backend из каталога backend или задай абсолютный argus.images-dir)", dir);
        }
    }
}