# RFC 0009 — Parallel Calibration

- **Status:** Partially implemented. Phases 1–4's mechanisms are all built, with 12 of the
  33 routines grouping; phases 5–6 open. §9 records what each phase covers and the
  status line under it says where each stands.
- **Author:** Martin Ahindura
- **Created:** 2026-08-18
- **Depends on:** RFC 0004 (the walk, the progress event, the report), RFC 0005 (the
  thirty-three-node graph), RFC 0006 (the plan on the wire and the drawing),
  RFC 0007 (escalation), RFC 0008 (provenance)
- **Touches:** `qpi-driver` (the DAG, the routine base, two new modules), `qpi-ui` (the
  progress accumulator, the graph drawing), `calibration.yml`. No new event type, no new
  collection, and no chip-geometry file — see D3.

Quantities here are per *topology*, never per device. A statement like "two groups"
holds for a class of connectivity graph and for any size of it; where a number depends
on the chip, this RFC says which config field it is computed from rather than quoting a
value. Nothing in it is derived from a particular lab's `quantify.*.yml`, all of which
are untracked local state.

## 1. The idea

The walk is two nested loops: for each routine, for each target. Every acquisition waits
for the one before it, and nothing about the chip requires that. A Rabi on two qubits far
enough apart drives different ports, reads different resonator frequencies and shares no
state; run sequentially they cost twice one of them, and run together they cost one.

The prize is not a constant factor. Sequentially, a full walk costs
`routines × targets`, so it is **linear in qubit count**; grouped, it costs
`routines × groups`, and the group count is a property of the connectivity graph and
**not of its size** (§5.5). A chain needs two groups for its single-qubit routines
whether it has five qubits or fifty. So a walk that takes hours on a small chip takes
days on a large one, and grouped it takes hours on both.

This RFC decides which targets may run together, how one schedule carries several of
them, and how the dashboard shows a set of components in flight rather than a point in a
list. It also fixes a defect found while writing it: **the calibration graph has a
`running` style and a legend entry that no walk has ever reached** (§7.1).

## 2. What exists today

Read, not assumed.

**The walk is strictly sequential.** `CalibrationDAG.run` iterates `order`, and inside it
`for target in targets`, calling `_run_one` per pair. There is one `SchedulerBackend` and
one `InstrumentCoordinator` behind it.

**A cluster is one arm-and-start resource, and the constraint is on `start`.**
`QuantifyBackend.run` does `prepare` → `start` → `wait_done` → `retrieve_acquisition`,
with `stop()` in a `finally`. Reading the scheduler's own cluster component: `start()`
*opens* with a cluster-wide `instrument.stop_sequencer()` — its comment says this is to
disarm everything so the no-argument `start_sequencer()` that follows starts only what it
just armed — and `stop()` calls `disable_sync()` on every module before another
cluster-wide `stop_sequencer()`. `wait_done` and `retrieve_acquisition` are cluster-wide
in the same way. D1 is about what that rules out.

**Sequencer indices are stable, not first-come.** A module's sequencer index comes from
the port-clock ordering in the hardware config (`_construct_all_sequencer_compilers`
walks `_extract_sequencer_compilation_configs()` and keeps the entries with data), so a
given port-clock compiles to the same sequencer in every schedule. Two independently
compiled schedules over disjoint targets would *not* collide on sequencer indices — a
claim an earlier draft of this RFC got wrong, and the reason D1 rests on `start` instead.

**Every fit reads acquisition channel zero.** `signal_of` takes
`dataset[list(dataset.data_vars)[0]]`. A dataset carrying several channels is read as
whichever one came first.

**Nothing in the tuner path sets `acq_channel`.** Routines call
`backend.Measure(target, acq_index=index, bin_mode=...)`, so the channel comes from the
element — and `acq_channel` on a transmon element is a parameter with
`initial_value=0`, so *every* element answers to channel 0 unless something says
otherwise. The executor path already solved this: `executors/quantify/conv.py:240` passes
`acq_channel=idx`, commented "Use unique acq_channel per qubit to avoid overlaps". The
fix is a pattern the repo already uses.

**The scheduler's gates are already multi-qubit.** `Reset(*qubits)` and
`Measure(*qubits, acq_channel=..., acq_index=...)` both take a variadic target list, and
`schedule.add` takes `ref_op`/`ref_pt`/`ref_pt_new`. Simultaneity inside one schedule is
expressible in the API as it stands.

**The wiring already says which targets contend.** The hardware config's
`connectivity.graph` maps each `<target>:<port>` to an instrument output. Two targets
whose ports resolve to *different* outputs contend for nothing; two that resolve to the
same output share its LO, its DAC and its sequencer pool. Readout is the usual case —
a shared feedline puts many resonators behind one output — and it is the only shared
resource this RFC has to reason about, because drive and flux ports are per target on
every wiring the repo supports.

**The instruction budget is per sequencer, not per schedule.** `MAX_SWEEP_POINTS = 700`
exists because a QRM_RF rejected a 12700-instruction program against a 12288 ceiling.
That ceiling is a sequencer's, and a fused schedule gives each target its own sequencer
running its own copy of the sweep. **Fusion costs sequencers, not instructions**, so none
of the sweep-size guards move.

**Routines carry per-target state.** `Rabi.build_schedule` writes `self._amplitudes` and
`self._amplitudes_ceiling`, the latter from `full_scale(device.get_element(target), ...)`.
`escalating` widens a `RoutineConfig` that belongs to the routine, not to a target.

**Fourteen routines override `measure`.** `resonator_spectroscopy`,
`qubit_spectroscopy`, `rabi`, `ramsey`, `t1`, `t2_echo`, `drag`, `fine_amplitude`,
`fine_amplitude_90`, `rb`, `drag_12`, `fine_amplitude_12`, `coupler_anticrossing`,
`interleaved_rb` — most of them only to call `escalating`. This is where most of a walk's
time goes, and §6.5 is about it. (An earlier draft said eleven, from a grep that
truncated.)

**A routine's sweep setpoints are per-routine state, not per-target.** `build_schedule`
writes `self._frequencies`, `self._amplitudes` and friends, and `analyse` reads them
back. For a sweep centred on a per-qubit value — every frequency sweep in the graph —
the grid *differs* between targets, so a fused group would leave `analyse` fitting every
target against the last one's grid. This is the constraint that decides what can be
converted, and §6.6 is about it.

**The device config has a coupling graph and no geometry.** A device config names
elements and edges (an edge carrying `parent_element_name`/`child_element_name`); the
hardware config names ports and outputs. Neither has coordinates, and quantify's
`QuantumDevice` has no field for them. §8.

**The progress event fires after a target finishes.** `_report_progress` is called once
before the walk with the plan, then inside the target loop *after* `_run_one` returns.
Nothing announces a target starting.

**The simulator supports several acquisition channels, caps entanglement at three, and
models no crosstalk.** `_to_dataset` groups acquisitions by channel and keys the data
variables by the integer channel. `MAX_ENTANGLED = 3`, because each qubit in a joint
register multiplies the Liouvillian's side by `levels²` and "four is not" survivable.
Crosstalk is listed under "What is not" modelled. D10 and §10.3.

## 3. The gap, in three parts

They are independent, and only the second is about physics.

1. **Nothing knows which targets may run together.** The coupling graph exists but
   nothing reads it as a conflict relation, and `calibration.yml` has no way to say
   "these three, at once".
2. **One schedule cannot carry several targets.** Every routine builds for one target,
   and every fit reads channel zero.
3. **The dashboard tracks a point, not a set.** `progress` names one routine and one
   target. There is no shape for "these three qubits are being measured right now" —
   and, prior to that, no shape for "this node is running" that ever fires.

## 4. Decisions

**D1 — Parallelism is fusion into one schedule, never concurrent submission.**

The tempting design is a worker per group, each with its own `backend.run`, and the
natural follow-up is to scope the teardown so the groups stop treading on each other.
Scoping is *possible* — `ClusterModuleComponent` has its own `stop()` and
`disable_sync()`, so a cluster-wide teardown could be narrowed to the modules a schedule
actually used. It does not unlock concurrency, for three reasons, and they are worth
separating because only the first is about the API:

1. **The blocker is on `start`, not `stop`.** `ClusterComponent.start()` begins by
   disarming *every* sequencer in the cluster, deliberately, so that its no-argument
   `start_sequencer()` fires only what it has just armed. Starting group B therefore
   disarms group A mid-flight. A per-module `stop` leaves this untouched, and
   `wait_done`/`retrieve_acquisition` are cluster-wide the same way, so B's retrieve
   would read A's acquisition memory alongside its own.
2. **The contended resource is usually one module.** The groups worth running at once
   are groups of qubits, and their readout typically shares a feedline and therefore one
   output on one module. Per-module scoping is precisely useless for the case that
   motivates it: the two groups would queue for the same module either way.
3. **Concurrent is not simultaneous, and calibration wants simultaneous.** Two
   submissions have no shared time origin. What a grouped measurement must answer is
   "how does this qubit behave *while* its neighbours are driven", and that requires the
   drives to land at a known offset — which is what one schedule on one sync network
   gives and two schedules never can, however carefully they are launched.

So there is nothing to align between schedules. **The alignment is the schedule** —
`ref_op`/`ref_pt` inside one of them, compiled once, armed once, started once, retrieved
once. Fusion delivers everything per-module scoping was reaching for, and delivers the
simultaneity as well.

*Two per-module observations are worth keeping even so.* `start()`'s opening disarm is
already a cluster-wide reset before every run, so the current code is consistent on that
point and needs no change. And narrowing `stop()` to the modules in the program would
still be a small, independent latency win on a large cluster — logged here as an aside,
out of scope, and not a prerequisite for anything below.

**D2 — A group is a colouring of a conflict graph, computed from the coupling graph.**

Not a hand-written list, because a hand-written list is wrong the first time a coupler is
added and nothing checks it. Not a solver, because greedy colouring is optimal where it
matters: two groups for single-qubit routines on any bipartite lattice at the default
spacing, and Δ for couplers — Vizing's lower bound, and what Sycamore's four coupler
patterns are. It is *not* optimal everywhere, and §5.5 records where it is not rather
than claiming otherwise. Explicit groups remain available as an override (§5.3), for a
chip whose measured crosstalk does not follow its topology.

**D3 — Grouping needs the coupling graph, not chip geometry. No layout file here.**

The conflict relation is *graph distance* over the coupling graph, which the device
config's edges already give. Coordinates would add nothing and would mislead: two qubits
physically close with no coupler between them are farther apart, for this purpose, than
two adjacent ones. The layout work is genuinely orthogonal (§8).

**D4 — Demultiplex in the DAG. `analyse` never learns it ran in company.**

One fused acquisition returns a Dataset with one data variable per channel. The DAG
slices it — `dataset[[channel]]` — and hands each routine a single-variable Dataset,
exactly the shape `signal_of` already reads. Thirty-three `analyse` implementations,
every fit in `tuners/fitting/`, and every existing test stay untouched. The alternative,
teaching each fit which channel is its own, is thirty-three chances to read another
qubit's data and fit a plausible curve to it.

**D5 — The acquisition channel is the target's position in its group, passed explicitly.**

Not the element's `measure.acq_channel`, which defaults to 0 on every element and which
fusion would otherwise have to mutate mid-walk. One `Measure` per target with an explicit
`acq_channel`, all aligned to the same start. Nothing on the device is written, so no run
can leave a chip's config carrying a channel assignment made for a group it was in an
hour ago.

**D6 — Fusion is opt-in per routine, behind a behaviour-preserving default.**

`build_group_schedule` joins `acquire`, `measure`, `applies_to` and `uncorrected` as a
base-class hook whose default reproduces today's behaviour: a group of one delegates to
`build_schedule`, a larger group is refused. A routine becomes fusable by implementing
it, one at a time, each with its own test. Nothing regresses on the day this lands,
because nothing has opted in.

**D7 — A group shares one sweep grid, or it is not a group.**

`Rabi`'s ceiling is `full_scale(element)` per target, so two targets can want different
amplitude grids, and fusing them would sweep one over the other's range. The group
builder compares the setpoints each target resolved and splits out any target that
disagrees, running it alone. Refusing the whole group instead would let one unusual
element disable parallelism for a chip.

**Implemented as `compatible_groups`, and it groups rather than isolates.** A routine
whose grid is derived per target overrides the hook and returns the subgroups that agree,
so the qubits that do share a window are still measured together — splitting one outlier
off does not cost the rest their fusion. `t2_echo` is the case it exists for: its window
is scaled from each qubit's measured T1 and an idle is dead time on every port at once,
so there is no per-target time axis to give them. The default returns a single group,
which is right for a fixed gate sequence or a grid the config states outright — and an
operator who names the delays has made one statement about the whole chip, which puts
every target back together.

**D8 — Failure and escalation are per target, and re-fusion is of the refused subset.**

A group of five with one refused fit is four results and one `RoutineError`, exactly as
five sequential runs would be. `escalating` then widens for the refused target only and
re-fuses *those*, because widening the whole group would re-sweep satisfied targets over
a range chosen for a different one — and `Rabi` documents why that is not free: sweeping
to full scale where half scale sufficed put `amp180` 6.9% out on the simulated chip.

**D9 — Shared-output feasibility is computed and refused, not assumed.**

Simultaneous tones behind one instrument output must fit one LO's addressable band and
one DAC's full scale, and must not need more sequencers than the module has. All three
are arithmetic on the two configs (§5.4). A group that does not fit is split, with the
numbers in the message — the failure mode otherwise is a clipped readout producing
confident wrong contrast on every qubit at once.

**D10 — The simulator can validate the crosstalk *detector*; it cannot set the radius.**

The simulator models no crosstalk today, and it can be made to for a **pair or a
triple**: `coupled.py` already holds two transmons in one register with an exchange term,
and a static ZZ coefficient is the same kind of addition. `MAX_ENTANGLED = 3` is the
ceiling, and it is a hard one — the Liouvillian's side grows as `levels^n`, and the
module says four qubits is not survivable — so a chip-scale group can never be simulated.

That bounds what simulation is for, and the bound is more useful than it sounds. A pair
is enough to test that the acceptance measurement of §5.6 *works*: switch a ZZ term on
and `parallel_penalty` must become non-zero; switch it off and it must return to zero.
That validates the detector. It cannot validate a radius, because the coefficient would
be a number this project chose — `coupled.py` is already candid that several of its
constants were picked rather than measured — and a test asserting a spacing is safe would
be asserting our own constant. **The detector is testable in simulation; the radius is
settled on hardware.** §5.6.

**D11 — The running node is fixed first, and separately.**

A node in flight has been undrawable since RFC 0006 shipped (§7.1). It is a small driver
change plus a branch in the server's accumulator, it is worth having on its own, and a
grouped run is unwatchable without it. It goes first, in its own phase, and its tests do
not mention parallelism.

## 5. Grouping

### 5.1 What the literature settles

"Which components may be calibrated at once" is "which gates may be applied at once",
which is well covered.

**Grouping is a graph colouring.** Kelly et al. put calibration on a DAG and note
parallelism across qubits as its natural extension; the ISCA 2025 hardware-aware protocol
makes it explicit, using graph traversal to identify compatible calibration operations
and splitting oversized subgraphs, and reports 8–25× less calibration overhead than
sequential. Sycamore's two-qubit layers are four patterns of disjoint couplers tiling the
grid — an edge colouring of a degree-4 lattice at Vizing's Δ.

**Adjacency is the conflict, and the guard band is empirical.** Simultaneous operation
degrades fidelity through residual ZZ coupling and drive leakage, and the degradation is
measured rather than derived: Gambetta et al.'s simultaneous randomized benchmarking
benchmarks each qubit alone and then together, and the difference in average gate
fidelity *is* the addressability. Murali et al. use SRB for pairwise crosstalk
characterisation, reduce it from all-pairs to a tractable set, then serialise the pairs
found to conflict — which is this RFC's structure exactly: characterise, group, and put
conflicting pairs in different groups. CAMEL partitions a frequency-tunable chip into
local windows for the same reason.

**Simultaneous readout behind a shared feedline is standard and bounded.** Heinsoo et al.
read five qubits in one 1.2 GHz channel with individual Purcell filters and found
simultaneous readout errors within 1% of individual ones. That is the result which makes
a shared feedline workable rather than disqualifying, and its preconditions are what D9
checks.

So the rule implemented here is the one the literature converged on: **conflict is
proximity in the coupling graph; the radius is a parameter; the parameter is set by
measurement.**

### 5.2 The rules

The conflict graph over qubits has an edge between `u` and `v` when any of:

- graph distance over the coupling graph is less than `qubit_spacing` — the *minimum*
  distance two qubits in a group must be apart. The default 2 excludes adjacent pairs,
  which leaves at least one idle qubit between every pair in a group; 1 imposes nothing
  and measures the whole chip at once; 3 leaves two;
- the pair is listed in `parallel.exclude`;
- the wiring cannot carry both — their ports resolve to the same instrument output and
  their clocks cannot coexist in it (§5.4).

Over couplers, an edge when the graph distance between the two couplers' endpoint sets is
less than `edge_spacing` — default 1, which requires only that they share no qubit; 2
additionally puts a qubit between them.

Groups are the colour classes of a greedy colouring in a deterministic order (the
config's target order), so the same config always produces the same groups and a run is
reproducible. `max_group` caps a class, which is what keeps a wide chip inside its
sequencer count.

**Added while implementing phase 2: an unreadable coupling graph is not an empty one.**
With no edges to read — a config targeting none, or a scheduler whose device no longer
exposes them — every qubit sits at infinite distance from every other and the colouring
returns a single group, which is the most aggressive setting available arrived at by
accident. So a walk with more than one target and no adjacency runs them one at a time
and says why.

### 5.3 The configuration

```yaml
parallel:
  enabled: true
  qubit_spacing: 2        # minimum graph distance between two qubits in a group
  edge_spacing: 1         # 1 = share no qubit; 2 = a qubit between two couplers
  max_group: 8
  exclude:                # never grouped, whatever the spacing allows
    - [q1, q3]
  groups:                 # explicit override; skips the colouring entirely
    qubits: [[q0, q2, q4], [q1, q3]]
```

Absent, `parallel` means `enabled: false` and the walk is exactly today's. That is the
opposite of `routines`' default — where absent means enabled — and deliberately so: a
missing `routines` entry cannot make a run measure nothing, whereas a `parallel` block
defaulting to on would silently change how every existing chip calibrates.

### 5.4 Shared-output feasibility

For a group whose targets' ports resolve to the same instrument output, three checks,
each arithmetic on the hardware and device configs:

- **One LO band.** Every clock in the group must lie within `backend.if_limit_hz` of the
  group's band centre, since one output has one LO and each sequencer's NCO offsets from
  it. Read the clocks from the device config, take the midpoint of their range, and
  compare the worst offset against the limit.
- **One DAC.** The group's simultaneous pulse amplitudes sum below full scale, because
  the tones are added before the converter. Read them from the device config.
- **Sequencer count.** One sequencer per port-clock pair, against the module's own count
  from the hardware description.

A group failing any of them is bisected and both halves retried. The message names the
measured figure and the ceiling, in the house style of `SchedulerBackend.allow` and for
the same reason: an operator can act on "worst offset X MHz against the Y MHz band" and
cannot act on "group too wide".

### 5.5 What it costs and what it saves

A sequential walk over `Rq` qubit routines and `Re` edge routines on `N` qubits and `E`
edges costs `Rq·N + Re·E` acquisitions. Grouped it costs `Rq·Gq + Re·Ge`, where `Gq` and
`Ge` are the colour-class counts. RFC 0005's graph gives `Rq = 27` and `Re = 6`.

`Gq` and `Ge` depend on the connectivity graph and not on its size:

| Topology | `Gq` at spacing 2 | `Gq` at spacing 3 | `Ge` at `edge_spacing: 1` |
| --- | --- | --- | --- |
| linear chain | 2 | 3 | 2 |
| square lattice (Δ=4) | 2 | 6–7 | 4 |
| heavy-hex (Δ≤3) | 2 | 4 | 3 |

Measured against the implementation, not derived — see `tests/utils/chips.py`.

Every `Gq` at the default spacing is 2, because each of these graphs is bipartite; `Ge`
is Δ, Vizing's lower bound, on all three. So the grouped cost is a constant —
`27·2 + 6·Ge` — for a chain of five qubits and a lattice of five hundred alike, while
the sequential cost grows with both `N` and `E`. **The speedup is therefore not a fixed
number to quote but `N/Gq`,** which is the whole reason to build this rather than buy a
faster fridge.

**Greedy is exact at the default spacing and not at spacing 3.** The five-colour Lee
tiling is the optimum for an infinite lattice; greedy colouring reaches six on a small
one and seven at 5×5. That costs runtime and never correctness, and the default spacing
is the case where greedy provably cannot do worse — two classes on a bipartite graph.
Closing the gap is graph colouring, which is NP-hard, and not worth it for one class on
a non-default setting.

Setting `qubit_spacing: 1` collapses `Gq` to 1 and is what the SRB literature does
routinely; §5.6 is how a chip earns it.

### 5.6 What licenses a tighter spacing

`qubit_spacing: 2` ships as the default because no chip has evidence for anything
tighter until the measurement is made. That measurement is Gambetta et al.'s, and the
graph already contains both halves of it: `rb` measures a qubit's average gate fidelity
alone, and a fused `rb` over a group measures it in company. The difference is the
addressability, per qubit, in the units the drift check already thresholds on.

So the acceptance test is a comparison of two runs the driver can already do, and it
belongs in the report rather than in a paper: `parallel_penalty` per target, beside the
fidelity it is derived from. An operator tightens `qubit_spacing` when that number is
small on their chip and loosens it when it is not. Nothing here decides for them, and
D10 says which half of this is testable without a fridge.

**Implemented as `parallel.measure_penalty`, off by default.** A benchmark's group runs
first and the same targets are then benchmarked one at a time into a throwaway report; the
difference lands on the grouped benchmark. The isolated pass is a *control* — its rows are
discarded, because the fused numbers are the ones that describe how the chip will actually
be driven. It doubles what benchmarking costs, which is why an operator opts in rather than
paying for it on every run. No new node and no change to the graph's shape (§11).

## 6. Fusion

### 6.1 The hook

```python
def build_group_schedule(
    self, targets: Sequence[str], device: Any, config: RoutineConfig,
    backend: SchedulerBackend,
) -> Any:
    """This experiment over every target at once, in one schedule.

    A fusable routine implements this and lets `build_schedule` delegate to it with a
    single target; the default here goes the other way, so an unconverted routine keeps
    working and declines a group.
    """
    if len(targets) == 1:
        return self.build_schedule(targets[0], device, config, backend)
    raise RoutineError(f"{self.name} cannot run {len(targets)} targets in one schedule")
```

`fusable` mirrors `measures_itself`: whether the subclass overrode the hook. The DAG
always calls `build_group_schedule`, so there is one path, and a group of one exercises
it on every existing routine.

A converted routine reads the way the sequential one did, with one addition — the
alignment:

```python
def build_group_schedule(self, targets, device, config, backend):
    self._amplitudes = ...
    schedule = backend.new_schedule(self.name, repetitions=int(config.get("shots", 1024)))
    for index, amplitude in enumerate(self._amplitudes):
        schedule.add(backend.Reset(*targets))
        anchor = None
        for target in targets:
            pulse = backend.Rxy(theta=180, phi=0, qubit=target, amp180=amplitude)
            anchor = anchor or schedule.add(pulse)
            if schedule.schedulables[-1] is not anchor:
                schedule.add(pulse, ref_op=anchor, ref_pt="start", ref_pt_new="start")
        for channel, target in enumerate(targets):
            schedule.add(
                backend.Measure(target, acq_channel=channel, acq_index=index, ...),
                ref_op=anchor, ref_pt="end", ref_pt_new="start",
            )
    return schedule
```

`Reset(*targets)` is one operation over the whole group, so the resets coincide for free.
The drives and measurements need `ref_op`/`ref_pt` — without them `schedule.add` appends,
and a group's resets and pulses serialise into a schedule as long as the sequential run
it was meant to replace. **That is the whole of "aligning the schedules", and it lives
inside one.**

### 6.2 Demultiplexing

```python
def channel_of(dataset: Any, channel: int) -> Any:
    """Acquisition channel *channel* alone, as a Dataset `signal_of` can read."""
```

Data variables are keyed by the integer channel — the simulator's `_to_dataset` builds
`variables[channel]`, and a cluster agrees — so this is a selection, not a
reconstruction. A group's result is `[channel_of(dataset, i) for i in range(len(group))]`
and each element goes to `analyse` unchanged.

A channel missing from the returned dataset is that target's failure and nobody else's:
recorded as a `RoutineError` against that target while the rest of the group succeeds
(D8).

### 6.3 Where fusion lives

```
qpi-driver/py/qpi_driver/tuners/base/
  grouping.py   # coupling graph -> conflict graph -> colour classes; feasibility
  fusion.py     # group schedule alignment; dataset demultiplex
```

Two modules because they answer unrelated questions and are tested against unrelated
things: `grouping.py` is graph theory over a config and needs no device, `fusion.py` is
scheduler mechanics and needs no topology. `dag.py` gains the group loop and nothing
else.

### 6.4 Timeouts and the budget

A fused acquisition's pulses are one target's, not the group's, because the sequencers
play concurrently — so `backend.allow` returns what it does today and the over-budget
refusal is unchanged. `start_accounting` moves from per-target to per-group, which is the
honest scope: the ceiling bounds one arm-and-wait cycle, and a group is one.

### 6.5 The fourteen that measure themselves

`escalating` widens a `RoutineConfig` and retries, and it is where most of a walk's hours
go. Over a group it becomes: acquire once, analyse each target, keep the fits that
landed, and re-fuse the subset that raised `OutOfRange` under a config widened for them.
Bounded as it is now, by `MAX_ESCALATIONS` per subset — so the worst case is the current
worst case, and the common case where nothing escalates is one acquisition for the whole
group.

**A fusable schedule is not enough.** `measure` is where escalation, refinement and any
between-pass write-back live, and the fused path runs none of them — so a routine with its
own loop is grouped only once it has a `measure_group` too, however group-capable its
schedule is. `ramsey` is the case: its loop refines `f01` over several passes, applying to
the device between them, and fusing on the schedule alone would have skipped all of it.
Its schedule is converted; its loop is not, so it still walks one target at a time.

Two routines stay sequential and should: `coupler_bias`, whose loop sets DC state between
acquisitions, and `qubit_spectroscopy`, whose next window depends on what the last one
found. Neither is a limitation to design around — a chip has one bias source, and a
search that branches per qubit is a different experiment per qubit.

### 6.6 What can be converted, and what needs more than a hook

Measured across the graph: **eight** routines store no per-target sweep state and can be
converted as they stand — `allxy`, `allxy_check`, `readout_discrimination`,
`readout_fidelity` and `three_state_discrimination`, plus the four edge routines whose
grids are target-independent (`cz_spectroscopy`, `cz_parametrization`,
`conditional_phase`, `cz_chevron`), which phase 5 takes. The first four are converted. **Ten** more on the plain path centre a grid on a per-qubit value —
`resonator_spectroscopy_excited`, `f12_spectroscopy`, `rabi_12`, `ramsey_12`,
`flux_spectroscopy` and the rest — and need their setpoints moved from routine state to
per-target state before they can fuse. That is a mechanical but broad change, and it is
the real content of the phase-4 work rather than the escalation loop alone.

The order matters and was not obvious when §9 was written: **per-target setpoint state is
a prerequisite for fusing most of the graph**, and `escalating` needs it too, since
widening a refused target's axis means holding a different grid for that target than for
its group. §9's phases 3 and 4 are re-scoped accordingly.

## 7. The dashboard

### 7.1 The running node has never been drawn

`CalibrationGraph` has a `running` style (`animate-pulse`, blue) and a legend entry for
it. Nothing reaches it.

`advanceNodes` folds a progress event, and a progress event fires *after* a target
finishes. So for a node with one target the first event it produces already has
`Done >= Total`, and `settledState` sends it straight to `done` — `running` is
unreachable for that node, always. The repo's own tests encode this without naming it:
the reducer test asserts a single-target node ends `done` after one event, and the test
for `running` needs a three-target node to reach the state at all. **A bring-up
calibrating one qubit at a time gives every node one target, so on that run the state is
unreachable everywhere and the legend entry is dead.** Even with several targets it is
late: a node is `pending` — indistinguishable from "not its turn" — for the whole of its
first target, which for a spectroscopy or an RB is minutes.

A second defect from the same cause: a target skipped for an unsatisfied prior (RFC 0007
§11) `continue`s before `_report_progress`, so a node whose every target is blocked
produces no event at all and is drawn `pending` for the rest of the run.

The logs are better than the graph but not complete. `run` logs
`[7/33] rabi running on <targets>` before the target loop, so the journal does name the
current routine; per *target* there is only the `ok`/`FAILED` line afterwards. An
operator watching a long node can see the routine and cannot see which target.

**The fix.** Emit a progress event *before* the work as well as after, carrying the
targets about to run:

```python
{"step": position, "total": len(order), "routine": name, "running": [...targets]}
```

`advanceNodes` treats an event carrying `running` as a start — set `State = "running"`,
leave `Done` alone — and an event without it exactly as it does now.
`CalibrationNodeState` gains `running: string[]`, and a skipped target emits a completion
event so a blocked node settles instead of sitting at `pending`. A driver that predates
this sends no `running` key and behaves as it does today.

**Refined while implementing phase 1: a skipped target needs a state of its own.** The
completion event settles the node, but counting a skip as `done` says a node measured
something when nothing ran. So the event also carries a cumulative `skipped`, and
`settledState` gains `blocked` — every target skipped — with `partial` for a mixture.
Failure outranks a skip, since a node with one of each has something to investigate.
`blocked` is a fifth walk-reported state and is distinct from the plan-derived `skipped`,
which still means "applies to nothing here".

### 7.2 The set in flight

`running: string[]` on the node state is also the answer to tracking components, and it
needs no new event and no new collection.

- The node in flight draws as `running`, the style that already exists.
- Its in-flight targets render as chips on the node and in `NodeCard` — target names
  under a pulsing box is "these components are being calibrated right now", which is the
  question asked.
- The group *is* the set, so nothing has to be joined client-side to recover it.

`layoutGraph` and `statusOf` do not change shape: `statusOf` already returns
`reported?.state ?? "pending"`, and this makes `reported.state` arrive on time.

The plan gains `groups` per routine — the colour classes the driver computed — so the
drawing can show a node's group count beside its target count, and an operator can see
where a run's parallelism went before it starts.

## 8. Chip layout — out of scope, and its own RFC

Quantify has no geometry: `QuantumDevice` has elements and edges, the hardware config has
ports and outputs, and there is no coordinate and no field to hold one. What exists is a
*coupling graph*, and per D3 that is all grouping needs.

A layout is wanted for a different job — drawing the chip the way other vendors' consoles
do, in the QPU driver and registry — and it should be designed there, as **RFC 0010**,
not appended here. The only decision this RFC needs to record is that it is not a
dependency: nothing in §5–§7 waits for a layout, and grouping must never be rewritten to
want one, because physical distance is the wrong metric for the question grouping asks.

## 9. Implementation plan

Six phases. Each is separately valuable and separately revertable.

**Where this stands.** Phases 1 and 2 are done, and every mechanism phases 3 and 4 call
for is built and tested: the group loop, per-target acquisition channels, the demultiplex,
the per-target `Sweep`, group-aware `escalating` and `amplified`, the `measure_group` hook,
and the `compatible_groups` split.

Twelve of the thirty-three routines group: `allxy`, `allxy_check`,
`readout_discrimination`, `readout_fidelity`, `t1`, `t2_echo`, `rabi`, `drag`,
`fine_amplitude`, `fine_amplitude_90`, `rb` and `interleaved_rb`. That is the whole
time-domain single-qubit chain from `rabi` onwards, plus both benchmarks.

What is left is application rather than design. Of the fourteen that measure themselves,
`ramsey` has its schedule converted but not its loop (§6.5), and `resonator_spectroscopy`,
`qubit_spectroscopy`, `drag_12`, `fine_amplitude_12` and `coupler_anticrossing` are
untouched — two of those §6.5 argues should stay sequential. Phase 5 (the remaining edge
routines) and phase 6 (the acceptance measurement) are open.

Grouping is off unless `calibration.yml` says otherwise, so none of this changes an
existing chip's walk until an operator turns it on.

**Phase 1 — the node in flight.** §7.1. A pre-work progress event in `dag.py`, a
completion event for a skipped target, the `running` branch in `advanceNodes`, the field
in `types.ts`. No grouping anywhere. Ships a graph that shows where a run has got to,
which every later phase is watched through.

**Phase 2 — grouping, computed and published, nothing executed.** `grouping.py`, the
`parallel` block, the feasibility checks, `groups` on the plan. `enabled: false` by
default, so the walk is unchanged and the phase is provable by unit test and by `--plan`
output. Answers "which components can run together" before anything depends on it being
right.

**Phase 3 — the fusion mechanism, and the routines that need only the hook.**
`build_group_schedule`, `add_together`/`add_after`, `channel_of`, the group loop and the
per-output narrowing in `dag.py`, and conversion of the routines with no per-target sweep
state (§6.6). The value is not the speedup — the expensive nodes come later — it is that
the channels, the alignment and the demultiplexing are proven first.

**Phase 4 — per-target setpoint state, then the routines that escalate.** §6.6 and §6.5.
Move each routine's swept setpoints off the routine and onto the target, which is what
unblocks the ten remaining plain-path nodes *and* group-aware `escalating` with
per-subset widening. This is where the bulk of the saving arrives, and it is a larger
piece of work than phase 3.

**Phase 5 — edges.** `edge_spacing`, and fusion for the six edge routines. Last because a
CZ occupies both its qubits and a flux line, so it has the most conflicts and the least
to gain, and because it is only meaningful once its qubits calibrate in parallel.

**Phase 6 — the acceptance measurement.** §5.6. `parallel_penalty` in the report from
fused-versus-isolated `rb`, plus the pair-scale ZZ term in the simulator that D10 says
can test the detector. This is what turns `qubit_spacing` from a guess into a setting.

## 10. Testing strategy

### 10.1 The floor

`grouping.py` and `fusion.py` are added to `PY_COV_INCLUDE` at `PY_COV_MIN = 96`. They
qualify where the routines do not: both are pure functions over configs and datasets with
no instrument behind them, so 96% is reachable honestly rather than by asserting that
mocks were called.

Shared topology fixtures go in `tests/utils/chips.py` — chain, square lattice, heavy-hex
and star, each parameterised by size — beside the existing `circuits.py` and
`simulation.py`.

### 10.2 What the tests must establish

Not "the code runs". Each of these is a way the feature can be wrong:

- **Grouping.** No two members of a group are within `qubit_spacing`; `exclude` beats the
  spacing; groups partition the targets exactly once each; the colouring is deterministic
  across runs; the class counts match §5.5's table for each topology at each spacing, and
  do so at two different sizes of the same topology — which is what pins the claim that
  the count does not depend on `N`.
- **Feasibility.** A group whose clocks straddle more than `if_limit_hz` is split and the
  message carries both figures; one whose amplitudes sum past full scale is split; one
  needing more sequencers than the module has is split.
- **Fusion.** A fused schedule's per-target operations share a start time — asserted on
  the schedule's own timing, not on the calls made to build it, which is why the test
  harness's schedule stand-in had to start tracking when each operation begins. A group
  of one goes down the path it did before fusion existed. And a following stage starts
  after the *longest* operation of the last, since a group whose targets have different
  pulse durations would otherwise overlap.
- **Demultiplexing.** Each target's `analyse` receives its own channel. The test that
  matters is the adversarial one: fuse two targets whose correct answers differ, and
  assert each fit lands on its own. A demultiplexer wired to channel zero passes every
  same-answer test ever written.
- **Per-target failure.** One refused fit in a group of five yields four results and one
  error against the right target, and the walk continues.
- **Escalation.** A group where one target refuses re-fuses that target alone, and the
  satisfied targets are not re-swept over the widened range.
- **The in-flight node.** A single-target node is reported `running` before it is
  reported `done` — the regression test for §7.1, and it fails against the code as it
  stands. A node whose every target is blocked settles rather than staying `pending`.
- **The crosstalk detector.** With the pair-scale ZZ term on, `parallel_penalty` is
  non-zero; with it off, zero. Asserts the detector responds, and asserts nothing about
  what spacing is safe (D10).
- **Backward compatibility.** No `parallel` block calibrates exactly as today, asserted
  by comparing the walk's schedule sequence against the sequential one.

### 10.3 What the simulator can and cannot show

It can run a fused schedule end to end and produce per-qubit populations from per-qubit
amplitudes, so every claim above except the last is testable without hardware. With
D10's addition it can also show that the crosstalk detector detects crosstalk — for a
pair or a triple, since `MAX_ENTANGLED = 3`. It cannot show that a *group* is
crosstalk-free: a chip-scale joint register is beyond the ceiling, and the coefficient
would be one this project chose. §5.6 is the hardware counterpart, and this RFC does not
move to Implemented until it has been run.

**Implemented as `CoupledTransmons.zz_mhz`, zero by default.** A diagonal term shifting
each qubit's frequency in proportion to the other's excitation, so a phase calibrated with
the neighbour in ``|0>`` is wrong with it in ``|1>`` — crosstalk that costs phase rather
than population, which is exactly why a routine measuring one qubit at a time cannot see
it. The tests assert it is zero by default, non-zero and proportional when set, and
diagonal; nothing asserts a safe spacing, which is the line D10 draws.

## 11. What this deliberately does not do

- **No concurrent instrument access.** D1. One cluster, one arm-and-start cycle. The
  per-module `stop()` narrowing D1 discusses is noted as an independent latency aside,
  not adopted.
- **No parallelism across routines.** Two independent branches of the DAG could in
  principle run at once, but they contend for the same cluster and would have to be fused
  into one schedule anyway — at which point the grouping is over targets again, and the
  ordering guarantees `diagnose` relies on get harder to keep. The win is over targets,
  and that is where this stops.
- **No chip-scale crosstalk model.** D10 bounds simulation to validating the detector.
  Learning a coupling matrix is a different RFC.
- **No frequency reallocation.** Klimov et al. optimise frequency trajectories to reduce
  crosstalk; that changes what the chip *is*, and this RFC only decides what to measure
  at the same time.
- **No chip geometry.** §8 — RFC 0010.
- **No change to the graph's shape, checks or provenance.** RFCs 0005, 0007 and 0008 hold
  as written; a group is a different number of targets per acquisition and nothing else.

## 12. References

- J. Kelly, P. O'Malley, M. Neeley, H. Neven, J. M. Martinis, *Physical qubit calibration
  on a directed acyclic graph*, [arXiv:1803.03226](https://arxiv.org/abs/1803.03226)
  (2018). The graph and `diagnose` already follow it; §5.1 follows its remarks on
  parallelism.
- J. M. Gambetta et al., *Characterization of addressability by simultaneous randomized
  benchmarking*, Phys. Rev. Lett. **109**, 240504 (2012),
  [arXiv:1204.6308](https://arxiv.org/abs/1204.6308). The acceptance measurement of §5.6.
- P. Murali, D. C. McKay, M. Martonosi, A. Javadi-Abhari, *Software Mitigation of
  Crosstalk on Noisy Intermediate-Scale Quantum Computers*, ASPLOS 2020,
  [arXiv:2001.02826](https://arxiv.org/abs/2001.02826). Tractable pairwise
  characterisation, then serialisation of the conflicting pairs.
- Y. Zhu et al., *Hardware-aware Calibration Protocol for Quantum Computers*, ISCA 2025,
  [doi:10.1145/3695053.3731036](https://doi.org/10.1145/3695053.3731036). Parallel
  calibration by graph traversal over compatible operations; subgraph splitting; 8–25×
  overhead reduction.
- J. Heinsoo et al., *Rapid high-fidelity multiplexed readout of superconducting qubits*,
  Phys. Rev. Applied **10**, 034040 (2018),
  [arXiv:1801.07904](https://arxiv.org/abs/1801.07904). Five qubits in one readout
  channel, simultaneous within 1% of individual — the precondition set of §5.4.
- *Quantum Crosstalk Analysis for Simultaneous Gate Operations on Superconducting
  Qubits*, PRX Quantum **3**, 020301 (2022),
  [doi:10.1103/PRXQuantum.3.020301](https://doi.org/10.1103/PRXQuantum.3.020301).
- P. V. Klimov et al., *Optimizing quantum gates towards the scale of logical qubits*,
  Nature Communications **15**, 2442 (2024),
  [arXiv:2308.02321](https://arxiv.org/abs/2308.02321). Frequency allocation as crosstalk
  mitigation — the alternative §11 declines.
- *CAMEL: Physically Inspired Crosstalk-Aware Mapping and gatE scheduLing for
  Frequency-Tunable Quantum Chips*,
  [arXiv:2311.18160](https://arxiv.org/abs/2311.18160). Local-window partitioning.
- F. Arute et al., *Quantum supremacy using a programmable superconducting processor*,
  Nature **574**, 505 (2019). The four coupler patterns — an edge colouring of the grid
  at Vizing's Δ.
