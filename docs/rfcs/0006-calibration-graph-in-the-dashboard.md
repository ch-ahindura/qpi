# RFC 0006 — The Calibration Graph in the Dashboard

- **Status:** Draft
- **Author:** Martin Ahindura
- **Created:** 2026-08-06
- **Depends on:** RFC 0004 (the `calibrate` operation, the DAG walk, the report),
  RFC 0005 (the thirty-three-node graph and its checks)
- **Touches:** `qpi-driver` (Python), `qpi-ui` (server and dashboard). One new event
  type; no new operation and no new collection.

## 1. The idea

The Calibration tab can now say `step 7 of 33 — rabi on q2`, and draw a bar. That is
a position in a list. The thing actually being walked is a graph, and its shape is
the part an operator reasons with: which nodes are downstream of the one that just
failed, whether the run is still in readout bring-up or has reached the two-qubit
gate, what a stalled node is waiting on.

The dashboard cannot draw that graph, for one reason: **it does not know it.** The
graph exists only as `depends_on` on thirty-three Python classes inside the driver.
Nothing crosses the wire that describes it.

This RFC gets the graph across, draws it, and makes a node clickable — first showing
what is already known about it, later showing the data behind its fit.

## 2. What exists today

Established by reading the code, not assumed:

**The graph.** Thirty-three routines, thirty-five dependency edges, a single root
(`resonator_spectroscopy`), eleven layers deep and at most eight nodes wide. Four
routines are benchmarks (`readout_fidelity`, `rb`, `interleaved_rb`, `allxy_check`),
six target edges rather than qubits, four carry a check. Serialised with the fields
below it is **5.3 kB of JSON** — small enough that how we ship it is not a
performance question.

**Per-node metadata already declared.** Every `CalibrationRoutine` has `name`,
`depends_on`, `targets` (`qubits`|`edges`), `is_benchmark`, `has_check`,
`measures_itself`, and `updates` — the device-config paths it writes, e.g. `rabi`
declares `("rxy.amp180",)`. Twelve routines declare no `updates` because they measure
without tuning (`t1`, `t2_echo`, `rb`, `allxy`, …). An info card has real content
available without inventing anything.

**The events.** `CalibrationQueued` announces a run before it starts;
`CalibrationProgress` fires after each routine-and-target with `step`, `total`,
`routine`, `target`, `succeeded`, `failed`, `elapsed_s`; `CalibrationResult` carries
the report at the end.

**The row.** A calibration has one `calibration_requests` record — dispatched ones
keyed by record id, self-triggered ones by the `job_id` field — carrying `status`,
`trigger`, and a `progress` object that each progress event *replaces*. The dashboard
subscribes to that collection, so a write to the row reaches the browser with no new
plumbing.

**The report.** `routine_results` holds per routine-and-target: `parameters` (the
fitted values), `timestamp`, `duration_s`. `benchmarks` holds `fidelity`,
`error_per_gate` and a free-form `raw_data` — and RB already ships its depths and
decay rate in there. **A chartable payload already crosses the wire for one class of
node.** That precedent matters in §9.

**The dashboard has no charting library, deliberately.** `ReadingsChart.tsx` is a
119-line hand-rolled SVG line chart, and says why: "no charting library — since the
dashboard build has no reliable access to install one in every environment it runs
in." The whole `dependencies` list is `react`, `react-dom`, `pocketbase`,
`lucide-react`. This is a constraint on the design, not a preference.

## 3. The gap, in three parts

They differ by an order of magnitude in cost, and should not be committed to as one
piece of work.

1. **The shape is not on the wire.** No node list, no edges. A bar is the most the
   dashboard can honestly draw.
2. **Progress is a scalar.** `progress` holds the latest position and nothing else.
   Colouring thirty-three nodes needs per-node state, which means accumulating rather
   than replacing.
3. **Nothing chartable exists per node.** Fitted parameters reach the report;
   the sweep behind them does not. `-o save_raw_data=true` writes datasets to the
   driver's own disk, which the server cannot read and nothing prunes.

## 4. Decisions

**D1 — The driver publishes the plan; the server does not hold a copy of the graph.**
A second copy is the thing this codebase repeatedly refuses (RFC 0003 §9), and here
it would be wrong as well as duplicated: the graph the dashboard must draw is the
*enabled subset* for this run, which depends on `calibration.yml`, on `mode`, and for
a partial run on what `diagnose` blamed. Only the driver can compute it. The Go test
that pins the catalog's `-o` keys to the Python builder exists because that
duplication was already tried once.

**D2 — A node is a routine, not a routine-and-target.** On five qubits with one edge
the walk is ~141 routine-and-target pairs; the graph is thirty-three nodes. The
drawing is of routines, each carrying its own `3/5 done` tally. Drawing 141 nodes
would be a picture of the sweep, not of the dependency structure that makes the
picture worth having.

**D3 — Per-node state lives on the request row, beside `progress`.** Same reasoning
as `CalibrationProgress` itself: a node's state is a current fact that supersedes the
last one, not history. The report is the history. This keeps one row per calibration
and reuses the subscription the dashboard already has.

**D4 — The drawing is dependency-free SVG with a layered layout we compute.** Forced
by the constraint in §2, and comfortable at this size: eleven layers, eight wide, a
longest-path layering is a dozen lines. `react-flow` plus `dagre` would be ~100 kB
against a 364 kB bundle for a graph whose shape barely changes.

**D5 — Charts come from the report, not from the driver's disk.** Extending the
`raw_data` precedent that `BenchmarkResult` already sets is a path that exists;
shipping HDF5 datasets off the node is a new subsystem (upload, storage, retention)
for a feature that wants a few hundred points. §9 bounds it.

**D6 — Three phases, shippable in order.** Phase 1 is worth having alone; phase 3
requires touching every routine's `analyse` and should not gate it.

## 5. Phase 1 — the plan on the wire

### 5.1 A new event

`CalibrationPlan`, driver → server, emitted once when a calibration starts, after
`CalibrationQueued` and before the first `CalibrationProgress`.

```json
{
  "job_id": "abc123def456ghi",
  "mode": "full",
  "nodes": [
    {
      "name": "rabi",
      "depends_on": ["qubit_spectroscopy"],
      "targets": ["q0", "q1", "q2"],
      "kind": "qubits",
      "is_benchmark": false,
      "has_check": true,
      "updates": ["rxy.amp180"]
    }
  ]
}
```

`targets` is the resolved list this run will walk — after `applies_to` has declined
the ones whose result has nowhere to go (RFC 0005 §7) — so a node the dashboard draws
as skipped is skipped for a reason the driver already computed. Nodes excluded from
the run entirely (disabled, or outside a partial's `only` list) are **still sent**,
marked `"planned": false`: seeing which parts of the graph a partial run is *not*
touching is most of the value of drawing it.

Emitted from `CalibrationDAG.run`, where the order and the resolved targets are
already computed, through the same sink `on_progress` uses. It is one event, so it
rides the existing best-effort path: a plan that fails to send costs the drawing, not
the calibration.

### 5.2 The server

`handleCalibrationPlan` resolves the row with the existing `findCalibrationRequest`
and writes the payload to a new `plan` json field. Same silent-on-miss treatment as
progress — a driver's drift check may have no row if its announcement was lost.

### 5.3 Per-node state

`progress` gains a `nodes` map, `{routine: {state, done, total, failed}}`, updated in
place as each progress event arrives. The handler becomes a read-modify-write of the
one node named in the event, which at a few hundred events per calibration is not a
load concern.

States: `pending`, `running`, `done`, `failed`, `partial` (some targets failed),
`skipped` (no targets applied), `not_planned`.

`running` needs care: a progress event fires *after* a target finishes, so the node
the event names has just completed one target, and the running node is that same node
until the next event names a different one. The handler marks the named node
`running` while its tally is short of its total, and the *previous* node `done` when a
new name appears.

### 5.4 The drawing

A new `CalibrationGraph` component under `CalibrationTab/elements/`.

Layout: layer = longest path from a root over `depends_on` (the eleven layers in §2);
within a layer, order by the plan's own sequence so the drawing matches the walk.
Nodes are boxes on a grid, edges are SVG paths. Eleven layers at ~64 px is ~700 px
tall, eight wide at ~140 px is ~1100 px — so it scrolls in its container on a narrow
screen, and the whole graph is visible on a desktop.

Colour by state, using the palette the tab already uses for status. A node shows its
name and, when it has more than one target, `3/5`.

## 6. Phase 2 — the node card

Clicking a node opens a card beside the graph. Everything it shows in phase 2 is data
already on the client:

- **What it is**: name, `qubits`/`edges`, benchmark and check flags.
- **What it writes**: the `updates` paths, or "measures only — writes nothing" for
  the twelve that declare none.
- **Where it sits**: what it depends on, and what depends on it. Clicking either
  navigates.
- **How this run went**: per target, the state, the duration, and — once the report
  has arrived — the fitted `parameters` from `routine_results`, and `fidelity` /
  `error_per_gate` for a benchmark.
- **Why it failed**: the matching entry from the report's `errors`, which is already
  keyed `routine[target]`.

For a run still in flight the card shows the tally and whatever targets have
finished; the parameters appear when the report lands.

## 7. Phase 3 — charts behind a node

This is the phase that needs new data, and the honest cost is per routine.

A routine's `analyse` receives the sweep dataset and returns fitted parameters. To
chart it, `analyse` must also return the **fit summary**: the x values, the measured
y values, and the fitted curve evaluated on the same x. For a 41-point sweep that is
~1 kB as JSON; for the whole graph on five qubits, a few hundred kB in the report.

That is a real increase in the size of a `calibration_results` row, and it should be
bounded explicitly:

- Downsample to at most 200 points per trace. Every routine's sweep in
  `calibration.example.yml` is 41 points or fewer, so this is a ceiling rather than a
  loss today.
- Two traces per target: measured and fitted. No residuals; they are a subtraction the
  client can do.
- A cap on the whole field, beyond which the summary is dropped and the card says so.
  A report that will not save is worse than a report with no chart in it.

The chart component is the `ReadingsChart` pattern: axes, a line, a scatter, no
library.

**A routine at a time.** `RoutineResult` gains an optional `fit` field, so a routine
that has not been converted simply has no chart and the card says the fit summary is
not available for it. The four benchmarks are the natural first ones — RB already puts
its decay curve in `raw_data`, so `rb` and `interleaved_rb` are a rendering job with
no driver change at all.

## 8. Implementation plan

Each phase is independently shippable and independently useful. An event the server
does not know is logged and dropped, and a field the dashboard does not know is
ignored, so driver and server can deploy in either order.

**Phase 1 — the graph, drawn and live.**

| Where | What |
| --- | --- |
| `qpi_driver/events.py` | `CALIBRATION_PLAN` |
| `tuners/base/dag.py` | build the plan in `run`, emit through the sink |
| `builtins/calibrate.py` | forward it as `CalibrationPlan` |
| `qpi-driver/{go,js}` | mirror the event type, as the others are |
| `api/schema.go` | `EventCalibrationPlan`, `CalibrationPlanPayload` |
| `api/nng_driver.go` | `handleCalibrationPlan`; per-node accumulation in `handleCalibrationProgress` |
| `db/models.go` | `Plan` on `CalibrationRequest` |
| `drivers/catalog.go` | add to the tuner specs' events |
| dashboard | `CalibrationGraph`, layout helper, types |

Tests: the plan's shape and that it precedes the first progress event (Python); the
handler writing it and node state accumulating across several progress events (Go);
the layering helper against the known eleven layers (a pure function, so a unit test
without a DOM).

**Phase 2 — the card.** Dashboard only, plus whatever the card needs that the plan
does not already carry. No driver or server change expected.

**Phase 3 — charts.** `RoutineResult.fit`, one routine at a time, starting with the
two RB benchmarks that need no driver change. A size cap with a test that a report
over it still saves.

## 9. Testing strategy

The layering helper and the state reducer are pure functions and get unit tests. The
drawing itself gets a Cypress test only if the harness gains the ability to seed a
`calibration_requests` row — it cannot today, which is why the in-flight banner is
untested. Seeding that row is a prerequisite worth doing on its own, and would cover
the progress bar shipped already.

The end-to-end path — a real walk against the simulator producing a plan whose nodes
match the report's `routine_results` — belongs in `test_calibration_e2e.py`, which
already runs the whole worker path.

## 10. Open questions

1. **Does the plan belong on `CalibrationQueued` instead of its own event?** It would
   save an event type. Against: a dispatched calibration's row is created by the
   server before the driver has resolved anything, and the plan needs the resolved
   target lists, so the two are not available at the same moment.
2. **Should a drift check draw at all?** It walks four benchmark nodes. The graph
   would be almost empty. Possibly the tab should show the list for
   `mode: fidelity_check` and the graph for the other two.
3. **What happens to the plan when the config changes mid-run?** The worker re-reads
   `calibration.yml` between jobs, not within one, so a plan is valid for its run. The
   drawing should be pinned to the plan it was sent, not re-derived.
4. **Retention.** `plan` and the node map live on the request row, which is never
   deleted. Thirty-three nodes is ~6 kB per calibration; a drift check every thirty
   minutes for a year is ~100 MB. A pruning policy for finished requests is out of
   scope here but should not stay unasked.

## 11. What this deliberately does not do

- **No graph editing.** Which routines run is `calibration.yml` on the driver's disk.
  A dashboard that edited the graph would be a second source of truth for it.
- **No re-running a single node from the card.** The DAG exists because a node's
  inputs come from its parents; re-running one in isolation is exactly what
  `diagnose` refuses to do (RFC 0005 §8). A "recalibrate from here" action is
  conceivable, but it is a partial dispatch with a seed, not a node-level button, and
  it needs its own design.
- **No raw dataset transfer.** See D5.
