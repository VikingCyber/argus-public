package com.argus.entity;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Index;
import jakarta.persistence.Table;

import java.time.LocalDateTime;

@Entity
@Table(name = "alert_events", indexes = {
        @Index(name = "idx_alert_events_camera", columnList = "camera_id"),
        @Index(name = "idx_alert_events_type", columnList = "alert_type"),
        @Index(name = "idx_alert_events_detected", columnList = "detected_at")
})
public class AlertEvent {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "camera_id", length = 64, nullable = false)
    private String cameraId;

    @Column(name = "alert_type", length = 32, nullable = false)
    private String alertType;

    /** Идентификатор трека ТС в вижене (для тревог по ТС: UNREAD_PLATE). */
    @Column(name = "track_id")
    private Long trackId;

    /** Номер ТС, если тревога привязана к конкретному номеру (UNKNOWN_PLATE). */
    @Column(name = "plate_number", length = 16)
    private String plateNumber;

    @Column(name = "detected_at", nullable = false)
    private LocalDateTime detectedAt;

    @Column(name = "image_path", length = 512)
    private String imagePath;

    public AlertEvent() {
    }

    public AlertEvent(String cameraId, String alertType, LocalDateTime detectedAt, String imagePath) {
        this.cameraId = cameraId;
        this.alertType = alertType;
        this.detectedAt = detectedAt;
        this.imagePath = imagePath;
    }

    public Long getId() {
        return id;
    }

    public void setId(Long id) {
        this.id = id;
    }

    public String getCameraId() {
        return cameraId;
    }

    public void setCameraId(String cameraId) {
        this.cameraId = cameraId;
    }

    public String getAlertType() {
        return alertType;
    }

    public void setAlertType(String alertType) {
        this.alertType = alertType;
    }

    public Long getTrackId() {
        return trackId;
    }

    public void setTrackId(Long trackId) {
        this.trackId = trackId;
    }

    public String getPlateNumber() {
        return plateNumber;
    }

    public void setPlateNumber(String plateNumber) {
        this.plateNumber = plateNumber;
    }

    public LocalDateTime getDetectedAt() {
        return detectedAt;
    }

    public void setDetectedAt(LocalDateTime detectedAt) {
        this.detectedAt = detectedAt;
    }

    public String getImagePath() {
        return imagePath;
    }

    public void setImagePath(String imagePath) {
        this.imagePath = imagePath;
    }
}