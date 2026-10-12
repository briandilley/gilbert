# Inbox rules

## Summary

An inbox rule reacts to new inbound email. It does three things in order: a free filter, a small AI match call, and an AI action turn. The action's reply is spoken on the speakers. The design keeps AI cost low, because most email stops at the free filter. [ADR-0023](../adr/0023-inbox-rules-free-filter-before-ai.md) records why.

## Details

### Where it runs

`InboxRulesService` (`src/gilbert/core/services/inbox_rules.py`) subscribes to `inbox.message.received`. The inbox service already polls each mailbox and publishes that event, so rules add no Gmail calls. The handler starts a tracked background task, because the event bus awaits every handler. One lock serializes the processing, because the volume is low.

The service always runs. With no rules, it does nothing. Each rule has its own `enabled` and `dry_run` switches.

### The three stages

1. Free filter. The rule's `sender_contains` and `subject_contains` lists match as case-insensitive substrings. The sender text includes the display name and `original_sender`. Mail that comes through a Google group shows the vendor only in the display name ("'Amazon.com' via Vendors"). A rule must set at least one of the two lists. `save_rule` refuses a rule without one.
2. Match call. `complete_one_shot` runs with `tools_override=[]` on the rule's `match_profile` (default `light`). It reads the subject and the first 1,500 characters of the body. It returns JSON with `match`, `dedupe_key` and `reason`.
3. Action turn. `chat` runs headless as `UserContext.SYSTEM`, with `tool_filter=("include", rule.action_tools)` and at most six tool rounds, on `action_profile` (default `standard`). Its final text is spoken as-is. The reply `SKIP` means say nothing.

The two system prompts are `ConfigParam(ai_prompt=True)` values under the `inbox_rules` configuration namespace. The rule's condition and instructions are rule data. The rules page edits them.

### Run records

Each (rule, message) pair gets one row in `inbox_rule_runs`, with the id `<rule_id>:<message_id>`. A message that already has a row is never evaluated again for that rule. The row is also the activity log on the rules page. The statuses are in `RunStatus` (`src/gilbert/interfaces/inbox_rules.py`):

| Status | Meaning |
|---|---|
| `no_match` | The match call said no. `reason` says why, for example "shipped notice". |
| `duplicate` | The dedupe key was handled inside `dedupe_window_hours`. |
| `held` | A match outside business hours. The flush job tries again. |
| `retry` | The action failed. The flush job tries again, up to three attempts in all. |
| `done` | The action ran. The reply was spoken when the rule announces. |
| `silent` | The action replied `SKIP`, or replied with nothing. |
| `dry_run` | The action ran. The reply is in `output` and was not spoken. |
| `stale` | The email is older than `max_age_hours`, or the rule was removed or disabled. |
| `failed` | The match call failed, or the action failed three times. |

Dedupe keys live in `inbox_rule_dedupe`, with the id `<rule_id>:<key>`. The key comes from the match call, because the service is generic and cannot know what an order number is.

### Business hours and staleness

A rule speaks only on `active_days` between `start_hour` and `end_hour`, in its `timezone`. A match outside those hours is held. The system job `inbox_rules.flush` runs every five minutes and moves held and retrying runs forward. An email older than `max_age_hours` (default 4) is dropped. As a result, an email that arrives at night is not announced the next morning. The action turn runs only when the reply can be spoken, so a held match costs nothing until then.

### Access

The `inbox_rules.*` RPCs and events are admin-only (`interfaces/acl.py`). The page is at `/inbox/rules`, under the Inbox menu.

### Seeded rules

Business-specific rules come from plugin migrations, not from core. The `current-data-sync` local plugin seeds the "Deliveries" rule. That rule uses the plugin's `current_expense_match` tool to connect a delivery to project expenses.

## Related

- `src/gilbert/interfaces/inbox_rules.py`: `InboxRule`, `RunStatus`, collection names, `InboxRulesProvider`
- `src/gilbert/core/services/inbox_rules.py`: the service
- `frontend/src/components/inbox/InboxRulesPage.tsx`: the rules page and activity log
- `tests/unit/test_inbox_rules_service.py`: unit tests
- [`inbox-service.md`](inbox-service.md): where `inbox.message.received` comes from
