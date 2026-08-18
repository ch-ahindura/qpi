# RFC 0008 — Parameter Provenance

- **Status:** Implemented
- **Author:** Martin Ahindura
- **Created:** 2026-08-12
- **Depends on:** RFC 0004 (the device file and its write-back), RFC 0005 (the completed
  graph), RFC 0007 (which defers this, and has four places waiting on it)
- **Touches:** `qpi-driver` (Python only — no new operation, no new event type, no
  server or SDK change)

## 1. The idea

A device config records what every parameter *is*. Nothing records **where it came
from** — and the two cases it cannot tell apart are the ones that matter: a value this
driver measured, and a value somebody typed in.

That is all "provenance" means here; §2 says it at more length.

The August 2026 bring-up carried `clock_freqs.f01: 4735509751.238763`. Nine significant
figures, so it reads as a measurement, and the qubit was 302 MHz away; the line had
never been there. Six calibration runs were spent on the consequences. Precision is not
provenance, and a file that cannot say which it is holding forces every reader, human or
routine, to assume the better case.

This RFC makes each parameter carry which routine last measured it, when, and from what
signal, so that "has this ever been measured?" is a question the driver can answer.

## 2. Vocabulary

- **Provenance** — literally *where a thing came from*. In a gallery it is the paperwork
  proving a painting is what it claims to be. Here it is the same idea for a single
  number: which routine last measured this parameter on this target, in which run, when,
  and what its fit looked like. The device file says a parameter **is** 4.7364 GHz;
  provenance says whether anything ever measured that.
- **Prior** — a value with no provenance. A design figure, a value from another control
  stack, a placeholder, or a measurement made before this RFC. Not necessarily wrong;
  just not attributable.
- **Sidecar** — a small file that travels beside a bigger one and describes it, without
  the thing it describes needing to know it exists. Here: provenance, and nothing else.

## 3. Decisions

| Decision | Resolution |
|---|---|
| New operation or event type? | **No.** Python driver only, as RFC 0007. The event payload is a contract asserted in Go and TypeScript; extending it is a separate decision. |
| Where provenance lives | **A sidecar the driver owns**, beside the device file, keyed by `(target, dotted path)`. Not either config file — §5. |
| Does it hold values? | **No, metadata only.** A second store of *values* would be a second source of truth for what the chip is, needing synchronisation with the file the executor reads. Metadata has no such coupling: it never holds a number anything needs to run a circuit. |
| What if it is missing | **Every parameter is a prior.** Conservative, correct, and identical to today's behaviour. Nothing may fail for its absence, or a fresh checkout could not calibrate. |
| How it is written | **Merged per key**, by the run that produces that parameter. Rewriting the file wholesale would erase the provenance of everything a partial run did not touch. |
| Is it an index over report history? | **No — it is the record itself.** RFC 0007 §10 assumed the content already existed and only a lookup was missing. It does not: reports leave the driver through a result queue and nothing persists them locally (§4). |
| Staging value commits until a run succeeds | **No**, and the diagnosis matters more than the answer — §7. |
| Failing a routine whose input is a prior | **Out of scope.** This RFC makes priors *visible*; deciding what refuses to run on one is RFC 0007 §11's ledger, which this then sharpens. |
| Backfilling provenance for existing chips | **Out of scope**, and unnecessary: absence already means "prior", which is the truth for every value written before this ships. |

## 4. What is recorded today, and where it goes

`RoutineResult` already carries almost exactly the right fields:

```python
routine_name: str      # which node
target: str            # which qubit or edge
parameters: dict       # what it wrote
timestamp: str         # when
duration_s: float
fit: dict | None       # the sweep behind it
```

RFC 0007 §10 concluded from this that provenance needed no new content, only an index.
**That is wrong, and it is worth correcting explicitly because it made the work look
smaller than it is.** The report is assembled, converted by `to_event_payload`, put on a
result queue, and sent to the server. Nothing writes it to disk. So the driver cannot ask
what it measured last week — the facts exist, but not anywhere the process needing them
can read.

Three consequences:

- A local sidecar is not a cache of something already available. It is the driver's only
  copy.
- Asking the server instead would put a network round-trip inside the calibration walk,
  and make a chip un-calibratable when the server is unreachable. A driver that cannot
  calibrate offline is a worse trade than a file.
- The sidecar and the server's report history will drift, and that is acceptable, because
  they answer different questions. The server has the audit trail; the sidecar has the
  one fact the walk needs, per parameter.

## 5. Where it goes, and why not the two existing files

**Not `quantify.device.yml`.** That file's schema is not ours. It deserialises into a
`QuantumDevice` whose parameters are qcodes parameters on real element classes, and
quantify's models reject unknown keys — `output_att` validated against the wrong config
class raised `extra_forbidden` while RFC 0007 was being researched. Provenance keys there
mean either a parallel structure inside someone else's format or a fork of it.

**Not `calibration.yml`.** It is hand-authored intent — the August 2026 one is mostly
reasoning about why each sweep is the size it is — so a machine writing into it either
destroys that or needs a comment-preserving round-trip to avoid doing so. RFC 0007 §7
declines to have the driver edit that file for the same reason, and it would put the
machine's output and the operator's input in one place, which is what makes both harder to
trust.

**So: a third file the operator never edits.** Beside the device file, whose path the
tuner already holds as `_device_config_path`, so the sidecar is found wherever the
device config is and moves with it. Keyed by target and dotted path:

```yaml
q0:
  clock_freqs.f01:
    routine: ramsey
    run: job-1743
    at: 2026-08-12T09:22:17Z
    fit: {snr: 13.0, span_over_scatter: 194.0}
```

Two properties to design for, both learned from how the rest of the driver has failed:

- **Merge per key, not per file.** A partial run touches few parameters. Rewriting the
  whole file each walk would erase the provenance of everything it did not measure, which
  is most of it.
- **Safe to delete and safe to be absent.** A missing sidecar means every parameter is a
  prior — conservative, correct, and exactly today's behaviour. Nothing may fail because
  it is not there.

## 6. What it makes possible

Four things are already waiting on this, three of them in RFC 0007:

| Consumer | Today's weaker version | With provenance |
|---|---|---|
| RFC 0007 §2's *prior* | Not decidable; the word is defined and unusable | `is there provenance for this parameter?` |
| RFC 0007 §11's ledger | "did *this walk* produce it?" | "was it ever measured, in any run?" — for reporting and for the pre-walk check, **not** for blocking; see the correction below |
| RFC 0007 §11's skipped nodes | Kept, unmarked | Kept and marked as unconfirmed by this run |
| RFC 0007 §11's withdrawn pre-walk check | Undecidable — a hand-supplied parameter and a disabled producer look alike | A disabled sole producer is an error only when the parameter has no provenance either |
| Write-back gating (§7) | All-or-nothing per run | Per parameter: commit what its producer measured and its guards passed |

The ledger row is the substantive one. RFC 0007 §11 blocks a node when *this run* failed
to produce a parameter it reads, which is right for a bring-up and too narrow afterwards:
on a partial recalibration almost nothing was produced by this run, so almost nothing is
checkable, and a parameter no run ever measured is indistinguishable from one measured
last week. Provenance separates those two, which is the whole of what the ledger needs.
Not *how old* the measurement is — see §8.

**Corrected while implementing phase 4: this must not relax the blocking rule.** The
tempting reading is that a node blocked because its input failed *this* run should run
anyway when an earlier run measured that input — the device does hold a real number. It is
wrong, and the August 2026 bring-up is the counterexample: a failure to *measure* f01 is
evidence against whatever f01 the file holds, because the usual reason spectroscopy finds
no line is that the qubit is not where the file says. Running the six nodes behind it
against last week's value fits the same noise, whatever the value's pedigree. So a failed
measurement still blocks, and provenance's two uses are the ones below it in the table —
report, and decide the pre-walk check. Blocking on *absent* provenance would be worse
still: every chip calibrated before this shipped has measured values and no sidecar, so it
would refuse the runs it exists to protect.

## 7. Why not stage the writes until the run succeeds

The obvious alternative — hold every fitted value until the whole walk succeeds, then
commit — was considered and declined. The problem underneath it is real, so it is
worth being precise about which part.

**The corruption it is aimed at was not caused by writing too early.** On the August 2026
chip, `rabi` wrote `rxy.amp180 = 0.0158` against a calibrated 0.5683, and every later run
inherited it. A staging store would have held that value for the length of the walk and
then committed it, because the walk did not fail: `require_in_range` accepted 0.0158 and
`rabi` reported success. Deferring a commit does not help when the producing node believes
it succeeded, which is the case that actually happened. The August 2026 fit guards are
what address that, and did.

**Nor can a value be withheld from the walk.** `rabi` needs the `f01` that
`qubit_spectroscopy` just wrote; downstream nodes read upstream results within a run by
construction. So the staging boundary could only ever be the file, never the in-memory
device.

**And all-or-nothing has a cost of its own.** A run that measures the resonator and `f01`
correctly and then fails at `rabi` would discard two good measurements, so the next run
starts from the same bad priors. On that chip, that is the difference between converging
and not.

What is worth taking from the idea is the per-parameter version, which is this RFC:
commit a parameter when the node that produced it succeeded *and* its guards passed, and
record which. That is a strictly finer boundary than a staging store, it needs no second
copy of any value, and it subsumes the all-or-nothing case.

## 8. Provenance does not expire, and the timestamp is not a deadline

Worth stating outright, because it is the obvious next thought and it is wrong.

Provenance records *when* a parameter was measured, so it is tempting to have something
judge a value stale once it is old enough — a readout frequency drifts in hours, an
anharmonicity does not move in months, so per-parameter expiry thresholds. **RFC 0005
already rejected exactly that**, and its reason still holds: it added a `check` form per
node so that "staleness is measured rather than remembered". An age threshold is
remembering. It guesses at the answer a three-point check can go and measure for the cost
of one acquisition.

So the two questions are separate, and neither needs the other:

| Question | Answered by | Kind of answer |
|---|---|---|
| Was this ever measured? | this RFC | yes or no, from whether a record exists |
| Is it still right? | RFC 0005's check nodes | measured, per run |

The timestamp is recorded for the operator and the report — *this f01 is from the run
before last* is worth reading — and for §6's ledger row, which asks whether a parameter
was measured in *some* run, not whether it was measured within N hours. Nothing here
compares it against a threshold, and no schema field for one is added.

The one place age might legitimately return is choosing *which* checks a drift run bothers
to evaluate, as a cost heuristic rather than a verdict. That is a scheduling question for
whatever owns the drift cadence, and it can read the timestamp this file already stores.

## 9. Testing strategy

- **Tier 1.** The sidecar's own round trip: merge per key, an absent file, a corrupt
  file, a file holding a target or path the device no longer has. Every one of those
  resolves to "prior" rather than an error.
- **Tier 2.** A calibration writes provenance for exactly the parameters its successful
  routines wrote, and for no others: a failed routine leaves no provenance, which is
  the property the whole thing rests on.
- **Tier 3.** Over the simulated chip: a walk, then a second walk that reads the first's
  provenance and finds every parameter attributable. Then the same with the sidecar
  deleted between them, which must calibrate identically and report everything as a
  prior.
- **The regression test.** A device file seeded with a nine-significant-figure `f01` that
  nothing measured, asserted to be reported as a prior. That is the August 2026 failure
  written down, and it is the one test that would have saved those six runs.

## 10. Implementation plan

1. **`tuners/base/provenance.py`.** Load, merge-per-key, save, and query, against a path
   derived from `_device_config_path`. Tier-1 tests. Nothing calls it yet, so nothing can
   regress.
2. **Record it.** The DAG already knows, per routine and target, what succeeded and what
   it wrote — RFC 0007 §11's ledger holds exactly that. Write provenance from the same
   place, beside the device write-back that RFC 0004 §10 gated on success.
3. **Report it.** Surface a parameter's provenance in the calibration report's notes and
   in the routine result, so an operator reading a failed run can see which inputs were
   attributable and which were guesses. Report-only; nothing changes behaviour yet.
   `RoutineResult.priors` stays out of `to_event_payload`, as `CalibrationReport.notes`
   already does, so §3's "no payload change" holds: the payload is one contract asserted
   in Go and TypeScript.
4. **Consume it.** Mark what a skipped node left unconfirmed and how old it is, and
   reinstate the pre-walk check that §11 withdrew for want of this. The ledger's blocking
   rule is deliberately left alone — see §6's correction, which is the one place the plan
   as drafted was wrong.

Phases 1 to 3 are additive and observable before anything depends on them, which is
deliberate: a provenance record that is wrong is worse than none, and phase 3 is where
that becomes visible on a real chip rather than in a test.

All four are implemented. Phase 4 is report-only too, in the end, for the reason §6 now
records — which means nothing in this RFC can refuse a run that would have succeeded
before it.

## 11. Resolved while implementing

No open questions remain. Four were open when this was drafted, and building it settled
all of them — three by finding the answer in the code and one by looking at the output.

| Question | Resolution |
|---|---|
| One sidecar or one per target? | **One file**, as the draft leaned. Merging per key makes a partial run's writes disjoint anyway, which was the only thing one-per-qubit bought, and one file is one thing to find, delete and back up. |
| What of the fit summary is worth keeping? | **The scalars a guard judged** — `snr`, `reach`, `contrast`, `separation` — and nothing else, since the rest of a fit payload is the sweep. Chosen by asking which numbers the guards actually compare: `require_resolved_curve` reads `reach`, the discriminators read `separation` and `contrast`. Infinities are dropped rather than stored, because `snr` is infinite when a fit had no residual scatter and YAML `.inf` does not travel. |
| Gate the write-back on provenance in phase 2 or phase 4? | **Neither, because it was already true.** `report.routine_results` holds only routines that succeeded, so writing provenance from `_persist` after a successful write-back *is* the per-parameter gate §7 asked for. No new mechanism, and no behaviour change to defer. |
| What does the dashboard do with it? | **Still out of scope**, and now cheaper to answer: `RoutineResult.priors` is computed and available locally: only §3's payload decision stands between it and RFC 0006. |

Three things the implementation found that the draft did not anticipate:

- **A prior must be filtered to paths the element actually has.** `spec.amplitude` and
  `measure.integration_time` do not exist on a `BasicTransmonElement`, so reporting them
  as unmeasured put a permanent warning in front of every run — the same noise that made
  RFC 0007 §11 withdraw its pre-walk check in the first place, arriving by a different
  door. A path that is not there is not an unmeasured one.
- **The run identifier is the report's timestamp, not the job id.** The job id is not
  plumbed into `Tuner.calibrate` and threading it there would change the tuner contract
  for a metadata field. Every walk has a timestamp, it distinguishes runs, and `at` still
  distinguishes parameters *within* a run.
- **§6's ledger row was wrong about blocking**, which is the one substantive correction —
  recorded there rather than here because it changes what the RFC claims, not just how it
  was built.

## 12. What this deliberately does not do

Two are worth stating because they are the obvious next thoughts:

- **Nothing expires.** §8's reasoning, unchanged by the implementation: no code compares a
  timestamp against a threshold, and there is no schema field for one. Staleness is
  measured by RFC 0005's check nodes.
- **Nothing refuses a run.** Every consumer built here reports. A chip that calibrated
  yesterday calibrates today, whether or not the sidecar exists, is readable, or says
  anything about the parameters in play.
