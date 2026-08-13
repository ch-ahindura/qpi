# RFC 0007 — Calibration Without Priors

- **Status:** Implemented, with one known gap open — §11.5
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
| How `reads` is known | **Declared, and tested by derivation.** A node's read set is static, so it is stated like `updates`; a test instruments `read_path` and asserts the declaration covers what the code really reads. Runtime derivation cannot cover the two `measure` implementors, which have no schedule to inspect. §11. |
| Reads inside `analyse` | **Hoisted into `build_schedule`** — six of them. A read after the acquisition is too late to be a prerequisite. §11. |
| Provenance | **Deferred to RFC 0008**, which carries the design this RFC's review settled: a metadata sidecar the driver owns, beside the device file, keyed by target and dotted path. §10 says what deferring it costs. |
| A skipped node's stale parameter | **Kept and marked**, not cleared. Clearing it would stop a chip that worked yesterday from running today. §11. |
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
Q1ASM instructions, against per-module ceilings of 16,384 for a QCM and **12,288 for a
QRM** — `MAX_NUMBER_OF_INSTRUCTIONS_*`, the same in both schedulers. The QRM's is the
binding one for anything that acquires. A 1-D band sweep fits with room; a 2-D sweep over
the same band — a frequency band against five drive powers — does not.

(An earlier draft quoted an empirical 12,376-works / 13,074-fails bracket from the August
2026 cluster. Those were *acquisitions × an estimated instructions-per-acquisition*, not
counted instructions, and the estimate straddles the QRM's real 12,288 — so the bracket
was measuring the estimate. Use the schedulers' constants and count the compiled program,
which `_log_program` already reports at debug level.)

Where it does not fit, the sweep is **split across several acquisitions** rather than
refused, because refusing puts the operator back to choosing a range by hand and that is
the thing this RFC exists to stop. `measure` already runs a routine's own loop, and the
allowance is already summed across it, so the machinery is in place.

One constraint on the split, worth stating because it is easy to get wrong: **the chunks
must overlap by at least one linewidth.** A line that lands exactly on a boundary is
otherwise half in each chunk and resolved in neither, which is a spurious refusal that
looks like a dead qubit.

Chunking moves the binding limit from the sequencer to wall-clock — a band against five
drive powers at 1024 shots is tens of minutes, fine for a bring-up and not for a drift
check. So each node carries **a cap on total acquisitions, with a default**, and chunks up
to it. That is a knob, and it is deliberately a different kind from the ones this RFC
removes: a resource budget needs no knowledge of the chip, where a range needs to know
roughly where the answer is. `routine_timeout_s` is already exactly this kind of knob, and
nobody has to know a qubit's frequency to set it.

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

0. **`reads`, and skipping on it — done** (August 2026). Declare what each routine
   consumes with a test that derives it, hoist the six `analyse`-time reads, block on an
   unproduced parameter, report the blocker. First because it makes the failures of the
   phases after it legible: a regression in one node shows as one failure and a list of
   skips rather than a graph-wide puzzle. It also stood alone, which is why it went in
   ahead of the rest.

   Landed in three commits: the hoists, the declarations and their derivation test, then
   the ledger the walk blocks on. Two corrections against what §11 predicted — the
   pre-walk config check was withdrawn as unwritable without provenance, and the one read
   the notation cannot express turned out to be `coupler_anticrossing`'s of its *parent
   qubit's* `f01`. Twenty-three of thirty-three routines read a device parameter at all.
1. **The accept side** (§6.2) — **done and merged.** Not as planned: scaling the
   signal-to-noise floor with the point count cannot work at any setting, because ``snr``
   divides a fitted parameter by the residual, and 16% to 55% of pure-noise fits cleared
   the old 3.0 floor with a tail into the thousands. What works is the fitted curve's
   travel over its residual scatter — noise maxes at 4.2 over 1800 trials, a real line
   reaches 93 to 139 — so the guard judges that at a floor of 5.0. `snr` keeps the one
   job it can do, ranking one drive power against another.

   Reproducing a centre across drive powers, the other half of what §6.2 proposed, turned
   out not to apply on the chip that motivated it: only one row there survives the
   linewidth test, so there is nothing to reproduce against.

   Before the derived ranges, not after, and that ordering paid twice. It exposed §13's
   detuning gap, and then two pieces of §5's physics-bounded class that no amount of
   phase-2 work would have reached. Both landed with it rather than waiting for phase 4:

   - **Only a power that shows a line may set the broadening reference.**
     `fit_spectroscopy_power` judged each row against its narrowest, and a row that
     converged, cleared the sweep step and still showed nothing *was* the narrowest — so
     `MAX_BROADENING` rejected every row that did show the line. The eligibility rule is
     `MIN_LINE_REACH`, which moved to `fitting/core.py`.
   - **The sweep that confirms a searched-out line is sized from the width the search
     measured**, not from the operator's span and not from a constant. The line's width is
     set by the drive amplitude that same sweep is choosing, so a fixed window is wrong
     for some power: 20 MHz refused outright, 40 MHz passed at a reach of exactly 5.0
     against a floor of 5.0, 100 to 400 MHz reached 31 to 84. `_search` now reports how
     wide as well as where, counted in bins rather than fitted.

   Together these take `test_calibration_loop.py` from 28 fixture errors to green, with
   f01 recovered 0.42 MHz from the truth at a reach of 27.
2. **`tuners/base/limits.py`** — **done and merged**, and it grew a second job.
   `addressable_band(device, port_clock, if_limit_hz)` from the LO and the backend's IF
   limit, which is 500 MHz and identical in both schedulers. Every frequency sweep and
   the widening search trim to it, because a span symmetric about a configured frequency
   is not symmetric about the LO — and the part outside fails compilation with
   `Attempting to set NCO frequency`, naming neither routine nor setpoint. Two bugs found
   here: two call sites never threaded the backend, and clamping to the edge exactly put a
   setpoint *on* the limit for the NCO's quarter-hertz rounding to push outside.
   `full_scale(element, path)` is still to do, with the amplitude sweeps in phase 3.
3. **The hardware-bounded class — done.** Frequency sweeps trim to the addressable band
   and `qubit_spectroscopy` widens over it. Amplitudes go to full scale, but by
   *escalation* rather than by default: starting there put `amp180` 6.9% out and took
   `rabi_12` off its sqrt(2) ladder, because a strongly driven transmon stops being the
   cosine the fit assumes. So the default measures where the model holds and `fit_rabi`
   asks for more when the pi pulse is above the sweep. Chunking a too-wide grid was not
   needed: nothing derived here exceeded the sequencer.
4. **The physics-bounded class — done, except one.** `readout_operating_point` takes 0.6
   of the measured linewidth, a coefficient two independent chips agree on; the two
   excited-state sweeps take eight of it. `f12_spectroscopy` bounds the anharmonicity it
   searches around *and* the one it fits. `CalibratedTransmon` gained
   `resonator.linewidth`, which RFC 0005 §13 had asked for, because the value was measured
   every run and thrown away.

   `three_state_operating_point` resisted, and the reason is recorded at the code. Sharing
   the two-state coefficient broke `ramsey_12` outright, and matching the old constant
   left it passing by nothing. Placement there wants more *points*, and points are
   capped at five by the sequencer's single-shot registers — so that span waits for the
   register budget, not for a better coefficient.

   `drag` is deliberately untouched: §5 proposed centring it on the measured
   anharmonicity, and nothing measured says the symmetric sweep is wrong. Its failure on
   the August 2026 chip was contrast, not placement.
5. **Escalation — done.** `OutOfRange` carries the axis and the direction; `escalating`
   follows it, bounded at three attempts, and leaves an operator who named the axis alone.
   Two directions turned out to be needed rather than one: a flat *decay* wants a longer
   window, and a flat *oscillation* wants a denser one, and asking for the wrong one makes
   the other worse.
6. **The acceptance test, then the knobs — done.** §8's test calibrates a chip known only
   from its design document, recovering f01 to 0.6 MHz from 214 MHz away with nothing
   supplied, and the loop config's four hints are deleted. Writing it found the last two
   range bugs, which is what it was for.

## 10. What this defers

One thing, and it is provenance. Everything else this RFC once listed here has since been
pulled into scope — the accept side of the guards is §6.2.

**A prior is still indistinguishable from a measurement.** After this RFC the driver finds
the qubit wherever it is, but nothing says whether `clock_freqs.f01` was measured by this
driver or typed in from a design document. The August 2026 chip carried
`f01: 4735509751.238763` — nine significant figures, and the line was never there.

Three things here want that distinction: §2's definition of a prior, §11's "no
trustworthy value", and §11's marking of what a skipped node did not confirm. A fourth is
the pre-walk check §11 withdrew during phase 0, which cannot tell a hand-supplied
parameter from a disabled producer without it.

Each has a weaker version that works without it, which is why this could be deferred at
all: §11's ledger asks "did this walk produce it?" rather than "was this ever measured?".
That is right for a bring-up and blind on a recalibration, which is the cost of the
deferral and the reason it should not be deferred indefinitely.

**RFC 0008 is now implemented, and answered three of the four rather than all four.** The
ledger's *blocking* rule stayed as it is, on purpose: a failure to measure a parameter is
evidence against whatever the file holds for it, whether or not an earlier run measured it
— see RFC 0008 §6. What provenance changed is what the run *says*: the withdrawn check
below is back as a report, a skipped node now says how old what it left standing is, and
each result names the inputs nothing had ever measured.

**RFC 0008** carries the design, which was argued out in review here: where provenance
lives, why neither config file is its home, and why staging value commits does not address
what actually went wrong. It also corrects a claim this section used to make — that the
content already existed and only a lookup was missing. Reports leave the driver on a
result queue and nothing persists them locally, so a sidecar is the driver's only copy,
not an index over one.

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
punch-through, which phase 3 fixes (§12). `time_of_flight` is off too, and under naive
propagation disabling either would skip the entire graph beneath it — which is to say,
everything. That both are off *because* of range bugs this RFC fixes does not help: the
operator must be able to switch a node off without the graph collapsing.

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
  makes the data dependencies explicit and separable from walk order. **Declared, and
  tested by derivation.** A node's read set is static — which paths it needs is fixed at
  authoring time, only the values are dynamic — so it can simply be stated, the way
  `updates` already is. The objection to declaring is that a list can drift from what the
  code really reads, and that is answered by a test rather than by a mechanism:
  `read_path` is the single way a routine touches the device, so instrumenting it during
  a build-and-analyse over the simulated chip derives the true set and asserts the
  declaration covers it.

  Declaring rather than deriving at runtime matters for one concrete reason. The two
  routines that override `measure` — `coupler_anticrossing` and `qubit_spectroscopy` —
  own their whole acquisition loop, so there is no `build_schedule` to inspect before
  deciding whether to run them. A derived-at-runtime set cannot cover those two at all;
  a declared one covers every node uniformly.

  This supersedes the previous resolution, which was derive-at-runtime via
  *build, inspect, decide*. What changed it: a node knows its reads before it runs, so the
  runtime machinery buys nothing a test does not, and it fails exactly where the interface
  is least uniform.
- **Six reads move out of `analyse`.** `ef.py:439`, `ef.py:660`, `single_qubit.py:233`,
  `single_qubit.py:513`, `spectroscopy.py:594` and `spectroscopy.py:1018` read the device
  after their acquisition — `resonator_spectroscopy_excited` reads `clock_freqs.readout`
  there to difference against, `ramsey` reads the `f01` it is about to correct. Each is a
  one-line hoist into `build_schedule`, stored on the instance as the setpoints already
  are, and each removes a read that happens too late to be a prerequisite. Worth doing
  regardless of this section: a value read before the sweep and a value read after it are
  the same number today only because nothing writes in between.
- A node is blocked when a parameter it reads has no trustworthy value — not produced
  in this walk, and no measured prior. A failed *refiner* leaves the value trustworthy,
  so nothing behind it is blocked.
- ~~A disabled node that is the only producer of a parameter something reads is a
  **config error reported before the walk starts**.~~ **Withdrawn on implementation.**
  The check cannot be written without §10's provenance, because it cannot tell "never
  produced by design" from "producer switched off". Two read paths —
  `measure.integration_time` and `r12.ef_duration` — have no producer anywhere in the
  graph and are supplied by hand on every chip, so a sole-producer rule fires on them
  every run; and the August 2026 chip disables `time_of_flight` while its
  `measure.acq_delay` is a perfectly good hand-set 200 ns. Nothing is lost by waiting:
  the parameter view below already declines to block on either case.
  **Reinstated by RFC 0008 as a report, not an error.** Provenance splits the three cases
  the rule could not: a prior a routine in this run will measure, a prior whose producer is
  out of this run, and a path no routine produces anywhere. Only the middle one is
  reported, and it names the routine that would produce it. It reports rather than refuses
  because every chip calibrated before the sidecar existed has measured values and no
  provenance, so refusing would take working chips down for want of a file.
- Blocked nodes are recorded as **skipped, with the blocker named** — not failed.
  Auto-failing would replace six misleading failures with six fabricated ones, and would
  feed the drift check a history of failures that never happened.
- A skipped node's parameter is **kept and marked, not cleared.** Clearing it would mean a
  chip that ran jobs yesterday cannot run today because one node was blocked, which is a
  worse outcome than running on a value this walk did not confirm — provided the report
  says which values were not confirmed. That proviso is §10's provenance field, and RFC
  0008 supplies it: a skipped node's note now names each parameter it left standing and the
  routine and time that last measured it.

`diagnose` already walks `depends_on` to blame the deepest failing ancestor rather than
the symptom (RFC 0005 §8), so the traversal exists and the calibrate path can borrow its
shape.

The *fully* correct version of "no trustworthy value" wants §10's provenance, since a
design value and a measurement are indistinguishable in the device file. A useful version
needs less: on a first calibration, "produced in this walk" is sufficient, and that is
exactly the case this RFC is about. It is also what makes §8's acceptance test readable:
on a chip known only from its design document the first walk will have failures, and
without skip-propagation its report is the same six-way puzzle that motivated this RFC.

### 11.1 `reads` was under-declared, so this did not fire on the B chip

**Fixed in August 2026.** Found on hardware, and it was the failure this section exists to
prevent, recurring for a reason the section did not anticipate.

q5 on the B chip produced eight failures from one fault. `qubit_spectroscopy` found no
line; the qubit was never excited; and then `rabi`, `t1`, `t2_echo`, `rb`,
`readout_discrimination`, `readout_fidelity` and `resonator_spectroscopy_excited` each ran
anyway and fitted its own noise, reporting seven further errors with seven different-looking
causes. The guards did their job — every one of those seven refused rather than writing —
but the walk should not have run them at all.

Why the ledger let them through:

| Node | Declares | Actually needs |
|---|---|---|
| `rabi`, `t1`, `t2_echo`, `rb` | *(nothing)* | `clock_freqs.f01`, and `rxy.amp180` for the last three |
| `readout_discrimination`, `readout_fidelity` | the two `measure_2state` paths | `rxy.amp180` as well |
| `resonator_spectroscopy_excited` | `clock_freqs.readout`, `resonator.linewidth` | `clock_freqs.f01` and `rxy.amp180` as well |

None of them reads those paths through `read_path`. They get the drive frequency and the pi
pulse from the *compiled gate* — `backend.Rxy` and the gate library resolve them off the
device element directly — so `test_a_routine_declares_every_parameter_it_reads`, which
derives the truth by instrumenting `read_path`, structurally cannot see the dependency.
The test asserts a lower bound and the declaration is the contract; here the contract is
simply short, and nothing was in a position to say so.

With honest declarations the same run reports **one** error naming `qubit_spectroscopy` and
seven skips: f01 unsatisfied skips `rabi`, which leaves `rxy.amp180` unsatisfied, which
skips the other six. That is what §11 promised.

**What was built.** An audit of all 34 routines found the gap wider than the seven: **22**
qubit-targeted nodes were short, not seven. Every one that plays a gate now declares
`clock_freqs.f01`, and every one that plays a gate without supplying its own amplitude also
declares `rxy.amp180`. `rabi` is the exception that proves the rule — it produces
`rxy.amp180`, so declaring that it reads it would make it block itself on a first bring-up.

**Deriving the true set is not possible, so the rule is asserted instead.** Instrumenting
`read_path` is the wrong probe for a dependency that never passes through it; the obvious
alternative — instrument the element and compile — does not work either, because quantify
compiles by way of `generate_device_config`, which serialises *every* parameter, so an
instrumented element reports all of them and distinguishes nothing.
`test_a_routine_playing_a_gate_declares_the_gate_parameters` therefore asserts the
structural fact: play a gate, declare the two paths. Coarse, and it is exactly the class
that bit us. `test_a_failed_qubit_spectroscopy_blocks_everything_that_needs_a_gate` pins the
graph-level consequence by walking the real routine set with `qubit_spectroscopy` failing.

**Edges needed a ledger change, not a declaration.** A gate on an edge is played on its
endpoint *qubits*, and the ledger keyed on `(target, path)` — `("q5_q10", "rxy.amp180")` is
a path no routine writes and no element has, so declaring it on `cz_chevron` would have been
true and inert. `blockers` now asks about an edge's endpoints as well as the edge itself,
splitting the name the way `CalibrationConfig.validate_targets` already does; that
convention was load-bearing before it got here, and `validate_targets`' own docstring states
the dependency ("a two-qubit gate is measured *through* its qubits") that the ledger could
not express. Both spellings are tried per path rather than classifying paths by where they
live, so an irrelevant spelling is silently absent rather than wrong.

So a failed `rabi` on either end of an edge now skips the edge, which is what
`validate_targets` refuses the *configuration* for and the walk previously allowed anyway.

### 11.2 The root of the graph could not re-centre its own sweep

**Fixed in August 2026,** and it is the refusal that opened the investigation §11.1 came
out of: `resonator_spectroscopy` on a B-chip qubit reported a centre 2.8 MHz below the
20 MHz window it had swept, and stopped. It writes the frequency every other node reads,
so a refusal there stops the chip rather than one routine — and a resonator a few MHz
outside its window is the commonest bring-up state there is, since fabrication scatter
alone moves one by tens of MHz.

It was the only node in §5's escalation-bounded class with no escalation, and pointing the
existing machinery at it would not have worked. Three separate reasons, all in `_widened`:

- It anchors a widened axis at `low` and extends **upward**, which is right for a delay
  and backwards for a resonator that sits below its window.
- It holds the point count, so a wider frequency span steps over the line it was widened
  to find — and `require_resolved_line` then refuses it for being thinner than the grid.
- It writes the result as explicit setpoints, and `_frequency_sweep` passes explicit
  ``frequencies`` through **unclamped**, so the next attempt asks the NCO for a frequency
  outside its ±500 MHz reach.

So escalation widens **`span`**, a scalar, and leaves centring, resolution and the band
clamp where they already live. `points` moves with it to hold the step size, bounded by
`MAX_SWEEP_POINTS` because the sequencer's ceiling is real — and that bound was set wrong
the first time. A frequency sweep costs three operations per point (`Reset`,
`SetClockFrequency`, `Measure`), which a 846-acquisition `resonator_punchout` on the B chip
measured at **15.0 Q1ASM instructions each** against the QRM's 12288: the "roughly 950
acquisitions" rule of thumb assumed one group per point and is 30% optimistic for exactly
the routines that escalate. quantify *warns* rather than raising when a program is too long
and the sequencer may still run it, which is the trap — a truncated sweep loses its last
setpoints, meaning the far end of the range escalation had just widened to reach.

**Both of the fit's refusals escalate, in opposite directions.** `require_in_range` — the
centre outside the window — asks for a wider span, by a factor derived from the excursion
and then doubled, since an extrapolated centre says which side the line is on and not how
far. `require_resolved_line` splits: a flat window asks for more spectrum, and a line
thinner than the grid asks for more grid over the same span. Getting that second one
backwards would make the failure worse, which is why the direction travels with the
refusal rather than being inferred from the axis.

Discovered while testing, and worth recording: for a line *far* outside a narrow window
there is no signal at all, so the flat-window guard fires before the range guard ever sees
the fit. Escalating only `require_in_range` would have covered a band of cases a few
linewidths wide and missed the one that motivated it.

An operator who names `span` is still left alone, per §7 — including on the B chip, whose
`calibration.yml` sets 4 MHz.

### 11.3 A broad spectroscopy line is not a wrong one — closed, having been wrong twice

**Closed. No guard was added, and the section is kept because it took two wrong attempts to
establish that none is wanted.**

The starting observation looked damning. `qubit_spectroscopy` on the B chip reported success
on a 48.0 MHz line in a 54.0 MHz window — 89% of it — at Q = 111, three orders below any
transmon, and the `rabi` that read the frequency could not find a pi pulse. It looked like a
fit of noise dressed as a measurement.

**First attempt: bound the linewidth against the span**, on the reasoning that a Lorentzian
filling its window has no baseline to be determined against. It refuses the B-chip fit and
accepts the resonator's 8%. It also refuses the *simulated* chip at 99%, and capping the
confirming span to make room broke §8's acceptance test — because that chip's line, measured
per drive power in the acceptance test's own configuration, reaches 124 with a centre 1.35 MHz
from truth while filling a 153 MHz window. So a line filling its own window can be a perfectly
good measurement, and `CONFIRM_SPAN_IN_WIDTHS` was right all along.

**Second attempt: test for power dependence**, since power broadening is by definition a
dependence on power, and the sweep already fits every row. That refuses a line whose width
grows with the drive that shows it, needs no threshold in Hz, and is portable between chips.

**It refuses the physics simulator's own textbook behaviour**, which is what settled the
question. That chip's linewidths are 1.51, 3.06, 6.12 and 12.37 MHz at drives of 0.005, 0.01,
0.02 and 0.04 — *proportional* to the amplitude, exactly as a driven two-level system
broadens, and `test_qubit_spectroscopy_finds_the_transmons_real_f01` passes throughout.

Which is the physics both attempts had wrong. **Power broadening is symmetric: it widens a
line without moving its centre.** The effect that pulls a centre is the AC Stark shift, which
is a different mechanism and does not follow from width. So a broad line is a *less precise*
measurement of f01, not a wrong one, and there is nothing here for a guard to refuse — the
existing floors already reject a line that is not there at all, which is the failure that
matters.

**What this means for the B chip's failure.** The 48 MHz line was power broadening at a drive
of 0.3, and its centre was not thereby wrong. `rabi` failed for a reason found separately and
since fixed: escalation widened its amplitude sweep past full scale and the compiler refused a
gain of 1.05 (§11.2's clamp). The two were unrelated, and reading the wide line as the cause
was a wrong inference from a coincidence of timing.

The one thing worth keeping from the investigation: `SimulatedTuner` and
`QuantifyTuner(is_simulated=True)` report 1.24 MHz and 99 MHz for the same measurement at the
same drive. The second is a fit pinned near its window rather than a linewidth, which is worth
knowing when reading either.

### 11.4 A routine may carry its own timeout

**Implemented in August 2026.** A single `routine_timeout_s` has to be set for the slowest
node, which makes it no ceiling at all for the fast ones. On the B chip `qubit_spectroscopy`
legitimately ran **299 s of a 300 s budget** — it pays for a search across everything the
drive port reaches and then a confirming sweep at each drive power — while a `rabi` taking
more than a few seconds is hung. Raising the global number to let spectroscopy average more
shots also lets every other node sit for five minutes.

So `RoutineConfig` gains `timeout_s`, and `CalibrationConfig.timeout_for` resolves it against
the walk's. A field rather than one of the sweep parameters, so a typo is a startup error
instead of a silently ignored key, and so nothing that widens an axis can mistake it for one.
Read through `timeout_for` at every point that enforces a ceiling — the acquisition, the check
schedules, and the over-budget refusal — so all three agree on which number applied, and the
refusal now names the routine and says which setting to raise.

It is a resource budget, which RFC 0007 §5 already distinguishes from the ranges this RFC
removes: it needs no knowledge of the chip, only of how long the operator is willing to wait.

### 11.5 The AllXY equator error: detuning fixed, the pi/2 amplitude now confirmed

**Half fixed.** Found on the B chip's first fully calibrated run, which is worth stating
because the chip was *working*: randomised benchmarking measured 0.9879 over seven depths, T1
63.6 us, T2echo 87.3 us, and an AllXY whose two plateaus read 0.0086 and 0.0090 rms against
their ideals. All of the error sat in the equator block, antisymmetrically:

```
-0.103 -0.126 -0.090 -0.077 -0.018 +0.013 -0.020 -0.039 +0.091 +0.109 +0.092 +0.064
```

The two pairs that are a single ``X90``/``Y90`` then an identity — which should land exactly on
the equator — read 0.397 and 0.374. That shape is read as *either* a residual detuning or a
pi/2 amplitude error, and one rms deviation cannot separate them.

**Fixed: `ramsey` now refines until the residual is unresolvable.** One pass could never land
on the answer, and the reason is in its own `analyse`: the correction is
``current_f01 - detuning``, and the detuning was measured with the *old* f01 in the drive. A
megahertz of error means the fringe was fitted a megahertz off resonance, so the correction
lands near the answer rather than on it — the B chip moved f01 by 1.032 MHz in a single pass
and had no way to ask what remained. Each pass now starts from where the last left the device,
so the residual falls geometrically.

Bounded three ways, and the first is the interesting one. It stops when the detuning is under
what the sweep could tell from zero — ``1/(2*pi*window)``, derived from the operator's own
delays rather than set as a constant, which is 6.6 kHz for the 24 us default and 27 kHz for a
6 us one. It stops if the residual stops falling, keeping the better of the two passes, since
another would be measuring noise. And it stops after `MAX_REFINEMENTS` regardless. Each pass
is a full `escalating` call, so a window too short for the chip is still widened by the guard
that already knows how.

**Blocked: nothing can correct a pi/2 amplitude error, and it is not this graph's fault.**
`fine_amplitude` refines ``rxy.amp180`` by repeating pi pulses; there is no equivalent for
pi/2 and no field to write one to. quantify's `rxy_drag_pulse` derives every angle from
``amp180`` by linear interpolation — its own docstring says so — so a separately calibrated
pi/2 amplitude has nowhere to live and nothing that would honour it. Correcting this needs a
custom pulse factory and a new element field on `CalibratedTransmon`, which changes how every
gate on every chip compiles.

That was not worth building before the detuning half was ruled out, which the refinement above
does automatically: if the equator block collapsed on the next run, this was detuning and there
was nothing further to do.

**It did not collapse, and that settles it.** The refinement drove the residual detuning from
1032421.7 Hz to -384.2 Hz — a factor of 2687, and three orders of magnitude below the 6.6 kHz
the window can resolve, so what is left is not detuning by any reading. The equator block moved
by 5%:

```
before   pairs 6-9  -0.0991   pairs 14-17  +0.0890   split  +0.1881
after    pairs 6-9  -0.0706   pairs 14-17  +0.1089   split  +0.1795
```

An antisymmetric split that survives the detuning going to zero is a pi/2 amplitude error, and
the mechanism is the one already suspected: ``amp180`` of 0.5757 sits above half of full scale,
where the rotation angle stops being linear in amplitude, so halving it does not halve the
rotation — which is exactly what quantify's interpolation assumes. The upstream work above is
therefore justified rather than speculative, and is the remaining half of this item.

**Deliberately not recommended: changing ``rxy.duration``.** A longer pulse needs less
amplitude and would move ``amp180`` out of the nonlinear region, but 56 ns is already 14 times
the 4 ns the measured 250.6 MHz anharmonicity sets as a leakage floor, and doubling it doubles
the decoherence-limited error per gate from 0.088% to 0.176% against an RB-measured 1.21%.
Trading a known cost for an unverified mechanism is the wrong way round, and the duration is a
chip-level choice rather than something this graph should move.

## 12. Resolved during review

No open questions remain. Recorded because the reasoning is worth keeping, and because
several of these changed the shape of the RFC rather than just settling a detail.

| Question | Resolution |
|---|---|
| Chunk a too-wide derived sweep, or refuse it? | **Chunk**, §5. Refusing hands range-picking back to the operator, which is the thing being removed. Chunks overlap by a linewidth so a line on a boundary is not lost in both. |
| How wide is a chunked sweep allowed to get? | **A per-node cap on acquisitions, with a default** (§5). A resource budget is not the kind of knob this RFC removes: it needs no knowledge of the chip, which is exactly what distinguishes it from a range. `routine_timeout_s` is already this. |
| Harden the *accept* side here, or in a sequel? | **Here**, §6.2. It is not a parallel concern: escalation only fires on a refusal, so a guard that accepts noise bypasses the entire RFC. It is the trigger condition. |
| `reads` declared or derived? | **Declared, with a test that derives** — reversing an earlier resolution in this table. A node's reads are static, so runtime derivation buys nothing a test does not, and it cannot cover the two `measure` implementors at all. §11. |
| Hoist the `analyse`-time reads? | **Yes, six of them** (§11). A read after the acquisition cannot be a prerequisite, and it is a one-line move per routine. |
| Does a skipped node keep its stale parameter? | **Keep and mark**, §11. Clearing it stops a chip that ran yesterday from running today. |
| Put provenance in `calibration.yml` rather than the device file? | **Neither — a sidecar the driver owns**, now RFC 0008 §5. And the blanket "no second store" from the round before was too blunt: it is sound against a second store of *values*, not against metadata that never holds a number anything needs to run a circuit. |
| Report a disabled sole producer before the walk? | **Withdrawn during phase 0** (§11). Undecidable without §10's provenance: two read paths have no producer anywhere and are hand-supplied on every chip, so the rule fires on them every run. |
| Where does the IF limit live? | **On `SchedulerBackend`, like `drag_span`** — but checked rather than assumed, and the two schedulers *agree*: `NCO_FREQ_LIMIT_STEPS / NCO_FREQ_STEPS_PER_HZ` is 500 MHz in quantify-scheduler 0.28 and qblox-scheduler 1.0.0b4 alike. That weakens the case for a property without removing it: the fact belongs to the backend either way, and no divergence is being modelled speculatively. |
| What does "high fidelity" mean in the acceptance test? | **Assert against the error the simulator was given**, not a constant — the last open question, settled in `TestFidelityAgainstWhatTheSimulatorInjected`. A constant is unfalsifiable low and simulator-tuning-dependent high. Two claims replace it: `rb` recovers the injected error, and a worse chip benchmarks worse. Both hold at any injected level. The expected number is derived, not written down: the simulator depolarises per primitive rotation, so a Clifford of n of them costs `0.5*(1-(1-p)**n)` with n read from `clifford_to_gates` — which also caught that the naive `p/2` was 3x low, since a Clifford averages 3.08 primitives. |
| Escalation in the DAG or in `measure`? | **In `measure`**, with the attempt count reported so the DAG and the report still see it. |
| Does `resonator_punchout` come back? | **Yes, and now actually.** This row first claimed phase 3 had fixed the amplitude grid; it had not — phase 3 raised the ceilings in `single_qubit.py` and `ef.py` and never touched `spectroscopy.py`, where `full_scale` did not appear at all. Found in August 2026 while debugging q5 on the B chip, and the leftover mattered there: carrying `output_att: 20` on its readout, a grid stopping at 0.5 is some 26 dB short of what the module can emit, so punch-through was unreachable rather than merely hard to reach. The grid now runs to `full_scale`, with no accuracy bound pulling the other way — a resonator driven hard does not stop being a resonator — so unlike `rabi` it needs no escalation. |
| Stage writes in a separate store until the run succeeds? | **No**, now RFC 0008 §7 — and the diagnosis matters more than the answer. The August 2026 corruption was not an early commit; `rabi` reported *success* while writing 0.0158, so a staging store would have committed it too. The finer boundary is per-parameter commit gated on provenance. |
| Treat operator-supplied ranges as suggestions with a derived fallback? | **Yes**, §7 — and it collapsed a distinction the draft was carrying for nothing: a supplied window is just escalation's first attempt. |
| Rewrite `calibration.yml` when a hint proves wrong? | **No**, §7. It is hand-authored reasoning, and `spec.amplitude`'s latch already showed what remembering a search hint costs. Report the range that worked and let the operator decide. |
## 13. The gate paths ignored the drive detuning — done, and narrower than stated

Found while building phase 1; **fixed** in August 2026, and the fix corrected this
section twice. Both corrections are worth keeping, because one of them removed a
blocker this RFC had invented.

**What was true.** `TransmonSimulator._anharmonic_hamiltonian(detuning_ghz=0.0)` is the
drive-frame Hamiltonian and already carried a ``delta * number`` term, but only
spectroscopy passed a detuning; `rabi`, `t1`, `t2_echo` and `ramsey` took the default. So
a drive far off resonance rotated the simulated qubit exactly as well as one on it. Each
now takes the detuning, and `SimulatedBackend` reads ``configured f01 - true f01`` off
the device it was handed, per run: a walk that corrects f01 at spectroscopy has to get
gates that then work. Measured on the integrator, rabi's peak-to-peak by detuning:
0.996 on resonance, 0.446 at 50 MHz, 0.038 at 302 MHz, which is
``Omega^2/(Omega^2 + delta^2)`` against a Rabi rate of about pi/20ns.

**Correction 1: this was never true of `test_calibration_loop.py`.** That suite runs the
tuners over `SimulatedCoordinator`, which reads clock frequencies off the compiled
schedule's clock resources and has always tracked per-qubit detunings — and it already
carried the negative test, `test_an_uncalibrated_chip_gets_the_answer_wrong`, asserting
that an X gate 214 MHz off leaves the qubit in ``|0>``. The blind spot was only ever in
`tests/utils/simulation.py`'s schedule-reading shortcut, which `test_calibration_e2e.py`
and the tier-3 tests use. Claiming "the suites" when one of the two was already honest
overstated it.

**Correction 2, and this is the one that mattered.** On the strength of correction 1, the
loop fixture's ``f01: 5e9`` is *load-bearing and correct* — spectroscopy's 600 MHz span
genuinely finds 5.2142 GHz, and the fixture's LO puts that line at 224 MHz of IF, well
inside the 500 MHz limit. So the ``5.040000e+08`` setpoint that suite rejects on
`wip/rfc0007-accept-side-and-band` is **not** a fixture question and never had three
possible fixes: it is the widening search overshooting a clamp that is not being applied.
Changing the fixture or widening the span would have restored the blind spot for nothing,
which is exactly the trap this section warned about — while pointing at the wrong door.

**What this leaves.** Phase 3's acceptance test can now be written honestly: a wrong f01
fails on its own through both simulator paths. And phase 2's remaining work is a plain
bug in `addressable_band`'s application, with a suite that can judge it.
