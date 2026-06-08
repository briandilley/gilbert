# Multi-user isolation: no per-user state on `self`, explicit `UserContext`, copied task context

Services are singletons shared across every user, so the codebase enforces three rules: never cache
per-user state on `self` (re-read storage each call); pass identity explicitly as a `UserContext`
rather than relying on the ambient `get_current_user()` context variable; and hand any spawned
`asyncio` task a copied context (`copy_context()`) so a sibling task can't mutate another user's
context variables.

This is a security boundary, not a style preference: caching one user's preferences on the singleton
service, or letting a background job (greeting, scheduler, detached task) read an ambient identity
that belongs to a different request, leaks one user's data into another's session. The cost is
re-reading storage on hot paths and threading `UserContext` through call signatures.
