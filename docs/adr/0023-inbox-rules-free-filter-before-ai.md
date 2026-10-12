# Inbox rules pass a free filter before any AI call, and the match call returns the dedupe key

An *inbox rule* runs in three stages. Stage 1 is a filter on sender, subject and keywords with no
AI cost. Stage 2 is one small AI call (`light` profile, no tools) on the subject and a snippet. It
answers "match or no match" and returns a dedupe key, for example `ups:<tracking>:delivered`. Stage 3
is an AI turn on the rule's action profile, with only the tools that the profile allows. It runs only for
a match whose dedupe key the rule did not handle yet.

Every rule must declare a stage 1 filter. A rule cannot send all mail to the AI. The mailbox
receives about 60 messages each day, and most of them are not relevant. Sending each one to a model
costs money for no result. The dedupe key comes from stage 2 because the engine is generic. It
cannot know what a tracking number or order number is, but the match call reads the email anyway.

## Amendment (2026-10-12): the engine does not speak

The first version spoke the action turn's reply on the speakers and had an `announce` flag and a
speaker list on each rule. Speaking is now just one tool among others. The rule's instructions ask
for it, and the rule's AI profile includes the `announce` tool. The days and hours stay in the
engine as the rule's *active window*, because an AI turn cannot defer itself to a later time. A held
match costs no tokens. Dry run now stops after the match call, because the engine cannot stop a
tool call inside the turn.

## Considered options

- **AI reads every email**: rejected. The cost grows with mail volume, not with the matches.
- **Python handler per use case**: rejected. Each new use case needs a code change. Rules are
  admin-editable records.
- **The engine speaks the action's reply**: replaced (2026-10-12). It tied a generic feature
  to one kind of action.
- **A tool list on each rule**: rejected (2026-10-12). Tool gating already lives on AI profiles,
  as it does for subagent types. A second list on the rule would let the two disagree.
- **The action turn searches a history tool to find duplicates**: rejected. Each duplicate (a
  carrier email and a vendor email for the same package) costs a full AI turn.
