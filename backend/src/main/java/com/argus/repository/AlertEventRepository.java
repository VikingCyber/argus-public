package com.argus.repository;

import com.argus.entity.AlertEvent;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.Optional;

public interface AlertEventRepository extends JpaRepository<AlertEvent, Long> {

    Optional<AlertEvent> findFirstByCameraIdAndAlertTypeOrderByDetectedAtDesc(
            String cameraId, String alertType);

    Optional<AlertEvent> findFirstByCameraIdAndAlertTypeAndPlateNumberOrderByDetectedAtDesc(
            String cameraId, String alertType, String plateNumber);

    /** Сколько строк старше отсечки — для отчёта ретеншна (dry-run и лог). */
    long countByDetectedAtBefore(LocalDateTime cutoff);

    /** Удаляет одну порцию старых тревог (см. PlateEventRepository.deleteBatchOlderThan). */
    @Modifying
    @Transactional
    @Query(value = """
            DELETE FROM alert_events
             WHERE id IN (SELECT id FROM alert_events
                           WHERE detected_at < :cutoff
                           LIMIT :batchSize)
            """, nativeQuery = true)
    int deleteBatchOlderThan(@Param("cutoff") LocalDateTime cutoff,
                             @Param("batchSize") int batchSize);
}
