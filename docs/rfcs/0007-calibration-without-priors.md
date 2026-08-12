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
| Wide sweeps and the instruction budget | In scope as a **constraint**, not a feature: a derived default must fit a sequencer. Chunking a band across several acquisitions is an open question, §12. |
| Skipping blocked nodes | In scope, §11 — and on *parameters*, not on failed nodes. `depends_on` orders the walk and is not a data dependency: `cz_chevron` depends on two nodes that write nothing at all. Blocked nodes are **skipped with the blocker named**, never auto-failed. |
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

## 6. Guards become signals

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

Each is wired to a terminal failure. Several of them are more usefully read as
instructions: *"the decay was never seen in this window"* is a request for longer
delays, not a verdict on the chip. `require_in_range` firing on the high side is a
request for more amplitude.

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

## 7. What the operator's config becomes

Today, a working `calibration.yml` for a new chip carries a paragraph of reasoning per
node and a hand-derived span for six of them, and every one of those numbers was found
by a failed run. After this RFC it carries: which qubits, which edges, a timeout, and
whatever the operator wants to *narrow* to save wall-clock on a chip they already know.

The test of that claim is stated in §8 and is not satisfiable by argument.

## 8. Testing strategy

Three tiers as RFC 0004 §7 has them, plus one acceptance test that is the whole point.

- **Tier 1.** `limits.py` against hand-written hardware configs: an LO at each end, a
  missing entry, an unreadable config. Every derived range asserted against the
  arithmetic, not against a recorded constant.
- **Tier 2.** Every derived default compiles. A band-wide frequency sweep is hundreds of
  setpoints, and the sequencer's instruction budget is the constraint that makes this
  more than a formality (§12.1).
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

0. **`reads`, and skipping on it** (§11). Declare what each routine consumes, block on an
   unproduced parameter, report the blocker. Independent of everything below it, and it
   goes first because it is what makes the failures of the phases after it legible — a
   phase-2 regression on one node should show as one failure and a list of skips, not as
   a graph-wide puzzle. It also stands alone: worth landing even if nothing else here is.
1. **`tuners/base/limits.py`.** `addressable_band(device, port_clock)` from the LO and
   the backend's IF limit; `full_scale(element, path)` from the element's own validator.
   Tier-1 tests. No routine changes, so nothing can regress.
2. **The hardware-bounded class.** Frequency sweeps default to a coarse pass over the
   addressable band, then the existing narrow sweep — the two-pass shape
   `qubit_spectroscopy` already has, lifted into a shared helper. Amplitude sweeps go to
   full scale. Finishes `qubit_spectroscopy`'s `search_span`, which is currently 600 MHz
   because the LO was not yet known to be readable from a routine.
3. **The physics-bounded class.** The two operating points and both excited-state
   resonator sweeps take their span from the measured linewidth. `f12_spectroscopy`
   keeps its prior but bounds it to `[150, 400]` MHz. `drag` centres on the measured
   anharmonicity. Cheapest phase, and it fixes a live readout bug.
4. **Escalation.** `OutOfRange`, the retry helper, and the bounded attempt count. Wire
   `t1`, `t2_echo`, `ramsey`, `ramsey_12`, and the two CZ duration sweeps.
5. **The acceptance test, then the knobs.** Land the test; then delete every `span` and
   `points` that phases 2–4 made redundant, from the routines' defaults and from the
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

**A wrong window can still be *accepted*.** Escalation triggers when a guard refuses.
Measured on the simulated chip while writing this: a 61-point window of pure noise
produced a Lorentzian that cleared `MIN_LINE_SNR`, and a narrow sweep 250 MHz from the
qubit returned a confident frequency. So a node can be confidently wrong rather than
refusing, and no amount of widening helps, because widening never runs. Hardening the
accept side — a signal-to-noise floor that scales with the number of points, or
requiring a candidate to reproduce across two amplitudes — is the natural sequel, and
is what would have caught this chip on run one rather than run six.

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
punch-through (§12.4). `time_of_flight` is off too. Under naive propagation, disabling
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

- Routines gain a `reads` declaration, the counterpart of the `updates` they already
  have. That makes the data dependencies explicit and separable from walk order.
- A node is blocked when a parameter it reads has no trustworthy value — not produced
  in this walk, and no measured prior. A failed *refiner* leaves the value trustworthy,
  so nothing behind it is blocked.
- A disabled node that is the only producer of a parameter something reads is a **config
  error reported before the walk starts**, not a cascade discovered during it. That is
  strictly more useful than either running or skipping.
- Blocked nodes are recorded as **skipped, with the blocker named** — not failed.
  Auto-failing would replace six misleading failures with six fabricated ones, and would
  feed the drift check a history of failures that never happened.

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

1. **Instruction budget versus band-wide sweeps.** A 1 GHz band at 2 MHz steps is 501
   acquisitions, ~6,500 Q1ASM instructions against an empirically bracketed
   12,376-works / 13,074-fails ceiling on this cluster. That fits. A 2-D sweep over the
   same band does not. Chunk across acquisitions inside `measure`, or refuse and say
   which axis to narrow?
2. **Where the IF limit lives.** ±500 MHz is Qblox. `drag_span` is already a
   `SchedulerBackend` property for exactly this reason — the two schedulers' DRAG
   parameters are different quantities — so `if_limit` probably belongs there too. Worth
   confirming against qblox-scheduler before assuming symmetry.
3. **Escalation in the DAG or in `measure`?** In `measure` keeps the DAG simple and the
   retry close to the physics. In the DAG makes the attempt budget uniform and visible
   in the report. Leaning `measure`, with the attempt count reported.
4. **Does `resonator_punchout` come back?** It is disabled on the August 2026 chip
   because its amplitude grid never reached punch-through, which is a range bug of
   exactly this kind. Phase 2 may simply fix it.
5. **What "high fidelity" means in the acceptance test.** A threshold low enough that
   the simulated chip's own gate error dominates is a weak test; one too high pins the
   test to simulator tuning. Perhaps assert against the simulator's injected error
   rather than a constant, as `test_rb_recovers_a_known_gate_error` does.
6. **Does `reads` get derived or declared?** Declared is explicit and can be wrong in a
   way nothing detects — a routine that reads a parameter it did not declare is exactly
   the bug §11 exists to prevent, reintroduced one level up. Deriving it from the paths a
   routine actually touches would need the device access to go through something
   observable, which `read_path` already is. Worth a look before hand-writing 33 lists.
7. **Does a skipped node keep its stale parameter, or clear it?** Keeping it means the
   chip runs jobs on a value this walk could not confirm; clearing it means a chip that
   worked yesterday will not run today. Probably keep and mark, which is §10's provenance
   again — the same missing field answers both.
