package com.argus.controller;

import com.argus.entity.AlertEvent;
import com.argus.entity.Camera;
import com.argus.entity.PlateEvent;
import com.argus.repository.AlertEventRepository;
import com.argus.repository.CameraRepository;
import com.argus.repository.PlateEventRepository;
import com.argus.repository.VehicleRepository;
import com.argus.service.EventService;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;

import java.time.LocalDateTime;
import java.time.format.DateTimeParseException;
import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api/v1")
public class EventController {

    private final EventService eventService;
    private final CameraRepository cameraRepository;
    private final PlateEventRepository plateEventRepository;
    private final AlertEventRepository alertEventRepository;
    private final VehicleRepository vehicleRepository;

    public EventController(EventService eventService,
                           CameraRepository cameraRepository,
                           PlateEventRepository plateEventRepository,
                           AlertEventRepository alertEventRepository,
                           VehicleRepository vehicleRepository) {
        this.eventService = eventService;
        this.cameraRepository = cameraRepository;
        this.plateEventRepository = plateEventRepository;
        this.alertEventRepository = alertEventRepository;
        this.vehicleRepository = vehicleRepository;
    }

    @PostMapping("/events/plates")
    public ResponseEntity<PlateEvent> savePlate(
            @RequestParam String cameraId,
            @RequestParam String plateNumber,
            @RequestParam(required = false) Double confidence,
            @RequestParam(required = false) Long trackId,
            @RequestParam(required = false) String detectedAt,
            @RequestParam(required = false) String cameraName,
            @RequestParam(required = false) MultipartFile image) {
        if (plateNumber.isBlank() || cameraId.isBlank()) {
            return ResponseEntity.badRequest().build();
        }
        eventService.registerCamera(cameraId, cameraName);
        PlateEvent event = eventService.savePlate(cameraId, plateNumber, confidence, trackId,
                parseTime(detectedAt), image);
        return ResponseEntity.status(HttpStatus.CREATED).body(event);
    }

    @PostMapping("/events/alerts")
    public ResponseEntity<AlertEvent> saveAlert(
            @RequestParam String cameraId,
            @RequestParam String alertType,
            @RequestParam(required = false) Long trackId,
            @RequestParam(required = false) String detectedAt,
            @RequestParam(required = false) String cameraName,
            @RequestParam(required = false) MultipartFile image) {
        if (cameraId.isBlank() || alertType.isBlank()) {
            return ResponseEntity.badRequest().build();
        }
        eventService.registerCamera(cameraId, cameraName);
        AlertEvent event = eventService.saveAlert(cameraId, alertType, trackId, parseTime(detectedAt), image);
        return ResponseEntity.status(HttpStatus.CREATED).body(event);
    }

    @GetMapping("/cameras")
    public List<Camera> getCameras() {
        return cameraRepository.findAll();
    }

    /** Простые итоги с момента установки. Сводка за период — /stats/summary. */
    @GetMapping("/stats")
    public Map<String, Object> getStats() {
        return Map.of(
                "platesTotal", plateEventRepository.count(),
                "platesUnique", plateEventRepository.countDistinctPlateNumber(),
                "alertsTotal", alertEventRepository.count(),
                "vehiclesTotal", vehicleRepository.count()
        );
    }

    private LocalDateTime parseTime(String value) {
        if (value == null || value.isBlank()) {
            return LocalDateTime.now();
        }
        try {
            return LocalDateTime.parse(value);
        } catch (DateTimeParseException e) {
            return LocalDateTime.now();
        }
    }
}