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

Fill order is by third-party popularity estimates, so the most-wanted games are searchable within hours
rather than days. Those estimates order the work and never become data. The partial-corpus week is a
documented property of a fresh deployment, not a bug to be explained away when recommendations are thin.
