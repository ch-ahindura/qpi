# Change log

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/)
and this project follows versions of format `{year}.{month}.{patch_number}`.

## [Unreleased]

### Fixed

- `qpi-driver/py`: a routine's swept setpoints belong to the target rather than to the
  routine. They were kept as `self._frequencies` and read back in `analyse`, so a routine
  measuring several targets in one schedule fitted every one of them against whichever
  target built last — a plausible curve against the wrong axis, not an error.

- `qpi-driver/py`, `qpi-ui`: the calibration graph draws the node a walk is on. A progress
  event only ever fired after a target finished, so a single-target node went straight from
  `pending` to `done` and the `running` style was unreachable. The driver now reports the
  targets it is about to measure, and a target skipped for a missing prerequisite reports
  too — a node whose every target was blocked stayed `pending` for the rest of the run.

### Added

- `qpi-driver/py`: a grouped routine measures its targets in one schedule, so a group
  costs one arm-and-wait cycle instead of one per target. Each target names its own
  acquisition channel and is fitted from that channel alone, so no routine's `analyse`
  changed. `allxy`, `allxy_check`, `readout_discrimination` and `readout_fidelity` have
  opted in; the rest keep running one target at a time until they do.
- `qpi-driver/py`: `parallel` in `calibration.yml` groups a routine's targets into sets
  that can be measured at once, coloured from the coupling graph — `qubit_spacing`,
  `edge_spacing`, `max_group`, `exclude`, or explicit `groups`. Off unless the file says
  otherwise. A group is narrowed further when the instruments cannot play it at once —
  readout clocks outside one LO band, amplitudes that would clip, more clocks than the
  module has sequencers — with the measured figure and the ceiling in the message.
- `docs`: RFC 0009 — Parallel Calibration. Designs grouping a routine's targets into sets
  that can be measured in one schedule, from a colouring of the coupling graph rather than
  a hand-written list, and records why concurrent submission to one cluster cannot work —
  the scheduler's `start` disarms every sequencer in the cluster before arming its own.
  Names the defect it starts by fixing: the calibration graph has a `running` style and a
  legend entry no walk has ever reached, so a single-target node draws as `pending` and
  then `done`, never as running.

## [0.4.2] - 2026-08-16

### Added

- `qpi-driver/py`: `readout_integration_time` calibrates `measure.integration_time` by
  sweeping the acquisition window and taking the one that separates `|0>` and `|1>` best.
  It was a config constant every discriminating node inherited, and it is the last free
  parameter in readout SNR. Holds its current value when no window beats it by more than
  shot noise.
- `qpi-driver/py`: `rabi_12` carries its trace on success as well as on refusal, and `rb`
  reports `decay_observed` — how much of the decay its deepest sequence actually saw, since
  `r` is extrapolated from the rest. A chip reporting 0.15% error per gate had seen 17.6% of
  a decay, below what its own T1 allows and 34x better than `allxy_check` on the same run.
- `qpi-driver/py`: `readout_operating_point` reports the *magnitude* contrast across its
  sweep, and how much of it survives at the point it picks. It optimises complex
  separation, which is right for a discriminator and invisible to the `signal_of` magnitude
  nearly every other node reads — and nothing measured the difference.
- `qpi-driver/py`: a routine refused by a guard keeps the sweep behind the refusal, so the
  report carries the trace and not only the sentence. It is marked as a refusal and is not
  attributed any parameter.
- `qpi-driver/py`: `allxy_check` reports its normalised response alongside the rms, so the
  21 pairs can be read after the single-qubit chain finishes. `allxy` runs before
  `fine_amplitude` and `fine_amplitude_90`, so it cannot show whether either helped.
- `qpi-driver/py`: `fine_amplitude_90` measures the pi/2 amplitude and writes it to a new
  `fine.amp90` on `CalibratedTransmon`. Both schedulers derived a pi/2 from `amp180` by linear
  interpolation, so a drive that compresses near full scale left an AllXY error nothing could
  correct.
- `qpi-driver/py`: the simulator has three-level physics for the 1-2 transition, so `rabi_12`
  can be tested without a chip. The sqrt(2) ladder between the two transitions comes out of
  the model rather than being written into it.
- `qpi-driver/py`: a routine may set its own `timeout_s` in `calibration.yml`, overriding the
  global `routine_timeout_s`. One ceiling had to be set for the slowest node, so it could not
  also catch a fast one hanging.
- `qpi-driver/py`: a quantify routine logs how long its schedule should take before
  running it, and its Q1ASM at debug level. A timeout previously gave no way to tell a
  schedule that needed longer from one that was stuck.
- `qpi-driver/py`: a timed-out quantify routine names the module and sequencer that did
  not stop, its state and its flags. qblox-instruments raises with a bare sequencer
  index, so the operator could not tell which of twelve modules had hung.
- `qpi-driver/py`: an end-to-end test asserts the benchmarked gate error against the one
  the simulator was given, so a calibration that leaves a gate wrong now fails the suite
  instead of clearing a fixed fidelity threshold.
- `qpi-driver/py`: a calibration writes a `*.provenance.yml` beside the device config
  recording which routine last measured each parameter, and when. A device config could
  not say whether a value was measured or typed in, so every reader had to assume the
  better case.
- `qpi-driver/py`: a calibration report names the inputs nothing has ever measured, per
  target and per routine. A run built on a hand-supplied frequency previously read exactly
  like one built on a measured one.
- `qpi-driver/py`: a skipped routine reports which parameters it left unconfirmed and when
  they were last measured, and a run whose producer for a never-measured parameter is
  switched off says so before the walk starts.

### Changed

- `qpi-driver/py`: `f12_spectroscopy` keeps the f12 already measured on a qubit when no
  drive power in the sweep resolves the line, rather than refusing. Only where a prior
  exists — a chip that has never resolved it still fails, because there is nothing to keep.
- `qpi-driver/py`: a node that measured something imprecise now reports it and flags
  `unresolved`, instead of refusing. `rb` and `t2_echo` write no device parameter, so a
  wide error bar is a fact about the chip and withholding it published nothing; `drag` and
  the fine-amplitude nodes keep the value they would have refined rather than writing a
  correction their own model could not describe.
- `qpi-driver/py`: `readout_integration_time` sweeps the whole reachable range of windows
  rather than a few factors either side of the configured one, so the window it writes is
  an optimum it bracketed instead of the edge it stopped at — and it says so when the best
  window is the hardware ceiling.
- `qpi-driver/py`: the 1-2 ladder guard drops its factor-of-two special case and judges a
  resolved sweep on periods and population swing alone — both properties of the sweep
  rather than of any chip. `ef_ladder` measures the same relation directly, so the modelled
  prediction is deliberately the weaker witness.
- `qpi-driver/py`: `rabi_12` now judges an off-ladder 1-2 pi by how much population it
  swings — `rabi` records its own contrast for the comparison — instead of refusing every
  amplitude a factor of two off the sqrt(2) ladder. A resolved sweep that moves the full
  population is turning a pi somewhere, so the ladder constant is the likelier thing to be
  wrong; refusing it cost four downstream nodes on a chip that measured cleanly.
- `qpi-driver/py`: `fine_amplitude_12` refines the EF pi on the ordinary 0-1 readout, by
  mapping |1> back the way `rabi_12` already does, and `three_state_operating_point` now
  depends on it. It used to read at the three-state point and sit *behind* that node — a
  deadlock, since populating |2> needs the refined pi that this node produces. On one chip
  it and the three nodes behind it never ran once in six attempts.
- `qpi-driver/py`: a guard that can tell a poor measurement from no measurement now passes
  the poor one and marks it degraded, rather than refusing and taking every node downstream
  with it. `require_resolved_curve` takes its floor from what noise fakes over that many
  points instead of a fixed 3x — noise reaches 3.5 at 21 points and 1.8 at 81, so the
  constant was wrong in both directions — and `three_state_operating_point` from the
  separation at which its closest pair reaches `MIN_ASSIGNMENT_FIDELITY`. One `rabi_12` at
  2.5x had been costing four nodes that each carry their own guard.
- `qpi-driver/py`: a schedule whose pulses outlast `routine_timeout_s` raises its own
  wait rather than failing, and says so. The ceiling bounds a sequencer that never
  stops; a 59 s punchout under a 30 s ceiling was failing for being large.
- `qpi-driver/py`: a routine running several schedules under one ceiling is judged on
  their summed allowance rather than the last one's. `qubit_spectroscopy`'s search is
  three acquisitions, and the last alone would fail a routine that never exceeded its
  allowance once.
- `qpi-driver/py`: every routine reads the device before its acquisition rather than
  after it (RFC 0007 §11). Six nodes read a parameter in `analyse`, which describes a
  sweep that had already happened and is too late to check as a prerequisite.
- `qpi-driver/py`: a routine declares the device parameters it `reads`, the counterpart
  of the `updates` it already declared (RFC 0007 §11). A test derives the true set from
  an instrumented `read_path` and fails a declaration that is short of it.
- `qpi-driver/py`: the walk skips a routine whose input this run failed to produce,
  naming the routine to blame, instead of measuring an uncalibrated chip (RFC 0007 §11).
  One failed `qubit_spectroscopy` cost six runs of debugging six downstream nodes that
  had each fitted the noise of a qubit still in its ground state.
- `repo`: Cleaned up and refactored `Makefile`.
- `repo`: Cleaned up `.github/workflows/ci.yml`.
- `qpi-driver/py`: Optimized `test-py-loop` execution speed with
`@functools.lru_cache` to `_cached_scqubits_eigenvals` in `transmon.py`.

### Fixed

- `qpi-driver/py`: `rb` pins its decay asymptote at `1/2^n` instead of fitting it, which
  is what makes the rate measurable — with it free the amplitude and the rate are
  inseparable and the answer comes off whatever bound stops the fit. `t2_echo` can no
  longer widen its delays past six times T1, where a rising signal is drift rather than an
  echo.
- `qpi-driver/py`: a Hahn echo is flagged past 2.4x T1 rather than 3x, and a spectroscopy
  line fitted wider than the window it was swept in is flagged too. Both were reporting
  numbers no measurement supports — a T2 of exactly 3x T1, and a 107 MHz linewidth across a
  20 MHz sweep — without saying so.
- `qpi-driver/py`: `readout_integration_time` stops the window at the readout pulse plus a
  couple of resonator ring-down times, rather than at the instrument's limit. Integrating
  past the pulse adds noise with no signal; on one chip it chose twice the pulse length and
  every magnitude-based node lost contrast for it.
- `qpi-driver/py`: `fine_amplitude_12` can act on the shortening its own refusal asks for,
  as `fine_amplitude` already could. Without it the refusal named a remedy nothing applied
  and the node could never run on a chip whose ef sweep overran.
- `qpi-driver/py`: `t2_echo` snaps its delays so that half of one lands on the hardware
  grid, which a window scaled from a measured T1 otherwise misses — the schedule compiled
  until qblox refused a time value.
- `qpi-driver/py`: `fit_rb_decay` refuses an amplitude a hundred times its own span rather
  than two hundred. A degenerate fit was squeezing under the old wall and reporting a
  per-gate error three orders below what AllXY measured on the same chip.
- `qpi-driver/py`: `fine_amplitude` judges whether the amplified rotation overran from the
  span of the data rather than from its own fitted slope, which under-reported by exactly
  the amount that made the guard necessary. A per-pulse error that does not clear its own
  standard error is now written as no correction instead of as noise.
- `qpi-driver/py`: `f12_spectroscopy` accepts an `anharmonicity_range` override, so a
  transmon deliberately built outside the usual -400 to -150 MHz is a config fact rather
  than a refusal.
- `qpi-driver/py`: `rb` scores survival against measured `|0>` and `X|0>` references
  instead of scaling to the sweep's own extremes, which forced one depth to exactly 0 and
  another to exactly 1 and could not tell a decay from a rise. No circuit count could fix
  it — the endpoints were arithmetic.
- `qpi-driver/py`: `t2_echo` sizes its delays from the measured T1 rather than a fixed
  100 us window. A Hahn echo can reach `2*T1`, so a fixed window truncates the decay on any
  chip whose T1 outruns it and `fit_t2` then refuses a decay more shots cannot bound.
- `qpi-driver/py`: `fit_drag` refuses a sweep whose rise across its whole beta range is
  under three times the scatter about the fitted line. The root of a line through noise
  landed inside the sweep and was written to every pulse afterwards.
- `qpi-driver/py`: the 1-2 ladder prediction was out by exactly two — `EF_ENVELOPE_AREA`
  read quantify's `nr_sigma` as spanning the whole DRAG pulse rather than each side of
  centre. It refused pulses sitting on the ladder; a test now pins the constant to the
  integrated waveform instead of to the arithmetic.
- `qpi-driver/py`: `fine_amplitude` and `fine_amplitude_90` refuse a sweep no straight line
  passes through. The demodulated signal is bounded at one, so its scatter has an absolute
  scale — a chip whose points sat 0.35 off their own fitted line still reported a quarter
  turn correct to 0.03%, and wrote the amplitude every gate afterwards uses.
- `qpi-driver/py`: `ramsey` measures which of the fringe's two roots is the chip's instead
  of assuming the smaller one. A fringe is a magnitude, so the artificial detuning only
  signs the correction while it is the larger of the two — past that `ramsey` moved f01
  6.18 MHz off where the other root sits 60 kHz from the chip's working value.
- `qpi-driver/py`: `three_state_operating_point` sizes its frequency sweep from the measured
  resonator linewidth instead of a 6 MHz constant, at the 1.8 linewidths that constant
  encoded. On a 327 kHz resonator 6 MHz is eighteen linewidths, so four of its five points
  sat where nothing comes back — the same way a constant span once broke
  `readout_operating_point`.
- `qpi-driver/py`: `t2_echo` refuses a T2 above the `2*T1` ceiling a Hahn echo cannot
  exceed, and `t1` now keeps its result on the element for it to read. A chip reported
  201 us of T2 against a 32.8 us T1 — 3.07x the ceiling — and every other guard passed it.
- `qpi-driver/py`: `rabi` no longer accepts a pi amplitude up to 10% above the top of its
  own sweep. A chip fitted 0.5060 against a sweep stopping at 0.5 and wrote it, where
  `ef_ladder` measured 0.1647 on the same grid — every node downstream then calibrated
  against a pulse turning three times too far. It escalates to a wider sweep instead.
- `qpi-driver/py`: the CZ's virtual-Z corrections cancel the phase the gate leaves instead
  of doubling it. `conditional_phase` wrote the measured fringe phase where it needed minus
  it, so every CZ left 151.7 degrees on the control and `interleaved_rb` came back as
  scatter. The conditional phase itself was unaffected, being a difference of two fringes.
- `qpi-driver/py`: an error message reaches QPI-UI without the Q1ASM program a library may
  have embedded in it. qcodes puts the value being set into a failed set's message, and the
  value quantify sets on a sequencer is its program — so one `Assembly failed` produced a
  2.4 MB error, a 1.37 MB payload, and a record the server refused for exceeding the 1 MB
  its JSON field takes. The calibration had run; its request stayed `running` for ever.
- `qpi-driver/py`: `drag`, `fine_amplitude_12`, `resonator_punchout` and `flux_spectroscopy`
  can actually be widened. Each kept its setpoints under a name escalation does not look
  for, so widening found nothing and the refusal named the range already swept — `drag`
  failed run after run with an optimum of -0.614 against a swept +/-0.2 and never widened.
- `qpi-driver/py`: `fine_amplitude_90` shortens onto odd repetition counts, and no
  shortening goes below the four points a fit takes. It cut its ladder to two and then
  refused for having two, spending its retry to complain about its own sweep.
- `qpi-driver/py`: `resonator_punchout`, `flux_spectroscopy` and `qubit_spectroscopy` run a
  2-D grid as one schedule per group of rows. `MAX_SWEEP_POINTS` bounds the points in a
  sweep, and a 2-D schedule is rows times points — eleven rows of a 700-point sweep is nine
  times the instructions a sequencer takes.
- `qpi-driver/py`: the timeout warning names the duration it actually compares. "needs
  256.0s of pulses, more than the 300s ceiling" was self-contradictory: 256s rounds to the
  instrument's 60s grid and gains a minute for arming, so what did not fit was 360s. It now
  says so, and names the 240s the pulses have to come under rather than the 300s ceiling.
- `qpi-driver/py`: RB runs a sweep too large for one schedule as several and combines them,
  instead of capping how hard it may average. A sequencer takes 12288 instructions and RB's
  cost is per gate, so 2413 Cliffords compiled to 1.13 MB of Q1ASM and would not assemble.
  The split is exact: the mean of a partition is the mean of the whole.
- `qpi-driver/py`: `ef_ladder` measures the sqrt(2) ladder directly, playing `rabi_12`'s own
  pulse on the 0-1 clock so the envelope and duration cancel. It writes nothing; it says
  whether an ef amplitude that misses the prediction misses the *ladder*.
- `qpi-driver/py`: `t1` and `t2_echo` widen their delays when the fitted coherence time
- `qpi-driver/py`: a benchmark that runs its own measurement loop reaches `report.benchmarks`.
  It previously appeared in `routine_results` and nowhere else, so it looked like it had run
  while the drift check compared against nothing.
- `qpi-driver/py`: a fit refused for scatter carries the sweep it refused, as the other
  refusals already did.
- `qpi-driver/py`: `t2_echo` and `t1` widen their delays when the fitted coherence time
  lands past the window, instead of refusing. A chip fitted 2.12 ms of T2 over a 100 us
  sweep and failed, because that guard named no axis for escalation to act on.
- `qpi-driver/py`: `rb` and `interleaved_rb` average more circuits per depth when the decay
  cannot be told from the scatter around it. "Average more circuits per depth" was already
  the advice the refusal gave, and nothing acted on it.
- `qpi-driver/py`: `drag` widens its beta sweep when the optimum lies outside it, as
  `drag_12` already did. A chip whose optimum was -0.4803 against a swept +/-0.2 refused a
  fit that had found its answer, leaving every node after it on an uncorrected pulse.
- `qpi-driver/py`: `fine_amplitude` and `fine_amplitude_90` shorten their repetition counts
  when the amplified rotation outruns the linearisation, instead of failing. How many
  repetitions the fit can take depends on the error it is measuring, so no default is right
  in advance.
- `qpi-driver/py`: an RB fidelity fitted off a straight line is refused. The amplitude is
  bounded to 200x the survival's own span and a fit that reaches that stop is rejected: a
  chip reported 0.9999887 and 0.9999978 — thirty to three hundred times better than its T1
  allows — from an amplitude of -807 and -4109 on a survival normalised to [0, 1].
- `qpi-driver/py`: `rabi_12`'s ladder guard accepts a resolved oscillation however far off the
  sqrt(2) ladder it sits, and refuses only a sweep holding less than one period. It exists to
  catch a cosine fitted to a partial rotation, which shows fewer oscillations than the sweep
  and never more — it had been refusing a clean three-and-a-half-period measurement.
- `qpi-driver/py`: the fine-amplitude fit takes an intercept instead of being pinned through
  the origin, and refuses a sweep whose rotation accumulates past a radian. Two runs of an
  unchanged pi/2 pulse reported errors twelve times apart because a real baseline offset was
  being absorbed into the slope.
- `qpi-driver/py`: an EF pulse defaults to the length of the 0-1 pulse rather than to a
  20 ns constant, and `rabi_12`'s ladder guard scales by the two durations instead of
  assuming they match. Against an `rxy.duration` of 56 ns the old default put the 1-2 pi at
  2.8x the 0-1 amplitude, past the top of the sweep, and the guard blamed the drive.
- `qpi-ui`: the fidelity card shows the measured gate fidelity rather than the lowest number
  in the payload. A run's `readout_fidelity` of 92.5% was displayed as being below the 99.9%
  one-qubit gate threshold while randomised benchmarking sat unread beside it.
- `qpi-driver/py`: `rabi_12` maps the qubit back to the ground state before measuring, so the
  1-2 oscillation appears in the population the readout is tuned to resolve. It previously
  asked a 0-1 discriminator to tell the two upper levels apart, and fitted a pi pulse six
  times too small from a trace that barely moved.
- `qpi-driver/py`: `ramsey` re-measures after correcting the qubit frequency, until the residual
  detuning is below what its own sweep can resolve. A single pass measured the detuning with
  the uncorrected frequency in the drive, so it landed near the answer rather than on it.
- `qpi-driver/py`: a reported fidelity is the worst of the protocols that measure a gate
  fidelity, and `allxy_check`'s diagnostic score no longer outvotes it. The default 0.999
  threshold demanded an AllXY rms of 0.001, so a drift check with AllXY enabled fired on every
  run of every chip.
- `qpi-driver/py`: `three_state_operating_point` refuses a point whose closest two clouds its
  own consumer would reject, instead of writing one and letting
  `three_state_discrimination` fail.
- `qpi-driver/py`: `drag_12` widens its beta sweep when the optimum lies outside it, and a
  widened sweep that is symmetric about zero stays symmetric — it previously dropped the whole
  negative half, which is where `drag` had measured its own optimum.
- `qpi-driver/py`: `rabi_12` refuses a 1-2 pi amplitude that the measured 0-1 one says cannot
  be one. A cosine fitted to a partial rotation reports a smaller amplitude with no sign
  anything is wrong, and the whole EF chain then measured a qubit still in the first excited
  state.
- `qpi-driver/py`: a widened drive-amplitude sweep stops at full scale instead of asking the
  AWG for more than it has. `rabi` starting at half scale and escalating past it compiled to
  a gain of 1.05, which the compiler refused while naming a pulse rather than the routine.
- `qpi-driver/py`: `qubit_spectroscopy`'s widening pass drives at the strongest power the
  run would try anyway, derived from `drive_amps`, rather than a constant that fell out of
  step with it. Raising `drive_amps` previously left the search probing weaker than the pass
  it exists to feed.
- `qpi-driver/py`: a sweep axis written as `4e-9` reaches the schedule as a number rather
  than the string PyYAML actually parsed it to, and a device config frequency written as
  `5.318e9` loads as one too. Both forms need a decimal point *and* a signed exponent to be
  numbers, which is invisible on the page.
- `qpi-driver/py`: `calibration.example.yml` reached full readout scale in
  `resonator_punchout`, sets DRAG in seconds rather than ten orders of magnitude out, and
  spells its exponents so YAML reads them as numbers.
- `qpi-driver/py`: 22 routines now declare the qubit frequency and pi-pulse amplitude their
  gates need, so a failed `qubit_spectroscopy` skips everything behind it. One dead
  frequency previously produced eight separate failures, each looking like its own fault.
- `qpi-driver/py`: `resonator_punchout` sweeps readout power to full scale rather than
  stopping at half, so punch-through is reachable on an attenuated readout line. On a chip
  with 20 dB of output attenuation the old ceiling was ~26 dB short of finding it.
- `qpi-driver/py`: a two-qubit routine is skipped when either of its qubits failed to
  calibrate, rather than measuring a gate through an endpoint that was never brought up.
- `qpi-driver/py`: `resonator_spectroscopy` widens and re-runs its own sweep when the
  resonator is outside the window, or samples it harder when the line is thinner than the
  grid. The root of the calibration graph previously stopped the whole chip instead.
- `qpi-driver/py`: an escalating sweep is capped at 700 points rather than 900. A frequency
  sweep costs three operations per point, measured at 15 Q1ASM instructions, so 900 built a
  program over the sequencer's 12288-instruction ceiling — which quantify only warns about.
- `qpi-driver/py`: `qubit_spectroscopy`'s confirming sweep holds the resolution the narrow
  pass asks for instead of a fixed 41 points, so a line the search power-broadened is no
  longer refused for being thinner than the step of the sweep sent to measure it.
- `qpi-driver/py`: a readout whose single shots are assigned little better than by chance is
  refused rather than written. `readout_operating_point` previously wrote an operating point
  it had measured at 53% assignment fidelity.
- `qpi-driver/py`: `allxy` and `allxy_check` refuse a response whose own reference plateaus
  are indistinguishable, instead of normalising noise to full scale — which had `allxy_check`
  reporting a fidelity of 0.53 to the drift check. `allxy_check` also now normalises the way
  `allxy` does, rather than by min and max, which inverts on half of all readout chains.
- `qpi-driver/py`: the simulator's `rabi`, `t1`, `t2_echo` and `ramsey` carry the drive
  detuning, so a wrong `clock_freqs.f01` costs a calibration its contrast. They built
  their Hamiltonian on resonance whatever the device was configured for, which is why no
  suite could fail for the reason a chip 302 MHz out of config did.
- `qpi-driver/py`: the spectroscopy roots judge a fitted line by how far its curve travels
  against the scatter around it, not by a signal-to-noise floor. 16% to 55% of pure-noise
  fits cleared the old floor of 3, because that ratio divides a fitted parameter by the
  residual and an optimiser can inflate it without limit.
- `qpi-driver/py`: `qubit_spectroscopy` only lets a drive power that shows a line set the
  broadening reference the other powers are judged against. A row that converged, cleared
  the sweep step and still showed nothing became the narrowest, and the 2x bound then
  rejected every power that did show the line.
- `qpi-driver/py`: a calibration finds a chip known only from its design document, with
  no sweep supplied. The fixture claims f01 = 5.0 GHz against a transmon at 5.21 GHz, and
  the driver recovers it to under a megahertz. `ramsey`'s default sweep is sized for both
  its constraints at once, and `rabi` reaches past half scale only when the fit says the
  pi pulse is above it.
- `qpi-driver/py`: `t1`, `t2_echo` and `ramsey` lengthen their delays and try again when
  the fit says the decay was never seen in the window, rather than failing. Bounded at
  three attempts, and an operator who named the delays themselves is not overruled.
- `qpi-driver/py`: the two excited-state resonator sweeps size their spans from the
  measured linewidth rather than a 20 MHz constant, which was 6 linewidths on one chip
  and 54 on another.
- `qpi-driver/py`: `f12_spectroscopy` refuses an anharmonicity that is not a transmon's,
  in its prior and in what it fits. A device file carrying `f12 = 4.8e9` against an f01
  near 4.7 GHz implied a *positive* anharmonicity on four of five qubits, and nothing
  objected.
- `qpi-driver/py`: `readout_operating_point` sizes its sweep from the measured resonator
  linewidth rather than a 2 MHz constant. That constant was 5.4 linewidths on a 370 kHz
  resonator, which put its outer setpoints off resonance altogether and it chose one.
- `qpi-driver/py`: a `CalibratedTransmon` keeps the resonator linewidth
  `resonator_spectroscopy` measured, and the two resonator checks judge against it
  instead of a 2 MHz constant (RFC 0005 §13). On a chip whose resonator is 370 kHz wide
  that constant was five times too wide, and nothing recorded the measured value.
- `qpi-driver/py`: `rabi` sweeps drive amplitude to full scale rather than to half of it,
  so a pi pulse above 0.5 can be found. `require_in_range` cannot catch one that is
  missing for being too large, since it checks the fitted value lies *inside* the swept
  range, and a chip whose own calibration used 0.5683 returned a flat Rabi every run.
- `qpi-driver/py`: `qubit_spectroscopy` sizes the sweep that confirms a searched-out line
  from the width the search measured, rather than reusing the operator's span. That span
  said where to look, and once the search has answered it is spent — re-centring a
  600 MHz window steps 10 MHz across a line about as wide.
- `qpi-driver/py`: every spectroscopy sweep is trimmed to the frequencies its port can
  actually be driven at — its LO plus or minus the module's intermediate-frequency
  limit. Asking outside it failed compilation with `Attempting to set NCO frequency`,
  naming neither the routine nor the setpoint.
- `qpi-driver/py`: the simulated backend carries the allowance the DAG judges a routine
  by, so the whole simulated calibration walks again. Without it every node of it died
  with `AttributeError: 'SimulatedBackend' object has no attribute 'last_allowance_s'`.
- `qpi-driver/py`: a quantify tuner or executor resets the cluster when it opens one.
  Sequencer offsets, NCO frequencies and `sync_en` survive a reconnect, so the driver
  inherited whatever the last process left emitting — which held a qubit in a
  mixture that made X the identity, and deadlocked `wait_sync` before that.
- `qpi-driver/py`: a quantify tuner or executor stops the cluster after every run,
  including a failed one. Only `stop` clears `sync_en` on the modules a schedule did
  not use, so one left in the sync network by an earlier routine hung every later one
  on `wait_sync`, at any `routine_timeout_s`.
- `qpi-driver/py`: the quantify tuner finds its cluster again.
  `InstrumentCoordinator.components` holds component *names*, so reading `.instrument`
  off them found nothing and `coupler_anticrossing` could not open a bias source inside
  the cluster.
- `qpi-driver/py`: `ramsey` rounds its delays to the 1 ns grid, as `ramsey_12` already
  did. Its default sweep steps 249.9 ns and the node never compiled.
- `qpi-driver/py`: the spectroscopy roots refuse a line no more than 3x above the
  residual scatter, and `resonator_spectroscopy` is guarded at all. A 1.53 MHz fit at
  snr 1.32 wrote an f01 5 MHz out, which put `ramsey_12` 1.5 MHz off and cost the run.
- `qpi-driver/py`: the two excited-state resonator sweeps are guarded too, and report
  the ground frequency they differenced against plus their own spectrum. A stale
  reference had them reporting a 186 kHz dispersive shift on a chip whose real shift
  was under 1 kHz.
- `qpi-driver/py`: `rabi` refuses an oscillation no taller than the scatter it was
  fitted through, rather than writing the `amp180` it implies. A first sweep through a
  starved readout wrote 0.0134 where the chip's calibrated value was 0.5683, and the six
  runs after it played an X pulse that rotated five degrees — leaving every 0-1
  measurement in the graph blind, and self-perpetuating, since a dead X gate guarantees
  the next Rabi sweep is flat.
- `qpi-driver/py`: `t1`, `t2_echo` and `ramsey` refuse a curve no taller than the
  scatter it was fitted through. `require_in_range` allowed a time constant ten times
  the window, so a flat sweep returned T1 = 169 us from a rising curve, and a ramsey
  with no fringe moved f01 by 173 kHz.
- `qpi-driver/py`: `fine_amplitude` and `fine_amplitude_12` refuse a demodulated sweep
  far past the bound their own model sets, rather than writing the `amp180` it implies.
  A readout that was not resolving the qubit gave a sweep reaching 144 where one is the
  maximum, and the slope through it set the amplitude every X pulse used.
- `qpi-driver/py`: `rb` and `interleaved_rb` refuse a decay no deeper than the scatter
  it was fitted through. Three consecutive runs reported 0.99999, 0.941 and 0.586 from
  non-monotonic noise, and the drift check compared them against a threshold.
- `qpi-driver/py`: `resonator_spectroscopy_excited` refuses a dispersive shift under 5%
  of the resonator linewidth, which no readout can resolve. A chip whose `f01` sat an
  anharmonicity away from its real transition reported 0.05% to 0.5% for six runs, while
  every node after it fitted the noise of an idle qubit.
- `qpi-driver/py`: `qubit_spectroscopy` widens to a 600 MHz search when no line turns up
  near the configured `f01`, rather than refusing — the configured value is a prior, not
  an answer. A chip 302 MHz from its design frequency gave six runs of "no drive power
  resolved a line", and the operator had to supply by hand the number the node measures.
- `qpi-driver/py`: `flux_spectroscopy` and `cz_chevron` decline a chip whose flux
  reaches the couplers rather than the qubits, instead of failing with
  `KeyError: 'q0:fl was not found in the connectivity.'`. `cz_chevron` was missing the
  architecture test both its parametric counterparts already make.

## [0.4.1] - 2026-08-07

### Fixed

- `qpi-driver/py`: a `quantify` tuner or executor creates its data directory's parents.
  quantify-core's own `mkdir` is not recursive, so `bin/data` in a fresh checkout
  failed at construction.

## [0.4.0] - 2026-08-06

### Added

- `qpi-driver/py`: a calibration publishes the graph it is about to walk on a second
  `CalibrationQueued` — every routine, its dependencies, its resolved targets, and
  whether this run touches it. A drift check sends none.
- `qpi-driver/py`: a calibration logs where it has got to — `[7/33] rabi q2 ok in 41.2s`
  per routine and target, each check's verdict as it is measured, and a closing summary.
  A full walk is hours that previously said nothing until it finished.
- `qpi-ui`: the Calibration tab draws the graph a run is walking, coloured per routine
  with each one's `3/5` tally. Previously a bar and `step 7 of 33`.
- `qpi-ui`: clicking a routine in the graph opens a card — what it writes, what it
  depends on and feeds, and per target the fitted parameters, the duration and any fit
  error. Both neighbour lists are clickable.
- `qpi-driver/py`: a report carries the sweep behind each fit — setpoints, measured
  signal and fitted curve — for the Rabi, Ramsey, DRAG, fine-amplitude, T1, T2, RB and
  spectroscopy fits. At most 200 points a trace; only the fitted numbers crossed before.
- `qpi-ui`: the node card plots that sweep against its fit, SI-prefixed ticks and a log
  axis where the sweep is logarithmic. A routine reporting no sweep shows no chart.
- `qpi-ui`: a calibration request stores its driver's plan, and `progress.nodes`
  accumulates each routine's state — `running`, `done`, `partial` or `failed`, with the
  targets finished out of its total. `progress` previously held only the latest position.
- `qpi-ui`: the Calibration tab shows a walk in flight — `step 7 of 33 — rabi on q2`,
  the ok/failed counts and a bar. Fed by a new `CalibrationProgress` event, which
  lands on the queued request rather than being kept as history.
- `qpi-ui`: a drift check and the recalibration it triggers get a request row of their
  own, marked as the driver's doing. Nobody dispatched them, so the tab previously
  showed a QPU busy for hours with no reason why.
- `qpi-driver/py`: `--data-dir` (env `QPI_DATA_DIR`) is universal, rather than each
  device's own `-o data_dir=`, which still overrides it. A device builder receives
  `data_dir` alongside the transport arguments, so one outside this SDK has to accept it.
- `qpi-driver/py`: `-o save_raw_data=true` keeps each acquisition and an instrument
  snapshot under the data directory. Qblox backends only; nothing to switch on quantify.
- `qpi-ui`: the calibration collections are pruned, where nothing in that path was
  before. `--calibration-request-retention` (720h) drops finished requests but never a
  running one, `--calibration-fit-retention` (720h) strips a report's fit traces and
  benchmark raw data while keeping every fitted number, and
  `--calibration-result-retention` (0, off) can drop whole reports.
- `repo`: `make test-dashboard` runs the dashboard's pure helpers under vitest. Nothing
  there could be unit-tested before without a live server.

### Changed

- `qpi-driver/py`: a routine overriding `measure()` is handed `timeout_s` and must pass
  it to each `backend.run` it makes, or its acquisitions keep the default ceiling rather
  than the configured one. Only `coupler_anticrossing` overrides it in-tree.
- `qpi-driver/py`: a `qblox` QPU no longer writes a dataset and a qcodes snapshot for
  every job, nor `qblox_tuner` for every routine. Nothing read them and nothing pruned
  them; `-o save_raw_data=true` asks for them back.

### Fixed

- `qpi-ui`: a dispatched calibration records which admin asked for it. `requested_by`
  had never been filled, and could not have been by that endpoint: it relates to
  `users` and only a superuser may dispatch.
- `qpi-driver/py`: a tuner installs with `install-systemd.sh` without editing
  `DRIVER_OPTIONS`. `OPERATION=calibrate` fills in the quantify and calibration config
  paths under `/var/qpi-driver/<service-name>`, the prompts offer `calibrate` and both
  tuners, and a config file that is not there yet is warned about rather than left to
  fail in the worker.
- `qpi-driver/py`: a `calibrate` driver writes under the data directory like a QPU does.
  `qblox_tuner` passed `bin/data` to its hardware agent whatever it was told, so a tuner
  installed as a service could not start: `/bin/data` is a `PermissionError`.
- `qpi-driver/py`: `-o job_timeout=` reaches a QPU's acquisition wait. It was read and
  then dropped, so a job was abandoned after the built-in 10s however the option was
  set — and on real hardware that is a `TimeoutError` rather than a slow job.
- `qpi-driver/py`: `routine_timeout_s` now bounds the wait on the instruments, which is
  what it was for. A tuner waited a hard-coded 60s instead, so a punchout on a real
  cluster failed as `Sequencer 0 did not stop in timeout period of 1 minutes`.
- `qpi-driver/py`: writing the calibration back no longer logs a qcodes `*IDN?` warning
  and traceback per element. It read `IDN` along with the calibration, so a clean walk
  ended looking like it had failed.
- `qpi-driver/py`: `quantify_tuner` sets quantify-core's data directory, so a hardware
  config with `sequence_to_file: true`, a hardware-log download or a diagnostics report
  no longer writes to `<cwd>/data` — `/data` for a service.

## [0.3.1] - 2026-08-04

### Added

- `qpi-driver`: a `sim` extra, so `-o is_simulated=true` no longer needs `scqubits`
  and `qutip` installed by hand: `pip install 'qpi-driver[cli,quantify_tuner,sim]'`.
- `qpi-ui`: admins can put a QPU under maintenance from the dashboard. It then takes
  no jobs, but can still be calibrated.
- `qpi-ui`: a QPU that is not taking jobs says why, on its card. `GET
  /api/qpus/availability` and `/api/qpus/{name}/availability` serve the reason.
- `QPUState` event, in all three SDKs: a driver is told when its QPU goes online,
  under maintenance or disabled. A tuner stops its own drift checks unless online.
- `qpi-driver`: config files are re-read when they change on disk, so a calibration
  takes effect without a restart. A new element and the hardware config still need one.

### Fixed

- `qpi-ui`: no tuner could be registered — the `kind` column never gained
  `quantify_tuner` or `qblox_tuner`.
- `qpi-ui`: the schema migration never revisited a field it had already created, so a
  select kept its original values forever.
- `qpi-ui`: a calibration report's `mode` and `status` are checked before insert,
  rather than failing inside the listener and losing the report.
- `qpi-ui`: switching a QPU off, maintenance, and a calibration in progress now stop
  jobs — refused at submission with the reason, held back at dispatch. Queued jobs
  wait rather than fail.
- `qpi-ui`: two tuners on one QPU could calibrate it at once. Calibration serialises
  per QPU, and connect returns 409 for a second driver of the same operation.
- `qpi-ui`: a driver's ports and goroutines are released when it disconnects, is
  deleted, or fails to bind — previously only when disabled. Drivers are marked
  offline at startup.
- `qpi-ui`: a QPU under maintenance is no longer drawn as offline.
- `qpi-ui`: the setup snippets for a tuner, and for the quantify and qblox QPU
  drivers, now pre-fill the config paths those drivers cannot start without.
- `qpi-ui`: the deb, rpm and apk packages ship `/etc/qpi.config.yml`.
- `qpi-driver`: the `quantify` and `qblox` extras no longer declare `scipy` (already
  a core dependency) or `lmfit` (imported nowhere).

### Changed

- `docs`: Removed redundancies in documentation

## [0.3.0] - 2026-08-01

### Added

- `qpi-driver`: Added a third operation, `calibrate` (RFC 0004) — a tuner that runs
  calibration experiments against a transmon chip and writes fitted parameters back to
  `quantify.device.yml`.

  ```bash
  qpi-driver start --operation calibrate --device quantify_tuner \
      -o calibration_config=./calibration.yml \
      -o quantify_device_config=./quantify.device.yml
  ```
- `qpi-driver`: Added two tuner devices, `quantify_tuner` and `qblox_tuner` (the
  `[quantify_tuner]`/`[qblox_tuner]` extras), running the same routine graph over
  either scheduler.
- `qpi-driver`: The calibration routine graph runs resonator spectroscopy through
  Rabi, Ramsey, T1, T2 echo, DRAG, AllXY, fine amplitude and randomized benchmarking,
  plus flux spectroscopy, the CZ chevron, conditional phase and interleaved RB for
  couplers — ordered by each routine's declared dependencies.
- `qpi-driver`: Added `calibration.yml` to select which routines run. A routine it
  doesn't mention still runs (opt-out, not opt-in); an unrecognised routine name is a
  startup error.
- `qpi-driver`: Added drift monitoring (`-o drift_check_interval`) — a tuner
  benchmarks on its own schedule and queues a partial recalibration of qubits/edges
  below `fidelity_threshold`/`fidelity_2q_threshold`.
- `qpi-ui`: Added `POST /api/op/calibrate/dispatch` (admin-only) and the
  `calibration_requests` queue collection it polls, so a request survives a driver
  restart or reconnect.
- `qpi-ui`: Added the `calibration_results` collection — fitted parameters, benchmark
  fidelities and failures per run, kept separate from `events` so retention doesn't
  prune it.
- `qpi-ui`: Added a Calibration tab (admin-only) — fidelity vs. threshold per target,
  current parameters by qubit, run history, and a trigger form.
- `qpi-driver`: `calibrate` is now in the Go and TypeScript SDK operation enums and
  event types, though only the Python SDK ships a tuner (RFC 0003 §8).
- `qpi-driver`: Added write-back safety for the device file (RFC 0004 §10) — nothing
  is written unless a routine produced a result, the candidate file is verified before
  replacing the original, and the previous file is kept as
  `quantify.device.yml.prev`.
- `qpi-driver`: Added a physics-based simulator tier for calibration tests (RFC 0004
  §7, tier 3) — acquisition data generated from a real transmon Hamiltonian
  (`scqubits`) and the Lindblad master equation (`qutip`) instead of each fit's own
  analytic form. `make test-py-sim`; the `sim` extra is optional.
- `qpi-driver`: The tier-3 simulator now exercises a full calibration end to end
  through the tuner's own entry point — full run, partial run, drift check and its
  recalibration, and write-back.
- `qpi-driver`: Added two-qubit gate simulation (`simulation/coupled.py`, RFC 0004
  §7) — a joint 9-dimensional register for a qubit pair, integrating the coupler (DC
  flux or a parametric drive) rather than asserting a gate.
- `qpi-driver`: `-o is_simulated=true` now works on both schedulers; qblox's
  `HardwareAgent` compiles offline and simulates only execution.
- `qpi-driver`: A coupler edge now carries its DC parking current
  (`bias.parking_current`, ±3.1 mA) and delivery mechanism (`bias.source`:
  `spi`/`qcm`, plus a new `-o spi_rack_address`), and can declare its CZ drive's
  transition (`clock_freqs.sideband_gap`).
- `qpi-driver`: `test_calibration_loop` now runs under both schedulers
  (`make test-py-loop`).
- `qpi-driver`: Added a readout resonator with a real linewidth and pulse envelopes
  with real shape to the simulator, so `resonator_spectroscopy`, `resonator_punchout`
  and `drag` measure something instead of a constant.
- `qpi-driver`: The calibration graph is finished (RFC 0005) — thirty-three nodes
  total, covering `measure.acq_rotation`, `measure.acq_threshold`, `clock_freqs.f12`,
  `measure.acq_delay` and the coupler parking current, all previously hand-typed.
  Readout calibration now straddles the qubit chain rather than sitting above it; a
  node may carry a staleness check alongside its sweep; and `CalibratedTransmon` gives
  the new parameters somewhere to live. Runs end to end in `make test-py-loop`; not
  yet run on physical hardware.
- `qpi-driver`: Added `CalibratedTransmon` (RFC 0005 §13) — an element (qcodes for
  quantify, pydantic for qblox) carrying `spec.amplitude`/`spec.amplitude_12` and room
  for the EF/three-state parameters below. Opt-in per element; a config on
  `BasicTransmonElement` still calibrates, just without persisting the extra values.
- `qpi-driver`: Added calibration checks (RFC 0005 §8) — a routine may supply a cheap
  check alongside its sweep, and `recalibrate`/`CalibrationDAG.diagnose` uses it to
  decide what to re-run, following Kelly et al.
  ([arXiv:1803.03226](https://arxiv.org/abs/1803.03226)). `resonator_spectroscopy` and
  `rabi` have checks to start; `RECALIBRATION_SEEDS` (formerly `RECALIBRATION_ROOTS`)
  is the fallback for routines with none.
- `qpi-driver`: Added `time_of_flight` (writes `measure.acq_delay`) and
  `resonator_relaxation` (reports resonator linewidth), sharing `fit_readout_timing`
  since arrival time and ring-up time constant can't be fit apart (RFC 0005 §7).
- `qpi-driver`: `qubit_spectroscopy` now sweeps drive power alongside frequency and
  picks the least-broadened row, instead of driving at a fixed 1% of full scale (RFC
  0005 §7).
- `qpi-driver`: Added `resonator_spectroscopy_excited`, measuring the dispersive
  shift between the `|0>` and `|1>` readout resonances (RFC 0005 §7). The simulated
  readout response now scales with drive power.
- `qpi-driver`: Added dispersive readout and `readout_discrimination` (RFC 0005 §7,
  §9) — the two IQ clouds are now derived from the readout response instead of
  hardcoded constants (`GROUND_IQ`/`EXCITED_IQ` are gone), with the routine fitting
  the rotation and threshold that separate them and reporting assignment fidelity.
- `qpi-driver`: `acq_rotation`/`acq_threshold` are now resolved per qubit
  (`executors/utils/discriminator.py`, RFC 0005 §7) instead of one pair applied to
  every qubit in a circuit.
- `qpi-driver`: The simulated chip's three qubits now have distinct
  `readout_phases_deg` instead of one shared value.
- `qpi-driver`: Added `readout_operating_point` (RFC 0005 §7) — a discriminator's
  best point trades magnitude contrast for phase separation, so it's now measured and
  applied separately (`measure_2state` submodule, `meas_level=2` only) from the
  magnitude-optimal point `resonator_spectroscopy`/`resonator_punchout` still write.
- `qpi-driver`: Added `f12_spectroscopy` and an EF drive (RFC 0005 §7), measuring the
  `|1>`-`|2>` transition, present on the device schema since RFC 0004 but never
  measured.
- `qpi-driver`: The EF drive is now a real drive on the full three-level ladder
  rather than a two-level subspace approximation (RFC 0005 §9) — fixes EF Ramsey
  fringe aliasing, gives the EF π pulse its correct `amp180/sqrt(2)` amplitude, and
  gives DRAG something (off-resonant 0-1 leakage) to correct.
- `qpi-driver`: The simulated acquisition now reports population across all three
  levels instead of collapsing `|2>` onto one of the other two clouds (RFC 0005 §9).
- `qpi-driver`: Added `rabi_12` (first EF gate parameter measured), RFC 0005 §7.
- `qpi-driver`: Added `resonator_spectroscopy_second_excited`, checking that the
  dispersive shift is evenly spaced across the ladder rather than measuring a
  parameter (RFC 0005 §7).
- `qpi-driver`: Added `three_state_discrimination` and `three_state_operating_point`,
  measuring leakage into `|2>` (RFC 0005 §7).
- `qpi-driver`: Added `ramsey_12`, refining f12 to kHz (RFC 0005 §7).
- `qpi-driver`: Added `drag_12`, the last node in the EF chain (RFC 0005 §7).
- `qpi-driver`: Added `fine_amplitude_12`, refining the EF π pulse below 0.05
  rad/pulse error (RFC 0005 §7).
- `qpi-driver`: The simulated coupler is now a mode with a frequency that responds to
  `bias.parking_current`, instead of the current being carried but ignored (RFC 0005
  §12).
- `qpi-driver`: Added `coupler_anticrossing`, measuring a coupler's parking current by
  walking its bias through the qubit and locating the crossing (RFC 0005 §12).
  Introduces `CalibrationRoutine.measure`, letting a routine take over its own
  acquisition loop for instrument state that can't be scheduled.
- `qpi-driver`: Added `cz_spectroscopy`, finding the drive frequency a parametric CZ
  needs (RFC 0005 §12), and `CalibrationRoutine.applies_to`, letting a routine decline
  edges it doesn't apply to (e.g. baseband-only couplers).
- `qpi-driver`: Fixed a parametric coupler drive losing its frequency during a
  held-offset flux pulse, which had left the parametric CZ non-functional in
  simulation (RFC 0005 §9).
- `qpi-driver`: Added `cz_parametrization`, the last node in the graph — measures a
  parametric CZ's exchange rate vs. drive amplitude (RFC 0005 §12).
- `qpi-driver`: An edge whose qubits aren't calibrated is now refused at startup,
  rather than producing a calibrated-looking but non-functional gate (RFC 0005 §13).
- `qpi-driver`: `resonator_punchout` gained a check for whether the configured power
  is still below the crossover (RFC 0005 §13).
- `qpi-driver`: `readout_fidelity` is now its own benchmark node, so it participates
  in drift monitoring (RFC 0005 §13).
- `qpi-driver`: The calibration graph is now wired to run on real hardware, not just
  the simulator (RFC 0005 §12b) — both tuners resolve a real coupler bias source (SPI
  rack or cluster output) via `resolve_bias_source`, with a new
  `-o spi_rack_address`; routines needing `CalibratedTransmon` submodules now decline
  gracefully on `BasicTransmonElement` instead of raising. Not yet verified against a
  physical chip.

### Fixed

- `qpi-driver`: `allxy` and `fit_chevron` assumed the readout's magnitude always
  rises when a qubit is excited, which isn't guaranteed; both now measure their own
  reference direction instead.
- `qpi-driver`: A fitted `acq_rotation` could fall in `np.angle`'s `(-180, 180]`
  range, which the hardware rejects outside `[0, 360)`. It's wrapped now.
- `qpi-driver`: A virtual Z (`rz`/`z`/`s`/`t`) did nothing under `is_simulated` —
  `ShiftClockPhase` never reached the coordinator.
- `qpi-driver`: `meas_level=0` returned a single sample instead of a time series, and
  `meas_level=2` returned IQ instead of 0/1 bits.
- `qpi-driver`: A raw trace over more than one qubit couldn't be taken at all (a
  Qblox module scopes one sequencer); the circuit now runs once per measured qubit.
- `qpi-driver`: The qblox tuner had never completed a calibration — `device.elements()`
  is a dict under qblox, edges took positional constructor args its pydantic models
  don't accept, and structural fields were written as calibration data.
- `qpi-driver`: `conditional_phase` applied nothing on either scheduler
  (`cz.phase_correction` is a name neither backend has); it now measures four fringes
  instead of two, which is what the conditional phase actually requires.
- `qpi-driver`: `drag` swept the same range for both schedulers despite `motzoi`
  (quantify) and `beta` (qblox) being different quantities, nine orders of magnitude
  apart; each backend now supplies its own span.
- `qpi-driver`: `drag` then wrote its result to `rxy.motzoi` unconditionally, which
  qblox calls `rxy.beta`.
- `qpi-driver`: `fit_chevron` picked the brightest pixel, which is as likely to be an
  off-resonant row as the gate; it now finds resonance by oscillation contrast.
- `qpi-driver`: `fit_chevron` then stopped at the first wiggle on the way back rather
  than the true round trip (55 ns instead of 110 ns) — the return is a level crossing
  now.
- `make test-py-loop` and `make test-py-sim` silently ran nothing when chained in one
  `make` invocation — `uv run` without `--no-sync` pruned the `sim` group the other
  target had just installed. Both now pin their own environment.
- `qpi-driver`: A raw trace under `is_simulated` carried single-shot noise while
  claiming to be averaged; it now falls as 1/√N like an integrated point.
- `qpi-driver`: A readout LO pinned per-port meant a second qubit sharing a QRM_RF
  broke every later schedule; the LO is pinned once instead.
- `qpi-driver`: `resonator_punchout` left the readout frequency stale after moving
  the power (the resonance moves with power too); it now writes both.
- `qpi-driver`: `fit_conditional_phase` took the zero crossing of the two fringes'
  difference instead of fitting each and subtracting, and its correction had a
  sign/units bug.
- `qpi-driver`: The coupler's CZ never reached the simulator (`compile_cz` loses the
  pair when lowering into a subschedule); it's now read from the port name.
- `qpi-driver`: Both device loaders added elements in file order, so an edge listed
  before its qubits failed.
- `qpi-ui`: A failed job showed nothing but three empty-state tabs; the driver's
  failure reason is shown instead.
- `qpi-ui`: The dashboard's IQ plot was hardcoded to `-0.5..1.5`; it now scales to
  the data.
- `make test` was red on macOS (code signature stripped by `uv sync`) and
  `test-docs-static` reported flags as removed against a stub CLI that always exits
  0.
- `qpi-driver`: The qblox tier-2 test asserted a schedule was built but never
  compiled it; both backends compile now.
- `qpi-driver`: The driver e2e's QPU-seconds check read its baseline before the
  approval it measured, making it flaky.

### Changed
- `make test-e2e-dashboard` accepts `SPEC=<glob>` to run a single Cypress spec.
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


