# RFC 0006 — The Calibration Graph in the Dashboard

- **Status:** Implemented — all four phases of §8. Phase 3 converts the fits that
  have a model curve (Rabi, Ramsey, DRAG, fine amplitude, T1, T2, RB, and the
  Lorentzian spectroscopies); the rest report no summary and their cards show no
  chart, which is what "a routine at a time" was for.
- **Author:** Martin Ahindura
- **Created:** 2026-08-06
- **Depends on:** RFC 0004 (the `calibrate` operation, the DAG walk, the report),
  RFC 0005 (the thirty-three-node graph and its checks)
- **Touches:** `qpi-driver` (Python), `qpi-ui` (server and dashboard). No new event
  type, no new operation, no new collection — the plan extends an event that exists
  and the retention work extends an engine that exists.

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
node.** That precedent matters in §7.

**The dashboard has no charting library.** `ReadingsChart.tsx` is a 119-line
hand-rolled SVG line chart, and says why: "no charting library — since the dashboard
build has no reliable access to install one in every environment it runs in." The
whole `dependencies` list is `react`, `react-dom`, `pocketbase`, `lucide-react`.

That reason does not survive checking. `make build-dashboard` runs `npm ci`, as do
the lint and format targets — the build already requires a registry it can reach, and
already installs four packages from it. A fifth changes nothing about which
environments can build. Whatever the comment was protecting against, it is not the
network. **D4a treats the absence as a choice to re-make, not a constraint to design
around.**

**There is already a retention engine.** `scheduler.PruneEvents` deletes rows from
the events log older than `cfg.EventsRetention` (default 720 h, `0` disables), in
batches of 500, driven by `RunEventsRetentionEngine` — a background loop that exits
immediately when the framework is off. §9 extends this rather than inventing
anything.

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

**D4a — The graph is drawn by hand; no graph library.** Not for the reason §2
retired, but because the job is small and the alternative is aimed elsewhere. Eleven
layers, eight wide, a fixed shape: longest-path layering is a dozen lines.
`react-flow` (~40 kB gz) plus `dagre` (~25 kB gz) exists to make nodes draggable,
pannable and connectable — an interactive editor, which §12 says explicitly we do not
want. We would be importing an editor to get a static picture.

**D4b — Charts get a library.** This is where hand-rolling stops paying. A fit plot
needs two traces, real axes with ticks, SI-prefixed units, log scales for a punchout,
a hover readout, and both themes. `ReadingsChart` is 119 lines for one line, no
ticks, no legend, one series — and it is the *easy* case. Thirty-three routines'
worth of fit plots is a charting library whether or not we import one, and the
written-here version will be the worse of the two.

**It is visx, and the cost is measured rather than estimated.** Built with a real
fit plot — two traces, ticked axes, an optional log x — imported and rendered so
nothing is tree-shaken away:

| Bundle | Raw | Gzipped | Δ gz |
| --- | --- | --- | --- |
| Baseline | 364.55 kB | **97.76 kB** | — |
| `@visx/scale` + `@visx/shape` | 401.07 kB | **112.61 kB** | +14.85 kB |
| … + `@visx/axis` | 423.74 kB | **120.21 kB** | +22.45 kB |

Twenty-two kilobytes gzipped for every fit plot in the graph is a good trade against
hand-rolling thirty-three of them, and it leaves a lever: `@visx/axis` is 7.6 kB of
that (it pulls in `@visx/text` for tick labels), so a deployment that cares can drop
it and draw ticks by hand while keeping the scales and shapes that are the actual
work. `recharts` (~100 kB gz) stays the batteries-included alternative but is not
recommended — it is four times the cost for a chart we have already specified
completely.

Reproduce with: add the three packages, render a `FitPlot` behind a condition the
bundler cannot prove false (an unused export is tree-shaken and measures nothing —
this is how the first attempt reported no change at all), `npm run build`, and
`gzip -c dist/assets/index-*.js | wc -c`.

**D5 — Charts come from the report, not from the driver's disk.** Extending the
`raw_data` precedent that `BenchmarkResult` already sets is a path that exists;
shipping HDF5 datasets off the node is a new subsystem (upload, storage, retention)
for a feature that wants a few hundred points. §9 bounds it.

**D6 — A drift check does not draw.** `mode: fidelity_check` walks four benchmark
nodes with no dependencies between them worth looking at; a graph of it would be four
disconnected boxes. The tab keeps the bar and the tally for that mode and draws the
graph for `full` and `partial`. The driver therefore sends no plan for a drift check
at all, which also keeps the most frequent calibration the cheapest to store.

**D7 — A drawing is pinned to the plan it was sent.** The worker re-reads
`calibration.yml` between jobs but never within one, so a plan describes its run for
that run's lifetime. The dashboard renders the stored plan and never re-derives the
shape from progress events, so a config edit mid-run cannot make the picture
disagree with the walk it is describing.

**D8 — Three phases, shippable in order.** Phase 1 is worth having alone; phase 3
requires touching every routine's `analyse` and should not gate it.

## 5. Phase 1 — the plan on the wire

### 5.1 No new event: the plan rides `CalibrationQueued`

`CalibrationQueued` already means "a calibration is starting, here is what it is".
The plan is the rest of that sentence, so it goes in the same payload rather than in
an event of its own.

The wrinkle is that the two facts become available at different moments, and in
different processes. The announcement is made in the driver's main process when the
job is queued — before the row exists, which is the point of it. The plan is resolved
inside the worker, in `CalibrationDAG.run`, which is the only place the enabled
subset and each node's applicable targets are known.

So `CalibrationQueued` is emitted **twice** for one calibration: once at queue time
without a plan, once from the walk with one. The handler already tolerates this —
it looks the row up and creates it only when absent (that idempotence was added for a
driver reconnecting mid-run) — and gains one line to attach the plan when the payload
carries one. A dispatched calibration, which has a row already and emits no
announcement today, now emits the second one, so it gets a plan the same way.

The alternative is to move the announcement into the worker so there is only one
emission. Rejected: it would delay the row until the job starts rather than when the
driver decides to run it, and losing that is worse than sending a small event twice.

The payload gains `plan`, absent on the first emission and on a `fidelity_check`
(D6):

```json
{
  "job_id": "abc123def456ghi",
  "mode": "full",
  "target_qubits": ["q0", "q1", "q2"],
  "reason": "dispatched",
  "plan": {
    "nodes": [
      {
        "name": "rabi",
        "depends_on": ["qubit_spectroscopy"],
        "targets": ["q0", "q1", "q2"],
        "kind": "qubits",
        "planned": true,
        "is_benchmark": false,
        "has_check": true,
        "updates": ["rxy.amp180"]
      }
    ]
  }
}
```

`targets` is the resolved list this run will walk — after `applies_to` has declined
the ones whose result has nowhere to go (RFC 0005 §7) — so a node the dashboard draws
as skipped is skipped for a reason the driver already computed. Nodes excluded from
the run entirely (disabled, or outside a partial's `only` list) are **still sent**,
marked `"planned": false`: seeing which parts of the graph a partial run is *not*
touching is most of the value of drawing it.

The plan is built in `CalibrationDAG.run` and travels through the same sink
`on_progress` uses, tagged so the result pump can tell the two apart and emit the
right event. It rides the existing best-effort path: a plan that fails to send costs
the drawing, not the calibration.

### 5.2 The server

`handleCalibrationQueued` gains one branch: when the payload carries a `plan`, write
it to a new `plan` json field on the row it resolved or created. Everything else
about that handler is unchanged, including the silent-on-miss treatment — a drift
check whose announcement was lost has no row, and a plan for it is not worth an
error.

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

The chart component uses the library D4b settles on, measured before it is adopted.
Two traces, ticked axes, SI-prefixed units, a log x-axis where the sweep is
logarithmic, and both themes — that is the list `ReadingsChart` would have to grow
into, and the reason not to write it again thirty-three times.

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
| `tuners/base/dag.py` | build the plan in `run`, send it through the sink, tagged |
| `builtins/calibrate.py` | route a tagged plan to a second `CalibrationQueued`; skip it for `fidelity_check` |
| `api/schema.go` | `Plan` on `CalibrationQueuedPayload` |
| `api/nng_driver.go` | attach the plan in `handleCalibrationQueued`; per-node accumulation in `handleCalibrationProgress` |
| `db/models.go` | `Plan` on `CalibrationRequest` |
| dashboard | `CalibrationGraph`, layout helper, types |

No new event type, so `events.py`, the Go and TypeScript enums and
`drivers/catalog.go` are untouched — that is what §5.1 buys.

Tests: the plan's shape, that it is absent for a `fidelity_check`, and that it
arrives before the first progress event (Python); the handler attaching a plan to a
row it did not create, and node state accumulating across several progress events
(Go); the layering helper against the known eleven layers (a pure function, so a unit
test without a DOM).

**Phase 2 — the card.** Dashboard only, plus whatever the card needs that the plan
does not already carry. No driver or server change expected.

**Phase 3 — charts.** Measure the candidate libraries against the 97.67 kB gz
baseline and pick one (D4b). Then `RoutineResult.fit`, one routine at a time,
starting with the two RB benchmarks that need no driver change. A size cap with a
test that a report over it still saves.

**Phase 4 — retention.** `PruneCalibrations` beside `PruneEvents`, three config
durations, wired into `RunEventsRetentionEngine`'s tick. Independent of phases 1–3
and overdue without them: nothing in the calibration path is pruned today.

## 9. Retention

Everything this RFC adds accumulates, and nothing in the calibration path is pruned
today. That is already true without it — the gap is pre-existing and this makes it
bigger — so the policy belongs here rather than in a later RFC.

**The pattern exists.** `scheduler.PruneEvents` deletes events-log rows older than
`cfg.EventsRetention` in batches of 500, run by `RunEventsRetentionEngine`, disabled
by setting the duration to `0`, and a no-op when the driver framework is off. What
follows copies it rather than inventing a second mechanism.

**What accumulates, and how fast.** For a five-qubit chip with a drift check every
thirty minutes:

| Row | Per calibration | Per year | Prune? |
| --- | --- | --- | --- |
| `calibration_requests` (+ `plan`, + node map) | ~6 kB for a full run; a drift check sends no plan (D6) and is ~1 kB | ~20 MB | **Yes — aggressively** |
| `calibration_results` today | ~10–30 kB (fitted parameters, benchmarks) | ~200 MB | **No — this is the chip's history** |
| `calibration_results` with phase 3 fit summaries | ~150 kB | ~2.5 GB | **The summaries only** |

**The policy, in three parts:**

1. **Finished requests are bookkeeping.** A `done` or `failed` row exists to have
   carried a status and a progress position while the run was live. Once the report
   is stored it holds nothing the report does not. Prune on
   `cfg.CalibrationRequestRetention`, default 720 h to match the events log, on
   `status != 'running' && status != 'pending' && created < cutoff`. A `running` row
   is never pruned however old, or a hung calibration would lose its record while
   still running.
2. **Reports are not bookkeeping and are not pruned by default.** They are what the
   chip was, and RFC 0004 §9's whole argument for storing them is that the history is
   the point. `cfg.CalibrationResultRetention` exists and defaults to `0` — disabled
   — so an operator with a disk problem has a lever and nobody else loses history by
   accident.
3. **Fit summaries are pruned separately from the reports that carry them.** They are
   ~90% of a phase 3 row and the least durable part of its value: nobody re-reads the
   Rabi trace from eight months ago, but the fitted `amp180` is the record of what the
   chip was. `cfg.CalibrationFitRetention`, default 720 h, strips `fit` from
   `routine_results` and `raw_data` from `benchmarks` on older rows, leaving
   everything else. The card says the trace has aged out rather than showing an empty
   chart.

Point 3 is why phase 3's cap in §7 is a cap and not a budget: a bounded row that is
also pruned is bounded twice, which is what makes 2.5 GB/year an acceptable worst
case rather than the number we live with.

**Driver-side raw data is out of scope and stays that way.** `-o save_raw_data=true`
writes to the driver's own disk, where this server cannot see it and this engine
cannot reach it. It is off by default for exactly that reason, and its own docs say
so. An operator who turns it on owns the directory.

## 10. Testing strategy

The layering helper and the state reducer are pure functions and get unit tests.

**The Cypress harness learns to seed a calibration**, which it can: `e2e/seed.py`
already authenticates against `_superusers` and POSTs straight into collections —
`qpus`, `users`, `api_tokens`, `time_slots` — and a superuser token bypasses the
`calibration_requests` create rule the same way it bypasses those. One function in
the same shape as the `time_slots` one, seeding a `running` request with a `plan`, a
node map and a `progress`, is all it takes.

Worth doing before phase 1 rather than as part of it. It is the missing piece that
makes the *already shipped* in-flight banner and progress bar testable — both are
untested today for exactly this reason — so it pays for itself before the graph
exists to use it.

The end-to-end path — a real walk against the simulator producing a plan whose nodes
match the report's `routine_results` — belongs in `test_calibration_e2e.py`, which
already runs the whole worker path.

## 11. Settled in review

Every question this RFC opened, and where it landed. Kept rather than deleted
because the reasoning against each is what makes the decision worth trusting.

1. **The plan rides `CalibrationQueued`; there is no `CalibrationPlan` event.** The
   objection was that the announcement and the plan are known at different moments,
   in different processes. That is true and the answer is to emit the event twice —
   the handler is already idempotent, and one small event sent twice is cheaper than
   a type nobody else needs. §5.1.
2. **A drift check does not draw.** Four disconnected benchmark boxes is not a
   picture. It keeps the bar, and the driver sends it no plan — so the most frequent
   calibration is also the cheapest to store. D6.
3. **A drawing is pinned to the plan it was sent.** D7.
4. **Retention is specified here, not deferred.** §9.
5. **visx, and it fits.** The draft asked for a measurement before committing, and
   got one: +22.45 kB gz for scales, shapes and axes against a 97.76 kB baseline, or
   +14.85 kB without the axis module — the lever if that ever needs pulling. D4b.
6. **The Cypress harness seeds the calibration.** It can: `seed.py` already writes
   into collections as a superuser. Doing it early makes the in-flight banner and
   progress bar — already in production, still untested — testable. §10.

Nothing is open. The RFC is ready to be moved to Accepted and executed in the four
phases of §8.

## 12. What this deliberately does not do

- **No graph editing.** Which routines run is `calibration.yml` on the driver's disk.
  A dashboard that edited the graph would be a second source of truth for it.
- **No re-running a single node from the card.** The DAG exists because a node's
  inputs come from its parents; re-running one in isolation is exactly what
  `diagnose` refuses to do (RFC 0005 §8). A "recalibrate from here" action is
  conceivable, but it is a partial dispatch with a seed, not a node-level button, and
  it needs its own design.
- **No raw dataset transfer.** See D5.
