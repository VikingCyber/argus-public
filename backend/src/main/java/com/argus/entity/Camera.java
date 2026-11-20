package com.argus.entity;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

import java.time.LocalDateTime;

@Entity
@Table(name = "cameras")
public class Camera {

    @Id
    @Column(name = "id", length = 64)
    private String id;

    @Column(name = "name", length = 255)
    private String name;

    @Column(name = "registered_at")
    private LocalDateTime registeredAt;

    public Camera() {
    }

    public Camera(String id, String name, LocalDateTime registeredAt) {
        this.id = id;
        this.name = name;
        this.registeredAt = registeredAt;
    }

    public String getId() {
        return id;
    }

    public void setId(String id) {
        this.id = id;
    }

    public String getName() {
        return name;
    }

    public void setName(String name) {
        this.name = name;
    }

    public LocalDateTime getRegisteredAt() {
        return registeredAt;
    }

    public void setRegisteredAt(LocalDateTime registeredAt) {
        this.registeredAt = registeredAt;
    }
}