package com.gamerec.library.controller;

import com.gamerec.library.dto.LibraryEntryRequest;
import com.gamerec.library.dto.LibraryEntryResponse;
import com.gamerec.library.dto.LibraryIdsResponse;
import com.gamerec.library.dto.PageResponse;
import com.gamerec.library.dto.UpsertResult;
import com.gamerec.library.entity.EntryStatus;
import com.gamerec.library.service.LibraryService;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Positive;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.support.ServletUriComponentsBuilder;

/**
 * HTTP in, HTTP out. Validates the request, calls the service, and picks the status code; no
 * business logic lives here.
 *
 * The constraint annotations on @PathVariable and @RequestParam are checked by Spring MVC itself
 * (method validation, Spring 6.1+), and a failure reaches ApiExceptionHandler as a 400.
 */
@RestController
@RequestMapping("/api/v1/users/{userId}/library")
public class LibraryController {

    // GameRec has no accounts, so a user id is whatever the caller uses: letters, digits, _ and -,
    // which also covers a 17-digit SteamID64. Bounded to the column's 64 characters.
    private static final String USER_ID = "[A-Za-z0-9_-]{1,64}";

    private final LibraryService service;

    public LibraryController(LibraryService service) {
        this.service = service;
    }

    /** Idempotent upsert: 201 with a Location header when the entry is new, 200 when it existed. */
    @PutMapping("/{appId}")
    public ResponseEntity<LibraryEntryResponse> upsert(
            @PathVariable @Pattern(regexp = USER_ID) String userId,
            @PathVariable @Positive int appId,
            @Valid @RequestBody LibraryEntryRequest request) {
        UpsertResult result = service.upsert(userId, appId, request.status());
        if (result.created()) {
            // The new resource lives at the URL that was just PUT to.
            return ResponseEntity
                    .created(ServletUriComponentsBuilder.fromCurrentRequestUri().build().toUri())
                    .body(result.entry());
        }
        return ResponseEntity.ok(result.entry());
    }

    @GetMapping
    public PageResponse<LibraryEntryResponse> list(
            @PathVariable @Pattern(regexp = USER_ID) String userId,
            @RequestParam(required = false) EntryStatus status,
            @RequestParam(defaultValue = "0") @Min(0) int page,
            @RequestParam(defaultValue = "20") @Min(1) @Max(100) int size) {
        return service.list(userId, status, page, size);
    }

    /** Only the appids, for the recommender: one small response instead of paging through entries. */
    @GetMapping("/ids")
    public LibraryIdsResponse ids(
            @PathVariable @Pattern(regexp = USER_ID) String userId,
            @RequestParam(required = false) EntryStatus status) {
        return new LibraryIdsResponse(service.appIds(userId, status));
    }

    @DeleteMapping("/{appId}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void delete(
            @PathVariable @Pattern(regexp = USER_ID) String userId,
            @PathVariable @Positive int appId) {
        service.delete(userId, appId);
    }
}
