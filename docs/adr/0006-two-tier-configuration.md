# Two-tier configuration: `gilbert.yaml` bootstrap vs `gilbert.config` entities

Only the settings that must exist *before storage is up* — `storage`, `logging`, `web` — live in
the `gilbert.yaml` file. Everything else is runtime-editable configuration stored in the
`gilbert.config` entity collection and managed through `/settings`.

Most configuration should be changeable at runtime without editing a file or restarting; only the
irreducible bootstrap minimum can't be, because it's needed to bring storage online in the first
place. The trade-off is that "where is this setting?" has two answers, and the boundary
(bootstrap-only vs runtime) has to be respected when adding new config.
