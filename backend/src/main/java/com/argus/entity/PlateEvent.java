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
@Table(name = "plate_events", indexes = {
        @Index(name = "idx_plate_events_camera", columnList = "camera_id"),
        @Index(name = "idx_plate_events_number", columnList = "plate_number"),
        @Index(name = "idx_plate_events_detected", columnList = "detected_at")
})
public class PlateEvent {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "camera_id", length = 64, nullable = false)
    private String cameraId;

    @Column(name = "plate_number", length = 16, nullable = false)
    private String plateNumber;

    @Column(name = "confidence")
    private Double confidence;

    /** Идентификатор трека ТС в вижене (одна машина = один проезд). */
    @Column(name = "track_id")
    private Long trackId;

    @Column(name = "detected_at", nullable = false)
    private LocalDateTime detectedAt;

    @Column(name = "image_path", length = 512)
    private String imagePath;

    /** ALLOWED — номер из белого списка, DENIED — неизвестный. */
    @Column(name = "decision", length = 16)
    private String decision;

    /** IN/OUT; пока камеры одна, направление не определяется (null). */
    @Column(name = "direction", length = 8)
    private String direction;

    public PlateEvent() {
    }

    public PlateEvent(String cameraId, String plateNumber, Double confidence,
                      LocalDateTime detectedAt, String imagePath) {
        this.cameraId = cameraId;
        this.plateNumber = plateNumber;
        this.confidence = confidence;
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

    public String getPlateNumber() {
        return plateNumber;
    }

    public void setPlateNumber(String plateNumber) {
        this.plateNumber = plateNumber;
    }

    public Double getConfidence() {
        return confidence;
    }

    public void setConfidence(Double confidence) {
        this.confidence = confidence;
    }

    public Long getTrackId() {
        return trackId;
    }

    public void setTrackId(Long trackId) {
        this.trackId = trackId;
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

    public String getDecision() {
        return decision;
    }

    public void setDecision(String decision) {
        this.decision = decision;
    }

    public String getDirection() {
        return direction;
    }

    public void setDirection(String direction) {
        this.direction = direction;
    }
}