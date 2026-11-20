package com.argus.repository;

import com.argus.entity.PlateEvent;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;

public interface PlateEventRepository extends JpaRepository<PlateEvent, Long> {

    @Query("SELECT COUNT(DISTINCT p.plateNumber) FROM PlateEvent p")
    long countDistinctPlateNumber();

    /** Сколько строк старше отсечки — для отчёта ретеншна (dry-run и лог). */
    long countByDetectedAtBefore(LocalDateTime cutoff);

    /**
     * Удаляет одну порцию старых строк. Пачками, а не одним DELETE: за 90 дней
     * накапливаются сотни тысяч строк, и одна транзакция на всё держала бы
     * блокировки и раздувала WAL.
     */
    @Modifying
    @Transactional
    @Query(value = """
            DELETE FROM plate_events
             WHERE id IN (SELECT id FROM plate_events
                           WHERE detected_at < :cutoff
                           LIMIT :batchSize)
            """, nativeQuery = true)
    int deleteBatchOlderThan(@Param("cutoff") LocalDateTime cutoff,
                             @Param("batchSize") int batchSize);
}
