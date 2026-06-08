# Backend secrets are masked-but-plaintext at rest in v1 (known gap)

Sensitive backend config (API keys, OAuth tokens, service-account JSON) is masked over the wire and
revealed only through audited RPCs, but it is **not encrypted at rest** — it sits plaintext in the
SQLite database, protected only by file permissions. Encryption-at-rest is deferred; a startup
warning documents the gap.

This is recorded so nobody mistakes the masking for encryption. The mask is a UI/transport
affordance; anyone with the database file has the secrets. Shipping the masked-plaintext stopgap was
chosen over blocking on a full at-rest encryption design.

## Consequences

The browser plugin is the deliberate exception — it Fernet-encrypts per-user credentials with a
per-install key, so credentials never enter an AI prompt. Losing that key makes those credentials
unrecoverable, so it must be backed up.
