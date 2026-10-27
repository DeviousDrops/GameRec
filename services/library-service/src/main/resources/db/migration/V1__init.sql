-- One row per (user, game). Flyway runs this once and records it in flyway_schema_history; a later
-- change to the schema is a new V2__ file, never an edit to this one.
create table library_entries (
    -- A surrogate key for JPA. The API never exposes it: an entry is addressed by (user_id, app_id).
    id         bigint generated always as identity primary key,
    user_id    varchar(64) not null,
    app_id     integer     not null check (app_id > 0),
    -- Matches the Java enum. The check means a typo cannot get in through some other client.
    status     varchar(16) not null check (status in ('OWNED', 'WISHLIST')),
    added_at   timestamptz not null,
    updated_at timestamptz not null,

    -- A game is in a user's library at most once. This is what makes PUT an upsert rather than an
    -- insert, and its index also serves every lookup of a single entry.
    constraint library_entries_user_app_uk unique (user_id, app_id)
);

-- Every read filters on user_id. A plain index on user_id alone would duplicate the unique index
-- above, whose leading column is already user_id, so this one adds status: it serves
-- "?status=OWNED", which is the query the recommender makes on every request.
create index library_entries_user_status_idx on library_entries (user_id, status);
