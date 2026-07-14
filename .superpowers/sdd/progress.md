# Music playlists — SDD progress

Plan: docs/superpowers/plans/2026-07-14-music-playlists.md
Branch: feat/music-playlists
Base: 85fb64b

Task 1: complete (commits bd2e36d..dfa4295, review clean — Spec ✅ / Quality Approved)

Open findings for final review to triage:
- [Important, plan-mandated] PlaylistStore.create() TOCTOU: _find() then _save() with no
  unique constraint; concurrent same-name creates could both land. Proper fix = stored
  normalized-name field + unique index. Low likelihood (single user, sequential awaits).
- [Minor] test_get_by_name_roundtrips_items doesn't populate items; the MusicItem
  dict round-trip is covered in Task 2 (test_add_item_persists_full_snapshot).
- Env note: ruff/mypy not in .venv (dev extras unsynced). Reviewer ran them via
  `uv run --no-sync --with mypy`. Task 6 must not run bare `uv sync --extra dev`
  (uv.lock churn risk) — use --with instead.
Task 2: complete (commits dfa4295..37533ff, review clean — Spec ✅ / Quality Approved)

ESCALATED: the TOCTOU finding now covers add_item/remove_at/update/delete too. Reviewer:
concurrent add_item calls race and one append is SILENTLY LOST (last-writer-wins on the
whole JSON blob). Recommend fixing before merge. Options: optimistic concurrency via an
updated_at/version check on write, or a normalized-name unique index (fixes create only).
Minor: no test asserts created_at survives a mutation; remove_at middle-of-list untested.
Task 3: complete (commits 37533ff..6039cfe, review clean — Spec ✅ / Approved)
  Required 2 fix rounds:
  - Fix 1 (5f98fa4): list_playlists description disambiguated both ways; SYSTEM-sentinel guard
    added to all 5 handlers (scheduled/email turns were silently creating "system"-owned
    playlists); event-emission test added; JSON-null name -> "None" fixed.
  - Fix 2 (6039cfe): CROSS-USER EVENT LEAK. music.playlist_* had no WS fan-out owner filter,
    so any signed-in user got {name, owner_user_id} of everyone else's playlists. Added
    can_see_music_event (prefix music.playlist_, fails closed). Payload key owner_user_id ->
    user_id (the convention every per-user filter reads). music.playback_started still
    broadcasts (shared speaker). Deliberately NO admin bypass, matching the store contract.
  Plan corrected mid-flight: UserContext is (user_id, email, display_name, roles) from
  interfaces/auth — not (username, role). SYSTEM guard added to Global Constraints for T4/T5.
Task 4: complete (commits 6039cfe..7b46895, review clean — Spec ✅ / Approved)
  Fix round (7b46895): split the try in _tool_add_to_playlist so the broad `except RuntimeError`
  covers only item RESOLUTION (correct: "backend not enabled"/"speaker unavailable" are bare
  RuntimeErrors, so the plan's narrow tuple was wrong) + logs it; the store call gets the narrow
  `except PlaylistError`, so infra failures no longer surface as cheerful prose. Test assertion
  tightened ("no results" + playlist stayed empty).
  Note for T5: an item added from now-playing uses its URI as its id (speaker exposes no track id)
  and carries service=""/didl_meta="". resolve_playable passes URIs through, so playback is fine;
  a now-playing add of a RADIO station may not replay cleanly (needs the DIDL envelope).
Task 5: complete (commits 7b46895..819e917, review clean — Spec ✅ / Approved)
  Fix round (819e917): the shuffle test was VACUOUS — set(order)==expected passes for the
  identity permutation. Reviewer mutation-tested: removing random.shuffle AND ignoring an
  explicit shuffle=True both survived the whole suite. Rewritten with a deterministic
  monkeypatched shuffle (seq.reverse()); both mutants now confirmed killed by two agents
  independently. Also: empty-playlist now asserts zero speaker calls; "1 tracks" grammar;
  dropped an unrequested volume guard that advertised a range it never enforced.
  Implementer correctly rejected the brief's `queued < total` message, which would have
  reported a queueless backend as "1 of 2 (1 unavailable)" — a capability gap misreported
  as missing tracks.
Task 6: complete (commits 819e917..e55f7b0, submodule e516519)
  Stale Spotify scope comment fixed in the sonos submodule. No README music-command list
  existed, so none was invented; root README Music bullet updated (it enumerated the tool
  surface). Full suite: 4935 passed, 2 pre-existing kokoro failures. ruff/mypy: zero NEW
  findings vs the pre-feature baseline.

FINAL WHOLE-BRANCH REVIEW: initially NOT READY — 2 blockers, both fixed in b01707b:
  1. play_playlist hard-failed on an unresolvable FIRST item (only the enqueue loop caught
     failures). One delisted track at position 1 killed the whole play — and under shuffle it
     was nondeterministic. Missed by every per-task review (each saw only one task's diff).
     Now: skip leading failures, play the first that works, prose if none play.
  2. PlaylistStore TOCTOU finally fixed: asyncio.Lock around the 5 mutators (reads unlocked;
     no self-deadlock — verified). Concurrency test proven to fail without the lock.
  Also fixed: the no-queue count was wrong once leading items could be skipped; enum
  serialization divergence (str(kind) -> kind.value).
RE-REVIEW: Task quality Approved — READY TO MERGE.

ACCEPTED (not blocking; follow-ups if wanted):
- Lock is correct only single-process/single-store. Multi-process would need CAS/version on
  StorageBackend (out of scope; noted in the class docstring).
- A now-playing add of a RADIO station stores kind=TRACK; NowPlaying.source could guard it.
  (SonosMusic.resolve_playable discards didl_meta for ALL items, so this is a backend gap.)
- AI emitting the STRING "false" for a boolean arg is truthy via bool().
- Multi-word playlist names need quoting in slash commands (pre-existing convention).
