# Backends self-register via `__init_subclass__` + side-effect import

A backend becomes available by subclassing its ABC — which auto-registers it under `backend_name`
through `__init_subclass__` — and being imported exactly once, typically via a
`from . import my_backend  # noqa: F401` side-effect import in a plugin's `setup()`. Core discovers
backends by name through `Backend.registered_backends()`, never by importing concrete classes.

## Considered options

- **Explicit registration calls in `app.py`** / **entry-point metadata** — rejected: both couple
  core to concrete vendor classes and make adding a backend a non-additive change.

## Consequences

The "unused" import is **load-bearing**. Deleting it (or letting a linter strip it) silently
unregisters the backend, which then fails to resolve at runtime with no import-time error.
