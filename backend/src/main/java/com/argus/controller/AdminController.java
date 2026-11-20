package com.argus.controller;

import com.argus.dto.RetentionResult;
import com.argus.service.RetentionService;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * Служебные операции. POST защищён ApiKeyFilter (X-API-Key), как и все
 * изменяющие запросы на /api/**.
 */
@RestController
@RequestMapping("/api/v1/admin")
public class AdminController {

    private final RetentionService retentionService;

    public AdminController(RetentionService retentionService) {
        this.retentionService = retentionService;
    }

    /**
     * Ручной запуск очистки старых данных.
     *
     * <pre>
     * curl -X POST "http://127.0.0.1:8080/api/v1/admin/retention?dryRun=true" ^
     *      -H "X-API-Key: <ключ из config.prod.yaml>"
     * </pre>
     *
     * @param dryRun true — только показать, что было бы удалено
     */
    @PostMapping("/retention")
    public RetentionResult runRetention(@RequestParam(defaultValue = "false") boolean dryRun) {
        return retentionService.run(dryRun);
    }
}
