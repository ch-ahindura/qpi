# RFC 0008 — Parameter Provenance

- **Status:** Draft
- **Author:** Martin Ahindura
- **Created:** 2026-08-12
- **Depends on:** RFC 0004 (the device file and its write-back), RFC 0005 (the completed
  graph), RFC 0007 (which defers this, and has four places waiting on it)
- **Touches:** `qpi-driver` (Python only — no new operation, no new event type, no
  server or SDK change)

## 1. The idea

A device config records what every parameter *is*. Nothing records where it came from,
and the two cases it cannot tell apart are the ones that matter: a value this driver
measured, and a value somebody typed in.

The August 2026 chip carried `clock_freqs.f01: 4735509751.238763`. Nine significant
figures, so it reads as a measurement, and the qubit was 302 MHz away; the line had
never been there. Six calibration runs were spent on the consequences. Precision is not
provenance, and a file that cannot say which it is holding forces every reader, human or
routine, to assume the better case.

This RFC makes each parameter carry which routine last measured it, when, and from what
signal, so that "has this ever been measured?" is a question the driver can answer.

## 2. Vocabulary

- **Provenance** — for one parameter on one target: the routine that last produced it,
  the run it was produced in, when, and a summary of the fit it came from.
- **Prior** — a value with no provenance. A design figure, a value from another control
  stack, a placeholder, or a measurement made before this RFC. Not necessarily wrong;
  just not attributable.
- **Sidecar** — the file this RFC adds, holding provenance and nothing else.

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
| RFC 0007 §11's ledger | "did *this walk* produce it?" | "was it ever measured, and how long ago?" |
| RFC 0007 §11's skipped nodes | Kept, unmarked | Kept and marked as unconfirmed by this run |
| RFC 0007 §11's withdrawn pre-walk check | Undecidable — a hand-supplied parameter and a disabled producer look alike | A disabled sole producer is an error only when the parameter has no provenance either |
| Write-back gating (§7) | All-or-nothing per run | Per parameter: commit what its producer measured and its guards passed |

The ledger row is the substantive one. RFC 0007 §11 blocks a node when *this run* failed
to produce a parameter it reads, which is right for a bring-up and blind on a
recalibration: a chip whose `f01` was measured six months ago and has since drifted looks
identical to one measured an hour ago. Provenance turns that into an age, and an age is
what a drift check is entitled to act on.

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

## 8. Testing strategy

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

## 9. Implementation plan

1. **`tuners/base/provenance.py`.** Load, merge-per-key, save, and query, against a path
   derived from `_device_config_path`. Tier-1 tests. Nothing calls it yet, so nothing can
   regress.
2. **Record it.** The DAG already knows, per routine and target, what succeeded and what
   it wrote — RFC 0007 §11's ledger holds exactly that. Write provenance from the same
   place, beside the device write-back that RFC 0004 §10 gated on success.
3. **Report it.** Surface a parameter's provenance in the calibration report's notes and
   in the routine result, so an operator reading a failed run can see which inputs were
   attributable and which were guesses. Report-only; nothing changes behaviour yet.
4. **Consume it.** Sharpen RFC 0007 §11's ledger from "produced in this walk" to "has
   provenance, and how old", mark what a skipped node left unconfirmed, and reinstate the
   pre-walk check that §11 withdrew for want of this.

Phases 1 to 3 are additive and observable before anything depends on them, which is
deliberate: a provenance record that is wrong is worse than none, and phase 3 is where
that becomes visible on a real chip rather than in a test.

## 10. Open questions

1. **One sidecar or one per target?** One file is simpler and merges per key; one per
   qubit makes a partial recalibration's writes obviously disjoint and is friendlier to
   whatever ends up watching the directory. Leaning one file until a reason appears.
2. **What of the fit summary is worth keeping?** The whole `fit` payload is large — RFC
   0005 caps it at `MAX_FIT_PAYLOAD_BYTES` for the event — and most of it is the sweep.
   The useful residue is probably the one or two numbers a guard judged:
   signal-to-noise, span over scatter. Deciding that is deciding what a future drift
   check can compare against.
3. **Does provenance expire?** An age is only actionable against a threshold, and a
   sensible threshold is per parameter: a readout frequency drifts in hours, an
   anharmonicity does not. That may want to live beside the routine that produces it
   rather than in this file.
4. **Should the write-back gate on it in phase 2 or wait for phase 4?** Gating early is
   the safer chip behaviour and the larger behaviour change; the plan above defers it,
   which is a judgement rather than a conclusion.
5. **What does the dashboard do with it?** RFC 0006 draws the graph; a node whose inputs
   are priors is arguably a different colour. Out of scope here, but the payload
   decision in §3 is what would have to change first.
