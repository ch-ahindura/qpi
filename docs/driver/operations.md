# Driver Framework Operations Runbook

This runbook covers running the extensible driver framework (RFC 0001) in
production: keeping the `events` log bounded, protecting the server from a
misbehaving driver, and the knobs you tune per deployment.

The driver framework is standard and active across all QPI deployments.
The collections, retention loops, and limits below run automatically according
to your configuration.

## Tuning knobs

Every setting follows the same precedence as the rest of QPI: **CLI flag > env
var > config file > default**.

| Setting | Flag | Env | Config key | Default | Purpose |
| --- | --- | --- | --- | --- | --- |
| Events retention window | `--events-retention` | `QPI_EVENTS_RETENTION` | `eventsRetention` | `720h` (30 days) | How long an entry stays in the `events` log before it is pruned. |
| Prune interval | `--events-prune-interval` | `QPI_EVENTS_PRUNE_INTERVAL` | `eventsPruneInterval` | `1h` | How often the retention loop runs. |
| Per-driver rate limit | `--event-rate-limit` | `QPI_EVENT_RATE_LIMIT` | `eventRateLimit` | `100` | Max inbound events per second accepted from each driver. |

Durations use Go's duration syntax (`720h`, `30m`, `90s`). Set
`eventsRetention` or `eventRateLimit` to `0` to disable pruning or the rate
limit respectively.

Example `qpi.config.yml`:

```yaml
eventsRetention: "168h"      # keep one week of events
eventsPruneInterval: "30m"
eventRateLimit: 50           # 50 events/sec per driver
```

## Retention and pruning

The `events` collection is the single trace log of every driver→UI event a
handler chooses to persist (for example a cryostat monitor's readings). Left
unbounded it grows with every reading, so a background loop
(`RunEventsRetentionEngine`) deletes entries older than `eventsRetention` on
each `eventsPruneInterval` tick.

- Pruning compares each row's `ts` (the event's own UTC timestamp) against
  `now - eventsRetention` and deletes in bounded batches, so a large backlog
  never blocks on a single transaction.
- The loop exits immediately if the framework is off or `eventsRetention` is
  `0`, so a legacy deployment starts no extra goroutine.
- Deletions are logged as `[Retention] pruned N expired events`.

To confirm pruning is keeping growth flat, watch the row count over a window
longer than the retention period under steady load; it should plateau rather
than climb.

```sql
SELECT count(*) FROM events;
```

### Index

A composite index `idx_events_type_ts` on `events(type, ts)` backs both the
dashboard's per-type, time-ordered charts and the retention scan. It is created
idempotently by the schema migration; no manual step is required.

## Per-driver rate limiting

Each connected driver's inbound event stream is guarded by its own token-bucket
limiter (`eventRateLimit` events/sec, bursting up to one second's worth). When a
driver exceeds its rate, the surplus events are logged and dropped —
`[DriverListener <id>] rate limit exceeded, dropping event` — and the listener
keeps running. One driver flooding events cannot starve the others: limiters are
per-driver, not global.

Tune `eventRateLimit` to comfortably exceed a healthy driver's emit rate. A
monitor emitting every few seconds needs only a handful per second; the default
of `100` leaves wide headroom. Set it to `0` only if you trust every driver and
want no cap.

## Adding a device on a production node

A driver runs one **operation** on one **device** (see
[RFC 0003](../rfcs/0003-driver-extensibility.md)). The operations are fixed — the
server must have a handler for each — but the devices are not, so a lab can run a
backend the SDK has never heard of without patching it.

Start by asking the node what it can already run. The list comes from the installed
SDK's own registry, so it describes that node rather than the documentation:

```bash
qpi-driver devices                    # names, by operation
qpi-driver devices --operation monitor
```

Names only. What a device does, and which `-o` keys it wants, is documented in QPI-UI
where the driver is registered — the driver publishes no catalog of its own, so there
is nothing to sync and nothing that can disagree ([RFC 0003 §9](../rfcs/0003-driver-extensibility.md)).

### Two ways in

**Installed** (Python only) — the route for a device meant to be versioned and
deployed. The distribution advertises it under the `qpi_driver.devices` entry-point
group; after `pip install`, it is indistinguishable from a built-in:

```bash
uv tool install --with mylab-devices "qpi-driver[cli]"
qpi-driver devices --operation process     # mylab's devices are now listed
```

An entry point that will not import is logged and skipped, never fatal — one broken
third-party package cannot stop the driver from starting. Check the journal for
`skipping device entry point` if a device you expected is missing.

**Named by import path** — the route for "I have a class in a file". No packaging:

```bash
# Python: module:attr, or module.attr
qpi-driver start --operation process --device mylab.executors:PrestoV2 …
# TypeScript: ./module.js#export   (`:` is a URL scheme separator in a JS specifier)
qpi-driver start --operation monitor --device ./dist/my-device.js#MyExport …
```

Both routes run code the operator installed or named; the server never supplies a
device value and cannot introduce one. The Go SDK has no import-path route — Go
resolves imports at compile time, so a device there is registered in your own `main`
and the binary rebuilt.

An executor named by import path is handed every `-o` key the SDK does not read
itself, as the string it was typed as: its constructor is the only thing that knows
those keys exist.

### What an option error looks like now

An `-o` key the chosen device never read is an error, where it used to be ignored.
This is the change most likely to surprise an existing unit file: a typo that
previously meant "running with a default nobody chose" now means the driver does not
start.

```
$ qpi-driver start --operation process --device mock --token … --ca-fingerprint … -o data_dirr=/data
Error: unknown option 'data_dirr' for process device 'mock'.
```

The other three are worth recognising in a journal:

```
Error: missing required option 'channels', e.g. -o channels=mapper.bf.tmc:K,mapper.bf.pmc:mbar
Error: bad value for -o job_timeout: invalid literal for int() with base 10: 'soon'
Error: bad value for -o data_dir: /var is not in a safe location
```

The last one is a safety check, not a bug: a driver may only read and write under a
small allow-list (`/var/qpi-driver`, `/etc/qpi-driver`, `/tmp`, `/var/tmp`, the
user's home). It applies to a device named by import path exactly as to a built-in.

All four exit 1 before the driver connects to anything, so a `Restart=on-failure`
unit will loop on them — check `systemctl status` rather than waiting for the
dashboard to show the driver online.

## When connecting is refused

```
409  "tuner-1" is already connected as this QPU's calibrate driver; one per QPU
```

One driver per role per QPU: two `process` drivers would hand one chip two schedules,
two tuners would each write the device file. Scoped by *operation*, so a
`quantify_tuner` refuses a `qblox_tuner`. Registering a second is fine — only
connecting it while the first is live is not, which is what makes a standby possible.

If nothing is actually connected and this still appears, the named driver is holding
its lease: disable it (`POST /api/op/drivers/toggle`, `enabled: false`) to release the
ports and goroutines. A dead process releases on socket detach, and a restarted server
starts with no leases and resets every driver to `offline`.

## Running a calibration on a production node

A `calibrate` driver differs operationally from the other two in one way that
matters: it writes a file the QPU beside it reads. Everything below follows from
that.

**Queue one, don't send one.** `POST /api/op/calibrate/dispatch` (admin-only)
adds a row to `calibration_requests`; the driver's dispatcher picks it up on its
next tick. That indirection is deliberate — a request survives a server restart,
is requeued on a send error, and runs when a driver that was offline reconnects.
The dashboard's Calibration tab is the same call with a form in front of it.

```bash
curl -X POST "$QPI_ADDR/api/op/calibrate/dispatch" \
  -H "Authorization: $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"driver_id": "<driver-id>", "mode": "fidelity_check"}'
```

**One at a time.** A tuner runs a single calibration; a second is not dispatched
until the first reports. A `full` run is hours, so prefer `fidelity_check`
(minutes, changes nothing) to find out whether a full run is warranted.

**Watching one run.** A tuner emits `CalibrationProgress` after each routine and
target, which lands on the queued request's `progress` field — so the Calibration
tab shows `step 7 of 33 — rabi on q2` and a bar, live. The same thing is in the
driver's own journal in more detail, one line per target plus each check's verdict:

```bash
journalctl -u <service-name>.qpi-driver.service -f
```

A driver's own drift check reports progress too, but it answers to no queued row,
so that one is visible only in the journal.

**The device file has a backup.** Every successful write-back leaves the previous
file as `quantify.device.yml.prev`. If a calibration makes fidelity worse, that
is the fastest way back — no restart, and no waiting on another calibration:

```bash
cp quantify.device.yml.prev quantify.device.yml
```

A failed calibration writes nothing at all, so a `.prev` older than the last run
means the last run failed.

## Taking a QPU out of service

Two levers, both settable from the QPU Registry tab:

- **Switched off** (`POST /api/op/qpu/toggle`, `enabled: false`) — out of service. No
  jobs, no calibration.
- **Maintenance** (`POST /api/op/qpu/maintenance`) — being worked on. No jobs, but a
  calibration can still be dispatched, which is usually the point.

Either way a queued job waits rather than failing, and a new submission is refused
with the reason. A QPU being calibrated stops taking jobs on its own.

Drivers are told, so a tuner stops its own drift checks under maintenance. That part
is cooperative; the server's gate is what actually holds jobs back.

## What takes effect without a restart

The device config is re-read when it changes on disk — by the `process` driver before
each job, by a tuner at the start of each calibration. So a calibration, a restored
`.prev`, or parameters worked out by hand all reach a running driver by the file
appearing where it reads. No signal to send, nothing to restart.

Two things still need one:

- **A new element.** Adding a qubit or an edge is structural, not calibration. The
  driver logs the names it did not recognise and carries on with the rest.
- **The hardware config.** It builds the instrument coordinator and the Cluster, so
  applying a new one means redialling the rack — and a failed reconnect leaves the
  driver with no coordinator and no way back, the old one already closed. The device
  config can fall back to memory; this cannot. A driver logs `Restart it to pick the
  new one up` once per change, so at least it is not silent.

A config that will not parse never replaces a working one. The device config keeps
what it has and the job proceeds; a bad `calibration.yml` fails the calibration
instead, because that file decides *which routines run*.

**Retention does not apply to calibration reports.** `calibration_results` is its
own collection, not part of the `events` log, so `eventsRetention` leaves it
alone. Reports are rare and small in number; if they ever need pruning it is a
separate decision, made deliberately.

**A skipped node is not a failed one.** Several routines write parameters only a
`CalibratedTransmon` element carries — the EF chain and the two readout operating
points — and on a chip whose elements are `BasicTransmonElement` they *decline*
rather than fail. The report comes back `success` with those nodes absent. That is
the intended reading: the chip calibrated everything its device file has room for.
To opt a qubit into the rest, point its `element_type.path` at
`CalibratedTransmon`; the [tuner reference](tuners.md) lists which node needs which
submodule.

**The coupler bias is set outside the schedule.** `coupler_anticrossing` walks a DC
current through the coupler, and no schedule can express that — it goes over qcodes,
to an S4g in an SPI rack (`-o spi_rack_address=`, and `bias.source: spi` on the
edge) or to a baseband output on the cluster (`bias.source: qcm`). Without a source
that can actually hold a current the node **fails**, deliberately — an edge
declaring a bias nothing can deliver is a misconfiguration, and a sweep against
something that applies nothing returns the same frequency at every point and would
fit a confident crossing out of a flat line. The node restores the previous current
when it finishes, including on failure.

## Troubleshooting

**The `events` table keeps growing.** Confirm `eventsRetention > 0` and that the
retention engine logged `[Retention] Engine started`. If retention is long
relative to event volume, growth up to the steady-state size is expected — the
count should plateau once the oldest events start aging out.

**A driver's readings are missing from the dashboard.** Check the server log for
`rate limit exceeded` lines; the driver may be emitting faster than
`eventRateLimit`. Raise the limit or slow the driver's `every()` interval. Also
confirm the event type has a registered handler — unknown types are logged and
dropped by design.

**Pruning deleted too much / too little.** `eventsRetention` is the only lever;
it takes effect on the next `eventsPruneInterval` tick without a restart only if
supplied via config reload — otherwise restart the server after changing it.

**A calibration is stuck in `running`.** The request is released when its report
arrives, so a request that never leaves `running` means the tuner died mid-run —
check the driver's journal. Nothing was written to the device file (a failed run
does not write), so the QPU is unaffected. Set the row's status to `failed` in
the admin UI to let the next calibration through.

**The tuner will not start: "calibration config not found".** Deliberate.
`-o calibration_config=` must point at a real file, because a tuner with nothing
to run would otherwise report success having measured nothing.

**Only `coupler_anticrossing` failed: "no source that can actually hold a parking
current".** The rack did not open. The reason is in the driver's journal — the
resolution is logged where it fails — and it is almost always a missing
`-o spi_rack_address=`, couplers disagreeing about `bias.source`, or `qcm` on a
node with no cluster. Everything else in the run is unaffected; the coupler is left
at the current it started from.

**Every routine failed with a fit error.** Expected against a dummy cluster,
which returns no real data — the fits refuse rather than writing zeros to the
device. Against real hardware it means the sweep ranges in `calibration.yml` do
not bracket the feature being measured.

## Verify

```
make test-go                               # config, index, prune, rate-limit tests
make test-e2e-driver EXECUTOR=mock            # the driver framework end to end
```
