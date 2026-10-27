package com.gamerec.library.dto;

/** The entry after a PUT, and whether the PUT created it. The controller turns `created` into 201 or 200. */
public record UpsertResult(LibraryEntryResponse entry, boolean created) {
}
