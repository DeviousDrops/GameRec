package com.gamerec.library.controller;

import com.gamerec.library.dto.LibraryEntryResponse;
import com.gamerec.library.dto.UpsertResult;
import com.gamerec.library.entity.EntryStatus;
import com.gamerec.library.exception.EntryNotFoundException;
import com.gamerec.library.service.LibraryService;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.http.MediaType;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

import java.time.Instant;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.header;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * The web layer alone: @WebMvcTest starts the controller and the exception handler with MockMvc,
 * and no database. The service is a Mockito mock, so each test decides what it returns.
 */
@WebMvcTest(LibraryController.class)
class LibraryControllerTest {

    private static final String ENTRY = "/api/v1/users/u1/library/413150";

    @Autowired
    private MockMvc mvc;

    @MockitoBean
    private LibraryService service;

    @Test
    void anUnknownStatusIsA400AndNeverReachesTheService() throws Exception {
        mvc.perform(put(ENTRY).contentType(MediaType.APPLICATION_JSON).content("{\"status\":\"BOUGHT\"}"))
                .andExpect(status().isBadRequest())
                .andExpect(content().contentType(MediaType.APPLICATION_PROBLEM_JSON))
                .andExpect(jsonPath("$.status").value(400));

        verifyNoInteractions(service);
    }

    @Test
    void aMissingStatusIsA400ThatNamesTheField() throws Exception {
        mvc.perform(put(ENTRY).contentType(MediaType.APPLICATION_JSON).content("{}"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.errors[0]").value("status: must not be null"));
    }

    @Test
    void aPageSizeOverTheLimitIsA400() throws Exception {
        mvc.perform(get("/api/v1/users/u1/library").param("size", "500"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.errors[0]").value("size: must be less than or equal to 100"));
    }

    @Test
    void deletingAnEntryThatIsNotThereIsA404() throws Exception {
        doThrow(new EntryNotFoundException("u1", 413150)).when(service).delete("u1", 413150);

        mvc.perform(delete(ENTRY))
                .andExpect(status().isNotFound())
                .andExpect(content().contentType(MediaType.APPLICATION_PROBLEM_JSON))
                .andExpect(jsonPath("$.detail").value("app 413150 is not in the library of user u1"))
                .andExpect(jsonPath("$.instance").value(ENTRY));
    }

    @Test
    void aNewEntryIsA201WithItsLocation() throws Exception {
        when(service.upsert("u1", 413150, EntryStatus.OWNED)).thenReturn(result(true));

        mvc.perform(put(ENTRY).contentType(MediaType.APPLICATION_JSON).content("{\"status\":\"OWNED\"}"))
                .andExpect(status().isCreated())
                .andExpect(header().string("Location", "http://localhost" + ENTRY))
                .andExpect(jsonPath("$.status").value("OWNED"));
    }

    @Test
    void anExistingEntryIsA200() throws Exception {
        when(service.upsert(any(), any(Integer.class), any())).thenReturn(result(false));

        mvc.perform(put(ENTRY).contentType(MediaType.APPLICATION_JSON).content("{\"status\":\"OWNED\"}"))
                .andExpect(status().isOk())
                .andExpect(header().doesNotExist("Location"));
    }

    private static UpsertResult result(boolean created) {
        Instant now = Instant.parse("2026-10-27T06:00:00Z");
        return new UpsertResult(new LibraryEntryResponse("u1", 413150, EntryStatus.OWNED, now, now), created);
    }
}
