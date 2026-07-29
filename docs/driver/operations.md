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

## Verify

```
make test-go                               # config, index, prune, rate-limit tests
make test-e2e-driver EXECUTOR=mock            # the driver framework end to end
```
