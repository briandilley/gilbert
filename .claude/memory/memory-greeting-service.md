# Greeting Service

## Summary
Personalized arrival greetings driven by `presence.arrived` events. Generates unique 1-2 sentence greetings via the AI and announces them over speakers. Tracks who has been greeted today (per-user lock guards concurrent races). When the WeatherService is available it enriches the greeting prompt with a deterministic one-line weather blurb.

## Details

### Trigger and gating
- Subscribes to `presence.arrived` and runs `_greet_user(user_id)` if the current time is inside `[start_hour, cutoff_hour)` in the configured timezone.
- Per-user `asyncio.Lock` (lazily created in `_greeting_locks`) prevents concurrent presence events from double-greeting the same person.
- "Greeted today" persistence in `greeting_state` collection: `last_greeting_date` (today) gates re-greets; `recent_greetings` (last 10) is fed back to the AI as anti-repetition context.
- Startup hook: scheduled `Schedule.once_after(45)` job calls `_greet_already_present` so a Gilbert restart while people are already at the shop doesn't drop their greeting (presence service suppresses events on the first poll).

### AI generation path
`_generate_greeting(name, recent)` calls `AISamplingProvider.complete_one_shot` with `tools_override=[]` (forces zero tool access regardless of profile) and `profile_name=self._ai_profile` (default `"light"`). The prompt mentions the name, the style instruction (if any), and includes the recent-greetings list to avoid repetition. Group greetings (`_generate_group_greeting`) batch the names and produce one combined message instead of stepping over each other.

### Weather integration (added in feature 02)
- `start()` does `self._weather = resolver.get_capability("weather")` and gates on `isinstance(svc, WeatherProvider)` — never an `isinstance` against the concrete class.
- New `include_weather` (BOOL, default true) and `weather_hint_template` (STRING, multiline, `ai_prompt=True`) ConfigParams. The template is a Python `str.format` template whose rendered output is interpolated into Gilbert's main greeting prompt as `Context: …`, so per the [AI Prompts Are Always Configurable](memory-ai-prompts-configurable.md) rule it ships with `ai_prompt=True` to expose the Author-with-AI affordance to operators.
- `_build_weather_blurb(user)` calls `WeatherProvider.get_current(user=user_ctx)`, catches `LocationNotConfiguredError` (silent skip) and `WeatherUnavailableError` (logged debug). Bare `except Exception` would hide programming bugs — typed catches surface them.
- Unit suffixes (`°C` vs `°F`, `km/h` vs `mph`) come from `current.units`. Location name comes from `current.location.name` (no "the shop" hardcoding). The blurb is injected into the AI greeting prompt under a `Context: …` line so the model sees it but isn't forced to mention it.
- The template's available placeholders: `{location_name}`, `{temperature}`, `{temp_suffix}`, `{condition_phrase}`, `{wind_speed}`, `{speed_suffix}`, `{feels_like_clause}`. The default template includes anti-fabrication guidance ("Quote only the values shown — never invent additional weather details.").

### Tools / slash commands
Single tool `greet` (slash command `/greet <name>`) — calls `_generate_greeting(person_name)` and announces. No multi-target slash group.

### Anti-repetition
`recent_greetings` rolls 10 entries; the most recent 7 are fed into the prompt's "Here are your recent greetings — do NOT repeat" section. Persisted per-user under `greeting_state.{user_id}`.

## Related
- [Weather Service](memory-weather-service.md) — provides the `WeatherProvider` capability that feeds the weather blurb.
- [Capability Protocols](memory-capability-protocols.md) — `WeatherProvider` is the protocol used to access weather without coupling to the concrete service class.
- [AI Prompts Are Always Configurable](memory-ai-prompts-configurable.md) — both `style` and `weather_hint_template` ship with `ai_prompt=True` since their values flow into the greeting AI prompt.
- [Presence Service](memory-presence-service.md) — publishes `presence.arrived`.
- `src/gilbert/core/services/greeting.py`

