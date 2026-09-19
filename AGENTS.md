# GameRec

RAG-based Steam game recommendation service, backed by [MinDB](https://github.com/typicallhavok/mindb)
(an embedded exact-kNN vector store in Go) over FlatBuffers-on-gRPC, deployed on
single-VM k3s.

## Commits and authorship

- **Anyone may author commits and open PRs.** Contributions are welcome from any account.
- **Merging to `main` requires approval from DeviousDrops.** Work lands on a branch and goes
  through a PR; no agent and no contributor self-merges. `main` should be branch-protected to
  enforce this rather than relying on convention.
- **Claude Code is never an author.** It does not appear as author, committer, or co-author.
  No AI attribution anywhere in git history or on GitHub: no `Co-Authored-By` trailers, no
  "Generated with Claude Code" footers, no bot signatures in commit messages, PR bodies, or
  issue comments. Commits made with agent assistance carry the human's identity.
- Write commit messages and docs in a plain human voice. Imperative subject line under ~72
  chars, a body explaining *why* when the change isn't self-evident. No emoji headers, no
  bullet-point walls, no AI boilerplate.
- Small, reviewable commits. One logical change each.
- `.claude/` is tracked deliberately — it's tooling config, not authorship evidence. Machine-local
  overrides (`settings.local.json`, plugin scratch state) stay ignored; shared config goes in
  `.claude/settings.json`.

## Agent skills

### Issue tracker

Issues live in this repo's GitHub Issues (`DeviousDrops/GameRec`), managed via the `gh` CLI.
See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage labels, used verbatim. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## Project conventions

- `DECISIONS.md` records every non-obvious choice as context → options → choice → trade-off.
- Prefer ASCII or Mermaid diagrams over long prose in docs.
- Kubernetes stays at plain-manifest level: no Helm, no operators, unless justified in
  `DECISIONS.md`.
- Config comes from env vars / ConfigMap; secrets come from k8s Secrets. No keys in the repo.
- MinDB lives at `../mindb` and is a separate repo. Don't change it without explicit approval.
