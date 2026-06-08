# Pure-text AI calls force zero tools at the call site (`tools_override=[]`)

Calls that want the model to *write text* rather than *do something* — greeting, roast — pass
`tools_override=[]`, which replaces profile-driven tool discovery entirely and guarantees no tools
are exposed regardless of which profile was resolved. The profile still picks backend and model.

This came from a real bug: a greeting routed to a tools-enabled profile, and the model invoked the
`announce` tool repeatedly as its "way of" greeting, firing a fresh TTS playback each time (the
Sonos audio-clip loop). Forcing tools off at the call site is the fix.

## Considered options

- **A dedicated "text-only" profile** — rejected: the profile's job is to pick backend/model, and a
  text-only call can legitimately run under any tier. The constraint ("no tools this call") belongs
  at the call site, not in a special profile users would have to know to select.
