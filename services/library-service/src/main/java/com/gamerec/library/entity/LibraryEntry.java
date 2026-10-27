package com.gamerec.library.entity;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

import java.time.Instant;
import java.time.temporal.ChronoUnit;

/** One game in one user's library. Maps to the table created by V1__init.sql. */
@Entity
@Table(name = "library_entries")
public class LibraryEntry {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "user_id", nullable = false, length = 64)
    private String userId;

    @Column(name = "app_id", nullable = false)
    private int appId;

    @Enumerated(EnumType.STRING)
    @Column(nullable = false, length = 16)
    private EntryStatus status;

    @Column(name = "added_at", nullable = false, updatable = false)
    private Instant addedAt;

    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    /** For JPA only: it builds an empty object and fills the fields from a row. */
    protected LibraryEntry() {
    }

    public LibraryEntry(String userId, int appId, EntryStatus status) {
        this.userId = userId;
        this.appId = appId;
        this.status = status;
        this.addedAt = now();
        this.updatedAt = this.addedAt;
    }

    /**
     * Change the status, and the timestamp only if something actually changed. A repeated PUT with
     * the same status is then a true no-op: Hibernate sees no dirty field and issues no UPDATE.
     */
    public void changeStatus(EntryStatus newStatus) {
        if (newStatus != status) {
            status = newStatus;
            updatedAt = now();
        }
    }

    /** Postgres stores microseconds, so the value is cut to that here and reads back identical. */
    private static Instant now() {
        return Instant.now().truncatedTo(ChronoUnit.MICROS);
    }

    public Long getId() {
        return id;
    }

    public String getUserId() {
        return userId;
    }

    public int getAppId() {
        return appId;
    }

    public EntryStatus getStatus() {
        return status;
    }

    public Instant getAddedAt() {
        return addedAt;
    }

    public Instant getUpdatedAt() {
        return updatedAt;
    }
}
