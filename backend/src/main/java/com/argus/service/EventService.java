package com.argus.service;

import com.argus.entity.AlertEvent;
import com.argus.entity.Camera;
import com.argus.entity.PlateEvent;
import com.argus.entity.Vehicle;
import com.argus.repository.AlertEventRepository;
import com.argus.repository.CameraRepository;
import com.argus.repository.PlateEventRepository;
import com.argus.repository.VehicleRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.multipart.MultipartFile;

import java.io.IOException;
import java.time.LocalDateTime;
import java.util.List;

@Service
public class EventService {

    private static final Logger log = LoggerFactory.getLogger(EventService.class);

    /** Окно дедупликации UNKNOWN_PLATE (мин): повторный алерт по той же камере не шлём. */
    @Value("${argus.alerts.unknown-dedup-minutes:15}")
    private int unknownDedupMinutes;

    private final CameraRepository cameraRepository;
    private final PlateEventRepository plateEventRepository;
    private final AlertEventRepository alertEventRepository;
    private final VehicleRepository vehicleRepository;
    private final ImageStorageService imageStorageService;
    private final EmailNotificationService emailNotificationService;

    public EventService(CameraRepository cameraRepository,
                        PlateEventRepository plateEventRepository,
                        AlertEventRepository alertEventRepository,
                        VehicleRepository vehicleRepository,
                        ImageStorageService imageStorageService,
                        EmailNotificationService emailNotificationService) {
        this.cameraRepository = cameraRepository;
        this.plateEventRepository = plateEventRepository;
        this.alertEventRepository = alertEventRepository;
        this.vehicleRepository = vehicleRepository;
        this.imageStorageService = imageStorageService;
        this.emailNotificationService = emailNotificationService;
    }

    @Transactional
    public Camera registerCamera(String cameraId, String cameraName) {
        Camera camera = cameraRepository.findById(cameraId).orElse(null);
        if (camera == null) {
            camera = new Camera(cameraId, cameraName != null ? cameraName : cameraId, LocalDateTime.now());
            cameraRepository.save(camera);
            log.info("Зарегистрирована камера: {} ({})", cameraId, camera.getName());
        } else if (cameraName != null && !cameraName.isBlank()) {
            camera.setName(cameraName);
            cameraRepository.save(camera);
        }
        return camera;
    }

    @Transactional
    public PlateEvent savePlate(String cameraId, String plateNumber, Double confidence,
                                Long trackId, LocalDateTime detectedAt, MultipartFile image) {
        String imagePath = saveImage(image, cameraId, "plate");
        String normalized = PlateNormalizer.normalize(plateNumber);

        PlateEvent event = new PlateEvent(cameraId, normalized, confidence, detectedAt, imagePath);
        event.setTrackId(trackId);
        boolean known = vehicleRepository.findAll().stream()
                .anyMatch(v -> PlateNormalizer.normalize(v.getPlateNumber()).equals(normalized));
        Vehicle fuzzyVehicle = known ? null : findUniqueFuzzyMatch(normalized);
        event.setDecision(known ? "ALLOWED" : fuzzyVehicle != null ? "ALLOWED_FUZZY" : "DENIED");
        plateEventRepository.save(event);

        if (known) {
            log.info("Проезд: номер {} с камеры {} (conf={}) — свой транспорт",
                    normalized, cameraId, confidence);
        } else if (fuzzyVehicle != null) {
            log.info("Проезд: номер {} с камеры {} — совпал с {} с одной ошибкой OCR",
                    normalized, cameraId, fuzzyVehicle.getPlateNumber());
        } else if (hasRecentUnknownAlert(cameraId, normalized)) {
            log.info("Проезд: номер {} с камеры {} — чужой (алерт по этому номеру уже был недавно, повтор пропущен)",
                    normalized, cameraId);
        } else {
            log.warn("Проезд: номер {} с камеры {} — НЕТ в белом списке", normalized, cameraId);
            AlertEvent alert = new AlertEvent(cameraId, "UNKNOWN_PLATE", detectedAt, imagePath);
            alert.setTrackId(trackId);
            alert.setPlateNumber(normalized);
            alertEventRepository.save(alert);
            notifyAlert(cameraId, "UNKNOWN_PLATE", detectedAt, imagePath);
        }
        return event;
    }

    /** Нечёткое совпадение разрешаем только при единственном кандидате. */
    private Vehicle findUniqueFuzzyMatch(String plate) {
        List<Vehicle> candidates = vehicleRepository.findAll().stream()
                .filter(v -> levenshtein(plate, v.getPlateNumber()) <= 1)
                .toList();
        return candidates.size() == 1 ? candidates.get(0) : null;
    }

    private int levenshtein(String left, String right) {
        int[] previous = new int[right.length() + 1];
        for (int j = 0; j <= right.length(); j++) previous[j] = j;
        for (int i = 1; i <= left.length(); i++) {
            int[] current = new int[right.length() + 1];
            current[0] = i;
            for (int j = 1; j <= right.length(); j++) {
                int cost = left.charAt(i - 1) == right.charAt(j - 1) ? 0 : 1;
                current[j] = Math.min(Math.min(current[j - 1] + 1, previous[j] + 1),
                        previous[j - 1] + cost);
            }
            previous = current;
        }
        return previous[right.length()];
    }

    /** true, если по этой камере (а для номерных — по этому номеру) алерт уже был в окне дедупликации. */
    private boolean hasRecentUnknownAlert(String cameraId, String plate) {
        if (plate != null && !plate.isBlank()) {
            return alertEventRepository
                    .findFirstByCameraIdAndAlertTypeAndPlateNumberOrderByDetectedAtDesc(
                            cameraId, "UNKNOWN_PLATE", plate)
                    .map(AlertEvent::getDetectedAt)
                    .map(t -> t.isAfter(LocalDateTime.now().minusMinutes(unknownDedupMinutes)))
                    .orElse(false);
        }
        return alertEventRepository
                .findFirstByCameraIdAndAlertTypeOrderByDetectedAtDesc(cameraId, "UNKNOWN_PLATE")
                .map(a -> a.getDetectedAt())
                .map(t -> t.isAfter(LocalDateTime.now().minusMinutes(unknownDedupMinutes)))
                .orElse(false);
    }

    @Transactional
    public AlertEvent saveAlert(String cameraId, String alertType, Long trackId,
                                LocalDateTime detectedAt, MultipartFile image) {
        String imagePath = saveImage(image, cameraId, "alert");
        AlertEvent event = new AlertEvent(cameraId, alertType, detectedAt, imagePath);
        event.setTrackId(trackId);
        alertEventRepository.save(event);
        log.info("Тревога {} с камеры {}", alertType, cameraId);
        notifyAlert(cameraId, alertType, detectedAt, imagePath);
        return event;
    }

    private void notifyAlert(String cameraId, String alertType, LocalDateTime detectedAt, String imagePath) {
        Camera camera = cameraRepository.findById(cameraId).orElse(null);
        // Вкладывать фото больше не будем — в письме путь к файлу (клик открывает).
        emailNotificationService.sendAlert(
                alertType, cameraId, camera != null ? camera.getName() : null, detectedAt,
                imagePath, imageStorageService.localPath(imagePath));
    }

    private String saveImage(MultipartFile image, String cameraId, String prefix) {
        if (image == null || image.isEmpty()) {
            return null;
        }
        try {
            return imageStorageService.save(image, cameraId, prefix);
        } catch (IOException e) {
            log.warn("Не удалось сохранить изображение: {}", e.getMessage());
            return null;
        }
    }
}
