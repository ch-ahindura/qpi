# RFC 0003 — Driver Extensibility

- **Status:** Implemented
- **Author:** Martin Ahindura
- **Created:** 2026-07-27
- **Touches:** the driver SDKs (`qpi-driver/py`, `qpi-driver/go`, `qpi-driver/js`), `qpi-ui` (Go/PocketBase), docs
- **Builds on:** [RFC 0001 — Driver Framework](./0001-driver-framework.md) §3, §4, §7
- **Issue:** TBD

## 1. The idea

RFC 0001 made "a driver" a first-class thing: an external process that exchanges
typed events with QPI-UI, built against an SDK. It delivered two kinds of driver —
a QPU that runs jobs (`process`) and a cryostat monitor that reports upward
(`monitor`) — and a CLI that launches either.

What it did not deliver is a way for anyone **outside this repository** to add one.
Today the set of runnable backends is two dictionaries compiled into the Python
SDK. A lab with a cryostat we have never heard of, or a QPU control stack we do not
ship an executor for, has exactly one route: fork the SDK, or write a bespoke
program and give up the CLI, the systemd installer, and the dashboard's setup
snippets along with it.

This RFC makes the backend set **open**, and does it without adding a second way to
do anything. Three ideas carry the whole design:

1. **An operation is a contract; a device is an implementation.** The operation
   (`process`, `monitor`) is a fixed enum, because QPI-UI must have a handler for
   the events it involves. The device — which backend implements that contract — is
   open-ended, and that is where extension happens.
2. **A device is described by data, and built by a function that returns a driver.**
   A `DeviceSpec` is a name, an operation and a builder. The builder returns the
   driver rather than running it, so every device is unit-testable without a server.
   What its `-o` keys mean is QPI-UI's to document, not the SDK's (§9).
3. **One CLI verb.** `qpi-driver start --operation <op> --device <dev>`. Adding an
   operation stops meaning "add a subcommand", and adding a device stops meaning
   "edit the SDK".

## 2. Vocabulary

Extends RFC 0001 §2, which defines *driver*, *event*, *event type* and *SDK*.

| Term | Meaning |
| --- | --- |
| **Operation** | What a driver *does*, as a contract with QPI-UI: which event types flow, in which direction, with which payload shapes. A closed enum — `process`, `monitor` today — because each operation needs a QPI-UI handler to exist. Generalises what RFC 0001 §4 called the CLI subcommand. |
| **Device** | The specific backend implementing an operation: `qblox` and `mock` are `process` devices, `bluefors_gen1` is a `monitor` device. Open-ended. What `--device` selects. |
| **Device spec** | The data-only description of one device: its name, its operation, its `-o` option schema, its install extra, and its builder. |
| **Device builder** | A function that takes the transport settings plus parsed `-o` options and **returns an unstarted driver**. It does not run it. |
| **Option schema** | The declared `-o key=value` settings a device accepts — key, help text, type, default, whether required. |

An operation is to a device roughly what an OS device class is to a driver for a
particular card: the class fixes the interface, the driver implements it for one
piece of hardware.

## 3. What is wrong today

Grounding, so an implementer fixes the actual problems rather than the ones they
imagine.

**Backends are a closed set.** `PROCESS_DRIVERS` and `MONITOR_DRIVERS`
(`qpi-driver/py/qpi_driver/builtins/__init__.py`) map a device name to a run
function. There is no registration hook, no plugin mechanism, and no way to name a
backend the SDK was not compiled with. Adding a device means a pull request here.

**`-o` options are undiscoverable and unchecked.** `--help` says "See the chosen
device for the keys it reads", which is an instruction to go read the source. Worse,
each device pulls the keys it wants out of the option dict and silently ignores the
rest, so `-o data_dirr=/data` runs cheerfully with the default data directory. The
same is true in all three SDKs.

**The pluggable unit runs instead of building, and so cannot be tested.** In
Python a device's entry point is `Callable[..., None]` that blocks until
interrupted. The only way to assert that `-o job_timeout=30` reaches the driver is
to monkeypatch the layer beneath it. Three separate things are also all spelled
`run` — the event loop, a construct-and-run convenience, and the per-device
adapter — which makes the call path harder to follow than it needs to be.

**Every operation costs a subcommand.** `process` and `monitor` are near-identical
command bodies differing in one dictionary, and the helper they share takes that
dictionary as its first parameter. A third operation means a third copy.

**The three SDKs have drifted.** Python's device entry point blocks and returns
`None`; Go's runs and returns `error`; TypeScript's builds and returns a driver
(which is the shape this RFC adopts). Neither Go nor TypeScript ships a `process`
device at all, yet both advertise `--device mock` as the default, so
`qpi-driver process` in those SDKs fails with `unknown process device "mock";
known devices: ` and an empty list.

**The catalog exists twice.** `qpi-ui/internal/drivers/catalog.go` lists the same
devices, extras and `-o` options as the SDKs do, in order to render the dashboard's
setup snippets (RFC 0001 §3 step 2). Nothing checks the two agree, and there is no
good reason for there to be two — see §9.

## 4. The command line

One verb. The operation and the device are both values:

```
qpi-driver start --operation <op> --device <dev> [-o key=value ...] [transport flags]
```

```bash
# a mock QPU
qpi-driver start --operation process --device mock -o data_dir=./data

# a cryostat monitor
qpi-driver start --operation monitor --device bluefors_gen1 \
  -o base_url=http://localhost:49099 -o channels=mapper.bf.tmc:K,mapper.bf.pmc:mbar

# a backend this SDK has never heard of
qpi-driver start --operation process --device mylab.executors:PrestoV2
```

`--operation` is required, takes one of the known operations, and falls back to
`QPI_OPERATION`. It has **no short form**: `-o` is already `--option`, and a
capital `-O` beside a lowercase `-o` is a typo waiting to happen. Being a
set-once value in a systemd unit, it does not need one.

`--device` is required, and falls back to `QPI_DEVICE`. There is no default: QPI-UI
generates the command that launches a driver and it always names a device, so a value
the SDK filled in could only be a guess (§9).

The transport flags are unchanged from RFC 0001: `--qpi-addr`/`-a`, `--token`/`-t`,
`--device`/`-d`, `--ca-file`, `--ca-fingerprint`, `--option`/`-o`,
`--recv-timeout-ms`, each with its existing environment variable and default.
`--name`/`-n` is gone (§11).

One command says what a build can run:

```
qpi-driver devices [--operation <op>]   # the registered names, by operation
```

### Why one verb rather than a subcommand per operation

An operation is data — it names a contract with the server. Encoding it in the
command name means every new operation is a new code path in every SDK, plus a new
branch in the installers and in the dashboard's snippet templates. Encoding it as a
validated enum value means a new operation is a new entry in a table, which is the
same thing this RFC does for devices. The uniformity is the point: an operator
learns one command, and a contributor adding a monitoring operation touches a
table rather than a CLI.

The cost is honest: `start --operation process` is longer to type than `process`,
and it is a breaking change to every published invocation. Both are accepted —
see §11.

## 5. A device is a name and a builder

```python
@dataclass(frozen=True)
class DeviceSpec:
    name: str                            # "qblox", "bluefors_gen1"
    operation: Operation
    build: DeviceBuilder                 # (options, **transport) -> unstarted driver
```

That is the whole of it. A device has no summary, no declared option schema and no
install target, because none of that is the driver's to publish — see §9. Specs live
beside the code they describe: the QPU module owns the `process` specs, the Bluefors
module owns its own.

`-o` values reach the builder as the strings they were typed as, in an `Options`
object with typed accessors that each take the fallback:

```python
data_dir = options.get_dir("data_dir", "./bin/data")
interval = options.get_float("poll_interval", 5.0)
channels = parse_channels(options.require("channels", "mapper.bf.tmc:K"))
```

Two consequences, both deliberate:

- **The default lives in the one piece of code that acts on it**, rather than as a
  string in a schema that something else parses.
- **What a device accepts is the set of keys it reads.** `Options` remembers the
  reads, so an `-o` key nothing looked at is reported after the build —
  `unknown option 'data_dirr' for process device 'mock'` — which keeps a typo an
  error without a declared list to check it against. That matters more than it
  sounds: every built-in executor's constructor takes `**kwargs`, so an unread key
  would otherwise vanish silently.

`qpi-driver devices` lists the names a build has, and nothing else. It answers the
one question the driver is the authority on — *what can this node run?* — which is
what an operator asks after `pip install`ing a distribution of devices or writing
one.

## 6. Extending: three routes, same registry

```python
def register(spec: DeviceSpec) -> None: ...
def devices(operation: Operation) -> tuple[DeviceSpec, ...]: ...
def resolve(operation: Operation, device: str) -> DeviceSpec: ...
```

**1. Built in.** The devices the SDK ships register themselves at import: one
declarative list of specs.

**2. Installed.** A third-party distribution advertises devices through the
`qpi_driver.devices` entry-point group. `pip install mylab-qpi-devices` and its
devices are listed by `qpi-driver devices` and accepted by `--device`, with no change
here. This is the route for a device meant to be shared, versioned and installed
on a production node.

**3. Named by import path.** `--device mylab.executors:PrestoV2` resolves the
device by importing it, with no packaging ceremony at all. This is the route for
"I have a class in a file and I want to run it now".

A device value is treated as an import path when it contains `.` or `:`, and as a
registered name otherwise — registered names are plain identifiers, so the two
never collide, and a mistyped `mockk` still gets `unknown process device 'mockk';
known devices: …` rather than an import error. Both `module:attr` and `module.attr`
are accepted, following Pydantic's `ImportString` convention.

What the imported object must be depends on the operation, because that is already
what a device means — for `process` a device is an executor, for `monitor` it is
the driver itself:

| Operation | Import target | Result |
| --- | --- | --- |
| `process` | `Executor` subclass or instance | wrapped in the built-in QPU driver |
| `monitor` | `DeviceSpec`, or a device builder | used directly |

Either operation also accepts a `DeviceSpec`, which is how a device gets a name of
its own rather than being known by the specifier that imported it. An executor
imported this way is handed every `-o` key the SDK does not read itself, as typed:
its constructor is the only thing that knows those keys exist.

Notably, there is **no separate mechanism for "a whole custom driver"**. Because an
operation is a contract QPI-UI must already implement, a custom driver is always a
custom device of an existing operation. One concept covers it.

## 7. The SDK shape

A device builder returns an unstarted driver; the CLI starts it:

```python
DeviceBuilder = Callable[..., QpiDriver]

driver = spec.build(options=parsed_options, **transport)
driver.run()
```

That inversion is small and buys three things: a device can be built and asserted
on in a unit test with no server; `QpiDriver.run()` becomes the only thing in the
SDK that runs anything; and the Python, Go and TypeScript SDKs finally agree on one
contract — TypeScript's, which had it right.

Everything else in RFC 0001 §4 is unchanged. A driver still subclasses the SDK base,
still implements `handle_event`, still calls `emit` and `every`. Writing a driver is
not what this RFC changes; making one *runnable by the CLI* is.

## 8. Cross-language parity

| Capability | Python | Go | TypeScript |
| --- | --- | --- | --- |
| `start --operation … --device …` | yes | yes | yes |
| Builder returns an unstarted driver | yes | yes | already does |
| Builder reads raw `-o` options via `Options` | yes | yes | yes |
| Exported, extensible registry | `register()` | `Register()` from an importable package | `registerDevice()` |
| `devices` lists what the build has | yes | yes | yes |
| Installed third-party devices | entry points | — | — |
| Device named by import path | `mod:attr` | **not possible** | `./mod.js#Export` |

Go has no runtime import by name, so its extension story is compile-time and the
SDK must make that pleasant rather than pretend otherwise: a downstream program
imports the devices package, calls `Register`, and delegates to the SDK's root
command. That program gets the same CLI, the same generated help, and the same
option validation as the stock binary. Its `--device` accepts registered names
only, which is not a lesser mechanism — in Go it is *the* mechanism.

Where an SDK ships no device for an operation, `start --operation process` must say
so plainly — that this SDK ships no `process` devices, and where to find one — rather
than report an empty list of known devices. No SDK has a default device (§4).

## 9. Where truth lives

**QPI-UI is the single source of truth, for operations and for devices alike.** It is
where a driver is registered, where a device is chosen, where its `-o` options are
described and filled in, and where the command that launches it is generated. The
driver SDKs publish no catalog at all.

This reverses an earlier version of this RFC, which split the two: operations to the
server, devices and their options to the driver, reconciled by `catalog --json` and a
drift test over checked-in fixtures. That did not survive contact with the work. The
split meant the same device was described twice — once in `qpi-ui/internal/drivers`,
once in each SDK — and the thing keeping the copies together was a `make` target,
`sync-driver-catalog`, that a person had to remember to run after touching either
side. Three SDKs' worth of `OptionSpec` tables, three checked-in JSON fixtures, a
renderer, a generated README table and a drift test, all in service of an agreement
that a single description makes free.

What the driver keeps is what only it can know:

- **Which devices this particular build has.** `qpi-driver devices` reads its own
  registry — the honest answer for a node, since it depends on the extras installed,
  the entry points present and, in Go, what was compiled in. Names only; a name is
  enough to run one.
- **Whether the options it was given make sense.** The builder reads what it
  understands and the CLI reports the rest (§5).

The driver is not another client of QPI-UI. It receives work and reports results; it
does not ask the server about itself. If a driver-side view of the catalog ever
becomes genuinely necessary, the server should serve it over the connection the driver
already has, authenticated by the token it already holds — but nothing needs it today,
and adding it would invert the relationship for no gain.

## 10. Security

Routes 1 and 2 of §6 change nothing: code arrives by `pip install`, as it always
has.

Route 3 — `--device mylab:Thing` — executes arbitrary importable code, exactly as
`python -m mylab` does. It is an operator-supplied argument on the operator's own
machine, not anything QPI-UI can influence: the server never sends a device name,
and a driver's own registration cannot introduce one. Two obligations follow, both
of them local:

- A failed import reports one clean line, not a traceback carrying filesystem
  paths into the journal.
- Neither the import-path route nor the entry-point route may bypass the existing
  safe-path checks on `--ca-file` or on a device's path-valued options.

RFC 0001 §9 otherwise stands unchanged: TLS everywhere, CA pinned by fingerprint,
tokens stored hashed, and QPI-UI still stores no driver code. Where an SDK has
drifted from that — accepting a connection without verifying the pinned
fingerprint, for instance — this work brings it back in line rather than leaving the
weaker path available by omission.

## 11. Migration

This is a breaking release for the driver CLI and the Python SDK's API. The project
is pre-1.0 and makes no stability promise, so the design breaks what has stopped
earning its keep instead of accumulating aliases. The obligation that comes with
that is to break **once**, and to publish a table rather than let users discover
removals one traceback at a time.

**Command line.** `qpi-driver process …` and `qpi-driver monitor …` become
`qpi-driver start --operation process …` and `--operation monitor …`. Every flag,
short form, environment variable and default is otherwise unchanged, bar one:
`--name`/`-n` and `QPI_DRIVER_NAME` are removed, because a driver's display label
belongs to the admin who registered it in the dashboard and `drivers/connect` hands
it back rather than accepting one. Unknown `-o` keys now fail instead of being
ignored — which is the point, though it will surface typos that had been quietly
tolerated. The systemd installers, the dashboard's generated snippets, and the
end-to-end suites move with it; the installers' `QPU_NAME` becomes `SERVICE_NAME`,
which is what it always named.

**Python API.** The QPU convenience wrapper gives way to constructing the driver
directly (`QpuDriver(...).run()`), the two device registries and their callable type
alias give way to the registry functions in §6, and the per-device run functions
become builders returning a driver. The full removed → replacement table ships in
`CHANGELOG.md` with the release.

The driver-authoring surface — `QpiDriver`, `handle_event`, `emit`, `every`,
`Event`, `EventType`, `Executor` — does not change. Someone who has written a driver
against RFC 0001 keeps it; only how it is launched and registered moves.

## 12. Implementation plan

Complete. The sequence was Python internals first (registry, extension routes), then
the command grammar across all three SDKs together with the installers and snippet
generators, then Go and TypeScript parity, then documentation, then coverage gates.
The phased plan itself was kept outside the repository.

## 13. Decisions

Recorded here rather than in a separate ADR, per the RFC conventions.

1. **An operation is a closed enum; a device is open.** §1, §2.
2. **One CLI verb, `start`, with the operation as a value.** §4.
3. **A device is a name, an operation and a builder; the builder returns an unstarted
   driver.** §5, §7.
4. **Three registration routes, one registry**, and no fourth mechanism for a "custom
   driver" — a custom driver is a custom device. §6.
5. **Import paths follow Pydantic's `ImportString`**, `module:attr` or `module.attr`;
   a device value is an import path when it contains `.` or `:`. §6.
6. **There is no option schema.** Reading an option declares it, and an option
   nothing read is an error. §5.
7. **`--operation` gets no short form**, because `-o` is `--option`. §4.
8. **QPI-UI holds the whole catalog; the driver publishes none.** Reverses an earlier
   decision taken here; §9 records why.
9. **Break once, with a published migration table.** §11.

**Rejected.** A dedicated flag naming a callable to run an operation: it answers
the question `--device` already asks, and it puts an SDK-internal concept in the
operator's vocabulary. A JSON-valued `-o` option mapping names to import paths:
nesting a second syntax inside a `key=value` flag, when the device name can simply
*be* the path. Keeping a subcommand per operation: cheaper today, a per-language
code path per operation forever. Making the built-in device list discovery-only:
entry points come from installed distribution metadata, so the SDK would break
wherever it is used from a source tree rather than an install.

## 14. Notes

The documentation predates the framework in places and describes the SDK as though
it were a QPU driver with some extras, rather than a driver framework of which the
QPU is one instance — including the top-level README and a Python architecture
diagram showing a result-sending process that is in fact a thread. Correcting that
framing is part of this work, not a follow-up: the extension mechanism above is
close to unusable if the surrounding prose does not present devices and operations
as the first-class things they are.
