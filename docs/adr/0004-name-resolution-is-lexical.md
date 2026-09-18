# Name resolution is lexical, not semantic

A Seed Game may arrive as a name a human typed. The obvious move — search the vector store for that
name — is wrong: **semantic similarity is not lexical similarity.** A vector search for "Portal"
cheerfully returns puzzle games that are not Portal, and a typo like "Prtal" has no semantic
neighbourhood at all. So names resolve against a separate Name Index using fuzzy string matching,
and the vector store is never asked a question about identity.

## Consequences

Two lookup systems exist, and the Name Index must be published with each Backup Generation to stay
consistent with the Corpus. In exchange, a failed match can explain itself: each entry carries a status
(`in_corpus`, `filtered_low_reviews`, `not_a_game`, `pending_ingest`), so a 404 can say "excluded: under
50 reviews" or offer `did_you_mean` candidates rather than returning a confident, wrong game.
