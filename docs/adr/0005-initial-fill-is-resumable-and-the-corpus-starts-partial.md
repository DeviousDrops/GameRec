# Initial Fill is resumable, and the Corpus starts partial

Steam's store API sustains roughly 208 requests per 5 minutes, metadata cannot be batched, and there are
~177,000 games — so populating an empty Corpus takes about six days. Rather than pretend otherwise, the
Initial Fill is a single long-running resumable Job that hands off to the nightly CronJob once the
Checkpoint reports it has caught up, and the service runs on a partial Corpus until then.

## Considered Options

A one-shot bootstrap script was rejected for being unrerunnable: the failure mode of a multi-day job is
being interrupted, so resumability is the whole requirement. Shrinking the Corpus to fit does not help,
because the Scope Filter needs review counts, which cost one request per game to obtain — the filter
saves index space and quality, not ingest time.

## Consequences

The ordering itself has to be fetched before any of it can be used, and SteamSpy serves it in pages of
1000 at one request per minute — so a full fill opens with roughly three hours of paging during which
nothing is ingested, and the Ingest Lease is renewed per page to cover it (D57). A fill bounded to the
first page skips that phase, which is the cheap way to get a useful corpus quickly.

Fill order is by third-party popularity estimates, so the most-wanted games are searchable within hours
rather than days. Those estimates order the work and never become data. The partial-corpus week is a
documented property of a fresh deployment, not a bug to be explained away when recommendations are thin.

## Update, 2026-10-27 — the fill ran, and the hand-off had a gap

The fill finished in 17 hours, not six days. SteamSpy's list ends at 82,506 apps rather than the
~177,000 games Steam's store counts, and the Scope Filter admits 30,509 of them without a request for
the other 51,997, so far fewer `appdetails` calls were needed than the estimate above assumed.

The hand-off to the nightly CronJob happened and did nothing for a month: the nightly considered only
the 2,000 most-reviewed games, all already ingested, so it never found a new one. D65 has the nightly
scan the whole list each night instead, which makes the fill a one-off in fact as well as in intent --
it is needed again only for an empty corpus or a large drop in the review floor.
