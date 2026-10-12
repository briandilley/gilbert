# Inbox rules pass a free filter before any AI call, and the match call returns the dedupe key

An *inbox rule* runs in three stages. Stage 1 is a filter on sender, subject and keywords with no
AI cost. Stage 2 is one small AI call (`light` profile, no tools) on the subject and a snippet. It
answers "match or no match" and returns a dedupe key, for example `ups:<tracking>:delivered`. Stage 3
is an AI turn on the `standard` profile with only the tools that the rule allows. It runs only for
a match whose dedupe key the rule did not handle yet.

Every rule must declare a stage 1 filter. A rule cannot send all mail to the AI. The mailbox
receives about 60 messages each day, and most of them are not relevant. Sending each one to a model
costs money for no result. The dedupe key comes from stage 2 because the engine is generic. It
cannot know what a tracking number or order number is, but the match call reads the email anyway.

## Considered options

- **AI reads every email**: rejected. The cost grows with mail volume, not with the matches.
- **Python handler per use case**: rejected. Each new use case needs a code change. Rules are
  admin-editable records.
- **The action turn searches a history tool to find duplicates**: rejected. Each duplicate (a
  carrier email and a vendor email for the same package) costs a full AI turn.
