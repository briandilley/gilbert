# Strict layered imports

The codebase enforces a one-way import hierarchy: `interfaces/` depends on nothing;
`core/`, `integrations/`, and `storage/` depend only on `interfaces/`; `web/` depends on
`interfaces/` + `core/`; and `app.py` is the only composition root that may import concrete
service/integration classes. Shared constants and policy data live in `interfaces/`, never imported
sideways between layers.

Coupling across these boundaries would defeat the whole point of the backend/plugin architecture —
swappable implementations behind stable abstractions. The cost is real (indirection, more protocol
definitions, the discipline of routing shared data through `interfaces/`), but it's what keeps any
backend or plugin replaceable without touching its callers.
