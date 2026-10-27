package com.gamerec.library.dto;

import com.gamerec.library.entity.EntryStatus;
import com.gamerec.library.entity.LibraryEntry;

import java.time.Instant;

/**
 * What the API returns for one entry. A record rather than the entity itself, so the JSON shape is
 * decided here and the database id stays internal.
 */
public record LibraryEntryResponse(
        String userId,
        int appId,
        EntryStatus status,
        Instant addedAt,
        Instant updatedAt) {

    public static LibraryEntryResponse from(LibraryEntry entry) {
        return new LibraryEntryResponse(entry.getUserId(), entry.getAppId(), entry.getStatus(),
                entry.getAddedAt(), entry.getUpdatedAt());
    }
}
