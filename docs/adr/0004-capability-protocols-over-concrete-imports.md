# Cross-service access via runtime-checkable protocols, never concrete imports or duck-typing

A service reaches another service's methods by resolving a capability by string name
(`resolver.get_capability("name")`) and `isinstance`-checking the result against a
`@runtime_checkable` Protocol defined in `interfaces/`. Importing the concrete service class,
`getattr`-style duck-typing, and reaching into another service's private attributes are all
explicit architectural violations — not merely discouraged.

This keeps consumers decoupled from whichever concrete service happens to register a capability
today, so services stay independently swappable and testable (a fake need only satisfy the
Protocol). The cost is defining a Protocol in `interfaces/` whenever a service exposes methods other
services need.
