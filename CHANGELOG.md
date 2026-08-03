# Change log

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/)
and this project follows versions of format `{year}.{month}.{patch_number}`.

## [Unreleased]

## [0.3.0] - 2026-08-01

### Added

**A third operation, `calibrate`** (RFC 0004). A tuner runs calibration
experiments against a transmon chip, fits the results, and writes the fitted
parameters back to the same `quantify.device.yml` the `process` driver reads for
every job. It registers as its own driver, beside the QPU rather than inside it.

```bash
qpi-driver start --operation calibrate --device quantify_tuner \
    -o calibration_config=./calibration.yml \
    -o quantify_device_config=./quantify.device.yml
```

- Two devices: `quantify_tuner` (quantify-scheduler) and `qblox_tuner`
  (qblox-scheduler), installed by the `[quantify_tuner]` and `[qblox_tuner]`
  extras. Both run the same routines — the two schedulers share a gate
  vocabulary, so the experiments are written once and a backend supplies only the
  schedule class and a way to run one.
- The routine graph runs from resonator spectroscopy through Rabi, Ramsey, T1,
  T2 echo, DRAG, AllXY and fine amplitude to randomized benchmarking, plus flux
  spectroscopy, the CZ chevron, conditional phase and interleaved RB for
  flux-tunable couplers. Ordering comes entirely from each routine's declared
  dependencies.
- `calibration.yml` says which routines run and over what. A routine the file
  does not mention **runs**: defaulting to disabled would make an empty file mean
  "calibrate nothing", and a run that does nothing raises nothing, so it would
  report success. A routine name matching no routine is a startup error, so a
  typo cannot silently disable an experiment.
- **Drift monitoring.** With `-o drift_check_interval=1800` a tuner benchmarks on
  its own schedule and queues a partial recalibration of the qubits whose
  fidelity has fallen below `fidelity_threshold` — or `fidelity_2q_threshold`,
  for an edge, since a CZ an order of magnitude worse than a single-qubit gate is
  normal rather than drift. A partial run narrows to those qubits and the edges
  touching them, and to the routines from qubit spectroscopy down — finding the
  resonator again is a bring-up step, not a drift one, and it is most of what
  makes a full calibration long.
- `POST /api/op/calibrate/dispatch` (admin-only) queues a calibration, and the
  new `calibration_requests` collection is the queue the driver's dispatcher
  polls. Queueing rather than sending is what makes a request survive a restart,
  requeue on a send error, and run when an offline driver reconnects. A tuner
  runs one calibration at a time.
- `calibration_results` stores each report: fitted parameters per routine and
  target, benchmark fidelities, and what failed. It is its own collection rather
  than part of the `events` log, so `eventsRetention` does not prune it.
- **A Calibration tab** in the dashboard, admin-only: measured fidelity per
  target against the threshold that governs it, the current parameters grouped by
  qubit, run history, and a trigger form defaulting to the cheap drift check
  rather than the multi-hour full run.
- `calibrate` is in the Go and TypeScript SDK operation enums too, with their
  event types, though only the Python SDK ships a tuner (RFC 0003 §8).

**Write-back safety** (RFC 0004 §10). A tuner writes the file every subsequent
job reads, so: nothing is written unless a routine produced a result; a failed
run writes nothing at all; the candidate file is verified to read back as written
before anything is replaced; and the previous file is kept as
`quantify.device.yml.prev` so a calibration that makes things worse can be undone
without a second run. Fitted values outside the sweep that produced them are
treated as failed fits, not as new parameters.

**A physics simulator for the calibration tests** (RFC 0004 §7, tier 3). The
routines are now checked against acquisition data generated from a real transmon
Hamiltonian (`scqubits`) and the Lindblad master equation (`qutip`), rather than
from the analytic form each fit already assumes. `make test-py-sim` runs it; the
dependencies live in the `sim` dependency group and the tests skip without them.

This catches a class of error the other tiers cannot. Fitting a *Gaussian* decay
to exponential relaxation passes tier 1 — which generates its data from the same
function it fits — and fails tier 3, because the master equation's relaxation is
genuinely exponential. Randomized benchmarking is exercised end to end against
real unitaries: sequences composed from this package's own Clifford
decomposition, closed with the computed recovery gate, and depolarised by a
known amount, with a control test proving the decay disappears when the recovery
gate is removed.

The same simulator runs a whole calibration end to end through the calibrate
worker's own entry point: a full run, a partial one, a drift check and the
recalibration it queues, and the write-back to the device file. That covers the
joins between the pieces rather than the pieces, and it is what caught a
benchmark fit that reported the same fidelity for every chip better than about 3%
error per Clifford — which, being below the default drift threshold, would have
had a healthy chip recalibrating on every check.

**Two-qubit gates in the simulator.** A CZ needs a joint state, so
`simulation/coupled.py` holds both transmons in one 9-dimensional register and
integrates the coupler rather than asserting a gate. Both mechanisms the hardware
has: DC flux on a qubit's port, and a parametric drive on a coupler, where the
drive *frequency* is the resonance condition. Its exchange rate comes from the
four calibrated edges of a real chip rather than being invented; the remaining constants
are chosen and say so, in the module and in RFC 0004 §7.

- `-o is_simulated=true` works on **both** schedulers now. qblox's
  `HardwareAgent` compiles offline, so a real agent keeps compilation and only
  execution is simulated — a schedule that would not assemble for a cluster still
  does not assemble.
- A coupler edge carries its DC parking current (`bias.parking_current`,
  validated to ±3.1 mA) and how it is delivered (`bias.source`: `spi` or `qcm`),
  set at startup over qcodes since no schedule can express it. New option:
  `-o spi_rack_address`. Switching mechanism is a config change.
- A coupler edge can declare the transition its CZ drive bridges
  (`clock_freqs.sideband_gap`), used in place of the simulator's own constant.
- `is_simulated` is offered by the driver catalog, so the dashboard's
  registration form can select it.
- `test_calibration_loop` runs under both schedulers rather than one, and
  `make test-py-loop` installs both.

**A readout resonator, and real pulse envelopes.** The simulator's readout was two
fixed points in the IQ plane, which is enough to tell `|0⟩` from `|1⟩` and nothing
at all for the two routines whose subject *is* the readout chain. It now has a
resonance with a linewidth to find and a power at which it moves, so
`resonator_spectroscopy` and `resonator_punchout` measure something — and reading
out away from that resonance costs contrast, which is what makes the readout
frequency matter to every routine after it.

Drive pulses are played as the envelope the schedule specifies rather than averaged
to a constant amplitude. That was what made the DRAG parameter a no-op: the
correction is the *derivative* of the envelope, which averages to exactly zero. The
leakage it cancels comes from the same three-level ladder that produces it, so
`drag` finds its optimum rather than being handed one.

Together these close the last gaps in running the whole calibration graph against
the simulator: `test_calibration_loop` now drives all of RFC 0004's sixteen over two
qubits and the edge between them, through the shipped tuner and a real device file,
and requires the report to come back `success` having skipped none of them.

**The calibration graph is finished** (RFC 0005). RFC 0004 built the machinery and
sixteen routines. The graph is thirty-three nodes now, and what was added is what the
device file could not do without: `measure.acq_rotation` and `measure.acq_threshold`
decide the bit of every `meas_level=2` shot — the default job path — and nothing
produced them. `clock_freqs.f12`, `measure.acq_delay` and the coupler's parking
current were in the same position. A parameter no routine writes is a parameter
somebody typed.

Three things came with the nodes. **Readout calibration no longer sits above the
qubit chain**: measuring the resonance with the qubit in `|1>` needs a π pulse, so the
readout chain *straddles* the qubit chain, which is a shape RFC 0004's graph could not
express and is why the missing nodes could not simply be appended. **A node may carry
a check** alongside the sweep that derives it, so a partial recalibration measures
staleness rather than assuming it. And **what the graph measures has somewhere to
go** — `CalibratedTransmon`, opt-in per element, with the nodes that need it declining
on a chip that keeps `BasicTransmonElement` rather than failing.

The whole graph runs against the simulated chip in `make test-py-loop`, and the two
things that stopped it running against a real one are fixed. It has still never been
run on physical hardware.

**`CalibratedTransmon`, an element with room for what the graph measures** (RFC 0005
§13). `BasicTransmonElement` has `clock_freqs`, `measure`, `ports`,
`pulse_compensation`, `reset` and `rxy`, and no home for a spectroscopy drive amplitude.
A routine with nowhere to write its result cannot exist, so the value was typed in by
hand instead. There is now one such element per scheduler — qcodes submodule for
quantify, pydantic for qblox — selected by `element_type.path` exactly as
`FluxTunableCoupler` already is for edges, carrying `spec.amplitude` and
`spec.amplitude_12`.

Opt-in, per element. A config that keeps `BasicTransmonElement` still calibrates:
`spectroscopy_amplitude_path` returns `None`, and the routine measures the best power,
uses it, and does not persist it. Phase 5's EF and three-state parameters
(`r12.ef_amp180`, `clock_freqs.readout_2`, a `measure_3state` submodule) have a home
here too, which is what unblocks them.

The drive ports in the hardware fixture pin their LO and let the intermediate
frequency float, for the reason the readout ports already did: two clocks on one output
cannot each derive the LO from their own configured frequency. Choosing the value is
tighter than it looks — the NCO's ±500 MHz window has to hold *both* the `.01`
spectroscopy sweep and the `.12` one, and picking each LO from the fixture's declared
`f01` put q1's sweep 5 MHz over the edge. 4.99 GHz leaves both inside with margin.

**Calibration checks, so staleness is measured rather than assumed** (RFC 0005 §8).
A routine may now supply a cheap *check* — "does this parameter still hold?" —
alongside the sweep that derives it, and `recalibrate` uses
`CalibrationDAG.diagnose` to decide what to re-run: a node whose check passes is left
alone, a node whose check fails is recalibrated, and if one of *its* dependencies
also fails the blame moves up, because recalibrating a node whose input is wrong
measures the wrong thing twice. Following Kelly et al.
([arXiv:1803.03226](https://arxiv.org/abs/1803.03226)).

Two checks to begin with. `resonator_spectroscopy` probes three points across the
line and reports how far the configured frequency sits from the peak — which makes a
readout drift **visible** for the first time, where before it had no symptom beyond
every downstream fit quietly getting worse. `rabi` amplifies any error in the stored
`amp180` over five π pulses, because one π pulse is second order in its own error and
so cannot be told apart from a readout whose gain moved.

A routine with no check is *unknown*, not stale, and cannot be blamed — otherwise
every diagnosis would walk to the graph's root and a partial recalibration would cost
more than a full one. With no checks at all, blame stops at the seed, which is what
RFC 0004's hardcoded `RECALIBRATION_ROOTS` did, so nothing changes until a check
exists; the constant survives as `RECALIBRATION_SEEDS` for exactly that case. A run
where every check passes now recalibrates nothing and says so, which was not
previously reachable.

**Two more nodes, measuring what was hand-set** (RFC 0005 §7).

`time_of_flight` writes `measure.acq_delay` — how long a readout signal takes to come
back through the cables. A working chip's config carries 200 ns there with nothing having
measured it. The routine opens the acquisition window *with* the readout pulse so the
dead time lands inside a raw trace, which is the whole trick: leaving the configured
delay in place would hide exactly the quantity being measured.

`resonator_relaxation` reports the resonator linewidth, which nothing else measures —
`resonator_spectroscopy` fits one from its Lorentzian and discards it. It deliberately
does **not** write `measure.integration_time`: the ring-up is a floor on that, not an
optimum, and the optimum trades signal-to-noise against relaxation during the window.
Three time constants would have cut the fixture's 1 µs window to 240 ns on a
criterion that never mentions noise.

Both read one trace and share `fit_readout_timing`, because the arrival time and the
fill time constant cannot be measured apart: a level crossing finds the arrival biased
late by a quarter of the ring-up — 20 ns here — and fitting the ring-up needs to know
where it began. One straight line through `ln(1 - rise)` gives both, consistent by
construction, recovering 148 ns and 2.02 MHz against a true 148 ns and 2.00 MHz.

**Spectroscopy no longer guesses how hard to drive** (RFC 0005 §7). `qubit_spectroscopy`
drove at a fixed 1% of full scale, which on the simulated chip moves the population half
a per cent — a signal-to-noise of about five, at which the same sweep returned centres
14 MHz low, 12 MHz high and 62 MHz low on nothing but noise. The full-DAG test was
passing on that margin with a hand-set 3% in its config.

It now sweeps drive power alongside frequency and reports both, the way
`resonator_punchout` reports the power it chose along with the frequency it found there.
The chosen row wins on contrast over residual scatter, not on peak height: height climbs
with power straight through saturation, so the tallest peak is reliably the most
broadened one. Rows more than twice as broad as the narrowest are dropped as
power-broadened, and rows fitting a line narrower than the sweep's own step are dropped
first — a row with no visible line still fits, narrowly and tidily, to the noise between
two setpoints, and left in it becomes the reference every real row is then rejected
against.

This could not be a node of its own. Choosing a spectroscopy power means comparing how
clearly each power shows the line, so it needs a line — and that is what
`qubit_spectroscopy` produces. Before it, the choice is a guess; after it, the guess is
already written to `clock_freqs.f01`.

**`resonator_spectroscopy_excited`, which measures the dispersive shift** (RFC 0005
§7). The readout works because the two qubit states pull the resonator to different
frequencies, and nothing measured by how much. This prepares `|1>` and sweeps the
readout clock, reporting that resonance and the shift between it and the ground-state
one. A characterisation: it writes nothing, like `resonator_relaxation`. The frequency
that best separates the two states is deliberately not derived from it — that depends
on how the two Lorentzians overlap, not on where their centres are.

The simulated readout response now **scales with the power it is driven with**, which
it did not. A reflected field is proportional to the drive that produced it, and
without that half the model made readout power purely harmful: the only thing left for
it to do was collapse the dispersive pull, so the best power was always the lowest one
and there was no operating point to find. `readout_gain` is now per unit amplitude
(14.4 x the fixture's 0.25 is the 3.6 it was), so every existing cloud sits exactly
where it did.

Two readout nodes were written against that model and **backed out**, and the reason is
worth recording. Optimising the readout operating point for *discrimination* moves it
away from the point that maximises *magnitude* contrast, and every calibration routine
reads magnitude. Measured on the simulated chip: complex separation rose 2.6% while
magnitude contrast fell 14%, and the CZ chevron's answer moved from 110 ns to 100 ns,
past its own tolerance. Doing it properly needs a discriminated-readout operating point
separate from the calibration one, with the executor overriding the readout clock for
`meas_level=2` — a change to every discriminated job rather than a routine to add. RFC
0005 §12 carries the numbers.

**Dispersive readout, and the routine that calibrates its discriminator**
(RFC 0005 §7, §9). Each qubit level pulls the resonator to its own frequency, so at one
drive frequency the levels return different *complex* responses — differing in phase as
much as in magnitude. The two IQ clouds are now derived from that response and an
amplifier chain, rather than being two constants the coordinator carried: `GROUND_IQ`
and `EXCITED_IQ` are gone.

That has a consequence worth stating plainly. `measure.acq_rotation` and
`measure.acq_threshold` decide the bit of **every `meas_level=2` shot** — the default
job path — and they default to zero, which is right only for a chain that happens to
put the clouds either side of the imaginary axis. On the simulated chip both land with
positive real parts, so at the defaults every shot reads `|1⟩`. That is the honest state
of an uncalibrated readout, and a model with hand-placed clouds could not express it
because the placement *was* the answer.

`readout_discrimination` measures it: prepare `|0⟩` and `|1⟩` single-shot, take the
rotation as the direction between the cloud centres — which is what makes one real
threshold sufficient — and the threshold as the spread-weighted midpoint, the
maximum-likelihood boundary for two Gaussians of unequal width. It reports the
assignment fidelity from the same shots (0.994 here) rather than leaving that to a
separate node, since splitting them would measure the same two clouds twice. It depends
on `rabi`, because preparing `|1⟩` needs a calibrated π pulse — the readout chain
straddles the qubit chain rather than preceding it.

**Every qubit is discriminated against its own line** (RFC 0005 §7). Both executors
read `acq_rotation` and `acq_threshold` from whichever device element had them first
and applied that one pair to every qubit in the circuit. It was invisible for as long
as nothing measured them — an uncalibrated chip carries zero everywhere, and one
qubit's zero is as good as another's — and it stopped being invisible the moment
`readout_discrimination` started writing real ones. Both the instrument path and the
software fallback now resolve the pair per qubit, through a shared
`executors/utils/discriminator.py`, and `ThresholdedAcquisition` is chosen only when
*every* qubit has a line to threshold against, since a schedule mixing the two
protocols returns one qubit's bits beside another's raw IQ.

Measured end to end: with the collapse in place, `x q[0]` on a calibrated pair reads
q1 — untouched, in its ground state — as `1` on all 400 shots.

The software fallback also rotated the wrong way. `fit_readout_discrimination` defines
the rotation as the direction between the cloud centres and turns the plane *back* by
it so the separation lands on the real axis; the simulated instrument does the same;
the fallback turned the other way. Turning the other way is the same rotation only
when it is zero, which is why an uncalibrated chip could never show it.

**The simulated chip's qubits have different cables.** `readout_phase_deg` was one
number for the whole device, and with the same gain, linewidth and pull, each qubit
read on its own resonance, all three came out with the same rotation and threshold to
four decimal places. That is one qubit copied three times, and it made the defect above
untestable — a first attempt at a test for it passed with the bug reinstated, because
it could only assert that two identical numbers differed in the last digits of shot
noise. `readout_phases_deg` overrides per qubit, in the pattern
`resonator_frequencies_ghz` already set.

**Discriminated readout has its own operating point** (RFC 0005 §7). The readout that
best separates `|0>` from `|1>` is not the one that returns the most signal, and the
graph now says so with two points instead of one compromise.

`resonator_spectroscopy` and `resonator_punchout` keep `clock_freqs.readout` and
`measure.pulse_amp` — where the most signal comes back, which is what every routine
that reduces an acquisition to a magnitude needs, and that is nearly all of them. A
discriminator uses the complex separation between the clouds, most of which is phase
once the drive is off resonance, so its best point is elsewhere: 2.6% more separation
for 14% less magnitude contrast, measured. Sharing one point moved the CZ chevron's
answer by 10 ns, which is why the two nodes that tried it were backed out last time.

`readout_operating_point` sweeps frequency and amplitude *together* and writes
`measure_2state`, a new submodule on `CalibratedTransmon`. One node over both axes
rather than one each, because the resonance moves with power — choosing a frequency
and then a power leaves the frequency stale by 183 kHz, a tenth of a linewidth, which
is the mistake `resonator_punchout` already exists to not make.

The executors apply it for `meas_level=2` only. The amplitude rides on `Measure`; the
frequency cannot, since the measure operation's clock is fixed at `{qubit}.ro` in the
device config, so it is applied by moving that clock — the same mechanism every
calibration routine already uses to sweep a readout. Levels 0 and 1 are untouched:
applying a point chosen for phase separation to a raw trace would degrade exactly the
measurement that wants the signal.

`readout_discrimination` runs after it and fits its line *at* that point, because a
line fitted where the clouds are not is a line fitted somewhere else.

Single-shot sweeps are bounded at 32 acquisitions per schedule, with an error that
says so. Every appended bin takes a sequencer register and a Q1 sequencer has 64; past
that the qblox backend dies inside its register allocator with a bare `IndexError`, a
long way from the sweep that asked for too much. `resonator_punchout` sweeps a far
larger grid unaffected because it averages — this cannot, since the width of each
cloud is what it measures.

**`f12_spectroscopy`, and an EF drive to run it against** (RFC 0005 §7). The
`|1⟩`-`|2⟩` transition has been a field on the transmon element all along with nothing
measuring it: the fixture pins `clock_freqs.f12` 131 MHz from where the
simulated transmon's actually is, unnoticed for as long as nothing read it. It is the
input to three-state readout and it sets where `|02⟩` sits for a CZ, so a wrong value
is not harmless, only silent.

The routine prepares `|1⟩` and sweeps the `.12` clock across it — so it depends on
`rabi`, which is the same straddle `readout_discrimination` sits in. The simulator
drives that clock now, in its own rotating frame with the detuning on `|2⟩` and `|0⟩` a
spectator; what that leaves out is the off-resonant `0-1` excitation, so a strong EF
pulse leaks more here than on a chip. Recovers 4.9304 GHz against a true 4.9312, and
reports the anharmonicity — −283.7 MHz against −282.9 — which nothing else measures.

**The EF drive is a real drive on the ladder, not a two-level subspace** (RFC 0005 §9).
It was `|1>`-`|2>` in a rotating frame of its own, and that one choice was behind three
separate blockers. It is now an ordinary drive on the full ladder, in the same frame as
everything else, offset from that frame by where its clock sits.

- **One frame.** An EF Ramsey at a 20 MHz artificial detuning shows a ~60 ns fringe.
  Before, the phase between two EF π/2 pulses ran at the whole anharmonicity — a 3.5 ns
  period, aliased to noise on any usable delay grid, so a Ramsey measured the gap
  between two frames rather than the transition.
- **The ladder's √2.** An EF π lands at `amp180 / sqrt(2)`, because the 1-2 matrix
  element belongs to the operator now instead of being folded into it. `rabi_12`
  measures 0.1476 against 0.1430 for a perfect ladder; the rest is relaxation during
  the pulse. The loop test that used to assert *equality* — documenting the limitation
  — now asserts the factor.
- **Something to leak into.** An EF pulse off-resonantly excites 0-1 at about 2.6%,
  where `|0>` used to be a spectator. That is what a DRAG quadrature cancels, so
  `drag_12` has a curve to fit rather than a flat line.

The sign of the frame offset is negative, which is not obvious and is not free: it
pairs with the drift's `+(f_drive - f_qubit)` and the `e^{+iφ}` on the raising
operator. The other sign drives nothing at all — P(`|2>`) stays under 0.003 at every
amplitude — while this one puts a π exactly at `amp180 / sqrt(2)`.

Stepping is the cost: a drive off its frame's own frequency is time-dependent however
flat its envelope. `ramsey_12` and `drag_12` are both writable on top of this; neither
is written yet.

**The simulated acquisition reports every level, not two** (RFC 0005 §9). The
simulator's transmon has had three rungs and correct three-level dynamics for a while
— `X` then an EF pi pulse leaves 98.5% of the population in `|2>` — but the
*acquisition* carried a single `P(excited)`, so a `|2>` shot was sampled onto one of
the other two clouds. The clouds were already derived per level; the sampler was not,
which is what made three-state readout unmeasurable while the physics underneath was
already right.

`_Acquisition` now carries a population vector. `_blobs` indexes the cloud by the
level drawn, `_averaged` and `_trace` weight every level, and the joint-outcome path
returns levels rather than booleans so an entangled register keeps its correlations.
Thresholded acquisition still returns one bit: the instrument is two-outcome however
many levels the chip has.

The draw is ordered `|1>` first, then `|0>`, then the rest — which looks arbitrary and
is not. It consumes the same uniforms as the `rng.random(n) < P(excited)` it replaces,
so a chip with no population above `|1>` draws exactly what it drew before. Every
existing expectation about this simulator was measured against that stream, and
reordering it would have moved all of them at once, leaving no way to tell a physics
regression from a reshuffle. A test asserts the two agree shot for shot.

**One number was resting on the bug.** `f12_spectroscopy` drove at 3% of full scale,
and that default was tuned when `|2>` was reported at `|0>`'s cloud: a 5% population
transfer swung the signal across the whole readout axis and the line looked strong.
Read correctly, `|1>` and `|2>` sit close together at a 0-1 readout point and the same
transfer is a 5% wiggle — enough to put the fitted centre 7.4 MHz out and fail its own
test. The default is now 10%, where the contrast is 26% and the fit lands within a
megahertz. The routine was never right; it was being flattered.

**`rabi_12`, the first EF gate parameter that is measured** (RFC 0005 §7). A transmon
is not a qubit, it is an anharmonic ladder used as one, and the third rung is the
difference: every gate leaks a little population into `|2>`, where two-state readout
reports it as one of the other two — so a leaked shot is not lost, it is counted as an
answer. Measuring that needs a calibrated pulse on the 1-2 transition, and this is it.

The routine prepares `|1>`, sweeps a raw pulse on the `.12` clock and fits the
oscillation into `|2>`. Raw, because neither scheduler has an EF gate — the device
config's operations are built for `rxy` on `.01` — so clock, port and envelope are
assembled by the routine. The result goes to `r12.ef_amp180`, a new submodule on
`CalibratedTransmon`.

It reads out on the two-state chain, which works because `|2>` has its own place in
the IQ plane: the dispersive pull is `chi(1-2n)`, so the three levels form a ladder and
a magnitude readout sees the population move. That is what lets this node come *before*
three-state readout rather than after — the discriminator needs a calibrated EF pulse
to prepare `|2>` in the first place.

The simulator recovers the same amplitude `rabi` does rather than the `1/sqrt(2)` a
chip would give, because it folds the 1-2 matrix element into the subspace operator
instead of taking the ladder's `sqrt(2)`. So the EF path can be tested for wiring —
right clock, right port, right envelope — but not for the ladder's own scaling.

**`resonator_spectroscopy_second_excited`, which checks the ladder rather than a
parameter** (RFC 0005 §7). The dispersive pull is supposed to go as `chi(1 - 2n)` —
`|0>` at `+chi`, `|1>` at `-chi`, `|2>` at `-3chi` — and nothing in the graph checked
that the spacing was *even*. This sweeps the resonator with the qubit in `|2>`, so the
excited-state sweep sees two shifts of gap and this one sees four; dividing each by its
own factor has to give the same chi, and that agreement is the measurement.

It matters because three-state readout rests on it. A ladder that bunched up would
leave `three_state_operating_point` optimising against a chip whose levels cannot be
separated however it is tuned, and that would read as a tuning failure rather than a
model one. A characterisation: it writes nothing.

**Leakage into `|2>` is measured** (RFC 0005 §7). The number the EF chain exists to
produce. Every gate leaves a little population on the third rung, and a two-state
readout does not lose those shots — it reports them as `|0>` or `|1>`, so leakage
arrives as an answer and every fidelity built on it is quietly optimistic.

`three_state_discrimination` prepares all three states and classifies each shot by
nearest centre. Not by a rotation and a threshold: those describe a *line*, and three
clouds have none — `|2>` sits off the axis joining the other two, because the
resonances are evenly spaced while the complex responses at one drive frequency are
not. It writes nothing; using three-state assignment in the job path would mean a
measurement level returning three outcomes, which is an API question rather than a
calibration one.

`three_state_operating_point` finds where to read. A third point rather than a variant
of the two-state one, because tuned for `|0>` against `|1>` the readout sits where
`|1>` and `|2>` both return almost nothing and collapse together — **2.95 sigma apart,
against 39** where this node puts them. It ranks on the *closest* pair of the three,
since a classifier is only as good as the two states it confuses most.

Both were written once before and reverted: they were correct, and the simulated
acquisition could not report a third level to feed them. It can now.

**`ramsey_12` refines f12 to kilohertz** (RFC 0005 §7). `f12_spectroscopy` drives a
20 ns pulse, so its line is Fourier-limited and it lands within a few megahertz —
enough to find the transition, not enough to drive it. Same split `qubit_spectroscopy`
and `ramsey` already have one rung down. It reports a T2* for the 1-2 coherence too,
which nothing else measures.

It detunes the clock rather than phase-advancing the second π/2, the opposite of what
`ramsey` does, and not a preference: a `ShiftClockPhase` on the `.12` clock produced no
fringe at all — the fitted detuning came back at minus the artificial one whatever f12
was set to.

**It also exposed a too-coarse three-state point.** Three frequencies stepped 3 MHz
against a 2 MHz linewidth is enough to *classify* three states and not enough for
anything measured at that point: `ramsey_12` reads `|1>` against `|2>`, and on the
coarse point its f12 came back a megahertz out — no better than the spectroscopy it
exists to refine. `three_state_operating_point` now sweeps five frequencies by two
amplitudes, the split chosen by which axis the criterion actually varies on.

**`drag_12`, the last of the EF chain** (RFC 0005 §7). A pulse on the 1-2 transition
sits only a few linewidths from 0-1, so it off-resonantly excites it — about 2.6% —
and the DRAG quadrature is what cancels that. Two sequences equal only at the right
coefficient, so their difference crosses zero there and the fit is a line.

It finds −0.043 against a swept span of 0.2: a real interior optimum, nowhere near
either edge. **Negative**, where the 0-1 optimum is positive, and that sign is physics
rather than convention — DRAG cancels leakage into the neighbouring level, which for
an 0-1 pulse is `|2>` above it and for an EF pulse is `|0>` below.

Reaching it needed a DRAG-shaped pulse on the `.12` clock, through a new
`SchedulerBackend.drag_pulse` rather than a bound class: the two schedulers disagree
about the argument names *and* the units — `G_amp`/`D_amp` as a ratio against
`amplitude`/`beta` in seconds, the divergence `drag_span` already exists for.

**`fine_amplitude_12` refines the EF pi pulse** (RFC 0005 §7). A π/2 pre-rotation then
n EF π pulses, so a per-pulse error grows linearly against a readout noise that does
not. That residual is invisible to `rabi_12`, which fits a whole oscillation and cannot
see a few per cent of over-rotation — a single π pulse is second order in its own
error. It refines to under 0.05 rad per pulse on both simulated qubits.

The first node to depend on `three_state_operating_point` for *contrast* rather than
for a classifier: its two references are `|1>` and `|2>`, all but on top of each other
at a 0-1 readout and 14 sigma apart in magnitude at the three-state point. That point
turns out to be what makes the whole EF chain measurable, not just the leakage number.

**The coupler is a mode with a frequency, not just a drive** (RFC 0005 §12). A
`TunableCoupler` sits above both qubits at its flux sweet spot and tunes down
quadratically with parking current, coupled to each qubit strongly enough to push it.

That push is the observable the whole of phase 6 waits on. `bias.parking_current` has
been carried, validated and applied since RFC 0004 and **nothing in the simulator
responded to it** — so a routine could have written any current at all and no
measurement would have contradicted it. Now a current in the device config moves the
qubits on that edge, and sweeping it walks the coupler through them: the push runs to
−44 MHz just below the crossing and flips to +63 MHz just above, which is the
signature `coupler_anticrossing` will look for.

**Measured from zero bias, not from nothing.** A qubit beside a coupler is always
repelled; what a bias changes is by how much. Defining the shift as the difference
from the unbiased push keeps the simulator's `f01` meaning what it always meant, so
every expectation measured before this model existed still holds for a chip whose
couplers are unparked — which is every fixture in the suite.

**`coupler_anticrossing` measures the coupler's parking current** (RFC 0005 §12). It
sets a DC bias, runs a spectroscopy, reads the qubit back, and repeats — walking the
coupler down through the qubit and locating the crossing from where the qubit moves
fastest. The current it writes is a stated fraction of that crossing, reported
alongside it: a coupler is parked *away* from its qubits, and how far is a choice about
residual coupling against CZ reach rather than a measurement.

**It is the node that broke the routine interface, deliberately.** A coupler's bias is
not a pulse — it is held over qcodes for as long as the fridge is cold, through an SPI
rack or a cluster output, and *neither can be scheduled*: the QCM path sets an output
offset over qcodes too. So a routine sweeping it has to set instrument state between
acquisitions.

RFC §4 said the routine interface stays unchanged; §11 said this one case should be
handled explicitly in the routine. `CalibrationRoutine.measure` resolves that toward
§11: one routine takes over its own acquisition loop rather than every node gaining a
second sweep axis. Exactly one routine overrides it.

It puts the coupler back when it finishes. A sweep that left the chip at whatever
current it tried last would corrupt everything after it — which is not hypothetical: a
first version of the test wrote into a shared fixture and left the coupler at 1.9 mA,
breaking the CZ tests that read it next.

**`cz_spectroscopy` finds the frequency a parametric CZ has to be driven at** (RFC
0005 §12). The coupler is modulated and a sideband bridges `|11>`-`|02>`: amplitude
sets how fast the exchange runs, frequency sets whether it runs *at all*, so a drive
off the transition is a gate that compiles, plays and does nothing. `clock_freqs.cz`
was hand-set on every edge that has one and nothing measured it.

It needed a routine-level applicability hook, `CalibrationRoutine.applies_to`. A
`CompositeSquareEdge` drives its CZ with a baseband flux pulse and has no drive
frequency: running this on one is not a failure, it is a question that does not arise.
The DAG filters targets by it, and the full-DAG test computes its expectation the same
way rather than asserting every routine runs on every edge.

**The test fixture could not play the coupler it declared.** `q1_q2:fl` was wired to a
baseband QCM output, which tops out at ±500 MHz against a 3.9 GHz sideband, and the
edge carried no `clock_freqs.cz` at all — so its parametric CZ was inert, and nothing
noticed because no test drove it. It is now on an RF module with an LO, and declares a
drive 50 MHz off its own sideband gap for the routine to correct.

**A parametric coupler drive kept its frequency through a held offset** (RFC 0005 §9).
The qblox backend emits a long flux pulse as a held DC offset plus a short tail, and
the branch replaying the held part dropped the drive frequency — so a 100 ns coupler
drive ran as 96 ns at zero, infinitely detuned, plus 4 ns at the real frequency.

Harmless for a baseband CZ, whose resonance is the flux amplitude and which ignores
the frequency entirely. Fatal for a parametric one, which is nothing without it: the
gate ran at 4% of its length and looked simply weak. The parametric CZ had never
worked in simulation, and nothing said so because no test drove it — `cz_spectroscopy`
is the first thing that did.

**`cz_parametrization`, the last node in the graph** (RFC 0005 §12). A parametric CZ is
`cz_chevron`'s counterpart, not a variant: the two gates are resonant in different
variables. A DC-flux CZ is pushed onto the `|11>`-`|02>` crossing by *amplitude*, so it
needs a 2D chevron; a parametric one is brought there by *frequency*, which
`cz_spectroscopy` already found, leaving amplitude to set only how fast the exchange
runs. There is no chevron left — the population oscillates in duration, linearly faster
with drive — and measuring that slope is what makes the gate predictable.

It recovers the simulator's `PARAMETRIC_RATE_MHZ` to within a few per cent. The
conversion carries a factor of four rather than two, written down rather than folded
in: the exchange term carries the rate undivided, so the population oscillates at twice
it, and a chip calibrated in another convention differs by exactly that factor.

It writes a full `|11> -> |02> -> |11>` round trip, not half of one. Half is complete
transfer into `|02>` — a perfectly good gate, measured just as confidently, and not a
CZ. That mistake cost `cz_chevron` a 55 ns duration against a 110 ns round trip for as
long as nothing checked the number itself.

**Three open questions closed** (RFC 0005 §13).

**An edge whose qubits are not calibrated is refused at startup.** A CZ is measured
*through* its qubits — the chevron prepares `|11>` with a π pulse on each and reads
one back — so over an uncalibrated qubit it still fits a curve, still writes an
amplitude and duration, and the gate does not work. The wrong answer is a
calibrated-looking gate rather than an exception, and startup is the only cheap moment
to catch it. This also changed partial recalibration: narrowing to a drifted qubit used
to carry in the edges touching it and leave their far ends alone, which is exactly the
configuration now forbidden. An edge brings both its ends.

**`resonator_punchout` gains a check.** Not a cheaper sweep — the question is not
"where is the resonance" but "is the power still below the crossover", which is about
how the resonance *responds* to power. Two short scans, at the configured power and
half of it: dressed, the line does not move; punched through, it walks. Punchout still
produces `measure.pulse_amp`, because nothing else does — making it check-only needs a
producer for the calibration amplitude, and `readout_operating_point` writes the
*discriminated* point deliberately instead.

**`readout_fidelity` is its own benchmark node.** It measures the same two clouds
`readout_discrimination` does, which is the cost. The reason it is worth paying: only
a node participates in drift monitoring. A benchmark is what a drift check runs and
what queues a recalibration; a number inside another routine's parameters is read by
nobody. Readout fidelity is the quantity that degrades quietly — every gate fidelity
on top of it inherits the error — so it is the last one that should be invisible.

**The calibration graph runs on a chip, not only on the simulator** (RFC 0005 §12b).
Two things stood between it and that.

**The coupler bias had no rack.** `coupler_anticrossing` was only ever handed a
simulated source, so on real hardware it declined and the graph was permanently a node
short. Both tuners now resolve a real one — an S4g over SPI, or a baseband output
inside the cluster — through the same `resolve_bias_source` the executor uses, take a
new `spi_rack_address` option, and release the rack on close.

The resolution asks with `require_current=False`, which is the difference between
parking and calibrating. Parking needs a rack only when there is a current to hold;
calibrating is the opposite case, because an uncalibrated chip has zero everywhere and
zero is exactly when the current must be measured. Requiring one to open the rack would
have meant the bias could never be calibrated on a chip that had not been calibrated.

A source that cannot touch the chip is now refused outright. `RecordingBias` applies
nothing, so a sweep against it returns the same frequency at every point and the fit
reports a crossing with complete confidence — a number written to the device that no
instrument produced. `BiasSource.holds_current` separates a rack from a notebook.

**Half the graph raised on a stock element.** The EF chain and both readout operating
points need submodules only a `CalibratedTransmon` carries, and they *raised* on a
`BasicTransmonElement` — which is what a device file written before this RFC uses. Six
failures for parameters the element was never going to have reads as six broken
routines. They now decline, so such a chip calibrates everything it can and returns
`success`. The tuner README lists which node needs which submodule.

### Fixed

- `qpi-ui`: **no tuner could be registered at all.** The `kind` column never gained
  `quantify_tuner` or `qblox_tuner`, so a registration passed every check the
  endpoint makes and then failed on the insert with `Invalid value quantify_tuner`.
- `qpi-ui`: the schema migration added missing fields but never revisited a field
  already there, so a select kept the values it was created with forever. Declared
  values are now added on every start, for every collection — added only, since
  narrowing would leave a record of a retired kind unable to save.
- `qpi-ui`: a calibration report's `mode` and `status` went from the driver's payload
  into two select columns with only an emptiness check, and a rejected insert
  surfaced as an error inside the listener goroutine — hours of calibration lost to
  a log line. Both are checked against what the column accepts, and the dispatch
  endpoint validates `mode` the same way instead of against its own copy of the
  list.
- `qpi-ui`: the deb, rpm and apk packages shipped no `/etc/qpi.config.yml`, though
  `qpi.service` starts the server with `--config-file /etc/qpi.config.yml`. Each
  format declares its own `contents` for its service unit, and nFPM replaces the
  shared list rather than adding to it. The server tolerates the missing file and
  runs on defaults, so an install worked — it just left nothing to configure.
- `qpi-ui`: two tuners registered against one QPU could calibrate it at the same
  time. The calibration queue serialized per driver, and each driver has its own
  dispatcher, so both DAGs swept the same qubits and both wrote the device YAML.
  `FetchNextCalibration` now withholds a request while any is running on that
  driver's QPU. A tuner's own `drift_check_interval` still bypasses this — it never
  asks the server (RFC 0004 §11).
- `qpi-driver`: the `quantify` and `qblox` extras declared `scipy>=1.11` and
  `lmfit>=1.3`. `scipy` is a core dependency and nothing imports `lmfit`, so both
  are gone.
- `qpi-driver`: `allxy` and `fit_chevron` assumed which direction the readout's
  magnitude moves when a qubit is excited. Nothing guarantees it: whether `|z|` rises or
  falls depends on which side of the resonator's line the readout sits, and
  `resonator_spectroscopy` puts it on the ground-state resonance, where an excited qubit
  reflects *less*. AllXY compared a descending response against an ascending staircase
  and reported an rms deviation of 0.65 on a well-calibrated qubit; the chevron hunted
  the wrong extreme and returned half the round trip — a complete population swap, which
  is a perfectly good gate and not a CZ. Both measure their own references now: AllXY
  normalises against the five `|0⟩` and four `|1⟩` pairs already in its sequence, and the
  chevron anchors on the level its resonant row reads at the shortest duration, where
  the least exchange has happened.
- `qpi-driver`: a fitted `acq_rotation` could be one the instrument refuses. `np.angle`
  returns `(-180, 180]` and the hardware requires `[0, 360)`, so half of all readout
  chains produced a value rejected at compile time — in every schedule *after* the one
  that wrote it, not in the routine at fault. Wrapped, which costs nothing since a
  rotation and that rotation plus a turn are the same rotation.

- `qpi-driver`: A virtual Z did nothing under `is_simulated` — `ShiftClockPhase`
  never reached the coordinator, so every `rz`, `z`, `s` and `t` ran as an
  identity, and a CZ's phase corrections could not work either.
- `qpi-driver`: `meas_level=0` returned a single sample instead of a time series,
  and `meas_level=2` returned IQ instead of the instrument's 0/1 — so a
  raw-waveform job produced a one-point waveform, and every level-2 job silently
  took the software discrimination path rather than the hardware one.
- `qpi-driver`: A raw trace over more than one qubit could not be taken at all; a
  Qblox module scopes one sequencer. The circuit is played once per measured
  qubit instead, at a cost of N runs for N qubits.
- `qpi-driver`: The qblox tuner had never completed a calibration. Its write-back
  called `device.elements()`, which is a *dict* under qblox; what it wrote, qblox
  could not read back, because edges carried positional constructor arguments and
  its edges are pydantic models; and `element_type`, `name`, `edge_type` and both
  endpoints were written as calibration when they are structural.
- `qpi-driver`: `conditional_phase` applied nothing, on either scheduler — it
  wrote `cz.phase_correction`, a name neither has, behind a `hasattr` guard that
  was therefore never true. It now measures four fringes rather than two, because
  the two corrections cancel each qubit's *single-qubit* phase, which is not the
  conditional phase.
- `qpi-driver`: The `drag` routine swept one range for both schedulers, whose DRAG
  parameters are not the same quantity: quantify's `motzoi` is the dimensionless
  ratio of the derivative component to the Gaussian, qblox's `beta` is that ratio
  times the pulse sigma, in seconds. Nine orders apart, and out in the large
  direction the waveform exceeds full scale so nothing compiles. Each backend
  supplies its own default span, and both recover the same physical optimum.
- `qpi-driver`: `drag` then wrote its result to `rxy.motzoi` unconditionally,
  which qblox calls `rxy.beta` — an optimum measured correctly with nowhere to go.
- `qpi-driver`: `fit_chevron` looked for the brightest pixel. The control reads
  ≈1 wherever the flux pulse did nothing, so the maximum was as likely to sit on
  an off-resonant row as on the gate; it finds resonance by oscillation contrast
  now, and refuses a sweep that stepped over the crossing.
- `qpi-driver`: `fit_chevron` then calibrated a population swap and called it a
  CZ. It walked to the first trough and on to the first sample that stopped
  rising, but the trough is not smooth — the exchange beats against nearby
  transitions — so it stopped at the top of a wiggle a few per cent deep, still in
  `|02⟩`: 55 ns for a round trip of 110. The return is a level crossing now, so the
  population has to reach the far side of the swing to count as having come back.
- `make test-py-loop` and `make test-py-sim` reported success having run nothing when
  either followed the other in one `make` invocation — which is the order `make test`
  uses. `uv run` without `--no-sync` re-syncs to the project's *default* dependency
  set, pruning the `sim` group the target had just installed, so every test skipped on
  `importorskip("scqubits")` and pytest exited 0. Both now pin their own environment.
- `qpi-driver`: A raw trace under `is_simulated` carried single-shot noise while
  documenting itself as averaged over repetitions, which are contradictory claims.
  It now falls as 1/√N like an integrated point does. The two halves mattered
  together: at single-shot noise the 10% level of a trace and its noise floor are the
  same number, so no arrival time could be found at any shot count.
- `qpi-driver`: The readout could be calibrated once and never again. The hardware
  fixture pinned each readout port's intermediate frequency and let the LO float,
  so moving one of three qubits sharing a QRM_RF asked the module for two LOs and
  every *later* schedule failed to compile. The LO is pinned instead, as the
  configuration a working chip uses does.
- `qpi-driver`: `resonator_punchout` left the readout pointing where the resonator
  used to be. The resonance moves with readout power — that movement *is* the
  experiment — so settling on a new power invalidated the frequency
  `resonator_spectroscopy` had measured at the old one, and nothing revisited it.
  Measured on the simulated chip: half a linewidth off, costing a fifth of the
  readout contrast for every routine downstream. Punchout writes the frequency as
  well as the power now, taken from the spectrum it already fitted at the power it
  chose — the two are one operating point, and neither is right without the other.
- `qpi-driver`: `fit_conditional_phase` took the zero crossing of the two
  fringes' difference, which sits at half the wanted angle displaced by the
  control's dynamical phase over the flux pulse — tens of turns. It fits each
  fringe and subtracts. Its correction was also `np.pi - crossing` with the
  crossing in degrees.
- `qpi-driver`: The coupler's CZ never reached the simulator — `compile_cz`
  lowers the gate into a fresh subschedule, losing the pair. It is read from the
  port name (`q1_q2:fl`) now.
- `qpi-driver`: Both device loaders added elements in file order, so an edge
  listed before its qubits failed — as any alphabetical rewrite of the YAML
  produces.
- `qpi-ui`: A failed job rendered nothing at all — a red badge and three tabs
  each reporting "No counts data available", while the driver's reason sat unread
  in the record. The reason is shown in place of the tabs.
- `qpi-ui`: The dashboard's IQ plot mapped both axes onto a hardcoded
  `-0.5..1.5`. IQ arrives in whatever units the readout chain produces, so most
  devices' clusters fell outside it and the tab rendered empty. It scales to the
  data.
- `make test` was red on macOS: only three of the eight targets that `uv sync`
  re-applied the code signature `uv` strips from qblox_instruments' q1asm
  assembler, and `test-docs-static` believed a stub CLI that exits 0 with an
  empty help, reporting 17 documented flags as removed.
- `qpi-driver`: The qblox tier-2 test asserted a routine's schedule was *built*
  but never compiled, so a schedule that would not compile passed. Both backends
  compile now.
- The driver e2e's QPU-seconds check read its baseline nine API calls before the
  approval it was measuring, so a job settling in the background in between turned
  "approving 300 seconds credits 300 seconds" into a flaky assertion that nothing
  else happened meanwhile. It reads the baseline where it means to.

### Changed
- `make test-e2e-dashboard` accepts `SPEC=<glob>` to run a single Cypress spec —
  seconds rather than minutes when iterating on one tab.
- The dashboard's `QPU` type no longer carries `calibration_data`, a field no
  collection on the server ever had.


## [0.2.0] - 2026-07-29

### Migration

This release breaks the driver CLI's grammar and the Python and TypeScript SDKs'
APIs (RFC 0003 §11). The project is pre-1.0 and makes no stability promise, so a
name that has stopped earning its keep is deleted rather than aliased — the
obligation that comes with that is to break **once** and to publish the table
rather than let anyone discover the changes by failure.

**Every driver is now launched with one verb.** `--operation` and `--device` are both
required, reading `QPI_OPERATION` and `QPI_DEVICE`. There is no default device: the
dashboard generates the command that launches a driver and it always names one, so a
value an SDK filled in could only be a guess.

| Before | After |
|--------|-------|
| `qpi-driver process --device qblox …` | `qpi-driver start --operation process --device qblox …` |
| `qpi-driver monitor --device bluefors_gen1 …` | `qpi-driver start --operation monitor --device bluefors_gen1 …` |
| `qpi-driver start --operation process …` (device defaulted to `mock`) | `qpi-driver start --operation process --device mock …` |

**A driver no longer publishes a catalog.** The device catalog lives in QPI-UI alone
(RFC 0003 §9). `qpi-driver catalog --json` is gone from all three CLIs, and with it
the option schemas the SDKs declared — a device is now a name, an operation and a
builder, and the `-o` keys it accepts are the ones its builder reads.

| Before | After |
|--------|-------|
| `qpi-driver catalog --json` | *(removed; the dashboard is the catalog)* |
| `qpi-driver devices` — operations, devices, every option with its type and default | `qpi-driver devices` — the registered names, by operation |
| `start --help` listing every device and option | Points at the dashboard, where they are documented |
| `OptionSpec` / `OperationSpec` (all three SDKs) | *(removed)* |
| `DeviceSpec.options` / `.extra` / `.summary` / `.accepts_any_option` | *(removed)* |
| `spec.parse_options(raw)` → converted values | `Options(raw)`, read by the builder: `options.get_int("job_timeout", 10)` |
| `devices.AsInt`/`AsBool`/`AsFloat`/`AsString` (Go), `asInt`/`asBool`/… (TypeScript) | *(removed; the accessors convert)* |
| `qpi-driver/catalog` (TypeScript entry point) | *(removed)* |
| `make sync-driver-catalog` | *(removed; nothing to sync)* |

**A driver no longer names itself.** `--name`/`-n` and `QPI_DRIVER_NAME` are gone
from all three CLIs, and `Name`/`name` from all three SDK configs. Delete the flag
from an existing unit file; there is nothing to replace it with, because the name an
admin typed in the dashboard is now what the driver is called. `SERVICE_NAME`
replaces the installers' `QPU_NAME`, which never named a QPU or a driver — it names
the unit file, its journal identifier and its data directory.

| Before | After |
|--------|-------|
| `qpi-driver start … --name cryostat-1` | `qpi-driver start …` — the name comes back from `drivers/connect` |
| `QPI_DRIVER_NAME=cryostat-1` | *(nothing; the dashboard is where the name is set)* |
| `install-systemd.sh` with `QPU_NAME=cryostat-1` | `SERVICE_NAME=cryostat-1` |
| `QpuDriver(name=…)`, `BlueforsGen1Driver(name=…)` | *(removed; read `driver.name` after `run()`)* |
| `qpidriver.Config{Name: …}` (Go) | *(removed; read `DriverName()` after `Run`)* |
| `QpiDriverOptions.name` (TypeScript) | *(removed; read `driver.name` after `run()`)* |
| `OperationSpec.default_name` / `DefaultName` / `defaultName` | *(removed; an operation has no name to default)* |

**Behaviour that changed without a rename**, and is worth checking an existing unit
file against:

| What | Was | Is now |
|------|-----|--------|
| An `-o` key no device reads | Silently ignored | Exits 1, naming the key and the device |
| An `-o` value of the wrong type | Coerced ad hoc, or ignored | Exits 1, naming the option |
| `--ca-fingerprint` omitted (TypeScript) | Connected **without verifying the pinned CA** | Exits 1 |
| `--recv-timeout-ms` (TypeScript) | Accepted and ignored | Removed |
| `--ca-file` (TypeScript) | Accepted and ignored | The CA is written there after it verifies |
| `start --operation process` on Go/TypeScript | `unknown process device "mock"; known devices: ` | Says the SDK ships no process devices, and where to find one |
| A `process` driver's dataset `backend` attribute | The driver's display label, hyphens turned to underscores | The executor's own name (`mock`, `qblox`, …) |
| Registering `kind=mock, language=go` | Accepted, with snippets for a device Go has not got | Exits 400, naming what that SDK does ship |

**Python SDK.** The driver-authoring surface is untouched — `QpiDriver`,
`handle_event()`, `emit()`, `every()`, `Event`, `EventType` and `Executor` keep their
names and signatures. What moved is how a driver is registered and launched:

| Removed | Replacement |
|---------|-------------|
| `run_driver(...)` | `QpuDriver(...).run()` |
| `qpu.run_process(device=..., ...)` | `qpu.build_from_options(executor=..., ...).run()` |
| `bluefors_gen1.run_monitor(...)` | `bluefors_gen1.build_from_options(...).run()` |
| `builtins.PROCESS_DRIVERS`, `builtins.MONITOR_DRIVERS` | `builtins.devices(operation)` / `builtins.resolve(operation, device)` |
| `builtins.DriverRunner` | `builtins.DeviceBuilder` (returns a driver rather than blocking) |
| `resolve_executor(executor, custom_executors, ...)` | `resolve_executor(executor, ...)` — pass the class or instance itself |
| `QpuDriver(custom_executors={"name": Cls})` | `QpuDriver(executor=Cls)` |
| `QpuDriver.OPERATION`, `BlueforsGen1Driver.OPERATION` | `DeviceSpec.operation` |
| `qpu.execute_job()` | `qpu.job_worker()` |
| A builder taking raw `-o` strings | A builder taking an `Options`, whose accessors convert and carry the default |

**TypeScript SDK:**

| Removed | Replacement |
|---------|-------------|
| `DeviceRunner` | `DeviceBuilder`, taking `(config, options: Options)` |
| `Options.raw()` / `.all()` / `.channels()` | `Options.str/int/num/bool/ms/require/remaining`, each taking its fallback |
| `QpiDriverOptions.caFingerprint?` | `QpiDriverOptions.caFingerprint` — required |
| `--recv-timeout-ms` | *(gone; the transport is event-driven)* |

**Go SDK** breaks nothing that was reachable: its device table was unexported inside
`package main`. It gains the importable `devices` and `cli` packages.

### Added

- `docs`: The three SDK READMEs now show both shapes of a custom device, and the same two in each: **reusing a driver the SDK ships** with your part plugged in (`bluefors_gen2`, a monitor for Bluefors Gen. 2 Control Software, which supplies its own channel reader and inherits the poll loop) and **writing one from scratch** (`thermometer`, a `QpiDriver` subclass with its own `every()` tick). The Python README keeps a third, which only it can offer: a QPU is an `Executor`, and `device_spec()` binds it to the whole shipped QPU driver — the worker subprocess, the result pump and the `JobResult` event included. Every block is compiled or executed by `make test-docs`.
- `docs`: The three SDK READMEs define **operation** and **device** where they first appear, instead of using both as if the reader had read RFC 0003. Likewise what `run()`/`Run` actually does, and what the flags "before the `-o` ones" have in common — previously "universal options", which named the category without saying what was in it. Two FIXMEs on the TypeScript README are answered in the text: what an import path resolving to a bare builder means for the device's name and operation, and *when* an unread `-o` key is reported (after the device is constructed, which is still before anything connects).

- `repo`: Added `make test-docs`, and a CI job that runs it on every pull request — `docs.yml` only ever ran on a `v*` tag, so nothing checked the documentation before a merge. Four steps, so a failure says which kind of claim broke: **static** (every `make` target, repository path, markdown link and `qpi-driver` flag a document names, the flags read from each SDK's own `--help`), **snippets** (the Python blocks executed against the real SDK, the Go and TypeScript blocks extracted and compiled against this checkout, and the four error transcripts in `docs/driver/operations.md` compared with what the CLI prints), **example** (`examples/custom_device` installed against the local SDK, then asked for via `qpi-driver devices`), and **site** (`mkdocs build --strict`, which catches a dead nav entry or internal link). A block that is meant to fail opts out with `<!-- docs-check: skip -->`; a compiled block opts in with `<!-- docs-check: compile=<name> -->`. `CHANGELOG.md` is exempt: it records commands that no longer work on purpose.
- `qpi-driver/py`: Added the device registry (RFC 0003 §6) — `Operation`, `DeviceSpec` and `DeviceBuilder` in `qpi_driver.builtins.registry`, with `register()`, `devices()` and `resolve()`. A device is a name, an operation and a builder, registered next to its own code, so `--device` accepts it without any change to the CLI. It is deliberately not a description of itself: the catalog lives in QPI-UI, and a second one would be a second one to keep in step (RFC 0003 §9).
- `qpi-driver/py`: Added `qpi_driver.Options` — the raw `-o` values, with `get_str`, `get_int`, `get_float`, `get_bool`, `get_path`, `get_dir`, `require`, `remaining` and `unread`. Each accessor takes the fallback used when the key is absent, so a device's default lives in the one piece of code that acts on it, and each names the option in any error it raises. Reads are remembered: an `-o` key nothing read is reported after the build, which keeps a typo an error without a declared schema to check it against — and matters because every built-in executor's constructor takes `**kwargs` and would swallow one silently.
- `qpi-driver/py`: Added `qpi_driver.builtins.qpu.device_spec()` for describing a `process` device that runs a given executor, including one the SDK does not ship.
- `qpi-driver/py`: Added `qpi-driver devices [--operation OP]`, which lists the device names this install can run. That is the one question the driver is the authority on — it depends on the extras installed and the entry points present — and it is the question an operator asks after installing a distribution of devices or writing one.
- `qpi-driver/py`: Added `qpi_driver.paths.as_safe_dir()`, so the safe-location check a `-o` directory needs happens where the directory is read (`Options.get_dir`) rather than in each builder.
- `qpi-driver/py`: A distribution can now advertise devices through the `qpi_driver.devices` entry-point group (RFC 0003 §6). `pip install mylab-devices` and its devices are listed by `qpi-driver devices` and accepted by `--device`, indistinguishable from a built-in, with nothing to change in the SDK. An entry point that will not import or does not resolve to a `DeviceSpec` is logged and skipped, never fatal.
- `qpi-driver/py`: A device can now be named by import path — `--device mylab.devices:PrestoV2`, or `mylab.devices.PrestoV2`, following Pydantic's `ImportString` convention. For `process` it must import to an `Executor` subclass or instance, for `monitor` to a device builder; either accepts a `DeviceSpec`, which is how it gets a name of its own. An executor named this way is handed every `-o` key the SDK does not read itself, as typed, since its constructor is the only thing that knows those keys exist; the ones the SDK does read, `data_dir` included, are still checked, so the safe-path check applies on this route too. A path that will not import exits 1 with one line, with the filesystem path Python volunteers stripped out of it (RFC 0003 §10).
- `qpi-driver/py`: Added `qpu.device_spec(pass_through=True)` for a custom process device whose executor reads `-o` keys the SDK has never heard of.
- `qpi-driver/py`: Added `qpi-driver/py/examples/custom_device/` — an `Executor`, a `DeviceSpec`, and the `pyproject.toml` entry-point stanza, with a README covering all three ways to run it.
- `qpi-driver/py`: `JobPayload` and `CircuitPayload` are now exported from `qpi_driver` itself, which is what writing an executor needs.
- `qpi-driver/go`: Added the importable `devices` package — `Operation`, `DeviceSpec`, `DeviceBuilder`, `Options`, `Registry`, `Register`, `Devices`, `Resolve` — the Go counterpart of the Python SDK's device registry (RFC 0003 §6, §8). The device table was an unexported `map[string]deviceRunner` in `package main`, so nothing downstream could extend it. `Options` reads the raw `-o` values with typed accessors that each take a fallback, accumulating any conversion failure in `Options.Err()` so a builder stays a flat run of statements, and remembering the reads so `Options.Unread()` can report a key nothing looked at.
- `qpi-driver/go`: Added the importable `cli` package with `NewRootCmd` and `Execute`. Go has no runtime import by name, so extension is compile-time: a build of your own calls `devices.Register` and then `cli.Execute`, and gets the same `start`/`devices`/`version` commands as the shipped binary. `qpi-driver/go/qpi-driver/main.go` is now exactly that — register the built-ins, hand off. The README documents it with an example that is compiled as part of verifying it.
- `qpi-driver/go`: Added `qpi-driver devices [--operation OP]`, listing the device names this build was compiled with — which in Go is genuinely a per-binary fact.
- `qpi-driver/go`: The `bluefors_gen1` monitor now registers a `DeviceSpec` beside its own code, and its builder reads its own `-o` options. `qpi-driver/go/cli` and `qpi-driver/go/devices` have tests where `qpi-driver/go/qpi-driver` had none at all.
- `qpi-driver/js`: Added the `qpi-driver/devices` entry point — `Operation`, `DeviceSpec`, `DeviceBuilder`, `Options`, `registerDevice`, `devices`, `resolve` — also re-exported from the package root (RFC 0003 §6, §8). Registering a device makes the CLI run it. `Options` reads the raw `-o` values with accessors that each take a fallback and throw naming the option, and remembers the reads so an unread key is reported after the build.
- `qpi-driver/js`: A device can now be named by import path — `--device ./dist/my-device.js#MyExport`. The separator is `#` rather than Python's `:` because `:` is a URL scheme separator in a JavaScript module specifier; a name without `#` is still read as a registered name, so a typo gets the known-devices error rather than an import failure. The export may be a `DeviceSpec` or a builder. A path that will not import exits 1 with one line, with the absolute paths Node volunteers reduced to their last segment (RFC 0003 §10).
- `qpi-driver/js`: Added `qpi-driver devices [--operation OP]`, listing the device names this build registers.
- `qpi-driver/js`: `src/builtins/cli.ts` has a test file, and `src/devices.ts` is covered too — 94 tests where the CLI had none.
- `qpi-driver/js`: `--ca-file` is now honoured: the downloaded root CA is written there after it has been verified, as the Python and Go SDKs do. It was parsed and ignored, so the file never appeared.
- `qpi-driver`, `qpi-ui`: Added coverage measurement and gates, where nothing measured coverage before. `make test-py-cli` enforces 96% over the Python framework modules, `make test-go-driver` enforces 94% over the Go `devices` and `cli` packages, and `npm test` enforces per-file thresholds on the TypeScript device registry, CLI and CA pinning. Each gate was checked by making coverage drop and watching it fail. The hardware executors and the NNG transport are reported but not gated: they need real instruments or a live server, and are covered by `make test-e2e-driver` — `.agents/ROADMAP-0003-driver-extensibility.md` records every exclusion with its reason.
- `qpi-driver/py`: The QPU worker, the result pump, the SDK's receive loop and shutdown, the pinned-CA download, and the job envelope's validation all have unit tests now — the failure paths the e2e suite cannot reach: an executor that will not resolve, a job that raises, a malformed payload, a socket that times out, a fingerprint that does not match.
- `docs`: Added an "Adding a device on a production node" runbook section (`docs/driver/operations.md`) — how an operator discovers what a node can run, the two ways a third-party device gets there, and what each of the four `-o` validation errors looks like, quoted from the CLI rather than paraphrased.
- `qpi-ui`: `drivers.Option` now carries `Help`, `Required`, `Default` and `InSnippet` alongside `Example`. This is the catalog — the only one there is (RFC 0003 §9) — so it has to say enough for an operator to fill a device's options in. The `process` devices' five `-o` keys are listed, where `processSpec` previously declared none. `InSnippet` decides what a copy-pasted command pre-fills: `bluefors_gen1` fills in `channels` and `base_url` and leaves `api_key`, `poll_interval` and `timeout` out, since the driver already defaults them sensibly.

### Changed

- `qpi-driver`: Removed the "was this option given?" query — `Options.Has` in Go, `Options.__contains__` in Python, and `Options.has` from the TypeScript public surface (it stays as an internal detail of `ms()`). It answered a question that mattered when `Options` held pre-parsed values and a caller had to tell "absent" from "the zero value"; now every accessor takes its own fallback, so nothing asked. `devices.Registry.Has` went with it: its only caller was the default-device resolution, which is gone along with default devices.


- `qpi-driver`: The `bluefors_gen1` monitor now takes *what reads one channel* as an argument, in all three SDKs — `read_channel=` in Python, `channelReader` in TypeScript, `Options.ReadChannel` in Go. Everything around that read is the same whatever is being read: the timer, one bad channel not losing the rest of the tick, and the `CryostatReading` event. Supplying a different reader is therefore the whole of what a monitor for other control software has to write — Bluefors Gen. 2, whose control software has its own API, being the obvious case. It is the arrangement `QpuDriver` already used for executors: the reusable driver holds the replaceable part as a value, so nothing is subclassed and a device cannot be broken by the driver's internals moving. In Go it also had to be a value, there being no inheritance to reach for; making the three agree was the point. `bluefors.Reading` is exported for it.


- `qpi-driver`: [BREAKING] The `process` and `monitor` subcommands are replaced by one `start` verb taking `--operation`, in all three SDKs at once (RFC 0003 §4). One verb because everything about launching a driver is the same whichever operation it is — and because a third party can add a device, but only QPI-UI can add an operation, so the operation is an argument rather than part of the grammar.

  | Before | After |
  |--------|-------|
  | `qpi-driver process --device qblox …` | `qpi-driver start --operation process --device qblox …` |
  | `qpi-driver monitor --device bluefors_gen1 …` | `qpi-driver start --operation monitor --device bluefors_gen1 …` |

  `--operation` is required, has no short form (`-o` is `--option`, and `-O` beside it would be a hazard), and reads `QPI_OPERATION`. `--device` is required too, reading `QPI_DEVICE`. The dashboard's setup snippets, all three `install-systemd.sh` installers and the e2e harness render the new grammar; the `OPERATION` environment variable the installers take is unchanged.
- `qpi-driver`, `qpi-ui`, `repo`: [BREAKING] **The device catalog lives in QPI-UI, and nowhere else** (RFC 0003 §9). An earlier draft of RFC 0003 split it — operations to the server, devices and their `-o` options to the driver — and reconciled the halves with `qpi-driver catalog --json`, checked-in `testdata/catalog.{python,go,typescript}.json` fixtures, a drift test and `make sync-driver-catalog`. That is now gone, along with `OptionSpec`, `OperationSpec`, the schema fields of `DeviceSpec`, the `catalog` subcommand in all three CLIs, the generated `-o` tables in the Python README and `scripts/render_catalog_table.py`. A device in an SDK is a name, an operation and a builder; the `-o` keys it accepts are the ones its builder reads.

  The split described the same device twice and held the copies together with a script somebody had to remember to run — fragile in exactly the way that is invisible until the two disagree. Registering a driver in the dashboard already knows the operation, the device and the options, and it already generates the command that launches it; the driver's job is to run what it is told. It is not a second client of QPI-UI, so nothing here replaces `catalog --json` with a request in the other direction. What the driver still answers is the question only it can: `qpi-driver devices` lists what *this* build has, which depends on the extras installed, the entry points present and, in Go, what was compiled in.

  An `-o` key nothing read is still an error rather than a setting silently ignored — `unknown option 'data_dirr' for process device 'mock'` — but the check is now what the device read rather than a declared list. That is not a weaker check: the built-in executors' constructors all take `**kwargs`, so a declared schema was the only thing standing between a typo and a driver running with a default nobody chose, and the reads are a description of what a device accepts that cannot fall out of step with it.
- `qpi-driver/go`, `qpi-driver/js`: [BREAKING] `start --operation process` now says that the SDK ships no process devices and where to find one, instead of `unknown process device "mock"; known devices: ` with an empty list and a default device that never existed there (RFC 0003 §8).
- `qpi-driver/js`: [BREAKING] **A CA fingerprint is now required, and there is no longer any code path that connects without checking it.** *Certificate pinning* is the industry term for what the check does: a driver downloads the server's root CA over plain HTTP, then refuses it unless the SHA-256 of its DER bytes equals the fingerprint the operator was handed out of band. Pinning one certificate is what makes the download safe — without it, anything that can answer for the server's address can hand the driver a CA of its own and read every job that follows. The SDK skipped the check entirely when `caFingerprint` was absent, so an unpinned connection was reachable by leaving an argument out — the kind of opt-out a copy-pasted command hits by accident (RFC 0003 §10). `QpiDriverOptions.caFingerprint` is now required, and `verifyFingerprint` throws on an empty one rather than returning quietly.
- `qpi-driver/js`: [BREAKING] `DeviceRunner` is now `DeviceBuilder`, and a builder receives `(config, options)` where `options` is an `Options` with typed accessors that each take the fallback, rather than a bare `Record<string, string>`. An `-o` key the chosen device never reads is now an error, where before it was silently ignored.
- `qpi-driver/js`: [BREAKING] `--recv-timeout-ms` is gone. It was parsed and ignored, and it names a polling interval this SDK does not have — the TypeScript transport is event-driven, so there is nothing for it to time out. Accepting it was advertising a knob that did nothing.
- `qpi-driver/py`: [BREAKING] An `-o` key the chosen device never reads is now an error, where before it was silently ignored. A typo such as `-o data_dirr=/data` used to mean a driver running with a default nobody chose; it now exits 1. A device whose executor genuinely reads keys the SDK has never heard of opts in with `qpu.device_spec(pass_through=True)`, which is what the import-path route does for you.
- `qpi-driver/py`: [BREAKING] A device builder is now handed an `Options` rather than a `dict` — `build_from_options(options=Options(raw))` — and reads the keys it understands from it. Calling a builder with a plain dict no longer works; wrap it.
- `qpi-driver/py`: [BREAKING] A device builder now returns an *unstarted* driver and the caller starts it, matching the TypeScript SDK (RFC 0003 §7). A driver can therefore be built and asserted on with no server running.

  | Removed | Replacement |
  |---------|-------------|
  | `run_driver(...)` | `QpuDriver(...).run()` |
  | `qpu.run_process(device=..., ...)` | `qpu.build_from_options(executor=..., ...).run()` |
  | `bluefors_gen1.run_monitor(...)` | `bluefors_gen1.build_from_options(...).run()` |
  | `builtins.PROCESS_DRIVERS`, `builtins.MONITOR_DRIVERS` | `builtins.devices(operation)` / `builtins.resolve(operation, device)` |
  | `builtins.DriverRunner` | `builtins.DeviceBuilder` (returns a driver rather than blocking) |
  | `resolve_executor(executor, custom_executors, ...)` | `resolve_executor(executor, ...)` — pass the class or instance itself |
  | `QpuDriver(custom_executors={"name": Cls})` | `QpuDriver(executor=Cls)` |
  | `QpuDriver.OPERATION`, `BlueforsGen1Driver.OPERATION` | `DeviceSpec.operation` |
  | `qpu.execute_job()` | `qpu.job_worker()` |

- `qpi-driver/py`: The driver-authoring surface is unchanged — `QpiDriver`, `handle_event()`, `emit()`, `every()`, `Event`, `EventType` and `Executor` keep their names and signatures. Only how a driver is registered and launched moved.

### Fixed

- `qpi-driver/js`: The SDK set no timeout on any outbound network call, where the Python and Go SDKs both use 10 seconds. A server behind a firewall that drops packets left the driver hanging inside `run()` instead of failing — Node's `fetch` falls back to undici's defaults, which are minutes rather than seconds, and `tls.connect` has none at all beyond the OS TCP timeout, so a TLS handshake that stalls after TCP connect waited indefinitely. Under systemd's `Restart=on-failure` such a unit is never restarted, because it never fails. The `drivers/connect` handshake, the root CA download and both NNG dials now share the same hard-coded 10s deadline as the other two SDKs, and one that expires says what timed out and against which address rather than raising a bare abort. The deadline bounds the dial only: an idle connection afterwards is normal for this event-driven transport, which is why `--recv-timeout-ms` was removed rather than repurposed.
- `qpi-driver/go`: `--help` and `devices` no longer advertise an operation's default device in a build that does not have it, and omitting `--device` in such a build now asks for one, naming what is registered, instead of failing over a device the operator never typed. Which devices a Go binary has is decided when it is compiled.
- `qpi-driver`, `qpi-ui`: [BREAKING] **The driver no longer names itself.** `--name`/`-n`, `QPI_DRIVER_NAME`, the `Name`/`name` config field in all three SDKs and `default_name`/`DefaultName`/`defaultName` on the operation specs are all removed, and `handleDriverConnect` no longer writes a name into the driver record. The token is a driver's whole identity — it is what the record is looked up by and, transitively, what says which QPU the driver belongs to — while `name` is a cosmetic, non-unique display label that an admin types in the dashboard. Nothing looks a driver up by it and no unique index exists, so the only thing a `--name` ever achieved was to overwrite what the admin chose, on every connect, from a unit file nobody re-reads. `POST /api/op/drivers/connect` now returns `name`, so a driver *learns* its label instead of asserting one: read `driver.name` (Python, TypeScript) or `DriverName()` (Go) after connecting. `Name` is gone from `DriverConnectRequest`; an older driver that still sends one is not rejected, the field is simply not read. `Host` and `Version` stay accepted and are flagged as dead on the wire — no SDK has ever sent either.
- `qpi-driver/py`: [BREAKING] A `process` device's datasets record the executor's own name as their `backend` attribute (`mock`, `qblox`, …) rather than the driver's display label. `QpuDriver` used to override the executor's name with its own, which is the only reason a `_sanitize_name` existed: a driver called `lab-1` produced datasets claiming a backend of `lab_1`. `_sanitize_name` is gone with it.
- `qpi-driver`: [BREAKING] `install-systemd.sh` reads `SERVICE_NAME` where it read `QPU_NAME`, in all three SDKs, and the dashboard's systemd snippet renders the new name. It never named a QPU or a driver: it names the unit file, its `SyslogIdentifier` and its data directory. Existing invocations must be updated; there is no alias.
- `qpi-driver/js`: `qpi-driver --help` and `--version` exit 0. `exitOverride()` makes commander throw instead of exiting, so that the command tree can be driven in-process by a test — but that also turned printing help into a rejected promise, which the bin reported as an error and exited 1 for. The Go and Python CLIs exit 0, and a `set -e` script that asks a CLI what it can do before using it died on the answer.
- `qpi-driver/go`: `install-systemd.sh` downloads and unpacks the Go toolchain into `/usr/local/go` when the node has none, instead of exiting with instructions to install it and run the script again. `GO_VERSION` overrides which; an architecture with no prebuilt tarball still exits with the link, and `QPI_SKIP_INSTALL=1` still skips the whole step.
- `qpi-driver/py`: A device installed through the `qpi_driver.devices` entry point is no longer skipped. `qpi_driver.builtins` ran `load_installed_devices()` at its own import time, and it is imported by `qpi_driver/__init__.py` on the way to binding `Executor` and `JobPayload` — so a device written the documented way (`from qpi_driver import Executor`) was loaded against a half-initialised package and skipped with "cannot import name 'Executor' from partially initialized module". The entry-point route, the SDK's whole story for shipping a device, therefore worked for nobody. Discovery now runs at the end of `qpi_driver/__init__.py`, the first moment the package a device imports is complete; importing any `qpi_driver` submodule runs that file first, so no route into the registry skips it. It went unnoticed because the only tests of this route mocked `importlib.metadata.entry_points`; `make test-docs` now installs `examples/custom_device` against the local SDK and asserts `qpi-driver devices` lists it.
- `qpi-driver/py`: Removed a dead branch in `qpu.job_worker`, which tested whether `data_dir` was already among the executor options — it never could be, being a parameter of that same function.
- `qpi-driver/go`, `qpi-driver/js`: [BREAKING] `install-systemd.sh` no longer offers devices the SDK does not ship. Both installers were copied from the Python one, so both prompted with the Python device list (`mock, qiskit_aer, quantify, qblox, presto, bluefors_gen1`) and defaulted to `OPERATION=process`, `DEVICE=mock`. Pressing return through the prompts wrote and enabled a unit whose `ExecStart` can never succeed — "this build ships no process devices" — and, with `Restart=on-failure`, crash-looped it. Both now prompt with and default to `monitor`/`bluefors_gen1`, the one device each actually has. An `OPERATION=process` passed explicitly is no longer offered anywhere and will still fail; run a QPU from the Python SDK or a device of your own.
- `qpi-driver/js`: `install-systemd.sh` installs Node.js with nvm when the target user has none, instead of exiting with instructions to install it and run the script again — the one thing a one-command installer should not do. It also writes a `PATH` into the unit file that contains that `node`: `qpi-driver` is a script with a `#!/usr/bin/env node` shebang, and systemd's default `PATH` has no nvm install on it, so a unit written without this started only for operators whose Node happened to be system-wide. `NVM_VERSION` and `NODE_VERSION` override what it installs, and `QPI_SKIP_INSTALL=1` still skips the whole step.
- `qpi-driver/py`: `install-systemd.sh` prompts for `DRIVER_OPTIONS` whatever the operation. It asked only for a `monitor`, on the reasoning that a `process` device's options are all defaulted — but a process device reads `-o` keys too (`job_timeout`, `is_dummy`), and only the data dir and the quantify config paths are the installer's own to fill in. Setting anything else meant editing the unit file afterwards.
- `qpi-driver`: `install-systemd.sh` gets to the end when it is piped into `bash`, in all three SDKs. The documented non-interactive form — `curl … | sudo … bash` — puts the script itself on stdin, so a prompt the environment had not already answered read EOF, returned non-zero, and ended a `set -e` script where it stood: no unit file, no service, and not one word of output to say why. The README's own example reaches it, setting every variable except `DRIVER_OPTIONS`. A prompt now happens only when there is a terminal to answer it; piped, a value with a default takes its default, and one without (`QPI_TOKEN`, `QPI_ADDR`, `CA_FINGERPRINT`, `SERVICE_NAME`) names the variable to set instead of exiting in silence. A `DRIVER_OPTIONS` with a stray semicolon (`;base_url=…`) ended the install the same wordless way, because the empty field it splits into made the option-appending helper return non-zero; empty fields are skipped.
- `docs`: The root `README.md` described `qpi-driver` as a Python QPU daemon; it now describes the driver framework it is, with the operation/device split that makes it extensible and a QPU as one device of one operation (RFC 0003 §14). Each SDK README states what it actually ships, since only the Python SDK has a `process` device.
- `docs`: Corrected the driver architecture in both the root and Python READMEs: results are pumped by a *thread* in the main process, not a third "Result Sender Process", and the whole worker arrangement belongs to the `process` operation — a `monitor` has none of it.
- `docs`: Fixed the custom-executor example in `qpi-driver/py/README.md` a second time: it defined only `execute()`, so `Executor`'s other abstract method made it impossible to instantiate. Every Python snippet in the driver documentation is now executed against the real SDK by `make test-docs`, so a third occasion is a failing build.
- `docs`: RFC 0003 is `Implemented`, and RFC 0001 §2 now points at it for the operation/device layer rather than being edited in place.
- `docs`: Rewrote the Python README's extension material to tell the same story the Go and TypeScript READMEs do: one heading, "Adding a device of your own", leading with the device — an executor plus a `device_spec` — and the entry point that ships it. `QpuDriver(executor=…)` is a short note under it rather than the first thing offered, because a reader with three co-equal mechanisms in front of them has to work out which one is theirs. The mechanism is unchanged.
- `docs`: `examples/custom_device` renamed its `ThermometerExecutor` to `QuantumXExecutor` and `probe_count` to `qubit_count`. It is a `process` device — a QPU — and a thermometer is a monitor, so the example named itself after the wrong operation. Its `execute()` also returned a `dict` where `Executor` declares `xr.Dataset`, which is the contract `process_result()` and the driver's own dataset writing depend on; it returns a dataset now. `pyproject.toml` depends on `qpi-driver[cli]` rather than the bare SDK — without the extra there is no `typer` and no `qpi-driver` script, so the README's next command could not run — and resolves it from the sibling source tree, so the test installs this checkout rather than a published wheel.
- `docs`: The three SDK READMEs used "the pinned root CA" as if it were self-evident. Each now says what it is where it first appears: refusing any root certificate whose SHA-256 is not the fingerprint the operator was handed out of band.
- `docs`: `qpi-driver/go/README.md` had an empty "Running a built-in as a systemd service" heading, and `qpi-driver/js/README.md` had its manual instructions commented out and its installer example asking for `--operation monitor --device qblox` — a `process` device that neither SDK ships. Both now document the `monitor` devices they actually have, by installer and by hand.
- `docs`: The "Upgrading?" note in each SDK README promised a migration table at `CHANGELOG.md#migration`, an anchor that moves to whichever release most recently had one. It now names the release boundary it is about and links the change log itself, so it cannot come to describe a release that never broke anything.
- `docs`: `qpi-driver/py/README.md` introduced itself as "The Go SDK".
- `docs`: The root `README.md` told the reader to `pip install ./qpi-driver[cli]`, a directory with no `pyproject.toml` in it, and pointed `-o quantify_device_config` at a `quantify.device.example.json` that has never existed — the file is YAML, and both example configs live under `qpi-driver/py/`.
- `docs`: `docs/driver/operations.md` closed with `make test-e2e-driver-framework`, a target that existed only in the Makefile's `.PHONY` list. Removed the phantom from `.PHONY` and named the real target.
- `qpi-ui`: [BREAKING] Registering a driver whose kind the chosen language's SDK does not ship is now a 400 naming what that SDK does ship. `POST /api/op/drivers/create` accepted any kind×language pairing, and `drivers.Snippets` rendered setup commands for all of them — so `kind=mock, language=go` returned a `--device mock` against a Go binary with no process device at all, a command that exits 1 the first time it is pasted and says nothing about why. The dashboard's language selector now disables the languages a kind is not available in, rather than offering a choice the server rejects. A QPU in Go or TypeScript is a `custom` driver, which is registerable in every language by definition.
- `qpi-ui`: Which SDK ships which device is recorded in `drivers.Spec.Languages`, so `POST /api/op/drivers/create` can refuse a pairing no SDK can honour and the dashboard's form can stop offering it. `qpi-driver devices` on the node is what confirms the list.
- `repo`: `qpi-driver/py/tests/half_imported_device.py` moved to `tests/fixtures/`: it is not a test module but an input to one, a module that raises `ImportError` on purpose.
- `qpi-driver/py`: Fixed the custom-executor example in `qpi-driver/py/README.md`, which passed a `custom_executor=` keyword that no function accepted and would have failed with `Unknown executor name 'custom'`.

## [0.1.2] - 2026-07-24

### Added

- `qpi-ui`: Added Admin Theme Management feature (RFC 0002) allowing administrators to create, preview, activate, and delete custom themes directly from the dashboard.
- `qpi-ui`: Added `themes` collection to PocketBase database for persisting theme records and custom branding configurations (logo, favicon).
- `qpi-ui`: Added `/api/theme/defaults`, `/api/theme/active`, `/api/theme/css`, and `/api/theme/js` endpoints to serve active theme configuration and injected assets.
- `qpi-ui`: Added `activeTheme` to `AppConfig` to serve as a high-performance, globally consistent in-memory cache for the active theme, avoiding expensive database queries.
- `qpi-ui`: Added React `ThemeContext` on the frontend for dynamic application of CSS variables (`rgb()` variants) and custom assets based on the active theme, gracefully falling back to a compiled-in default theme.
- `qpi-ui`: Added a Theme management UI to the Admin Dashboard (Settings -> Appearance) for customizing Design Tokens (JSON) and raw Custom CSS/JS with real-time preview functionality.
- `qpi-ui`: Optimized `OnThemeUpsert` hook to use a raw database query to efficiently deactivate sibling themes, avoiding nested hook executions.
- `docs`: Added `docs/theming.md` documentation guide for the Dashboard Theming engine.

### Fixed

- `qpi-driver/js` and `qpi-client/js`: Fix failing npm publish in GitHub actions

## [0.1.1] - 2026-07-23

### Added

- `qpi-ui`: Added the event-based driver framework (RFC 0001) with the `drivers` collection (`name`, `qpu`, `kind`, `language`, `events`, `token`, `status`, NNG ports) for registering and managing external driver processes.
- `qpi-ui`: Added `POST /api/op/drivers/create`, `POST /api/op/drivers/connect`, and `POST /api/op/drivers/toggle` endpoints for driver lifecycle, token issuance, and TLS/NNG port negotiation.
- `qpi-ui`: Added the `events` trace log collection for driver-to-UI events (`source`, `driver`, `qpu`, `type`, `payload`, `ts`) with composite index `idx_events_type_ts` on `events(type, ts)`.
- `qpi-ui`: Added background retention pruning for the `events` log (`events-retention` / `QPI_EVENTS_RETENTION`, default `720h`; `events-prune-interval` / `QPI_EVENTS_PRUNE_INTERVAL`, default `1h`).
- `qpi-ui`: Added per-driver inbound event rate limiting (`event-rate-limit` / `QPI_EVENT_RATE_LIMIT`, default `100`/sec).
- `qpi-ui`: Added the **Drivers** and **Monitoring** dashboard pages for superusers — managing driver records, copying setup snippets, viewing live status, and displaying real-time `CryostatReading` telemetry charts over PocketBase realtime.
- `qpi-ui`: Added `CryostatReading` event type and handler that persists telemetry readings to the `events` collection.
- `qpi-driver`: Added the Python driver SDK (`qpi-driver/py`, `QpiDriver` base class with `handle_event()`, `emit()`, `every()`), typed event envelope (`Event`, `EventType`), and re-expressed QPU execution as `QpuDriver` (`run_driver`).
- `qpi-driver`: Added the TypeScript driver SDK (`qpi-driver/js`, npm `qpi-driver`) with zero runtime dependencies, implementing NNG PULL/PUSH over Node's built-in `tls`.
- `qpi-driver`: Added the Go driver SDK (`qpi-driver/go`, `go get github.com/sopherapps/qpi/qpi-driver/go`) implementing `Base` over `go.nanomsg.org/mangos`.
- `qpi-driver`: Added the official `bluefors_gen1` cryostat monitoring driver across Python (`qpi-driver[cli,bluefors_gen1]`), TypeScript (`qpi-driver/builtins/bluefors-gen1`), and Go (`qpi-driver/go/qpi-driver/bluefors`), polling the Bluefors Remote Access Control API Gen. 1.
- `qpi-driver`: Added unified CLI runners (`process`, `monitor`, `version`) and systemd installer scripts (`install-systemd.sh`) across Python, TypeScript, and Go.
- `docs`: Added RFC 0001 (`docs/rfcs/0001-driver-framework.md`) and the driver framework operations runbook (`docs/driver/operations.md`).

### Changed

- `qpi-ui` & `qpi-driver`: Unified all QPU driver operations on the event-based driver framework (RFC 0001), replacing legacy direct QPU connections with event-driven `QpuDriver` instances.
- `qpi-driver`: [BREAKING] Reorganised the Python SDK repository directory from `qpi-driver/` to `qpi-driver/py/`, matching `qpi-driver/js` and `qpi-driver/go`.
- `qpi-driver`: [BREAKING] Reorganised the driver CLI around operations (`process` for QPUs, `monitor` for telemetry sensors) dispatched by `--device` with repeatable `-o key=value` options instead of the legacy `start --executor` interface.
- `qpi-ui`: Moved driver catalog definitions into a data-driven `internal/drivers` registry keyed by operation.
- `e2e`: Updated verification suite and test runners to connect all drivers via `POST /api/op/drivers/connect` and validate `bluefors_gen1` monitoring events across Python, TypeScript, and Go.
- `qpi-ui`: [BREAKING] The `QPU` struct was stripped of connection-related state that is now managed by the Driver framework. Removed `AccessToken`, `NNGCommandPort`, `NNGResultPort`, `DeviceConfig`, and `DriverVersion` fields, converting it into a pure registry entity.
- `qpi-ui`: [BREAKING] Simplified `handleQPUCreate` as it no longer generates tokens or sets up legacy executor configurations.
- `qpi-ui`: [BREAKING] Stripped removed QPU fields from `QPUCreateResponse`, `QPUUpdateResponse`, etc.
- `e2e`: Updated the backend/driver integration test (`verify.py`) to correctly retrieve authentication tokens using the new `drivers/create` endpoint rather than the removed fields on the QPU response.
- `e2e`: Fixed a local environment flakiness in the cypress script by utilizing `npm install --no-package-lock`.
- `docs`: Change driver docs folder structure to resemble that for clients due to qpi-driver folder restructure.

### Fixed

- `qpi-driver`: [BREAKING] Fixed inconsistent result dictionary shape returned by `build_qiskit_result`: `circuit_results` is now always present as a list of per-circuit experiment dicts regardless of circuit count, and redundant top-level `hex_counts` has been removed in favor of `circuit_results` and top-level `counts`.

### Removed

- `qpi-ui`: [BREAKING] Removed the legacy non-event QPU connection endpoint (`POST /api/op/qpus/connect`) and old dispatcher/listener routines. All drivers now connect through `POST /api/op/drivers/connect`.
- `qpi-driver`: [BREAKING] Removed legacy non-event driver module (`qpi_driver/driver.py`). Drivers now run via `QpuDriver` (`qpi_driver.builtins.qpu`).

## [0.1.0] - 2026-07-23

- Yanked

## [0.0.42] - 2026-07-21

### Added

- `qpi-driver`: Added `CRZGate` and `CPhaseGate` support to both the qblox and quantify executors' gate conversion.
- `qpi-driver`: Replaced the one-off Toffoli-only unitary test with at a test covering every unitary gate branch in `to_qblox_gates`/`to_quantify_gates`.

### Fixed

- `qpi-driver`: Fixed the misnamed `hex_counts` output of `build_qiskit_result`, which returned binary-string-keyed counts (duplicating `counts`) instead of hex-keyed counts: it now genuinely converts to hex via the existing `counts_to_hex` helper, and the redundant Qiskit `Result` construction (and its `build_experiment_result` helper) used only to derive that value was removed.
- `qpi-driver`: Fixed 'can only handle OpenQASM 2.0, but given 3.0' error caused by genuine error in OpenQASM 3
- `qpi-driver`: Fixed meas_level=2 counts collapsing all shots into one bin
- `qpi-driver`: Fixed meas_level=2 counts being keyed by qubit index/width instead of the classical register: a qubit measured into more than one clbit now reports each measurement as an independent bit, `measure q[i] -> c[j]` positions bits by clbit index `j` (little-endian, `c[0]` rightmost) rather than qubit index, and the bitstring width matches `num_clbits` instead of `2 ** n_qubits`.
- `qpi-driver`: Corrected the Toffoli (CCX) decomposition in both qblox and quantify executors.
- `qpi-driver`: Fixed the qblox and quantify executors only running the first circuit of a batch: `execute` now runs every circuit in `payload.circuits`, honouring per-circuit `shots` and `parameter_values`, and concatenates the results along a `circuit_index` dimension like the simulator executors.
- `qpi-driver`: Fixed multi-circuit batches with heterogeneous classical-bit/qubit widths raising or misaligning in the mock, qiskit-aer, qblox and quantify executors: per-circuit datasets are now bundled independently instead of being force-concatenated onto a shared axis, and the recorded `shots`/`n_qubits` metadata reflects what was actually used per circuit rather than the batch default or only the last circuit.
- `qpi-driver`: Fixed fragile `ThresholdedAcquisition` discrimination that relied on the backend returning exactly `1.0`: the discriminator now uses a midpoint threshold (`r >= 0.5`), correctly classifying floating-point values just below `1.0` and averaged fractional bins as `|1>`, consistent with the simulator path.
- `qpi-driver`: Fixed the qblox `FluxTunableCoupler` CZ compilation anchoring the virtual-Z phase corrections ambiguously: both `ShiftClockPhase` corrections now reference the square pulse explicitly (`ref_op=pulse`, `ref_pt="start"`) instead of the child correction implicitly chaining off the parent correction, so both are unambiguously applied at the pulse start and match the quantify executor's behaviour.
- `qpi-driver`: Removed a dead condition in the qblox `_apply_parameters`: `callable(attribute) and not hasattr(attribute, "__class__")` was always `False` since every object has `__class__`, so it never contributed to the branch decision; the condition now expresses only the check that actually applies.
- `qpi-driver`: Documented that `PhaseGate` and `RZGate` are intentionally mapped to the same `Rz` operation in both the qblox and quantify executors' gate conversion: they differ only by an unobservable global phase for a standalone gate. No functional change.

## [0.0.41] - 2026-07-20

### Changed

- `qpi-driver`: Made logging more verbose in qpi driver

### Fixed

- `qpi-driver`: Fixed invalid YAML error when loading quantify hardware json file
- `qpi-driver`: Fixed connection reset by peer errors caused by qblox-instruments >= 1.3.0

## [0.0.40] - 2026-07-17

### Fixed

- `qpi-driver`: Fixed 'Frequency settings underconstrained for freqs.clock=0. Neither LO nor IF supplied (freqs.LO=None, freqs.IF=None).'
- `qpi-driver`: Fixed 'ValueError: Operation 'CZ(qC='q1',qT='q2')' contains an unknown clock 'q1_q2.cz''

## [0.0.39] - 2026-07-17

### Changed

- `qpi-ui`: Reduced built binary size by compiling with `-ldflags="-s -w"` (stripping symbol table and DWARF debug information).

## [0.0.38] - 2026-07-17

### Added

- `qpi-driver`: Added safe-path validation for `--data-dir` and `--ca-file` to prevent writing to unsafe/unauthorized locations.
- `qpi-driver`: Added environment variable defaults for `QPI_DATA_DIR`, `QPI_CA_FILE`, `QPI_QUANTIFY_DEVICE_CONFIG`, and `QPI_QUANTIFY_HARDWARE_CONFIG` to the systemd service installer.
- `qpi-driver`: Added the FluxTunableCoupler as a CompositeSquareEdge for qblox and quantify executors

### Changed

- `docs`: Updated README files to provide detailed instructions for installing the server via pre-compiled binaries, native Linux packages (`.deb`), and using the non-interactive/interactive systemd installation script for the driver.
- `qpi-driver`: Sanitized driver/device names to replace hyphens (`-`) with underscores (`_`) for executor compatibility.
- `qpi-driver`: Added `--prerelease allow` flag to the `uv tool install` command for the `qblox` executor in `install-systemd.sh`.

### Fixed

- `qpi-driver`: Fixed permissions/directory creation bugs and improved `uv` location detection in `install-systemd.sh`.
- `qpi-driver`: Fixed standard Python test output teardown issues by gracefully unregistering the default QCoDeS instrument closing handler from `atexit` and closing them in a test session fixture while logging is still active.
- `qpi-driver`: Ensured the target parent directory for the CA certificate exists before saving the file.
- `qpi-driver`: Fixed minor code linting errors.

## [0.0.37] - 2026-06-29

### Fixed

- `docs`: Enabled mermaid diagram rendering in mkdocs material theme by adding the `pymdownx.superfences` markdown extension in `mkdocs.yml`.

## [0.0.36] - 2026-06-29

### Changed

- `qpi-ui/dashboard`: Moved the React dashboard path from `/dashboard/` to the root path `/` to improve user experience.

## [0.0.35] - 2026-06-27

### Fixed

- `qpi-ui`: Fixed a race condition where a driver failing to dial the NNG socket would incorrectly leave the QPU marked as `online`. QPU online status is now strictly determined by the NNG socket attachment lifecycle.
- `qpi-ui`: Fixed an issue where regenerating root CA certificates returned an empty fingerprint, causing authentication failures for new driver connections.

## [0.0.34] - 2026-06-27

### Fixed

- `qpi-ui`: Fixed an issue in the admin dashboard where dismissed system notifications reappeared on page refresh. Dismissals are now correctly persisted via proxy user API requests.
- `qpi-driver`: Fixed a `panic: nng is not fork-reentrant safe` error in multiprocessing environments by deferring the NNG TLSConfig initialization until after the worker processes have forked.

## [0.0.33] - 2026-06-27

### Added

- `qpi-ui`: Added `--ip-addr` (or `QPI_IP_ADDR`, or `ipAddr` in config) to explicitly specify the public IP for binding TLS sockets. The provided IP is now properly encoded in the X509 certificate's SAN IP block.

### Changed

- `qpi-driver`: Updated NNG setup logic. The driver now establishes connections using the explicit NNG IP address returned by the server via `ConnectResponse`, decoupling it from the HTTP QPI address.

### Fixed

- `qpi-ui`: Removed the `fetchHostIPs()` autodiscovery logic which caused unintended behavior when deployed behind proxies.
- `qpi-driver`: Fixed a race condition where the result sender process could attempt to read the CA certificate from disk before the main process had downloaded it.

## [0.0.32] - 2026-06-26

### Fixed

- `qpi`: Fixed various linting errors across the Go and React UI codebases.

## [0.0.31] - 2026-06-26

### Changed

- `qpi-ui`: Made the metric cards on the Overview dashboard (Active QPUs, Queue Status, Next Booking) clickable so they quickly route to their respective tabs.
- `qpi-ui`: Clarified the "Load Example" button text and icon in the Jobs Console to read "Load Bell State Example".

### Fixed

- `qpi-ui`: Fixed "authentication required" error that occurred when superusers attempted to submit a quantum job. Superusers are now transparently issued a proxy `users` record with unlimited QPU seconds to satisfy relational constraints.

## [0.0.30] - 2026-06-26

### Added

- `qpi-ui`: Light mode support for the dashboard UI with a theme toggle. Dark mode remains the default.
- `qpi-ui`: Added an admin option to delete QPUs from the QPU Registry, complete with a confirmation modal.
- `qpi-ui`: Added a user profile dropdown menu in the dashboard top bar for quick access to settings and signing out.
- `qpi-ui`: Synchronized auth sessions across tabs and between the `/_/` admin UI and `/dashboard`, automatically signing users in/out when state changes globally.

### Changed

- `qpi-ui`: Restricted the "Create QPU" and "Toggle Status" buttons in the QPU Registry tab to administrators only, while still allowing standard users to view available QPUs.
- `qpi-ui`: Conditionally hide the username and password login fields if `passwordAuth` is disabled in the PocketBase users collection.

### Fixed

- `qpi-ui`: Fixed the QPU Registry cards to properly display the Executor Driver (`executor_type`).

## [0.0.29] - 2026-06-25

### Fixed

- `ci`: Fixed failing python test step in CI.

## [0.0.28] - 2026-06-25

### Fixed

- `ci`: Fixed failing lint step in CI by updating `uv sync` flags.

## [0.0.27] - 2026-06-25

### Fixed

- `qpi-ui`: Fixed `CHANGELOG.md` versioning mismatch and correctly restored `0.0.25` entries. Bumping version to `0.0.27` due to tag immutability on `0.0.26`.

## [0.0.26] - 2026-06-25

### Fixed

- `qpi-ui`: Reverted the hiding of the "QPU Registry" dashboard tab for standard non-admin users so that they can see existing QPUs (but cannot register or toggle them).

## [0.0.25] - 2026-06-25

### Added

- `qpi-driver`: Added `install-systemd.sh` script to automate installation of the driver as a systemd background service.
- `qpi-ui`: Added an admin-only endpoint `GET /api/op/version` to retrieve the server's version.
- `qpi-ui`: Added a dynamic version label to the dashboard sidebar (visible only to admins).
- `qpi-ui`: Updated the QPU Registration success modal to generate and display a copyable `install-systemd.sh` execution snippet.
- `ci`: Added a dedicated E2E testing job (`test-systemd-installer`) in GitHub Actions to validate the systemd installation script via a Docker container.

### Changed

- Global: Renamed all instances of "Orchestrator" to "Server" (and "orchestrator" to "server") across documentation, code, and CI scripts.
- Global: Renamed all instances of "Hardware Driver" to "QPU Driver" (and "hardware driver" to "QPU driver") across the project.
- `qpi-ui`: Simplified the `README.md` introduction with a shorter description, a simpler mermaid diagram, and pulled the Quick Start section to the top.

### Fixed

- `qpi-ui`: Fixed a bug in the dashboard (`App.tsx` and `Sidebar.tsx`) where the "QPU Registry" tab was still visible to standard non-admin users.
- `qpi-ui`: Fixed a double-hashing bug in `handleQPUCreate` that caused driver connection snippet tests to fail with `401 Unauthorized`.

## [0.0.24] - 2026-06-23

### Fixed

- Updated the CHANGELOG appropriately.

## [0.0.23] - 2026-06-23

### Fixed

- `qpi-ui`: Used GoReleaser NFPM overrides to separate Debian/RPM and Alpine `init` script configurations, preventing `dpkg` installation crashes (`Default-Start contains no runlevels`) and eliminating improper `systemd` dependencies in `.apk` packages.


## [0.0.22] - 2026-06-23

### Fixed

- `qpi-ui`: Added `draft: true` to all intermediate `softprops/action-gh-release` asset upload steps to prevent them from prematurely publishing the GitHub release and triggering immutable release errors on subsequent jobs.

## [0.0.21] - 2026-06-23

### Added
- `qpi-ui`: Added macOS ARM64 (Apple Silicon) native installer packaging to the CI pipeline.

### Fixed

- `qpi-ui`: Fixed directory pathing error during the macOS binary build step in GitHub Actions.
- `qpi-ui`: Fixed macOS pkg output path evaluation and eliminated a GitHub release asset race condition between parallel macOS runners.

## [0.0.20] - 2026-06-23

### Fixed

- `qpi-ui`: Configured GoReleaser to create a `draft` release and automated publishing at the end of the pipeline to avoid GitHub's immutable release asset errors during Windows MSI and macOS PKG uploads.

## [0.0.19] - 2026-06-23

### Fixed

- `github-actions`: Updated Node.js version from 20 to 22 in CI jobs to resolve deprecation warnings.

## [0.0.18] - 2026-06-23

### Fixed

- `qpi-ui`: Added `wixl` to the apt-get install step to fix missing command during Windows MSI packaging.

## [0.0.17] - 2026-06-23

### Fixed

- `qpi-ui`: Fixed GoReleaser LICENSE path and removed invalid NFPM contents entry.

## [0.0.16] - 2026-06-23

### Fixed

- `qpi-ui`: Fixed GoReleaser v2 syntax errors and NFPM script names.

## [0.0.15] - 2026-06-23

### Changed

- `qpi-ui`: Upgraded the packaging to support 'rpm', 'apk', macOS and windows installers.

## [0.0.14] - 2026-06-23

### Fixed

- `qpi-ui`: Fixed the loading of flags which were not taking effect even when supplied.

## [0.0.13] - 2026-06-21

### Fixed

- Fixed broken links and typos in docs website.

## [0.0.12] - 2026-06-21

### Fixed

- Fixed failing deployment of documentation site in GitHub actions on push to new tag.

## [0.0.11] - 2026-06-21

### Fixed

- Fixed failing deployment of documentation site in GitHub actions

## [0.0.10] - 2026-06-21

### Added

- Documentation site configuration (`mkdocs.yml`) with automated deployments (`docs.yml`) to GitHub Pages via MkDocs Material.

## [0.0.9] - 2026-06-21

### Added

- TLS connection between the server (qpi-ui) and the driver (qpi-driver)
- `qpi-driver`: added the `--ca-file` and `--ca-fingerprint` params to the qpi-driver all
- `qpi-ui (dashboard)`: updated the code snippet shown to the user on QPU creation to include `--ca-fingerprint`.
- Comprehensive Cypress E2E test suite covering all dashboard sections:
  - **Auth & Navigation** — login error flow, role-based navigation, hash routing, back/forward sync, logout
  - **QPU Registry** — admin QPU registration (with token and command verification), toggle online/offline, regular user restrictions
  - **Jobs Console** — default form state, job submission and results, QPU dropdown filtering, empty state
  - **Bookings** — booking a time slot, validation (end before start), cancel with confirmation, visibility (user vs admin)
  - **Admin Panel** — user quota allocations, time request approval/rejection, broadcast announcements, notification badge, approval quota updates
  - **Overview & Header** — metrics row accuracy, quick-action navigation, recent jobs table, notifications panel (dismiss individual/clear all), notification targeting (broadcast vs targeted), notification dismiss isolation (per-user), header page title sync
  - **Settings & Request Time** — profile settings (email, quota, role badge), request time modal submission, validation (empty reason/seconds)
  - **Error & Edge Cases** — empty states (no jobs, no selected job, no QPUs), network failure handling (`alert()` messages), unauthorized access to `/#admin`
- Backend unit tests for `OnQPUTimeRequestUpdateRequest` hook:
  - Approval adds requested seconds to user quota; rejection leaves it unchanged
  - Non-superusers are forbidden from updating time requests
  - Already-processed (approved/rejected) requests cannot be modified

## [0.0.8] - 2026-06-17

### Added

- Added READMEs for all client packages (Go, JS, Python) and the QPU driver

## [0.0.7] - 2026-06-17

### Added

- Added logo to CLI and README

### Fixed

- Fixed error with 'make package' failing due to missing dashboard built files

## [0.0.6] - 2026-06-17

### Fixed

- Fixed failing tests on GitHub CI and reduced pocketbase's verbosity.

## [0.0.5] - 2026-06-17

### Changed

- Fixed GitHub Actions matrix for tests sleeping.

## [0.0.4] - 2026-06-17

### Changed

- `qpi-driver`: [BREAKING] Changed the format of the `element_type` in quantify.device.yml to include
  `path (str)`, `args (tuple)` and `kwargs (dict)`
- `qpi-driver`: Unskipped the e2e errors for 'quantify' executor
- `qpi-driver`: Added a log file for the driver at `data/{executor}-driver.log` during e2e tests 

### Fixed

- `qpi-driver`: Failing e2e errors for 'qblox' executor. Specifically:
  - Fixed 4ns grid rounding misalignment on custom durations for `Delay` operations.
  - Added support for OpenQASM `Delay` instructions by mapping them to `IdlePulse`.
  - Added concurrent anchoring (`ref_pt="start"`) for parallel multi-qubit Qiskit instructions (e.g., `Measure`, `Delay`, `Barrier`).
  - Handled invalid `-1` hardware acquisition dummy data thresholds during Qblox and Quantify dummy measurements.
- `qpi`: Resolved Apple Silicon macOS codesign binary integrity crashes during the E2E suite due to dynamically installed `q1asm_macos`.


## [0.0.3] - 2026-06-16

### Changed

- `qpi-ui`: Refactored the hooks.go files to make them easier to read
  

## [0.0.2] - 2026-06-16

### Added

- `qpi-ui`: Centralized API payload and database collection schemas as Go structs in the new `qpi/internal/schema` package (including `User`, `APIToken`, `QPU`, `TimeSlot`, `QuantumJob`, `QPUTimeRequest`, `Notification`, and corresponding request/response payloads).
- `qpi-ui`: Added `*FromRecord` helper mapping functions in the `schema` package to safely construct database model structs from PocketBase `*core.Record` objects.
- `qpi-ui`: Added `qpi_addr` dynamically computed field to the `/api/op/qpus/create` JSON response.
- `qpi-ui/internal/dashboard`: Updated the QPU registry tab to show a success modal upon QPU registration, including copy-to-clipboard icons for both the raw access token and a copyable `qpi-driver` start command.
- `qpi-driver`: Added support for the Qblox Scheduler (`qblox-scheduler`) package via a new `QbloxExecutor` (`qblox`).
- `qpi-driver`: Added `qblox` optional-dependencies group to `pyproject.toml` and a compatibility layer at `qpi_driver/compat/qblox.py` to gracefully handle cases where `qblox-scheduler` is not installed.
- `qpi-driver`: Created automated test suite at `qpi_driver/tests/test_qblox.py` and integrated `test-py-qblox` test target into `GitHub CI` matrix.
- `qpi-client/go`: Added `QpiAddr` field to the `QpuRecord` struct.
- `qpi-ui`: Added `FindAndDeleteOne` and `FindOneByFilter` helpers to the database query layer (`internal/db/queries.go`) to support cleaner repository queries.
- `qpi-ui`: Added validation-tagged API DTO models (`QPUCreateRequest`, `QPUCreateResponse`, `QPUToggleResponse`, `DispatchPayload`, `JobResultUpdate`) under `internal/api/schema.go`.

### Changed

- `qpi-ui`: Integrated the centralized `schema` structs into all custom REST controllers and handlers inside the `api` package, replacing duplicate local private struct definitions.
- `qpi-driver`: [Breaking] Removed deprecated `-H`/`--host` and `-P`/`--port` options from CLI and `run_driver` in favor of `--qpi-addr` / `-a` (env: `QPI_ADDR`, default: `http://127.0.0.1:8090`).
- `qpi-client`: Updated Go/Python/JS client E2E test suites to use the `QPI_ACCESS_TOKEN` environment variable.
- `qpi-ui`: Refactored all HTTP REST handlers (`handleNotificationDismiss`, `handleTokenDelete`, `handleQPUConnect`, `handleQPUToggle`) to use database models and generic queries instead of raw `core.Record` objects.
- `qpi-ui`: Removed reflection from `internal/db/queries.go` by refactoring methods to accept a pre-allocated model destination interface, improving performance.
- `qpi-ui`: Separated access token lookup and status validation in `handleQPUConnect` to correctly return `401 Unauthorized` for invalid tokens and `403 Forbidden` for disabled QPUs.
- `qpi-ui/internal/dashboard`: Updated `App.tsx` quantum job submission callback to extract `id` instead of `job_id` from the backend response.

## [0.0.1] - 2026-06-14

### Added

- `qpi-ui`: Added `notifications` collection with admin-only CRUD, user visibility rules, broadcast/targeted targeting, time-window filtering, and per-user dismiss support via `POST /api/notifications/{id}/dismiss`.
- `e2e/verify.py`: Added the `test_notifications_crud` E2E test to verify broadcast/targeted visibility, time-window filtering, per-user dismiss, and admin-only CUD enforcement.
- `qpi-ui`: Added `enabled` boolean field to `qpus` collection to allow administrators to toggle QPU drivers on and off.
- `qpi-ui`: Added an update event hook on the `qpus` collection that cancels/stops dispatcher and listener goroutines (and sets status to `"offline"`) when `enabled` is set to `false`, and starts goroutines (and sets status to `"online"`) when `enabled` is set to `true`.
- `qpi-ui`: Enforced `enabled` check in the `/api/op/qpu/register` route to reject registration of disabled QPUs with a `403 Forbidden` response.
- `e2e/verify.py`: Added the `test_qpu_toggle_switch` E2E test to verify the QPU disabled/enabled lifecycle, goroutine lifecycle, and registration blocking.

- `qpi-ui`: Added authenticated CRUD rules and validation hooks for the `qpu_time_requests` collection, supporting user requests, admin approvals/rejections, automatic QPU seconds crediting, and handled request immutability.
- `qpi-ui`: Added authenticated CRUD rules and validation hooks for `time_slots` collection, implementing interval order, overlap checks, auto-population of owner, past booking/update/delete restrictions, and admin bypass capability.
- `qpi-ui`: Added admin-only `PATCH /api/admin/users/{id}` endpoint for superusers to update `qpu_seconds` and `api_tokens` on any user record.
- `qpi-client/py`: `QPIBackend.run()` now supports `parameter_values` kwarg for parameterized circuit execution, automatically binding parameters and forwarding ordered values to the API payload.
- `qpi-driver/tests`: Added `@pytest.mark.skipif` decorators to CLI and quantify tests so they gracefully skip when optional dependencies (`typer`, `quantify_scheduler`, `qblox_instruments`) are not installed.
- `Makefile`: Added granular `test-py-base`, `test-py-cli`, `test-py-aer`, and `test-py-quantify` targets for testing each `pyproject.toml` extra in isolation.
- `qpi-driver`: Added an abstract `process_result()` method to the `Executor` interface, letting executors handle their own data processing (e.g. state discrimination, IQ memory formatting) directly in the worker process.
- `qpi-driver`: Implemented state discrimination, average/single IQ memory formatting, and raw trace handling in `MockExecutor`, `QiskitAerExecutor`, and `QuantifyExecutor`.
- `qpi-driver`: Support for `ThresholdedAcquisition` protocol in `QuantifyExecutor` when threshold/rotation parameters are defined on device elements, automatically falling back to software discrimination via `SSBIntegrationComplex`.

### Changed

- `qpi-ui`: Default `qpu_seconds` for new users changed from `1000` to `0`. Users must now be granted QPU time explicitly by an admin via the `PATCH /api/admin/users/{id}` endpoint. The `OnRecordCreate` hook that previously set the default has been removed.
- `qpi-driver`: Renamed the `translator` process to `result sender` and simplified it to forward processed dicts via NNG PUSH directly from a queue, eliminating intermediate `.pkl` filesystem serialization overhead.


