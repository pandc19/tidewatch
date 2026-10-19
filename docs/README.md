# Documentation Index

## Start here

- [GETTING_STARTED.md](../GETTING_STARTED.md): local setup
- [SETUP_CHECKLIST.md](../SETUP_CHECKLIST.md): every external piece to set up, in order, with how to check it and what is unverified
- [TESTING.md](../TESTING.md): manual test plan, with the reading list to do first
- [guides/code-flow.md](guides/code-flow.md): how one trade moves through the system, and why
- [guides/developer-guide.md](guides/developer-guide.md): conventions, tests, migrations, adding things

## Operations

- [deployment.md](deployment.md): DigitalOcean droplet with docker-compose

## Decisions

- [adr/README.md](adr/README.md): index and template

## Root files

- [README.md](../README.md): overview and status
- [NEXT_STEPS.md](../NEXT_STEPS.md): pending work only
- [CLAUDE.md](../CLAUDE.md): rules for AI coding assistants

```
docs/
├── README.md
├── deployment.md
├── guides/
│   ├── code-flow.md
│   └── developer-guide.md
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
