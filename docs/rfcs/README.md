# QPI RFCs

Design documents for substantial QPI features. Each RFC is self-contained: it
holds both the system design and its phased implementation plan, so a contributor
(human or coding agent) can execute it without re-deriving the architecture.

## Index

| RFC | Title | Status |
| --- | --- | --- |
| [0001](./0001-driver-framework.md) | Driver Framework | Implemented |
| [0002](./0002-dashboard-theming.md) | Dashboard Theming | Implemented |
| [0003](./0003-driver-extensibility.md) | Driver Extensibility | Implemented |
| [0004](./0004-calibration-tuners.md) | Calibration Tuners | Implemented |
| [0005](./0005-calibration-graph-completion.md) | Calibration Graph Completion | Implemented |
| [0006](./0006-calibration-graph-in-the-dashboard.md) | The Calibration Graph in the Dashboard | Draft |

Neither calibration RFC has been verified against physical hardware; both say so
where it matters.

## Conventions

- Number sequentially: `000N-short-slug.md`.
- Keep design, plan, and decisions in the one RFC file — no separate ADRs. Record
  design decisions in a "Decisions" section of the RFC itself. Split only if a
  document becomes genuinely unwieldy.
- RFCs are living documents; move status Draft → Accepted → Implemented as the
  feature progresses.
