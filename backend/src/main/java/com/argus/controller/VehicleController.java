package com.argus.controller;

import com.argus.entity.Vehicle;
import com.argus.repository.VehicleRepository;
import com.argus.service.PlateNormalizer;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

@RestController
@RequestMapping("/api/v1/vehicles")
public class VehicleController {

    private final VehicleRepository vehicleRepository;

    public VehicleController(VehicleRepository vehicleRepository) {
        this.vehicleRepository = vehicleRepository;
    }

    @GetMapping
    public List<Vehicle> list() {
        return vehicleRepository.findAll();
    }

    @PostMapping
    public ResponseEntity<Vehicle> create(
            @RequestParam String plateNumber,
            @RequestParam(required = false) String description) {
        String normalized = PlateNormalizer.normalize(plateNumber);
        if (normalized.isBlank()) {
            return ResponseEntity.badRequest().build();
        }
        if (vehicleRepository.existsByPlateNumber(normalized)) {
            return ResponseEntity.status(HttpStatus.CONFLICT).build();
        }
        Vehicle vehicle = vehicleRepository.save(new Vehicle(normalized, description));
        return ResponseEntity.status(HttpStatus.CREATED).body(vehicle);
    }

    @DeleteMapping("/{id}")
    public ResponseEntity<Void> delete(@PathVariable Long id) {
        if (!vehicleRepository.existsById(id)) {
            return ResponseEntity.notFound().build();
        }
        vehicleRepository.deleteById(id);
        return ResponseEntity.noContent().build();
    }
}
