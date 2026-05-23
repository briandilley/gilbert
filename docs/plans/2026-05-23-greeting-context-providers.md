# Greeting Context Providers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the GreetingService's hardcoded weather/news/health integration with a capability-discovered `GreetingContextProvider` pattern, so any service can contribute a labeled prose fact to the greeting prompt — and the user's custom prompt template decides what to do with it.

**Architecture:**

- New `@runtime_checkable Protocol GreetingContextProvider` in `interfaces/greeting.py`. Each implementer returns an `(id, label, prose)` triple per user (or `None` to skip). GreetingService discovers providers via `resolver.get_all("greeting_context")`, calls each enabled one, concatenates them into a labeled block, and folds the block into the arrival prompt as a single `{available_context}` placeholder. There is no slot order — the AI integrates the facts based on the prompt template.
- WeatherService, FeedBriefingService, and HealthService each implement the protocol; the per-source format templates and "show this in the greeting" toggles that currently sit on GreetingService move onto the owning service. GreetingService keeps a single `enabled_context_providers: list[str]` config and a new WS RPC that the settings page uses to list discovered providers as toggleable rows.
- The hardcoded arrival prompt becomes a `ConfigParam(ai_prompt=True)` so users can write rules like "always mention weather if it's extreme."

**Tech Stack:** Python 3.12, asyncio, capability protocols (`@runtime_checkable` Protocol), `Service` / `ServiceInfo` / `ServiceResolver` framework, pytest, React/TypeScript SPA, WS RPC handlers.

---

## File Structure

**Create:**
- `src/gilbert/interfaces/greeting.py` — `GreetingContextProvider` Protocol + `GreetingContext` dataclass. Pure interfaces, no logic.
- `src/gilbert/migrations/0002_greeting_context_providers.py` — translate old greeting config keys into the new shape and move per-provider templates onto their owning services.
- `tests/unit/test_greeting_context_protocol.py` — the protocol-shape tests.
- `tests/unit/test_greeting_context_integration.py` — GreetingService consumes providers correctly.

**Modify:**
- `src/gilbert/core/services/greeting.py` — drops `_weather` / `_feeds` / `_health` injection + `include_*` flags + `_build_weather_blurb` / `_fetch_health_brief` / `_format_health_brief` / `_maybe_briefing_text`; adds provider discovery, `enabled_context_providers` config, `{available_context}` placeholder, and a `greeting.context_providers.list` WS handler. Arrival prompt becomes configurable.
- `src/gilbert/core/services/weather.py` — adds `"greeting_context"` to capabilities, adds `weather_hint_template` `ConfigParam`, implements `greeting_context()` (port `_build_weather_blurb` logic).
- `src/gilbert/core/services/feed_briefing.py` — adds capability + `briefing_max_seconds` ConfigParam + `greeting_context()` (port `_maybe_briefing_text` logic including the "already briefed today" guard).
- `src/gilbert/core/services/health.py` — adds capability + `greeting_context()` wrapping the existing `health_brief_for_greeting`.
- `frontend/src/components/settings/...` (greeting settings page) — replace the static `include_weather` / `include_briefing` / `include_health_brief` switches with a dynamic list driven by the new WS RPC.
- `frontend/src/hooks/useWsApi.ts` — add `listGreetingContextProviders` binding.
- `README.md`, `docs/architecture/agent-service.md` (or wherever greeting lives) — short paragraph documenting the new extension point.

---

## Task 1: Define the protocol + dataclass

**Files:**
- Create: `src/gilbert/interfaces/greeting.py`
- Test: `tests/unit/test_greeting_context_protocol.py`

- [ ] **Step 1: Write the failing protocol-shape test**

Create `tests/unit/test_greeting_context_protocol.py`:

```python
"""Shape tests for the GreetingContextProvider protocol.

The protocol must be runtime-checkable so GreetingService can
``isinstance(svc, GreetingContextProvider)`` after a generic
``resolver.get_all("greeting_context")``.
"""

from gilbert.interfaces.greeting import GreetingContext, GreetingContextProvider


def test_protocol_is_runtime_checkable() -> None:
    """Required so the framework can identify providers without
    importing concrete service classes."""

    class _Conformant:
        @property
        def greeting_context_id(self) -> str:
            return "demo"

        @property
        def greeting_context_label(self) -> str:
            return "Demo"

        async def greeting_context(self, user_id: str) -> GreetingContext | None:
            return None

    assert isinstance(_Conformant(), GreetingContextProvider)


def test_non_conformant_rejected() -> None:
    class _Missing:  # no greeting_context method
        @property
        def greeting_context_id(self) -> str:
            return "x"

        @property
        def greeting_context_label(self) -> str:
            return "X"

    assert not isinstance(_Missing(), GreetingContextProvider)


def test_greeting_context_dataclass_fields() -> None:
    """Frozen dataclass — providers shouldn't mutate after returning."""
    ctx = GreetingContext(provider_id="weather", label="Weather", prose="Sunny, 72°F.")
    assert ctx.provider_id == "weather"
    assert ctx.label == "Weather"
    assert ctx.prose == "Sunny, 72°F."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_greeting_context_protocol.py -v`

Expected: ImportError — `gilbert.interfaces.greeting` doesn't exist.

- [ ] **Step 3: Create the interface module**

Create `src/gilbert/interfaces/greeting.py`:

```python
"""Greeting context provider capability.

Any service can advertise the ``"greeting_context"`` capability and
implement this protocol to contribute a labeled prose fact to the
auto-generated arrival greeting. GreetingService collects these into
a single bag of facts; the AI (guided by the user's prompt template)
decides what to use.

Adding a new contributor (e.g., calendar events for today, tasks due
today, doorbell counts) is purely additive — no edits to GreetingService.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class GreetingContext:
    """A single labeled fact contributed to the greeting prompt.

    ``provider_id`` matches the contributing service's
    ``greeting_context_id`` so the AI prompt (and the settings UI)
    can refer to specific contributions.
    """

    provider_id: str
    label: str
    prose: str


@runtime_checkable
class GreetingContextProvider(Protocol):
    """Services that contribute a labeled fact to arrival greetings.

    The contract is fire-and-forget: providers must never raise; on
    error or "no data right now" they return ``None`` and the greeting
    proceeds without them. They must not depend on greeting order.
    """

    @property
    def greeting_context_id(self) -> str:
        """Stable short id used in settings + the labeled prose block.

        Lowercase, snake_case, must match the value the settings UI
        uses to enable/disable this provider.
        """
        ...

    @property
    def greeting_context_label(self) -> str:
        """Human-readable label for the settings UI toggle row."""
        ...

    async def greeting_context(self, user_id: str) -> GreetingContext | None:
        """Return a labeled fact for ``user_id``'s greeting, or None.

        Implementers should:
        - Return None when the underlying capability is disabled,
          unconfigured, or has no data right now (cold start, error,
          quiet hours, already-shown-today guard, …).
        - Never raise. Catch and log internally.
        - Return prose that's a complete sentence (or two) — no
          leading label, no trailing newline. GreetingService adds the
          ``{label}:`` prefix when assembling the block.
        """
        ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_greeting_context_protocol.py -v`

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/interfaces/greeting.py tests/unit/test_greeting_context_protocol.py
git commit -m "greeting: define GreetingContextProvider capability protocol"
```

---

## Task 2: WeatherService implements GreetingContextProvider

**Files:**
- Modify: `src/gilbert/core/services/weather.py:443-470` (service_info + capabilities), and config_params + class body
- Test: `tests/unit/test_weather_service.py` (add new test cases)

This task moves `weather_hint_template` off GreetingService and gives WeatherService a `greeting_context()` method. GreetingService will stop touching this template after Task 6.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_weather_service.py` (use whatever fixture pattern the file already uses — typically `_service(...)` style):

```python
async def test_greeting_context_returns_labeled_blurb(...) -> None:
    """WeatherService.greeting_context returns a GreetingContext with the
    rendered weather_hint_template when current data is available."""
    from gilbert.interfaces.greeting import GreetingContext

    svc = ...  # construct service with a backend stub that returns
              # current=(loc='Cleveland, OH', temp=72.0, ..., condition_phrase='clear sky')
    ctx = await svc.greeting_context(user_id="alice")
    assert isinstance(ctx, GreetingContext)
    assert ctx.provider_id == "weather"
    assert ctx.label == "Weather"
    assert "Cleveland" in ctx.prose
    assert "72" in ctx.prose  # rendered template includes temperature


async def test_greeting_context_returns_none_when_unconfigured(...) -> None:
    """No backend or no current data → None (greeting proceeds without)."""
    svc = ...  # construct without backend or with backend that raises
    assert await svc.greeting_context(user_id="alice") is None


def test_weather_service_advertises_greeting_context_capability(...) -> None:
    svc = ...
    info = svc.service_info()
    assert "greeting_context" in info.capabilities
    assert svc.greeting_context_id == "weather"
    assert svc.greeting_context_label == "Weather"


def test_weather_service_has_weather_hint_template_config(...) -> None:
    """The template was on GreetingService; this task moves it here."""
    svc = ...
    keys = {p.key for p in svc.config_params()}
    assert "weather_hint_template" in keys
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_weather_service.py -k greeting_context -v`

Expected: FAIL — `greeting_context` method / `greeting_context_id` property / `weather_hint_template` config don't exist on WeatherService.

- [ ] **Step 3: Add the capability + template config**

Edit `src/gilbert/core/services/weather.py`:

1. Near the existing `_DEFAULT_*_PROMPT` constants at the top, add:

```python
_DEFAULT_WEATHER_HINT_TEMPLATE = (
    "Current weather at {location_name}: {temperature:.0f}{temp_suffix} "
    "{condition_phrase}, wind {wind_speed:.0f}{speed_suffix}"
    "{feels_like_clause}. Mention it casually if it fits the moment, "
    "otherwise ignore. Quote only the values shown — never invent additional "
    "weather details."
)
```

(Use the exact string currently in `greeting.py` — copy it verbatim so the migration in Task 9 is a clean move.)

2. In `__init__`, add:

```python
self._weather_hint_template: str = _DEFAULT_WEATHER_HINT_TEMPLATE
```

3. In `service_info()`, add `"greeting_context"` to capabilities:

```python
capabilities=frozenset({"weather", "ai_tools", "greeting_context"}),
```

4. In `config_params()`, append:

```python
ConfigParam(
    key="weather_hint_template",
    type=ToolParameterType.STRING,
    description=(
        "Prose template inserted into the greeting context block "
        "when this provider is enabled. Placeholders: {location_name}, "
        "{temperature}, {temp_suffix}, {condition_phrase}, "
        "{wind_speed}, {speed_suffix}, {feels_like_clause}."
    ),
    default=_DEFAULT_WEATHER_HINT_TEMPLATE,
    multiline=True,
    ai_prompt=True,
),
```

5. In `on_config_changed`, read it:

```python
if "weather_hint_template" in config:
    self._weather_hint_template = (
        config["weather_hint_template"] or _DEFAULT_WEATHER_HINT_TEMPLATE
    )
```

6. Add the protocol implementation as new methods on the class:

```python
@property
def greeting_context_id(self) -> str:
    return "weather"

@property
def greeting_context_label(self) -> str:
    return "Weather"

async def greeting_context(self, user_id: str) -> "GreetingContext | None":
    """Render the current-weather template, or return None on any
    error / no-data condition. Never raises."""
    from gilbert.interfaces.greeting import GreetingContext

    try:
        # Port the existing `_build_weather_blurb` logic from
        # greeting.py:780-815: fetch current() via the backend, build
        # the placeholder dict with temp/condition/wind/feels_like,
        # then `self._weather_hint_template.format(**placeholders)`.
        # Return None if backend missing, fetch fails, or template
        # raises KeyError/IndexError/ValueError.
        ...
        prose = self._weather_hint_template.format(**placeholders)
        return GreetingContext(provider_id="weather", label="Weather", prose=prose)
    except Exception:
        logger.debug("WeatherService.greeting_context failed for %s", user_id, exc_info=True)
        return None
```

Copy the placeholder-building logic byte-for-byte from `greeting.py:780-815` so behavior is unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_weather_service.py -v`

Expected: all weather tests pass, including the 4 new ones.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/weather.py tests/unit/test_weather_service.py
git commit -m "weather: implement GreetingContextProvider (own weather_hint_template)"
```

---

## Task 3: FeedBriefingService implements GreetingContextProvider

**Files:**
- Modify: `src/gilbert/core/services/feed_briefing.py` (service_info, config_params, class body)
- Test: `tests/unit/test_feed_briefing_service.py` (or whatever the existing test file is)

`FeedBriefingService` owns the news briefing logic. It implements the provider with id `"briefing"`. It encapsulates the once-per-day "already briefed" guard so GreetingService doesn't need to know about `feed_briefing_state`.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_feed_briefing_service.py`:

```python
async def test_greeting_context_returns_briefing_text(...) -> None:
    from gilbert.interfaces.greeting import GreetingContext

    svc = ...  # construct with a feeds capability stub whose build_briefing
              # returns text="Today: 3 items about X." and storage backend
              # with no prior feed_briefing_state for alice
    ctx = await svc.greeting_context(user_id="alice")
    assert isinstance(ctx, GreetingContext)
    assert ctx.provider_id == "briefing"
    assert ctx.label == "News briefing"
    assert "Today" in ctx.prose


async def test_greeting_context_suppresses_when_already_briefed_today(...) -> None:
    """If feed_briefing_state shows the user got a briefing today,
    return None — don't double-brief."""
    svc = ...  # storage backend pre-loaded with today's briefing state
    assert await svc.greeting_context(user_id="alice") is None


async def test_greeting_context_returns_none_when_no_feeds_capability(...) -> None:
    svc = ...  # constructed without FeedsProvider
    assert await svc.greeting_context(user_id="alice") is None


def test_feed_briefing_advertises_capability_and_config(...) -> None:
    svc = ...
    info = svc.service_info()
    assert "greeting_context" in info.capabilities
    keys = {p.key for p in svc.config_params()}
    assert "briefing_max_seconds" in keys
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_feed_briefing_service.py -k greeting_context -v`

Expected: FAIL.

- [ ] **Step 3: Implement**

Edit `src/gilbert/core/services/feed_briefing.py`:

1. Add `"greeting_context"` to `capabilities` in `service_info()`.
2. Add `briefing_max_seconds: int = 60` field in `__init__`.
3. Append the matching `ConfigParam` (type `INTEGER`, default `60`, description: "Soft cap on briefing length (~2.5 words/sec) for the greeting context contribution.").
4. Read it in `on_config_changed`.
5. Add `greeting_context_id` / `greeting_context_label` properties returning `"briefing"` / `"News briefing"`.
6. Implement `greeting_context(user_id)`:
   - Read `feed_briefing_state` for the user from storage; if `state.last_briefing_date == today` → return `None`.
   - Look up the `feeds` capability via the stored resolver; if missing → return `None`.
   - Call `feeds_svc.build_briefing(user_id=user_id, max_spoken_seconds=self._briefing_max_seconds)`.
   - On exception or empty text → return `None`.
   - On success: write the briefing-state record (so the next call today returns None) and return `GreetingContext(provider_id="briefing", label="News briefing", prose=result.text)`.

The state-management logic should be copied from `greeting.py:_maybe_briefing_text` (around lines 671-720) so we preserve the exact same "don't double-brief" semantics.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_feed_briefing_service.py -v`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/feed_briefing.py tests/unit/test_feed_briefing_service.py
git commit -m "feed_briefing: implement GreetingContextProvider with same-day suppression"
```

---

## Task 4: HealthService implements GreetingContextProvider

**Files:**
- Modify: `src/gilbert/core/services/health.py` (service_info + new methods)
- Test: `tests/unit/test_health_service.py`

HealthService already has `health_brief_for_greeting(user_id) -> GreetingBrief`. The protocol implementation is a thin wrapper that formats the brief into prose. The prose-formatting logic moves from `greeting.py:_format_health_brief` to here.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_health_service.py`:

```python
async def test_greeting_context_returns_prose_from_brief(...) -> None:
    from gilbert.interfaces.greeting import GreetingContext

    svc = ...  # construct with a HealthBackend stub whose
              # health_brief_for_greeting returns a populated GreetingBrief
              # (sleep_hours=7.5, steps_today_so_far=4200, flags=["high_hr"])
    ctx = await svc.greeting_context(user_id="alice")
    assert isinstance(ctx, GreetingContext)
    assert ctx.provider_id == "health"
    assert ctx.label == "Health"
    assert "7.5" in ctx.prose
    assert "4,200" in ctx.prose  # formatted with thousands separator
    assert "high_hr" in ctx.prose


async def test_greeting_context_returns_none_for_empty_brief(...) -> None:
    """If the brief has no data, the provider returns None — the AI
    shouldn't even see an empty Health line."""
    svc = ...  # backend returns GreetingBrief.empty("alice")
    assert await svc.greeting_context(user_id="alice") is None


def test_health_service_advertises_capability(...) -> None:
    svc = ...
    assert "greeting_context" in svc.service_info().capabilities
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_health_service.py -k greeting_context -v`

Expected: FAIL.

- [ ] **Step 3: Implement**

Edit `src/gilbert/core/services/health.py`:

1. Add `"greeting_context"` to capabilities in `service_info()`.
2. Add the two properties:

```python
@property
def greeting_context_id(self) -> str:
    return "health"

@property
def greeting_context_label(self) -> str:
    return "Health"
```

3. Add `greeting_context(user_id)`:

```python
async def greeting_context(self, user_id: str) -> "GreetingContext | None":
    from gilbert.interfaces.greeting import GreetingContext

    try:
        brief = await self.health_brief_for_greeting(user_id)
    except Exception:
        logger.debug("HealthService.greeting_context failed for %s", user_id, exc_info=True)
        return None
    if brief is None or not brief.has_data:
        return None
    parts: list[str] = []
    if brief.sleep_hours is not None:
        parts.append(f"Last night's sleep: {brief.sleep_hours:.1f}h.")
    if brief.steps_today_so_far is not None:
        parts.append(f"Steps today so far: {brief.steps_today_so_far:,}.")
    if brief.weight_latest is not None:
        parts.append(f"Latest weight: {brief.weight_latest:g} {brief.weight_unit.value}.")
    if brief.resting_hr_latest is not None:
        parts.append(f"Latest resting HR: {brief.resting_hr_latest:g} bpm.")
    if brief.flags:
        parts.append(f"Flags: {', '.join(brief.flags)}.")
    if not parts:
        return None
    return GreetingContext(provider_id="health", label="Health", prose=" ".join(parts))
```

(Copy the parts-building loop from `greeting.py:_format_health_brief` verbatim.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_health_service.py -v`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/health.py tests/unit/test_health_service.py
git commit -m "health: implement GreetingContextProvider with prose-formatted brief"
```

---

## Task 5: GreetingService discovers providers and exposes the list

**Files:**
- Modify: `src/gilbert/core/services/greeting.py` (config + on_config_changed + service_info + new helper)
- Test: `tests/unit/test_greeting_service.py` (extend) and `tests/unit/test_greeting_context_integration.py` (new)

This task adds discovery + the `enabled_context_providers` config + a helper that returns the rendered context block. The actual *wiring into the prompt* lands in Task 6 — separating discovery from prompt-rewrite keeps the diff small.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_greeting_context_integration.py`:

```python
"""GreetingService context discovery + assembly tests.

The wiring-into-the-prompt half lands in Task 6's tests; here we only
verify discovery + the assembled labeled block.
"""

import pytest

from gilbert.core.services.greeting import GreetingService
from gilbert.interfaces.greeting import GreetingContext, GreetingContextProvider


class FakeProvider:
    def __init__(self, provider_id: str, label: str, prose: str | None) -> None:
        self._id = provider_id
        self._label = label
        self._prose = prose

    @property
    def greeting_context_id(self) -> str:
        return self._id

    @property
    def greeting_context_label(self) -> str:
        return self._label

    async def greeting_context(self, user_id: str) -> GreetingContext | None:
        if self._prose is None:
            return None
        return GreetingContext(provider_id=self._id, label=self._label, prose=self._prose)


class FakeResolver:
    def __init__(self, providers: list[FakeProvider]) -> None:
        self._providers = providers

    def get_all(self, capability: str) -> list[object]:
        if capability == "greeting_context":
            return list(self._providers)
        return []

    def get_capability(self, name: str) -> object | None:
        return None


@pytest.fixture
def svc_with_providers() -> tuple[GreetingService, list[FakeProvider]]:
    weather = FakeProvider("weather", "Weather", "Sunny, 72°F.")
    briefing = FakeProvider("briefing", "News briefing", "Three items today.")
    health = FakeProvider("health", "Health", None)  # returns None
    providers = [weather, briefing, health]
    svc = GreetingService()
    # Discovery happens inside start() when the resolver is passed in;
    # for unit-test purposes we set the resolver directly and call the
    # public ``available_context_providers()`` method introduced by this
    # task.
    svc._resolver = FakeResolver(providers)
    return svc, providers


async def test_available_context_providers_lists_all_discovered(svc_with_providers) -> None:
    svc, _ = svc_with_providers
    entries = svc.available_context_providers()
    ids = [e["id"] for e in entries]
    assert ids == ["weather", "briefing", "health"]
    assert entries[0]["label"] == "Weather"


async def test_collect_context_returns_only_enabled_with_prose(svc_with_providers) -> None:
    """``health`` returned None — must be excluded.
    ``briefing`` is disabled via config — must be excluded."""
    svc, _ = svc_with_providers
    svc._enabled_context_providers = ["weather", "health"]  # briefing disabled
    block = await svc.collect_context_block(user_id="alice")
    assert "Weather:" in block
    assert "Sunny" in block
    assert "News briefing" not in block  # disabled
    assert "Health" not in block  # returned None


async def test_collect_context_returns_empty_when_none_enabled(svc_with_providers) -> None:
    svc, _ = svc_with_providers
    svc._enabled_context_providers = []
    assert await svc.collect_context_block(user_id="alice") == ""


async def test_collect_context_survives_provider_exception(svc_with_providers) -> None:
    """A buggy provider must never block the greeting."""

    class _Boom:
        @property
        def greeting_context_id(self) -> str:
            return "boom"

        @property
        def greeting_context_label(self) -> str:
            return "Boom"

        async def greeting_context(self, user_id: str) -> GreetingContext | None:
            raise RuntimeError("provider crashed")

    svc, providers = svc_with_providers
    providers.append(_Boom())  # mutates the resolver-backed list
    svc._enabled_context_providers = ["weather", "boom"]
    block = await svc.collect_context_block(user_id="alice")
    assert "Sunny" in block
    assert "Boom" not in block  # crash suppressed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_greeting_context_integration.py -v`

Expected: FAIL — `available_context_providers` / `collect_context_block` don't exist; `_enabled_context_providers` field doesn't exist.

- [ ] **Step 3: Add discovery + assembly to GreetingService**

Edit `src/gilbert/core/services/greeting.py`:

1. In `__init__`, add:

```python
self._enabled_context_providers: list[str] | None = None  # None = "all"
self._context_providers: list[GreetingContextProvider] = []
```

2. Add a new `ConfigParam`:

```python
ConfigParam(
    key="enabled_context_providers",
    type=ToolParameterType.ARRAY,
    description=(
        "Which context providers to include in the available_context "
        "block of the greeting prompt. Discovered providers — Weather, "
        "News briefing, Health, etc. — appear here as toggleable rows. "
        "Leave empty to disable all extras; omit (default) to include "
        "every discovered provider."
    ),
    default=None,  # None means "all discovered"
    required=False,
),
```

3. In `on_config_changed`, read it:

```python
if "enabled_context_providers" in config:
    raw = config["enabled_context_providers"]
    self._enabled_context_providers = list(raw) if raw is not None else None
```

4. In `start(...)` (or wherever `_resolver` is set), call `_discover_context_providers` once the resolver is available:

```python
def _discover_context_providers(self) -> None:
    if self._resolver is None:
        self._context_providers = []
        return
    self._context_providers = [
        svc for svc in self._resolver.get_all("greeting_context")
        if isinstance(svc, GreetingContextProvider)
    ]
```

5. Add the two public helpers:

```python
def available_context_providers(self) -> list[dict[str, str]]:
    """Return the discovered providers' ids + labels for the settings
    UI. Order is stable (registration order)."""
    return [
        {"id": p.greeting_context_id, "label": p.greeting_context_label}
        for p in self._context_providers
    ]

async def collect_context_block(self, user_id: str) -> str:
    """Call each enabled provider, format non-None results into a
    labeled block. Returns ``""`` when no providers contribute.

    A provider raising is logged and skipped — never blocks the
    greeting.
    """
    if not self._context_providers:
        return ""
    enabled = (
        set(self._enabled_context_providers)
        if self._enabled_context_providers is not None
        else None
    )
    entries: list[GreetingContext] = []
    for provider in self._context_providers:
        if enabled is not None and provider.greeting_context_id not in enabled:
            continue
        try:
            ctx = await provider.greeting_context(user_id)
        except Exception:
            logger.debug(
                "GreetingContextProvider %s raised; skipping",
                provider.greeting_context_id,
                exc_info=True,
            )
            continue
        if ctx is None or not ctx.prose:
            continue
        entries.append(ctx)
    if not entries:
        return ""
    return "\n".join(f"{e.label}: {e.prose}" for e in entries)
```

6. Add the imports near the top of the file:

```python
from gilbert.interfaces.greeting import GreetingContext, GreetingContextProvider
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_greeting_context_integration.py -v`

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/greeting.py tests/unit/test_greeting_context_integration.py
git commit -m "greeting: discover context providers and assemble labeled block"
```

---

## Task 6: GreetingService prompt rewrite (drops hardcoded plumbing)

**Files:**
- Modify: `src/gilbert/core/services/greeting.py` (rip out the old code, switch to `{available_context}`)
- Test: `tests/unit/test_greeting_service.py` (update existing tests + add new)

This is the surgical step. Now that the new pool model works (Task 5), this task removes the old hardcoded weather/feeds/health code and points the arrival prompt at the new helper. The bundled arrival prompt also becomes a `ConfigParam(ai_prompt=True)`.

- [ ] **Step 1: Update / add tests**

In `tests/unit/test_greeting_service.py`, update any test that currently asserts on `_build_weather_blurb`, `_maybe_briefing_text`, `_format_health_brief`, or `{weather_section}` / `{health_section}` placeholders. The new contract is:
- The arrival prompt has `{available_context}` exactly once.
- `_generate_greeting` calls `self.collect_context_block(user_id)` and substitutes.

Add this test (or adapt an existing one):

```python
async def test_generate_greeting_includes_context_block(...) -> None:
    """The rendered prompt must contain whatever collect_context_block
    returns, under an ``Available context:`` header."""
    svc = ...  # GreetingService with a fake resolver advertising one
              # FakeProvider that returns prose="Sunny, 72°F."
    # Patch the AI call so we capture the prompt the model sees.
    captured: list[str] = []

    async def _fake_complete_one_shot(*, messages, **_):
        captured.append(messages[0].content)
        class _R:  # minimal shape
            class message:
                content = "Hi there!"
        return _R()

    ai_svc = ...  # provider whose complete_one_shot delegates to _fake
    svc._resolver = ...  # resolver advertising both `ai_chat` and the provider
    text = await svc._generate_greeting("Alice", recent=[])
    assert text == "Hi there!"
    assert "Available context:" in captured[0]
    assert "Weather: Sunny, 72°F." in captured[0]


async def test_generate_greeting_omits_context_section_when_empty(...) -> None:
    """No providers / all disabled → no ``Available context:`` header in
    the prompt at all (don't dangle a header with nothing under it)."""
    svc = ...
    svc._enabled_context_providers = []
    # ... capture prompt ...
    assert "Available context:" not in captured[0]
```

- [ ] **Step 2: Run tests to see them fail**

Run: `uv run pytest tests/unit/test_greeting_service.py -v`

Expected: the two new tests fail; old tests that referenced `_weather` / `_feeds` / `_health` / `_include_*` / `_weather_hint_template` / etc. also fail because those attributes are about to disappear. Delete or rewrite those — they're testing implementation details that this task removes.

- [ ] **Step 3: Replace the inline prompt with a ConfigParam**

In `greeting.py`, add a new bundled default at the top:

```python
_DEFAULT_ARRIVAL_GREETING_PROMPT = """\
Generate a morning greeting for {name} who just arrived at the shop. \
You're Gilbert, an AI assistant at a business. Be creative — vary your \
tone across days (witty, warm, dramatic, deadpan, poetic, nerdy, etc.). \
Mention their name. 1-2 sentences max. Write ONLY the greeting — no \
quotes, no preamble.{style_instruction}{context_section}{avoid_section}"""
```

(The two unchanged placeholders — `{style_instruction}`, `{avoid_section}` — keep their semantics. The new `{context_section}` is what Step 5 fills in.)

In `__init__`:

```python
self._arrival_greeting_prompt: str = _DEFAULT_ARRIVAL_GREETING_PROMPT
```

Append a `ConfigParam`:

```python
ConfigParam(
    key="arrival_greeting_prompt",
    type=ToolParameterType.STRING,
    description=(
        "Prompt template for the auto-generated arrival greeting. "
        "Placeholders: {name}, {style_instruction}, {avoid_section}, "
        "{context_section}. The context section is a labeled list "
        "(\"Weather: ...\", \"Health: ...\", \"News briefing: ...\") "
        "drawn from whichever GreetingContextProvider services are "
        "enabled under enabled_context_providers — you can write "
        "rules like \"always mention the weather if it's extreme.\""
    ),
    default=_DEFAULT_ARRIVAL_GREETING_PROMPT,
    multiline=True,
    ai_prompt=True,
),
```

And in `on_config_changed`:

```python
if "arrival_greeting_prompt" in config:
    self._arrival_greeting_prompt = (
        config["arrival_greeting_prompt"] or _DEFAULT_ARRIVAL_GREETING_PROMPT
    )
```

- [ ] **Step 4: Rewrite `_generate_greeting`**

Replace the existing method body (greeting.py ~lines 859-933, the f-string-built prompt + the `_build_weather_blurb` + `_fetch_health_brief` + `_format_health_brief` + section-string assembly):

```python
async def _generate_greeting(
    self,
    name: str,
    recent: list[str] | None = None,
    *,
    user_id: str | None = None,
) -> str:
    """Generate a personalized arrival greeting via AI, with fallback.

    ``user_id`` is required to fetch per-user context; when it's None
    the greeting still works, just without the contextual block.
    """
    if self._resolver is None:
        return f"Good morning, {name}!"
    ai_svc = self._resolver.get_capability("ai_chat")
    if not isinstance(ai_svc, AISamplingProvider):
        return f"Good morning, {name}!"

    style_instruction = f"\nStyle: {self._style}." if self._style else ""

    avoid_section = ""
    if recent:
        avoid_section = (
            "\n\nHere are your recent greetings — do NOT repeat or closely "
            "paraphrase any of these. Be completely different in tone, "
            "structure, and word choice:\n" + "\n".join(f"- {g}" for g in recent[-7:])
        )

    context_section = ""
    if user_id:
        block = await self.collect_context_block(user_id)
        if block:
            context_section = (
                "\n\nAvailable context (use what's relevant, skip what isn't):\n"
                + block
            )

    prompt = self._arrival_greeting_prompt.format(
        name=name,
        style_instruction=style_instruction,
        avoid_section=avoid_section,
        context_section=context_section,
    )

    try:
        response = await ai_svc.complete_one_shot(
            messages=[Message(role=MessageRole.USER, content=prompt)],
            profile_name=self._ai_profile,
            tools_override=[],
        )
        text = response.message.content.strip()
        if text and len(text) < 500:
            return text
    except Exception:
        logger.warning("AI greeting generation failed", exc_info=True)
    return f"Good morning, {name}!"
```

- [ ] **Step 5: Delete dead code from GreetingService**

Remove these methods entirely (they're now obsolete or live on the contributing services):
- `_build_weather_blurb`
- `_maybe_briefing_text`
- `_fetch_health_brief`
- `_format_health_brief`

Remove these fields from `__init__`:
- `self._weather`
- `self._feeds`
- `self._health`
- `self._include_weather`
- `self._weather_hint_template`
- `self._include_briefing`
- `self._briefing_max_seconds`
- `self._include_health_brief`

Remove the corresponding `ConfigParam` entries (`include_weather`, `weather_hint_template`, `include_briefing`, `briefing_max_seconds`, `include_health_brief`).

Remove the matching reads in `on_config_changed` and the seed-from-section reads (greeting.py ~lines 228-244 + ~lines 472-488).

Remove the `resolver.get_capability("weather")` / `resolver.get_capability("feeds")` / `resolver.get_capability("health")` lookups that populate `self._weather` etc. Replace with the `_discover_context_providers()` call from Task 5.

Also remove the unused imports (`WeatherProvider`, `FeedsProvider`, `HealthProvider`, `GreetingBrief` if it's only used here).

Update all call sites of `_generate_greeting(name, recent)` to pass `user_id=` keyword. Find them with: `grep -n "_generate_greeting" src/gilbert/core/services/greeting.py`. There are typically two: the arrival handler and `_generate_group_greeting` (which calls through for single-person groups).

For `_generate_group_greeting`: pass `user_id=user_ids[0]` (the primary arrival's user_id) so multi-person greetings still get context — keep it simple. Document this in a one-line comment.

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/unit/test_greeting_service.py tests/unit/test_greeting_context_integration.py -v`

Expected: all pass. If existing greeting tests fail because they asserted on removed internals, rewrite them to assert on observable behavior (the rendered prompt content) instead.

Then run the full suite:

Run: `uv run pytest -x -q`

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/gilbert/core/services/greeting.py tests/unit/test_greeting_service.py
git commit -m "greeting: replace hardcoded weather/feeds/health with provider discovery"
```

---

## Task 7: WS RPC to enumerate providers for the settings UI

**Files:**
- Modify: `src/gilbert/core/services/greeting.py` (add WS handler + advertise `"ws_handlers"`)
- Test: `tests/unit/test_greeting_service.py`

The settings page needs a dynamic list of discovered providers; that's a WS RPC, not a static config option. We add `greeting.context_providers.list` returning `[{id, label, enabled}]`.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_greeting_service.py`:

```python
async def test_ws_list_context_providers_returns_discovered_set(...) -> None:
    """The handler returns every discovered provider with the current
    enabled-state flag computed from ``enabled_context_providers``."""
    svc = ...  # two FakeProviders advertised; enabled_context_providers=["weather"]
    handler = ...  # however the test framework gets a WS handler — see
                   # existing tests in this file for the pattern
    result = await handler({"type": "greeting.context_providers.list"})
    assert result == {
        "providers": [
            {"id": "weather", "label": "Weather", "enabled": True},
            {"id": "briefing", "label": "News briefing", "enabled": False},
        ],
    }


async def test_ws_list_context_providers_treats_unset_config_as_all_enabled(...) -> None:
    svc = ...  # enabled_context_providers=None (the default)
    result = await ...
    assert all(p["enabled"] for p in result["providers"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_greeting_service.py -k context_providers -v`

Expected: FAIL — the handler doesn't exist.

- [ ] **Step 3: Implement the handler**

In `greeting.py`:

1. Ensure `"ws_handlers"` is in the service's capabilities (likely already is — check `service_info()`).
2. Add the handler method:

```python
async def _ws_list_context_providers(
    self, frame: dict[str, Any], _user: UserContext
) -> dict[str, Any]:
    """List discovered greeting-context providers and whether each is
    currently included in arrival greetings."""
    enabled_set = (
        set(self._enabled_context_providers)
        if self._enabled_context_providers is not None
        else None  # None = all enabled
    )
    return {
        "providers": [
            {
                "id": p.greeting_context_id,
                "label": p.greeting_context_label,
                "enabled": enabled_set is None or p.greeting_context_id in enabled_set,
            }
            for p in self._context_providers
        ],
    }
```

3. Register the handler. Follow the existing pattern in this file — look for whatever method exposes `ws_handlers` (typically `ws_handlers()` returning a dict of frame_type → method). Add:

```python
"greeting.context_providers.list": self._ws_list_context_providers,
```

4. Verify the ACL default in `src/gilbert/interfaces/acl.py` already covers `greeting.*` at the right level — it should already be there from the broader presence/greeting prefix. If not, add `"greeting.": 100` (user) to `DEFAULT_RPC_PERMISSIONS`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_greeting_service.py -k context_providers -v`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/greeting.py src/gilbert/interfaces/acl.py tests/unit/test_greeting_service.py
git commit -m "greeting: expose discovered context providers over WS RPC"
```

---

## Task 8: Settings UI — dynamic provider toggles

**Files:**
- Modify: `frontend/src/hooks/useWsApi.ts` (add binding)
- Modify: `frontend/src/components/settings/...` (the greeting settings page)
- No new TS types needed unless the settings page is heavily typed.

- [ ] **Step 1: Add the WS RPC binding**

In `frontend/src/hooks/useWsApi.ts`, find the Greeting RPC section (search for `"greeting."`). Add:

```ts
listGreetingContextProviders: () =>
  rpc<{ providers: { id: string; label: string; enabled: boolean }[] }>({
    type: "greeting.context_providers.list",
  }).then((r) => r.providers),
```

- [ ] **Step 2: Locate the greeting settings page**

Run: `grep -rln "include_weather\|include_briefing\|include_health_brief" frontend/src/`

The hit(s) show the greeting settings page. Open it.

- [ ] **Step 3: Replace the static switches with a dynamic list**

In the greeting settings page:

1. Remove the three explicit switches (`include_weather`, `include_briefing`, `include_health_brief`) and the `weather_hint_template` / `briefing_max_seconds` editor rows.
2. On page mount, call `listGreetingContextProviders()` and render one row per provider with a checkbox bound to a local `enabledIds: Set<string>`.
3. When the user toggles any row, write the resulting list to the `greeting.enabled_context_providers` config key via whatever settings-save mechanism the page already uses.
4. Add a section heading "Context contributors" with a short description: "Choose which services contribute to the greeting's available_context block."

The exact React/component idiom follows whatever the page already uses for arrays-of-checkboxes (look for similar patterns elsewhere in `frontend/src/components/settings/` for the established style — don't invent a new one).

- [ ] **Step 4: Build the frontend**

Run: `cd frontend && npx vite build`

Expected: build succeeds with no TS errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/hooks/useWsApi.ts frontend/src/components/settings/
git commit -m "settings(greeting): replace static toggles with dynamic provider list"
```

---

## Task 9: Migration — translate old config keys

**Files:**
- Create: `src/gilbert/migrations/0002_greeting_context_providers.py`
- Test: `tests/unit/test_migration_greeting_context_providers.py`

Existing installations have `greeting.include_weather`, `greeting.include_briefing`, `greeting.include_health_brief`, `greeting.weather_hint_template`, `greeting.briefing_max_seconds`. Translate them.

Per `CLAUDE.md`: **idempotent**. The runner records success *after* `up()` returns, so write the script so a partial prior application doesn't break re-runs (re-reading the source config and re-writing the target config is idempotent because we're computing the same outputs from the same inputs).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_migration_greeting_context_providers.py`:

```python
"""Translates greeting.include_* flags and per-source templates into
the new shape. Idempotent — re-running yields the same end state.

Migration modules whose filename starts with a digit can't be imported
via dotted syntax, so we load by path with ``importlib.util`` — the
same pattern the migration runner uses.
"""

import importlib.util
from pathlib import Path

import pytest

from gilbert.migrations.runner import MigrationContext


def _load_migration():
    repo_root = Path(__file__).resolve().parents[2]
    path = repo_root / "src/gilbert/migrations/0002_greeting_context_providers.py"
    spec = importlib.util.spec_from_file_location("m0002", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ctx(storage):
    """Minimal MigrationContext — only the ``storage`` field is read."""
    import logging
    return MigrationContext(
        storage=storage,
        logger=logging.getLogger("test"),
        repo_root=Path("."),
    )


async def test_migration_translates_per_source_flags(sqlite_storage) -> None:
    M = _load_migration()
    await sqlite_storage.put("gilbert.config", "greeting", {
        "include_weather": True,
        "include_briefing": False,
        "include_health_brief": True,
        "weather_hint_template": "custom template {temperature}",
        "briefing_max_seconds": 45,
    })
    await M.up(_ctx(sqlite_storage))
    greeting = await sqlite_storage.get("gilbert.config", "greeting")
    assert "include_weather" not in greeting
    assert "include_briefing" not in greeting
    assert "include_health_brief" not in greeting
    assert "weather_hint_template" not in greeting
    assert "briefing_max_seconds" not in greeting
    assert greeting["enabled_context_providers"] == ["weather", "health"]

    weather = await sqlite_storage.get("gilbert.config", "weather")
    assert weather["weather_hint_template"] == "custom template {temperature}"

    feed_briefing = await sqlite_storage.get("gilbert.config", "feed_briefing")
    assert feed_briefing["briefing_max_seconds"] == 45


async def test_migration_is_idempotent(sqlite_storage) -> None:
    M = _load_migration()
    await sqlite_storage.put("gilbert.config", "greeting", {
        "include_weather": True,
        "include_briefing": True,
        "include_health_brief": False,
    })
    await M.up(_ctx(sqlite_storage))
    snapshot1 = await sqlite_storage.get("gilbert.config", "greeting")
    await M.up(_ctx(sqlite_storage))  # apply again
    snapshot2 = await sqlite_storage.get("gilbert.config", "greeting")
    assert snapshot1 == snapshot2


async def test_migration_handles_missing_greeting_config(sqlite_storage) -> None:
    """Fresh install — no greeting config row yet. Migration must not crash."""
    M = _load_migration()
    await M.up(_ctx(sqlite_storage))  # should be a no-op
    assert await sqlite_storage.get("gilbert.config", "greeting") is None
```

Use whatever `sqlite_storage` fixture the integration tests already provide — see `tests/integration/conftest.py` for the established pattern; the migration runner tests are also a good reference. If you need to construct the `MigrationContext` differently, mirror what `tests/unit/test_migration_runner*.py` (or its integration equivalent) already does.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_migration_greeting_context_providers.py -v`

Expected: ImportError — the migration file doesn't exist.

- [ ] **Step 3: Write the migration**

Create `src/gilbert/migrations/0002_greeting_context_providers.py`:

```python
"""Translate greeting.include_* + per-source templates onto the
GreetingContextProvider model.

Idempotent: re-running computes the same outputs from the same inputs.
After translation, the old keys are removed from greeting's config.
"""

from __future__ import annotations

from gilbert.migrations.runner import MigrationContext

description = "Translate greeting include_* flags to enabled_context_providers"


async def up(ctx: MigrationContext) -> None:
    storage = ctx.storage
    greeting = await storage.get("gilbert.config", "greeting")
    if greeting is None:
        return

    old_include_weather = greeting.pop("include_weather", None)
    old_include_briefing = greeting.pop("include_briefing", None)
    old_include_health = greeting.pop("include_health_brief", None)
    old_weather_template = greeting.pop("weather_hint_template", None)
    old_briefing_max = greeting.pop("briefing_max_seconds", None)

    # Only emit enabled_context_providers if at least one old flag was
    # set explicitly. If none were set, leave the new key absent so the
    # service falls back to its "all discovered" default.
    explicit_flags = any(
        v is not None
        for v in (old_include_weather, old_include_briefing, old_include_health)
    )
    if explicit_flags:
        enabled: list[str] = []
        if old_include_weather is not False:  # treat None as default-on (matches pre-migration default)
            enabled.append("weather")
        if old_include_briefing is True:  # default-off, only include when explicitly True
            enabled.append("briefing")
        if old_include_health is not False:
            enabled.append("health")
        greeting["enabled_context_providers"] = enabled

    await storage.put("gilbert.config", "greeting", greeting)

    if old_weather_template:
        weather = await storage.get("gilbert.config", "weather") or {}
        weather["weather_hint_template"] = old_weather_template
        await storage.put("gilbert.config", "weather", weather)

    if old_briefing_max is not None:
        feed_briefing = await storage.get("gilbert.config", "feed_briefing") or {}
        feed_briefing["briefing_max_seconds"] = old_briefing_max
        await storage.put("gilbert.config", "feed_briefing", feed_briefing)
```

Verify the pre-migration defaults: `include_weather=True`, `include_briefing=False`, `include_health_brief=True`. The "default-on" mapping above preserves behavior for users who never explicitly disabled a contributor.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_migration_greeting_context_providers.py -v`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/migrations/0002_greeting_context_providers.py tests/unit/test_migration_greeting_context_providers.py
git commit -m "migration: translate greeting include_* flags to context-provider toggles"
```

---

## Task 10: Documentation

**Files:**
- Modify: `README.md` (Greeting bullet, or wherever greetings are described)
- Modify: `docs/architecture/agent-service.md` (or add `docs/architecture/greeting-service.md` if there isn't one yet — check first)

- [ ] **Step 1: Update the project README**

Edit `README.md`. Find the existing greeting / morning-arrival description and replace any "weather and news are wired in" language with the extension-point story:

```
The Greeting service produces personalized morning arrivals via a
**GreetingContextProvider** capability — any service can advertise
``"greeting_context"`` and contribute a labeled prose fact. The bundled
WeatherService, FeedBriefingService, and HealthService each implement
this; the user's prompt template decides what to mention. A
"Context contributors" toggle in the greeting settings page lists
every discovered provider, so installing a new plugin that contributes
context is plug-and-play with no greeting-service edits.
```

- [ ] **Step 2: Update the architecture doc**

Check `ls docs/architecture/` for an existing greeting doc. If none exists, create `docs/architecture/greeting-service.md` with a single section "Context providers" describing:
- The protocol shape (id, label, async `greeting_context(user_id)`).
- The discovery mechanism (`resolver.get_all("greeting_context")` in `_discover_context_providers`).
- The assembly contract ("labeled lines, AI integrates them, providers must never raise").
- The pool model (no ordering, user prompt decides).
- The `enabled_context_providers` config + the `greeting.context_providers.list` WS RPC.

If a greeting doc already exists, add the section to it.

- [ ] **Step 3: Run the architecture audit**

Run the `validate-architecture` skill in audit mode. It should report clean — but specifically watch for:
- Hardcoded AI prompts: the new `arrival_greeting_prompt` ConfigParam must be `ai_prompt=True` (verified by the audit's grep).
- README freshness: covered by Step 1.

If anything else surfaces, fix it inline.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/architecture/
git commit -m "docs: GreetingContextProvider extension point + arrival prompt config"
```

---

## Self-Review Checklist

After all 10 tasks land, walk through these before declaring done:

- [ ] The `validate-architecture` skill audit reports clean.
- [ ] `uv run pytest -x -q` passes (no regressions from any pre-existing test).
- [ ] `grep -n "_weather\b\|_feeds\b\|_health\b\|_include_weather\|_include_briefing\|_include_health_brief\|_build_weather_blurb\|_maybe_briefing_text\|_fetch_health_brief\|_format_health_brief\|_weather_hint_template\|_briefing_max_seconds" src/gilbert/core/services/greeting.py` returns nothing.
- [ ] `grep -n "weather_section\|health_section" src/gilbert/core/services/greeting.py` returns nothing.
- [ ] The greeting settings page in the SPA shows a dynamic "Context contributors" section, populated by `listGreetingContextProviders()`.
- [ ] Toggling a contributor off and reloading the SPA shows the toggle persists (via `greeting.enabled_context_providers` config).
- [ ] Running the migration against an installation with the old keys + a stub gilbert.config greeting row produces the new shape with no data loss.
- [ ] The `arrival_greeting_prompt` is a `ConfigParam(multiline=True, ai_prompt=True)` (not a literal string in `_generate_greeting`).
