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
| [0007](./0007-calibration-without-priors.md) | Calibration Without Priors | Draft |

RFCs 0004 and 0005 were written before the graph had run on a chip, and say so where it
matters. RFC 0007 is the opposite case: it exists because of what running it on one
found.

## Conventions

- Number sequentially: `000N-short-slug.md`.
- Keep design, plan, and decisions in the one RFC file — no separate ADRs. Record
  design decisions in a "Decisions" section of the RFC itself. Split only if a
  document becomes genuinely unwieldy.
- RFCs are living documents; move status Draft → Accepted → Implemented as the
  feature progresses.
