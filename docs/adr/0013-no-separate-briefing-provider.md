# `build_briefing` lives on the feeds service, not behind a separate `BriefingProvider`

The daily-briefing *text builder* is a method on the feeds service rather than its own capability
protocol or service, because it already reads feed storage and uses feed-scoped scoring policy and
prompt config. Introducing a second protocol for a single method on a service the caller has already
resolved was judged overengineering.

Note the deliberate asymmetry: the briefing *schedule and fan-out* **is** split into a separate
service (it decides who gets briefed and when, and owns no AI calls). Only the text builder stays on
feeds. A future reader tempted to extract a `BriefingProvider` for symmetry should read this first.
