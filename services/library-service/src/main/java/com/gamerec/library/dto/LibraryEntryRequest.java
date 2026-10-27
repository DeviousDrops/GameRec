package com.gamerec.library.dto;

import com.gamerec.library.entity.EntryStatus;
import jakarta.validation.constraints.NotNull;

/**
 * The body of a PUT. @NotNull catches a missing status; an unknown value such as "BOUGHT" never gets
 * this far, because Jackson cannot turn it into an EntryStatus and the request is rejected as unreadable.
 */
public record LibraryEntryRequest(@NotNull EntryStatus status) {
}
