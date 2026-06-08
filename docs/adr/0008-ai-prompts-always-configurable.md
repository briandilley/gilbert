# AI prompts are always operator-configurable ConfigParams

Every non-trivial string passed to the AI as a system prompt (`complete_one_shot(system_prompt=…)`,
`chat(system_prompt=…)`, a `SYSTEM` `Message`) must be exposed as a
`ConfigParam(multiline=True, ai_prompt=True)` on the owning service, with the bundled string as its
`default`. The active value is read from a cached field (`self._foo_prompt`, refreshed in
`on_config_changed`), never from the `_DEFAULT_*` constant directly.

Operators must be able to tune the assistant's behavior at runtime without a code change — and the
rule is deliberately strict: even a one-line context blurb interpolated into a prompt triggers it.
The cost is boilerplate (a ConfigParam + cached field) per prompt; the payoff is that nothing the
model is told is hardcoded out of an operator's reach.
