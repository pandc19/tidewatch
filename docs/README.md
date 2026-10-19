# Documentation Index

## Start here

- [GETTING_STARTED.md](../GETTING_STARTED.md): local setup
- [SETUP_CHECKLIST.md](../SETUP_CHECKLIST.md): every external piece to set up, in order, with how to check it and what is unverified
- [TESTING.md](../TESTING.md): manual test plan, with the reading list to do first
- [glossary.md](glossary.md): the trading and operations terms used here, with this project's values
- [git-flow.md](git-flow.md): branches, PRs, commit conventions, CI and the GitHub settings to apply
- [guides/code-flow.md](guides/code-flow.md): how one trade moves through the system, and why
- [guides/running-modes.md](guides/running-modes.md): dry run vs demo vs rehearsal, one bot per testnet account, resetting a database; with [guides/queries.sql](guides/queries.sql) for DBeaver
- [guides/developer-guide.md](guides/developer-guide.md): conventions, tests, migrations, adding things

## Operations

- [coolify.md](coolify.md): deploying with Coolify (the plan: push to `main` builds the Dockerfile and starts the bot)
- [deployment.md](deployment.md): the manual route, a droplet with docker-compose

## Decisions

- [adr/README.md](adr/README.md): index and template

## Root files

- [README.md](../README.md): overview and status
- [NEXT_STEPS.md](../NEXT_STEPS.md): pending work only
- [CLAUDE.md](../CLAUDE.md): rules for AI coding assistants

```
docs/
├── README.md
├── coolify.md
├── deployment.md
├── git-flow.md
├── glossary.md
├── guides/
│   ├── code-flow.md
│   ├── developer-guide.md
│   ├── running-modes.md
│   └── queries.sql
└── adr/
    ├── README.md
    ├── 001-decimal-and-exchange-filters.md
    ├── 002-positions-and-order-schema.md
    ├── 003-safe-defaults-and-single-position.md
    ├── 004-stream-is-notification-rest-is-truth.md
    ├── 005-engine-evaluates-state.md
    ├── 006-risk-controls-and-the-entry-signal.md
    └── 007-telegram-alerts-are-queued.md
```
