# RFC 0003 — Driver Extensibility

- **Status:** Draft
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
   A `DeviceSpec` says what a device is called, what `-o` options it takes, and how
   to build it. Nothing about a device is knowable only by running it — so
   `--help` can list every option, and every device is unit-testable without a
   server.
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

**QPI-UI keeps its own copy of the catalog.** `qpi-ui/internal/drivers/catalog.go`
independently lists the same devices, extras and `-o` options in order to render
the dashboard's setup snippets (RFC 0001 §3 step 2). Nothing checks the two agree.

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

`--device` defaults to its operation's default device (`mock` for `process`,
`bluefors_gen1` for `monitor`), so `qpi-driver start --operation process` runs a
mock QPU with no further arguments.

The transport flags are unchanged from RFC 0001: `--qpi-addr`/`-a`, `--token`/`-t`,
`--name`/`-n`, `--device`/`-d`, `--ca-file`, `--ca-fingerprint`, `--option`/`-o`,
`--recv-timeout-ms`, each with its existing environment variable and default.

Two commands make the catalog inspectable:

```
qpi-driver devices [--operation <op>]   # human-readable: operations → devices → options
qpi-driver catalog --json               # the whole registry, machine-readable
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

## 5. The device catalog

A device is described by data. Nothing about it should require running it to
discover.

```python
@dataclass(frozen=True)
class OptionSpec:
    key: str
    help: str
    parse: Callable[[str], Any] = str    # str | int | float | bool | Path | parse_channels
    default: Any = None
    required: bool = False
    example: str = ""

@dataclass(frozen=True)
class DeviceSpec:
    name: str                            # "qblox", "bluefors_gen1"
    operation: Operation
    build: DeviceBuilder                 # (options, **transport) -> unstarted driver
    options: tuple[OptionSpec, ...] = ()
    extra: str = ""                      # "qpi-driver[cli,qblox]"
    summary: str = ""
```

This is deliberately the same data-only shape `qpi-ui/internal/drivers` already
uses for its half of the catalog, extended with the fields `--help` needs. Specs
live beside the code they describe — the QPU module owns the `process` specs, the
Bluefors module owns its own — so a device and its description cannot drift apart.

Operations are described the same way, with their default device and the event
types they involve, mirroring what QPI-UI records for each kind:

```python
@dataclass(frozen=True)
class OperationSpec:
    name: Operation
    summary: str
    default_device: str
    events: tuple[EventType, ...]
```

The schema earns its keep four times over:

- **`--help`** lists every device for the chosen operation and every `-o` key it
  accepts, with defaults and examples — generated, never hand-maintained.
- **Unknown `-o` keys are rejected**, naming the valid keys, instead of being
  silently ignored.
- **Coercion happens once**, from `OptionSpec.parse`, rather than each device
  hand-rolling `int(...)` / boolean-ish string parsing / path validation.
- **`catalog --json`** gives QPI-UI a machine-readable copy to check its own
  catalog against (§9).

A device whose optional dependency is not installed is listed as
`qblox — unavailable: pip install "qpi-driver[cli,qblox]"`. Discovering what exists
must never require having installed all of it.

## 6. Extending: three routes, same registry

```python
def register(spec: DeviceSpec) -> None: ...
def operations() -> tuple[OperationSpec, ...]: ...
def devices(operation: Operation) -> tuple[DeviceSpec, ...]: ...
def resolve(operation: Operation, device: str) -> DeviceSpec: ...
```

**1. Built in.** The devices the SDK ships register themselves at import: one
declarative list of specs, the same way QPI-UI's catalog is one list of specs.

**2. Installed.** A third-party distribution advertises devices through the
`qpi_driver.devices` entry-point group. `pip install mylab-qpi-devices` and its
devices appear in `--help`, in `catalog --json`, and to `--device`, with no change
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

Either operation also accepts a `DeviceSpec`, which is how someone who wants their
own option schema and `--help` entry gets one.

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
| Spec + option schema as data | yes | yes | yes |
| Exported, extensible registry | `register()` | `Register()` from an importable package | `registerDevice()` |
| Generated `--help`, `devices`, `catalog --json` | yes | yes | yes |
| Installed third-party devices | entry points | — | — |
| Device named by import path | `mod:attr` | **not possible** | `./mod.js#Export` |

Go has no runtime import by name, so its extension story is compile-time and the
SDK must make that pleasant rather than pretend otherwise: a downstream program
imports the devices package, calls `Register`, and delegates to the SDK's root
command. That program gets the same CLI, the same generated help, and the same
option validation as the stock binary. Its `--device` accepts registered names
only, which is not a lesser mechanism — in Go it is *the* mechanism.

Where an SDK ships no device for an operation, `start --operation process` must say
so plainly — that this SDK ships no `process` devices, and where to find one — not
report an empty list of known devices. An SDK's default device comes from what it
actually registers.

## 9. Where truth lives

The catalog is split, and the split is not arbitrary:

- **Operations belong to QPI-UI.** An operation is the set of event types and
  payload shapes the server has handlers for. A driver cannot invent one; the SDKs
  mirror the operation enum the way they already mirror `EventType` (RFC 0001 §2).
  QPI-UI is the single source of truth, and if a driver ever needs to discover the
  operations a server supports, the server should advertise them rather than each
  SDK guessing.
- **Devices and their options belong to the driver.** Which backends exist, what
  `-o` keys each takes, which extra installs it — the SDK knows and the server does
  not. QPI-UI's copy exists only to render setup snippets at registration.

So `catalog --json` flows driver → server, and the operation enum flows server →
driver. Until the server can fetch a catalog from a connected driver, the near-term
mechanism is a test: QPI-UI checks its catalog against a checked-in
`catalog --json` fixture and fails when they disagree, so changing the device set
becomes a deliberate act rather than a silent divergence.

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
short form, environment variable and default is otherwise unchanged. Unknown `-o`
keys now fail instead of being ignored — which is the point, though it will surface
typos that had been quietly tolerated. The systemd installers, the dashboard's
generated snippets, and the end-to-end suites move with it.

**Python API.** The QPU convenience wrapper gives way to constructing the driver
directly (`QpuDriver(...).run()`), the two device registries and their callable type
alias give way to the registry functions in §6, and the per-device run functions
become builders returning a driver. The full removed → replacement table ships in
`CHANGELOG.md` with the release.

The driver-authoring surface — `QpiDriver`, `handle_event`, `emit`, `every`,
`Event`, `EventType`, `Executor` — does not change. Someone who has written a driver
against RFC 0001 keeps it; only how it is launched and registered moves.

## 12. Implementation plan

Maintained separately from this RFC, as with RFC 0001 §11: per-phase objectives,
status, definition-of-done checklists and verification commands. The sequence is
Python internals first (registry, schemas, extension routes), then the command
grammar across all three SDKs together with the installers and snippet generators,
then Go and TypeScript parity, then the catalog drift check, then documentation,
then coverage gates.

## 13. Decisions

Recorded here rather than in a separate ADR, per the RFC conventions.

1. **An operation is a closed enum; a device is open.** Operations require server
   handlers, so they cannot be added unilaterally; devices are pure
   implementation, so they can. This division is what makes a single extension
   mechanism sufficient.
2. **One CLI verb, `start`, with the operation as a value.** A new operation
   should cost a table entry, not a subcommand in three languages. §4.
3. **Devices are data plus a builder, and the builder returns an unstarted
   driver.** Testability and a single `run` in the SDK. §5, §7.
4. **Three registration routes, one registry** — built-in, entry point, import
   path — and no fourth mechanism for "custom drivers", because a custom driver is
   a custom device. §6.
5. **Import paths follow Pydantic's `ImportString`**, accepting `module:attr` and
   `module.attr`; a device value is an import path when it contains `.` or `:`.
   §6.
6. **The option schema is declared, and unknown options are errors.** Silently
   ignoring a mistyped option is worse than refusing to start. §5.
7. **`--operation` gets no short form**, because `-o` is `--option` and `-O`
   beside it is a hazard. §4.
8. **Operations flow server → SDK; the device catalog flows SDK → server**, with a
   fixture-based drift check until a driver can advertise its catalog on the wire.
   §9.
9. **Break once, with a published migration table**, rather than carrying
   deprecated aliases through a pre-1.0 project. §11.

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
