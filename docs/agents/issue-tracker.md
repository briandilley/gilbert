# Issue tracker: GitHub

Issues and PRDs for this repo live as GitHub issues on [`briandilley/gilbert`](https://github.com/briandilley/gilbert). Use the `gh` CLI for all operations.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`. Use a heredoc for multi-line bodies.
- **Read an issue**: `gh issue view <number> --comments`, filtering comments by `jq` and also fetching labels.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."`
- **Apply / remove labels**: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **Close**: `gh issue close <number> --comment "..."`

`gh` infers the repo from `git remote`. This clone has two remotes — `origin` →
`briandilley/gilbert` (canonical) and `jereanon` → a fork. `gh` targets `origin` by default;
pass `--repo briandilley/gilbert` if you ever need to be explicit.

> **Submodule note.** Plugins live in the `std-plugins/` submodule, which is its own repo
> (`briandilley/gilbert-plugins`). Issues about a specific plugin may belong on that repo's tracker
> — run `gh` from inside `std-plugins/`, or pass `--repo briandilley/gilbert-plugins`. When in
> doubt, file against `briandilley/gilbert` and cross-link.

## When a skill says "publish to the issue tracker"

Create a GitHub issue.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --comments`.
