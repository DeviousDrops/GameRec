package com.gamerec.library.dto;

import java.util.List;

/** The appids in a library. Wrapped in an object so a field can be added later without breaking callers. */
public record LibraryIdsResponse(List<Integer> appIds) {
}
