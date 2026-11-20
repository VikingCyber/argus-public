package com.argus.repository;

import com.argus.entity.Camera;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.Optional;

public interface CameraRepository extends JpaRepository<Camera, String> {
    Optional<Camera> findById(String id);
}