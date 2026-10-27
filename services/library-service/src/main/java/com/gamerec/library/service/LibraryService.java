package com.gamerec.library.service;

import com.gamerec.library.dto.LibraryEntryResponse;
import com.gamerec.library.dto.PageResponse;
import com.gamerec.library.dto.UpsertResult;
import com.gamerec.library.entity.EntryStatus;
import com.gamerec.library.entity.LibraryEntry;
import com.gamerec.library.exception.EntryNotFoundException;
import com.gamerec.library.repository.LibraryEntryRepository;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.PageRequest;
import org.springframework.data.domain.Pageable;
import org.springframework.data.domain.Sort;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;
import java.util.Optional;

/** The business rules. Knows nothing about HTTP: it takes plain values and returns DTOs. */
@Service
public class LibraryService {

    // Newest first, with the id as a tie-break so a page boundary never shuffles equal timestamps.
    private static final Sort NEWEST_FIRST = Sort.by(Sort.Direction.DESC, "addedAt", "id");

    private final LibraryEntryRepository repository;

    // Constructor injection: the dependency is final, required, and a unit test can pass a mock in.
    public LibraryService(LibraryEntryRepository repository) {
        this.repository = repository;
    }

    /**
     * Create the entry, or change the status of the one already there. Running it twice with the
     * same arguments leaves the same row, which is what makes PUT idempotent.
     *
     * Two PUTs racing to create the same entry can both find nothing; the unique constraint then
     * rejects the second insert, and the exception handler answers it with 409 so the client retries.
     */
    @Transactional
    public UpsertResult upsert(String userId, int appId, EntryStatus status) {
        Optional<LibraryEntry> existing = repository.findByUserIdAndAppId(userId, appId);
        if (existing.isPresent()) {
            LibraryEntry entry = existing.get();
            // No save() needed: the entity is managed, so Hibernate writes the change at commit.
            entry.changeStatus(status);
            return new UpsertResult(LibraryEntryResponse.from(entry), false);
        }
        LibraryEntry created = repository.save(new LibraryEntry(userId, appId, status));
        return new UpsertResult(LibraryEntryResponse.from(created), true);
    }

    @Transactional(readOnly = true)
    public PageResponse<LibraryEntryResponse> list(String userId, EntryStatus status, int page, int size) {
        Pageable pageable = PageRequest.of(page, size, NEWEST_FIRST);
        Page<LibraryEntry> entries = status == null
                ? repository.findByUserId(userId, pageable)
                : repository.findByUserIdAndStatus(userId, status, pageable);
        return PageResponse.from(entries, LibraryEntryResponse::from);
    }

    @Transactional(readOnly = true)
    public List<Integer> appIds(String userId, EntryStatus status) {
        return status == null
                ? repository.findAppIds(userId)
                : repository.findAppIdsByStatus(userId, status);
    }

    @Transactional
    public void delete(String userId, int appId) {
        LibraryEntry entry = repository.findByUserIdAndAppId(userId, appId)
                .orElseThrow(() -> new EntryNotFoundException(userId, appId));
        repository.delete(entry);
    }
}
