# Source-update switches branches via `gilbert.sh` + a sentinel file, with LKG auto-rollback

To move the running instance to a different git branch, the source-update service writes a two-line
sentinel file (`<remote>` / `<branch>`) that `gilbert.sh` consumes to perform the actual `git switch`
during a supervised restart — the live Python process never switches its own branch. Before
switching, the supervisor records the current branch as last-known-good (LKG) and **auto-rolls-back**
if the new branch crashes within a ~90-second probe window. Only locally-configured remotes are
accepted (validated via `git remote get-url`).

A broken import on the target branch must not be able to wedge the live instance mid-switch, and a
bad deploy should self-heal rather than require manual recovery. The accepted, bounded threat model
is "any admin can run code by pushing to a configured remote and clicking Apply" — which is why
arbitrary URLs are refused and the service is intentionally **not** toggleable (disabling it would
strand an admin who needs it to recover from a broken deploy).
