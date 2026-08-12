# RFC 0007 — Calibration Without Priors

- **Status:** Draft
- **Author:** Martin Ahindura
- **Created:** 2026-08-12
- **Depends on:** RFC 0004 (routines, the DAG walk), RFC 0005 (the completed graph,
  check/calibrate/diagnose)
- **Touches:** `qpi-driver` (Python only — no new operation, no new event type, no
  server or SDK change)

## 1. The idea

The graph is complete and every node writes what it should. It still cannot calibrate
a chip nobody has calibrated before, because almost every sweep in it is a *window
around a value the config already holds* — and on a new chip that value is a guess.

The operator is therefore required to know, in advance, roughly what each parameter
is. That inverts the purpose of the thing: not knowing the parameters is the reason
the nodes exist.

This is not a theoretical complaint. On a 5-qubit flux-tunable-coupler chip in
August 2026, with a driver whose graph was complete and whose fits were all guarded:

**`qubit_spectroscopy` failed six consecutive runs.** Its default sweep is ±20 MHz
about `clock_freqs.f01`. The config carried 4.7364 GHz, taken from a
`VNA_f01_frequency` in a device description that nothing in this driver had ever
measured. The qubit was at 4.4339 GHz — 302 MHz away, outside every window the node
would ever look in. The refusal it printed, *"no drive power in the sweep resolved a
line"*, was true and said nothing about the window. Six nodes downstream then measured
an unexcited qubit and fitted its noise, which read as six unrelated failures.

**`rabi` could not have found the π pulse either.** Its default sweep is
`linear_setpoints(0.0, 0.5, 41)` while the element validates `amp180` in `[0, 1]` —
half the addressable range. That chip's own working calibration, from another
control stack, used `amp180 = 0.5683`. Above the top of the sweep. And
`require_in_range` cannot catch it: it checks that the fitted value lies *inside* the
swept range, which is the opposite test.

**`readout_operating_point` sweeps ±1 MHz over 3 points.** The resonator's measured
linewidth was 370 kHz, so two of its three points sat 2.7 linewidths off resonance and
the node chose one of them. The linewidth is measured by `resonator_spectroscopy` two
nodes earlier and is sitting right there in the report. The node uses a constant
instead. It had to be hand-set to 200 kHz for readout to work at all.

Three different nodes, one shape: **a sweep range that is a constant or an operator's
guess, where a bound was derivable.**

This RFC makes every sweep derive its own range, from the instrument or from physics
or by escalation, and reduces the operator's config to an optimisation that may narrow
a search but is never required to make one possible.

## 2. Vocabulary

- **Prior** — a value in the device config that has not been measured by this driver.
  A design figure, a value from another control stack, a placeholder. Indistinguishable
  in the file from a measured one, which is half the problem (§10).
- **Addressable band** — the frequencies a port can actually produce. For a Qblox RF
  module, its LO ±500 MHz. Outside it there is no experiment, only a compile error.
- **Bound class** — where a sweep's range comes from: hardware, physics, or escalation
  (§5).
- **Escalation** — widening a sweep and re-running because the result said the window
  was wrong, rather than failing.

## 3. Decisions

| Decision | Resolution |
|---|---|
| New operation or event type? | **No.** `calibrate` carries this. Python driver only. |
| What a `RoutineConfig` means | **Changed, and this is the core of the RFC.** Today a sweep parameter is often load-bearing: omit it and the node cannot work on this chip. After this, every sweep has a derived default that works on any chip the hardware can address; config may only *narrow* a search to save time. A node that cannot run without an operator-supplied range is a bug. |
| Where a bound comes from | Hardware config for instrument limits, upstream measurements for physical ones, escalation for the rest. New `tuners/base/limits.py`; the hardware config is already reachable from a routine via `device.hardware_config()`, as `has_flux_port` shows. |
| Guards as signals | **Changed.** The six "your window is wrong" guards added in August 2026 raise prose. They gain a structured form the caller can act on, so the same detection drives a retry instead of a failure. §6. |
| Routine interface | **Unchanged.** `measure` already absorbs a routine whose setpoints depend on an earlier acquisition — `qubit_spectroscopy` is the second implementor. No third interface. |
| A supplied range | **A suggestion, tried first, never required.** An operator's `span` becomes the first attempt and the derived bound the fallback — which is the same two-pass shape §6 already needs, not a second mechanism. A wrong hint costs one wasted sweep, not a failure. §7. |
| Rewriting `calibration.yml` | **No.** A hint that keeps failing is reported, not edited away. That file is hand-authored intent — the August 2026 one is mostly reasoning — and `spec.amplitude` already shows what remembering a search hint costs. §7. |
| Guards that wrongly *accept* | **In scope**, §6, moved in from "does not fix". Escalation only fires on a refusal, so a guard that accepts noise bypasses this whole RFC. It is the trigger condition, not a sequel. |
| Wide sweeps and the instruction budget | In scope as a **constraint**. A derived default must fit a sequencer, and where a 2-D band does not, it is **chunked across acquisitions** with overlapping edges rather than refused. §5. |
| Skipping blocked nodes | In scope, §11 — and on *parameters*, not on failed nodes. `depends_on` orders the walk and is not a data dependency: `cz_chevron` depends on two nodes that write nothing at all. Blocked nodes are **skipped with the blocker named**, never auto-failed. |
| How `reads` is known | **Derived, not declared.** Build the schedule against an instrumented device, record the paths it read, then decide whether to run. A hand-written list can be wrong in exactly the way §11 exists to prevent. §11. |
| A skipped node's stale parameter | **Kept and marked**, not cleared. Clearing it would stop a chip that worked yesterday from running today. §11. |
| Staging writes outside the device file | **No second store.** The device file gains provenance; a parallel database would need synchronising with the file the executor actually reads. §10. |
| Mixer calibration | **Out of scope.** Out-of-band, as RFC 0005 had it. |
| Crosstalk | **Out of scope**, unchanged from RFC 0005. |
| Removing `span`/`points` from configs | In scope, and last. Deleting a knob before its derived default is proven would strand the operator. |

## 4. The gap, concretely

Every sweeping node, what it sweeps, and where its range comes from today. "Prior"
means the range is a window about an unmeasured config value; "constant" means it
ignores what is known.

| Node | Sweeps | Default range | Today | Class |
|---|---|---|---|---|
| `resonator_spectroscopy` | readout freq | ±10 MHz, 51 | prior | hardware |
| `resonator_punchout` | freq × amp | ±10 MHz × 0.01–0.5 | prior + partial | hardware |
| `time_of_flight`, `resonator_relaxation` | trace window | 2 µs | constant | physics |
| `qubit_spectroscopy` | freq × amp | ±20 MHz, 51 (+600 MHz search) | **partly done** | hardware |
| `rabi` | amp | 0–0.5, 41 | **partial range** | hardware |
| `resonator_spectroscopy_excited` | readout freq | ±10 MHz, 51 | constant | physics |
| `readout_operating_point` | freq × amp | ±1 MHz, 3 | constant | physics |
| `three_state_operating_point` | freq × amp | ±3 MHz, 5 | constant | physics |
| `f12_spectroscopy` | ef freq | f01−300 MHz ±200 MHz, 81 | **already derived** | physics |
| `rabi_12` | ef amp | 0–0.5, 41 | partial range | hardware |
| `drag`, `drag_12` | motzoi | ±`drag_span`, 31 | constant | physics |
| `ramsey` | delay | 4 ns–10 µs, 41 | constant | escalation |
| `ramsey_12` | delay | 4 ns–30 µs, 241 | constant | escalation |
| `t1`, `t2_echo` | delay | 0–100 µs, 41 | constant | escalation |
| `flux_spectroscopy` | flux × freq | ±0.2 × ±50 MHz | constant | hardware |
| `coupler_anticrossing` | current × freq | 0–3 mA × ±100 MHz | constant | hardware |
| `cz_spectroscopy` | cz freq | ±200 MHz, 81 | prior | hardware |
| `cz_parametrization` | amp × duration | 0.1–0.6 × 20–200 ns | constant | hardware + escalation |
| `cz_chevron` | amp × duration | — × 20–400 ns, 39 | constant | hardware + escalation |
| `conditional_phase` | phase | 0–360°, 25 | **complete by construction** | — |

Two rows are worth dwelling on, because they show the answer is already in the
codebase and was applied once.

`f12_spectroscopy` centres on `f01 + anharmonicity_prior` and **ignores the config's
`f12` entirely** — a physical relationship beats an unmeasured field, and its docstring
says so. That is exactly the pattern this RFC generalises. It is also why that node was
the only one that ever found the August 2026 chip's qubit: it was the only node
searching from physics rather than from a prior.

`conditional_phase` sweeps 0–360°. A phase has no range to guess at, so it never had
this problem. Every other node's range should be as unarguable as that one's.

## 5. Three classes of bound

**Hardware-bounded.** The range is a property of the instrument and is readable. A
Qblox RF module reaches its LO ±500 MHz — `NCO_FREQ_LIMIT_STEPS` over
`NCO_FREQ_STEPS_PER_HZ` in quantify's own constants — and the LO is in the hardware
config:

```python
device.hardware_config().hardware_options.modulation_frequencies["q0:mw-q0.01"].lo_freq
# 4550000000.0
```

So q0's drive port addresses 4.05–5.05 GHz and nothing else, and `qubit_spectroscopy`
can sweep that band by construction. Amplitudes are the same kind of fact: the element
validates `[0, 1]`, so a sweep that stops at 0.5 is not a bound, it is a typo with a
long life. There is nothing for an operator to know here, and no chip on which a
different answer is right.

**Physics-bounded.** The range follows from something already measured plus a
constraint. A transmon's anharmonicity is negative and a few hundred MHz, so
`f12` lies in `[f01 − 400 MHz, f01 − 150 MHz]`. A readout optimum is within a few
linewidths of the resonance, and the linewidth was *measured upstream*:
`resonator_spectroscopy` reports it, and `readout_operating_point` ignores it. The
two trace nodes need a window a few ring-up times long, which is the same linewidth read
as a time. A DRAG optimum is near `−1/(2·alpha)`, which is why `drag_span` exists; it
should be centred on the measured anharmonicity rather than on zero.

Note the direction of the dependency: every one of these is downstream of the node that
measures what bounds it. The graph already orders them correctly, so nothing here needs
a topology change — only for a node to read the report instead of a constant.

**Escalation-bounded.** Time constants have no upper bound to derive. A fixed 0–100 µs
T1 sweep is wrong in both directions: at T1 = 300 µs the curve decays 28% and the
August 2026 span-over-scatter guard rightly refuses it, and at T1 = 2 µs it is over by
the second point. This is the only class that needs a loop, and it is the interesting
part of the design.

### A derived range that will not fit is chunked, not refused

A derived bound is not free. A 1 GHz band at 2 MHz steps is 501 acquisitions, about 6,500
Q1ASM instructions against a ceiling empirically bracketed at 12,376-works /
13,074-fails on the August 2026 cluster, so a 1-D band sweep fits. A 2-D sweep over the
same band — a frequency band against five drive powers — does not.

Where it does not fit, the sweep is **split across several acquisitions** rather than
refused, because refusing puts the operator back to choosing a range by hand and that is
the thing this RFC exists to stop. `measure` already runs a routine's own loop, and the
allowance is already summed across it, so the machinery is in place.

One constraint on the split, worth stating because it is easy to get wrong: **the chunks
must overlap by at least one linewidth.** A line that lands exactly on a boundary is
otherwise half in each chunk and resolved in neither, which is a spurious refusal that
looks like a dead qubit.

## 6. Guards, in both directions

The six guards added in August 2026 all detect the same thing from different angles:
*the window you swept cannot support the number you are about to write.*

| Guard | What it detects |
|---|---|
| `require_resolved_line` | line narrower than the step, or not above the noise |
| `require_resolved_curve` | fitted curve no taller than its own residual scatter |
| `MIN_SHIFT_TO_LINEWIDTH` | the X gate did not excite the qubit |
| `MIN_SEARCH_PEAK` | nothing in a wide search stands above the scatter |
| `MAX_DEMODULATED` | a fine-amplitude sweep far past its model's bound |
| `require_in_range` | fitted value outside what was swept |

Each is wired to a terminal failure. That is right when the chip is dead and wrong when
the window is. And there is a second failure mode, on the other side of the same
decision, which turns out to matter more.

### 6.1 Refusals become retry signals

Several of these are more usefully read as instructions: *"the decay was never seen in
this window"* is a request for longer delays, not a verdict on the chip.
`require_in_range` firing on the high side is a request for more amplitude.

The proposal is a structured error the caller can act on:

```python
class OutOfRange(RoutineError):
    """The sweep was wrong, and in a known direction."""
    axis: str          # "delays", "amplitudes", "frequencies"
    direction: str     # "wider" | "narrower" | "higher"
    suggested: float   # a scale factor or an endpoint
```

and one shared helper on the base routine that re-sweeps on it, with a bounded number
of attempts and a hard stop at the class's own bound — full scale for an amplitude, the
addressable band for a frequency, a configured ceiling for a delay. A node that
escalates three times and still cannot resolve its curve has found a dead qubit, which
is the only case that should fail.

Two properties this must keep, both learned the hard way:

- **Escalation must never widen what gets written.** The August 2026 search pass
  chooses where to look and nothing else; the value still comes from a narrow sweep
  that clears `require_resolved_line`. Widening a *reported* range would turn a guard
  into a rubber stamp.
- **The timeout must follow the sweep, not bound it.** Already true: `allow()` raises
  a schedule's wait to fit its own pulses, and as of August 2026 a multi-schedule
  routine is judged on the sum of its allowances. So escalation cannot fail for being
  slow, only for being stuck. Nothing further is needed here.

### 6.2 Acceptances need a floor that scales

Escalation only fires when a guard refuses. So a guard that *accepts* noise does not
merely report one wrong number — it silently bypasses everything else in this RFC,
because the widening never runs. This is the trigger condition for §6.1, which is why it
belongs here rather than in a sequel.

It is not hypothetical. Measured on the simulated chip while writing this: a 61-point
window of pure noise 250 MHz from the qubit produced a Lorentzian that cleared
`MIN_LINE_SNR` and returned a confident frequency; a 101-point window did the same. The
existing floor is a constant 3.0, and the tallest of *n* noise draws grows with *n* —
about `sqrt(2 ln n)`, so 2.6 at 30 points and 3.4 at 300. A constant floor is therefore
too tight at one end of the range and too loose at the other, and 3.0 is on the wrong
side for every sweep worth calling wide.

Two changes, both cheap:

- **Scale the floor with the number of points.** `MIN_SEARCH_PEAK = 6.0` was already
  chosen this way for the wide search, against a measured 115–126 on the line and
  2.5–2.9 off it. `require_resolved_line` should use the same reasoning, not a constant.
- **Require the line to reproduce.** `qubit_spectroscopy` already sweeps drive amplitude
  and `fit_spectroscopy_power` already fits every row; today it picks the best row and
  discards the rest. Requiring the chosen centre to agree with a second row to within a
  linewidth costs nothing, since the data is already acquired, and noise does not
  reproduce across powers. On the August 2026 chip this would have refused run one rather
  than run six: its three rows fitted 782.7 kHz, 8.5 kHz and 28 kHz, which no real line
  does.

The second is the stronger test and the cheaper one, and it generalises: any node that
already sweeps a second axis can ask its answer to survive that axis.

## 7. What the operator's config becomes, and what it means

Today a working `calibration.yml` for a new chip carries a paragraph of reasoning per
node and a hand-derived span for six of them, and every one of those numbers was found by
a failed run. After this RFC it carries which qubits, which edges, a timeout — and
optionally a hint.

**A supplied range is a suggestion, not a requirement.** It is tried first; if the guard
refuses what it produced, the derived bound runs as the fallback. This is not a second
mechanism: it is §6.1's escalation with the operator's window as the first attempt. The
narrow-then-widen shape `qubit_spectroscopy` already has *is* this design, and reading it
that way removes a distinction the earlier draft was carrying for nothing.

So a good hint saves a wide sweep, and a wrong hint costs one wasted narrow sweep before
the fallback — bounded, visible in the report, and never a failure. Which means an
operator can guess freely, and that is the point: a hint that could break the run is not
a hint, it is a requirement wearing a suggestion's clothes.

**The config is not rewritten.** A tempting extension is to have the driver comment out
or replace a hint it proved wrong, so later runs skip it. This RFC declines, for two
reasons. `calibration.yml` is hand-authored intent — the August 2026 one is mostly
*reasoning*, and a machine that edits it either destroys that or needs a comment-
preserving YAML round-trip to avoid doing so. And remembering a search hint is a known
failure on this codebase already: `spec.amplitude` latched at 0.16 in the *device* file
and from then on `qubit_spectroscopy` only ever tried multiples of it, never reaching back
to its own defaults, which is why the August 2026 config had to name amplitudes outright
to break the latch. A self-updating hint is that bug with a wider blast radius.

Report it instead. The run already produces a report; it can say *this hint was tried,
fell back, and here is the range that worked* — the same information, in a place the
operator chooses to act on. And if the derived default does its job, a hint that keeps
failing is one nobody needs to keep.

The test of all this is §8, and it is not satisfiable by argument.

## 8. Testing strategy

Three tiers as RFC 0004 §7 has them, plus one acceptance test that is the whole point.

- **Tier 1.** `limits.py` against hand-written hardware configs: an LO at each end, a
  missing entry, an unreadable config. Every derived range asserted against the
  arithmetic, not against a recorded constant.
- **Tier 2.** Every derived default compiles, and a chunked one compiles per chunk with
  its edges overlapping. A band-wide frequency sweep is hundreds of setpoints, and the
  sequencer's instruction budget is what makes this more than a formality (§5).
- **Tier 3.** Per class: a simulated chip whose true value sits outside the *old*
  default and inside the derived one. The August 2026 work has two of these already
  (`test_a_configured_f01_hundreds_of_mhz_out_is_still_located`, and the refusal case).
- **Acceptance.** `test_a_chip_known_only_from_its_design_document_calibrates`: seed the
  device with frequencies good to ±300 MHz, `amp180 = 0`, no coherence times, and
  require the full walk to complete and `rb` to clear a fidelity threshold.

That last test is the definition of "start from knowing nothing", and it belongs in
`test_calibration_e2e.py`, which walks the whole DAG over a simulated chip. Note for
whoever picks this up: that suite was red from late July to 12 August 2026 — every node
died on `AttributeError: 'SimulatedBackend' object has no attribute 'last_allowance_s'`
— and the failure was read as a stable baseline for two weeks of work. A suite that
proves the headline claim of the driver deserves a CI leg that cannot be mistaken for
noise, which is an argument for making it non-optional rather than `-m scqubits`.

## 9. Implementation plan

In this order, so each step is independently mergeable and the escalation loop comes
after the two classes that need no loop at all.

0. **`reads`, and skipping on it** (§11). Derive what each routine consumes, block on an
   unproduced parameter, report the blocker. Independent of everything below it, and it
   goes first because it is what makes the failures of the phases after it legible — a
   phase-2 regression on one node should show as one failure and a list of skips, not as
   a graph-wide puzzle. It also stands alone: worth landing even if nothing else here is.
1. **The accept side** (§6.2). Scale `require_resolved_line`'s floor with the number of
   points, and require `qubit_spectroscopy`'s chosen centre to reproduce across a second
   drive power. Before the derived ranges, not after: a guard that accepts noise means the
   escalation those phases rely on never fires, so measuring their effect would be
   measuring it through a broken detector. Also the cheapest phase here: the second test
   needs no new acquisition, only rows `fit_spectroscopy_power` already fits and drops.
2. **`tuners/base/limits.py`.** `addressable_band(device, port_clock)` from the LO and
   the backend's IF limit; `full_scale(element, path)` from the element's own validator.
   Tier-1 tests. No routine changes, so nothing can regress.
3. **The hardware-bounded class.** Frequency sweeps default to a coarse pass over the
   addressable band, then the existing narrow sweep — the two-pass shape
   `qubit_spectroscopy` already has, lifted into a shared helper, with a supplied `span`
   as its first attempt (§7). Amplitude sweeps go to full scale. Chunking where a derived
   grid does not fit. Finishes `qubit_spectroscopy`'s `search_span`, currently 600 MHz
   because the LO was not yet known to be readable from a routine.
4. **The physics-bounded class.** The two operating points and both excited-state
   resonator sweeps take their span from the measured linewidth. `f12_spectroscopy`
   keeps its prior but bounds it to `[150, 400]` MHz. `drag` centres on the measured
   anharmonicity. Cheapest of the range phases, and it fixes a live readout bug.
5. **Escalation.** `OutOfRange`, the retry helper, and the bounded attempt count. Wire
   `t1`, `t2_echo`, `ramsey`, `ramsey_12`, and the two CZ duration sweeps.
6. **The acceptance test, then the knobs.** Land the test; then delete every `span` and
   `points` that phases 3–5 made redundant, from the routines' defaults and from the
   operator's `calibration.yml`. A knob removed before its replacement is proven is a
   regression, which is why this is last.

## 10. What this does not fix

**A prior is still indistinguishable from a measurement.** After this RFC the driver
finds the qubit wherever it is, but the device file still cannot say whether
`clock_freqs.f01` was measured by this driver or typed in from a design document. The
August 2026 chip carried `f01: 4735509751.238763` — nine significant figures, and the
line was never there. Provenance per parameter (which node wrote it, when, from what
signal-to-noise) would make a stale value visible instead of merely wrong. It is a
device-file format change and belongs in its own RFC.

Three things here are waiting on that one field: §2's definition of a prior, §11's "no
trustworthy value", and §11's marking of what a skipped node did not confirm.

**Why not stage the writes somewhere else until the run succeeds?** Considered, and
declined as posed — but the problem underneath it is real, so it is worth being precise
about which part.

The corruption on the August 2026 chip was not caused by writing too early. It was caused
by writing a *wrong* value at all: `rabi` wrote `amp180 = 0.0158` and every later run
inherited it. A staging store would have held that value for the length of the walk and
then committed it, because the walk did not fail: `require_in_range` accepted 0.0158 and
`rabi` reported success. Deferring the commit does not help when the producing node
believes it succeeded, which is the case that actually happened. §6's guards are what
address that, and did.

Nor can the value be withheld from the *walk*: `rabi` needs the `f01` that
`qubit_spectroscopy` just wrote, so downstream nodes read upstream results within the run
by construction. The staging boundary can only ever be the file, not the device object.

And an all-or-nothing file commit has a cost of its own. A run that measures the
resonator and f01 correctly and then fails at `rabi` would discard two good measurements,
so the next run starts from the same bad priors — on this chip, that is the difference
between converging and not.

What is worth taking from the idea is the per-parameter version, and it is the provenance
field again: commit a parameter when the node that produced it succeeded *and* its guards
passed, and mark what it was. That is a strictly finer boundary than a staging store, it
does not need a second datastore to synchronise with the file the executor reads, and it
subsumes the all-or-nothing case. It belongs in the provenance RFC.

## 11. Skipping what cannot succeed

A node whose prerequisite was never produced cannot measure anything, and running it
anyway is how one failure became six. The August 2026 chip is the worked example:
`qubit_spectroscopy` failed, and `rabi`, `resonator_spectroscopy_excited`,
`readout_discrimination`, `allxy`, `drag` and `readout_fidelity` all then measured a
qubit still in `|0⟩` and reported confident numbers from its noise. Six failures with
six different-looking causes, none of them naming the one that mattered. Before the
August 2026 guards existed those nodes did not even fail — they wrote the noise to the
device file, and the next run inherited it.

So this is worth doing, and the wall-clock saving is the smaller half of the benefit.
The mechanism matters, though, because the obvious one — *if a node fails, skip its
dependents* — is wrong on this graph in three separate ways.

**`depends_on` is not a data dependency.** It orders the walk. `cz_chevron` depends on
`rb` and `flux_spectroscopy`, and *neither writes a parameter* — both have empty
`updates`. Blocking two-qubit calibration because a benchmark came out low would be
plainly wrong. Twelve of the thirty-three nodes write nothing at all, so nothing can
depend on their output, and some are still depended on in the walk order.

**Disabled is not failed.** `qubit_spectroscopy` depends on `resonator_punchout`, which
is switched off on the August 2026 chip because its amplitude grid never reaches
punch-through (§12.3). `time_of_flight` is off too. Under naive propagation, disabling
either would skip the entire graph beneath it — which is to say, everything.

**A refiner is not a producer.** Seven parameters have two writers, where the first
produces and the second refines:

| Parameter | Produced by | Refined by |
|---|---|---|
| `clock_freqs.readout` | `resonator_spectroscopy` | `resonator_punchout` |
| `clock_freqs.f01` | `qubit_spectroscopy` | `ramsey` |
| `clock_freqs.f12` | `f12_spectroscopy` | `ramsey_12` |
| `rxy.amp180` | `rabi` | `fine_amplitude` |
| `r12.ef_amp180` | `rabi_12` | `fine_amplitude_12` |
| `cz.square_amp`, `cz.square_duration` | `cz_parametrization` | `cz_chevron` |

`drag` depends on `ramsey`, but `ramsey` only refines an `f01` that
`qubit_spectroscopy` already produced. If `ramsey` fails, `f01` keeps a measured value
and `drag` can legitimately run — as can `allxy`, `fine_amplitude`, `rb` and
`allxy_check` behind it. Node-level propagation would skip five nodes for nothing.

**The proposal: block on an unsatisfied parameter, not on a failed node.**

- Routines gain a `reads` set, the counterpart of the `updates` they already have, which
  makes the data dependencies explicit and separable from walk order. **Derived, not
  hand-declared:** `read_path` is already the single way a routine touches the device, so
  building the schedule against an instrumented device records exactly what that node
  needs. A hand-written list can omit a path the routine really reads, which is this
  section's own bug moved one level up and made invisible.

  The order this implies is *build, inspect, then decide*: build the schedule (cheap, no
  instrument), see what it read, block if any of it is untrustworthy, otherwise run.
  Caveat to settle in implementation: a few nodes also read in `analyse` — for instance
  `resonator_spectroscopy_excited` reads `clock_freqs.readout` there to difference against
  — and those reads happen after the acquisition, too late to block on. Either they are
  hoisted into `build_schedule`, or the first walk is treated as the discovery run and the
  derived set cached.
- A node is blocked when a parameter it reads has no trustworthy value — not produced
  in this walk, and no measured prior. A failed *refiner* leaves the value trustworthy,
  so nothing behind it is blocked.
- A disabled node that is the only producer of a parameter something reads is a **config
  error reported before the walk starts**, not a cascade discovered during it. That is
  strictly more useful than either running or skipping.
- Blocked nodes are recorded as **skipped, with the blocker named** — not failed.
  Auto-failing would replace six misleading failures with six fabricated ones, and would
  feed the drift check a history of failures that never happened.
- A skipped node's parameter is **kept and marked, not cleared.** Clearing it would mean a
  chip that ran jobs yesterday cannot run today because one node was blocked, which is a
  worse outcome than running on a value this walk did not confirm — provided the report
  says which values were not confirmed. That proviso is §10's provenance field.

`diagnose` already walks `depends_on` to blame the deepest failing ancestor rather than
the symptom (RFC 0005 §8), so the traversal exists and the calibrate path can borrow its
shape.

This section depends on §10's provenance problem for the *fully* correct version: "no
measured prior" is not decidable today, because a design value and a measurement look
identical in the device file. A useful version needs less — on a first calibration,
"produced in this walk" is sufficient, and that is exactly the case this RFC is about.
It is also what makes §8's acceptance test readable: on a chip known only from its
design document the first walk will have failures, and without skip-propagation its
report is the same six-way puzzle that motivated this RFC.

## 12. Open questions

1. **Where the IF limit lives.** ±500 MHz is Qblox. `drag_span` is already a
   `SchedulerBackend` property for exactly this reason — the two schedulers' DRAG
   parameters are different quantities — so `if_limit` probably belongs there too. Worth
   confirming against qblox-scheduler before assuming symmetry.
2. **Escalation in the DAG or in `measure`?** In `measure` keeps the DAG simple and the
   retry close to the physics. In the DAG makes the attempt budget uniform and visible
   in the report. Leaning `measure`, with the attempt count reported.
3. **Does `resonator_punchout` come back?** It is disabled on the August 2026 chip
   because its amplitude grid never reached punch-through, which is a range bug of
   exactly this kind. Phase 2 may simply fix it.
4. **What "high fidelity" means in the acceptance test.** A threshold low enough that
   the simulated chip's own gate error dominates is a weak test; one too high pins the
   test to simulator tuning. Perhaps assert against the simulator's injected error
   rather than a constant, as `test_rb_recovers_a_known_gate_error` does.
5. **How wide a chunked 2-D sweep is allowed to get.** §5 chunks rather than refuses, and
   the sequencer stops being the binding limit once it does — wall-clock takes over. A
   band-wide sweep against five drive powers at 1024 shots is tens of minutes, which is
   fine for a bring-up and not for a drift check. Probably a per-node cap that a bring-up
   raises, but that is a knob, and this RFC is about removing those.
6. **Whether `reads` needs `analyse`-time reads hoisted.** §11 derives the set at build
   time, and a handful of nodes read the device in `analyse` instead, which is too late to
   block on. Hoisting them is a small mechanical change to maybe four routines; caching a
   discovery run is less work and less honest. Decide when the four are counted.

## 13. Resolved during review

Recorded because the reasoning is worth keeping, and because two of these changed the
shape of the RFC rather than just settling a detail.

| Question | Resolution |
|---|---|
| Chunk a too-wide derived sweep, or refuse it? | **Chunk**, §5. Refusing hands range-picking back to the operator, which is the thing being removed. Chunks overlap by a linewidth so a line on a boundary is not lost in both. |
| Harden the *accept* side here, or in a sequel? | **Here**, §6.2. It is not a parallel concern: escalation only fires on a refusal, so a guard that accepts noise bypasses the entire RFC. It is the trigger condition. |
| `reads` declared or derived? | **Derived** from `read_path`, §11. A hand-written list can omit a path the routine really reads — this section's own bug, one level up and invisible. |
| Does a skipped node keep its stale parameter? | **Keep and mark**, §11. Clearing it stops a chip that ran yesterday from running today. |
| Stage writes in a separate store until the run succeeds? | **No**, §10 — and the diagnosis matters more than the answer. The August 2026 corruption was not an early commit; `rabi` reported *success* while writing 0.0158, so a staging store would have committed it too. The finer boundary is per-parameter commit gated on provenance. |
| Treat operator-supplied ranges as suggestions with a derived fallback? | **Yes**, §7 — and it collapsed a distinction the draft was carrying for nothing: a supplied window is just escalation's first attempt. |
| Rewrite `calibration.yml` when a hint proves wrong? | **No**, §7. It is hand-authored reasoning, and `spec.amplitude`'s latch already showed what remembering a search hint costs. Report the range that worked and let the operator decide. |