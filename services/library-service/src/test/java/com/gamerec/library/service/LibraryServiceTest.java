package com.gamerec.library.service;

import com.gamerec.library.dto.UpsertResult;
import com.gamerec.library.entity.EntryStatus;
import com.gamerec.library.entity.LibraryEntry;
import com.gamerec.library.repository.LibraryEntryRepository;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/** Plain JUnit and Mockito: no Spring context, no database. The repository is a mock. */
@ExtendWith(MockitoExtension.class)
class LibraryServiceTest {

    @Mock
    private LibraryEntryRepository repository;

    @InjectMocks
    private LibraryService service;

    @Test
    void anEntryThatDoesNotExistIsCreated() {
        when(repository.findByUserIdAndAppId("u1", 413150)).thenReturn(Optional.empty());
        // save() hands back what it was given, as JPA does for a new entity.
        when(repository.save(any(LibraryEntry.class))).thenAnswer(call -> call.getArgument(0));

        UpsertResult result = service.upsert("u1", 413150, EntryStatus.WISHLIST);

        assertThat(result.created()).isTrue();
        assertThat(result.entry().status()).isEqualTo(EntryStatus.WISHLIST);
        assertThat(result.entry().addedAt()).isEqualTo(result.entry().updatedAt());
        verify(repository).save(any(LibraryEntry.class));
    }

    @Test
    void anEntryThatExistsIsUpdatedInPlace() {
        LibraryEntry existing = new LibraryEntry("u1", 413150, EntryStatus.WISHLIST);
        when(repository.findByUserIdAndAppId("u1", 413150)).thenReturn(Optional.of(existing));

        UpsertResult result = service.upsert("u1", 413150, EntryStatus.OWNED);

        assertThat(result.created()).isFalse();
        assertThat(result.entry().status()).isEqualTo(EntryStatus.OWNED);
        assertThat(existing.getStatus()).isEqualTo(EntryStatus.OWNED);
        // A managed entity is written back at commit by Hibernate's dirty checking, so an update
        // must not call save(): that would be a second, pointless write path.
        verify(repository, never()).save(any());
    }
}
