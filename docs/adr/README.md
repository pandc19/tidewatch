# Architecture Decision Records

An ADR records a significant technical decision: the context, the decision, and the consequences. It answers "why is the code like this?" Write one when choosing between viable approaches, making something hard to reverse, or deliberately not doing something obvious. Skip it for routine implementation details.

## Index

| ADR | Title | Status |
|---|---|---|
| [001](001-decimal-and-exchange-filters.md) | Decimal money and exchange filter rounding | Accepted |
| [002](002-positions-and-order-schema.md) | Positions table and order schema | Accepted |
| [003](003-safe-defaults-and-single-position.md) | Safe defaults and one position at a time | Accepted |
| [004](004-stream-is-notification-rest-is-truth.md) | The account stream is a notification, REST is the truth | Accepted |

## Template

```markdown
# ADR-XXX: Title

## Status
Proposed | Accepted | Deprecated | Superseded by ADR-XXX

## Date
YYYY-MM-DD

## Context
## Decision
## Consequences
```
