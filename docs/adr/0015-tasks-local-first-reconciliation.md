# Tasks use local-first reconciliation

Every task mutation lands in entity storage immediately (marked pending-push), pushes upstream
inline, and is retried by a sync tick if that push fails; upstream is authoritative for fields not
currently in flight. The AI tool never blocks or errors on a slow or down backend — it answers
"added, syncing in the background."

## Consequences

- There are brief windows where the local copy and the upstream provider disagree; the sync state
  (`PENDING_PUSH` / `SYNCED` / `PUSH_FAILED` / `PENDING_DELETE`) tracks reconciliation.
- Upstream updates are **patch-shaped**, not full-task writes, so a user's edits on their phone to
  fields Gilbert didn't touch survive.
- Deletes are soft by default; hard-delete is admin-only and never exposed to the AI or slash
  commands, so the model can't irrecoverably destroy data.
