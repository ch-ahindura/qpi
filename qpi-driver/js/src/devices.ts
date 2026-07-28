/**
 * The operation and device catalog for the TypeScript SDK (RFC 0003 §5, §6, §8) —
 * the counterpart of the Python SDK's `qpi_driver.builtins.registry` and the Go
 * SDK's `devices` package.
 *
 * Two ideas, deliberately asymmetric:
 *
 * An **operation** is a contract with QPI-UI — `process` runs jobs pushed to it,
 * `monitor` reports upward on its own schedule. QPI-UI needs a server-side handler
 * for each, so the set is closed: {@link Operation} is the whole of it, and growing
 * it is a coordinated change across the SDKs and the server.
 *
 * A **device** is one backend implementing an operation. The set is open, and this
 * is where they land — by {@link registerDevice} from your own code, or by import
 * path on the command line (`--device ./my-device.js#MyExport`).
 *
 * A device is described by data ({@link DeviceSpec}), never by running it, so
 * `--help`, `catalog --json` and QPI-UI's own catalog are all generated from one
 * source. Specs live beside the code they describe.
 *
 * @example Registering a device of your own
 * ```typescript
 * import { QpiDriver } from "qpi-driver";
 * import { asInt, registerDevice, Operation } from "qpi-driver/devices";
 *
 * registerDevice({
 *   name: "thermometer",
 *   operation: Operation.Monitor,
 *   summary: "Reads a made-up thermometer.",
 *   options: [{ key: "probes", help: "How many.", type: "int", parse: asInt, default: "1" }],
 *   build: (cfg, opts) => new ThermometerDriver({ ...cfg, probes: opts.int("probes") }),
 * });
 * ```
 *
 * @packageDocumentation
 */

import type { QpiDriver } from "./driver.js";
import { EventType } from "./events.js";

/**
 * What a driver does, and the contract QPI-UI implements for it. Closed by design:
 * every value needs a handler on the server side, so a new operation is a
 * coordinated change across the SDKs and QPI-UI, never a third-party extension
 * (RFC 0003 §13.1). Devices are the open half.
 */
export enum Operation {
  Process = "process",
  Monitor = "monitor",
}

/** The universal transport options every driver is built with. */
export interface DeviceConfig {
  qpiAddr: string;
  token: string;
  name: string;
  caFingerprint: string;
  caFilePath?: string;
}

/**
 * Builds — but does not run — the driver for one device, from the universal
 * transport config and the device's own parsed options.
 *
 * Returning a driver rather than running one is what lets a device be built and
 * asserted on with no server, and keeps `driver.run()` the only thing that starts
 * anything (RFC 0003 §7). TypeScript had this right first; the Python and Go SDKs
 * were brought into line with it.
 */
export type DeviceBuilder = (
  config: DeviceConfig,
  options: Options,
) => QpiDriver;

/** Turns one raw `-o` string into the value a builder wants. */
export type Parser = (raw: string) => unknown;

/** One `-o key=value` setting a device reads. */
export interface OptionSpec {
  /** The option name as typed, e.g. `"channels"`. */
  key: string;
  /** One line for generated help. */
  help: string;
  /**
   * The kind of value expected — `"str"`, `"int"`, `"float"`, `"bool"`,
   * `"channels"`. It is what `catalog --json` reports, and must match what the
   * other SDKs report for the same device, since a test compares the catalogs
   * (RFC 0003 §9).
   */
  type?: string;
  /** Converts the raw string. Defaults to {@link asString}. */
  parse?: Parser;
  /**
   * The value used when the option is absent, written as it would be typed on the
   * command line; it goes through {@link OptionSpec.parse} like any other value.
   * Omitted means there is no default.
   */
  default?: string;
  /** Whether omitting the option is an error. */
  required?: boolean;
  /** A ready-to-paste value for generated help and snippets. */
  example?: string;
}

/** The data-only description of one device. */
export interface DeviceSpec {
  /** How `--device` names it, e.g. `"bluefors_gen1"`. */
  name: string;
  /** Which operation this device implements. */
  operation: Operation;
  /** Returns an unstarted driver; see {@link DeviceBuilder}. */
  build: DeviceBuilder;
  /** The `-o` keys this device reads. */
  options?: OptionSpec[];
  /**
   * The install target that ships it, e.g. `"qpi-driver[cli,qblox]"` — meaningful
   * for the Python SDK's devices, empty here where a device is imported.
   */
  extra?: string;
  /** One line describing the device, for generated help. */
  summary?: string;
  /**
   * Pass an undeclared `-o` key through as a raw string instead of rejecting it.
   * For a device with no schema to check against; a device in the catalog declares
   * its options and leaves this alone, so a typo stays an error.
   */
  acceptsAnyOption?: boolean;
}

/** The data-only description of one operation. */
export interface OperationSpec {
  name: Operation;
  summary: string;
  /**
   * The device used when `--device` is omitted. Empty when this SDK registers
   * none, in which case the CLI says so rather than offering a device it cannot
   * honour (RFC 0003 §8).
   */
  defaultDevice: string;
  /** The driver name used when `--name` is omitted. */
  defaultName: string;
  /** The event-type names drivers of this operation take part in. */
  events: EventType[];
}

/**
 * Every operation, in the order help and `catalog --json` list them. Deliberately
 * the same order and content as the Python and Go SDKs'.
 */
const operationSpecs: OperationSpec[] = [
  {
    name: Operation.Process,
    summary: "Run quantum jobs pushed by QPI-UI and report their results.",
    defaultDevice: "",
    defaultName: "qpu_sim_01",
    events: [EventType.JobDispatch, EventType.JobResult],
  },
  {
    name: Operation.Monitor,
    summary: "Report readings upward on a timer.",
    defaultDevice: "bluefors_gen1",
    defaultName: "qpi-monitor",
    events: [EventType.CryostatReading],
  },
];

/** Every operation, in declaration order. */
export function operations(): OperationSpec[] {
  return operationSpecs.map((spec) => ({ ...spec }));
}

/** The operation names, in declaration order, for help and error messages. */
export function operationNames(): string[] {
  return operationSpecs.map((spec) => spec.name);
}

/** The spec for one operation, or undefined if there is no such operation. */
export function lookupOperation(operation: string): OperationSpec | undefined {
  return operationSpecs.find((spec) => spec.name === operation);
}

/**
 * The devices registered so far, keyed by operation and then by name. Names are
 * unique per operation rather than globally, since `--device` is always read in the
 * context of an operation.
 */
const registry = new Map<Operation, Map<string, DeviceSpec>>();

/**
 * Add a device to the registry.
 *
 * Throws rather than replacing a device of the same name: silently replacing one
 * would make the winner depend on which module was imported first.
 */
export function registerDevice(spec: DeviceSpec): void {
  if (!spec.name || !spec.build) {
    throw new Error("qpi-driver: a device spec needs a name and a builder");
  }
  if (!lookupOperation(spec.operation)) {
    throw new Error(
      `qpi-driver: unknown operation '${spec.operation}' for device '${spec.name}'; ` +
        `valid operations: ${operationNames().join(", ")}`,
    );
  }
  const forOperation = registry.get(spec.operation) ?? new Map();
  if (forOperation.has(spec.name)) {
    throw new Error(
      `qpi-driver: ${spec.operation} device '${spec.name}' is already registered`,
    );
  }
  forOperation.set(spec.name, spec);
  registry.set(spec.operation, forOperation);
}

/**
 * Every device registered for an operation, sorted by name — sorted rather than in
 * registration order so generated help and `catalog --json` are stable no matter
 * what was imported first.
 */
export function devices(operation: Operation): DeviceSpec[] {
  return [...(registry.get(operation)?.values() ?? [])].sort((a, b) =>
    a.name.localeCompare(b.name),
  );
}

/** Whether an operation has a device of this name registered. */
export function hasDevice(operation: Operation, device: string): boolean {
  return registry.get(operation)?.has(device) ?? false;
}

/**
 * Remove every registered device. For tests, which must not leak devices into each
 * other or into what `--help` shows.
 */
export function clearDevices(): void {
  registry.clear();
}

/**
 * Look a device up within an operation, by registered name or by import path.
 *
 * A specifier containing `#` is an import path — `./my-device.js#MyExport` — where
 * Python uses `mod:attr`. The separator differs because `:` is a URL scheme
 * separator in a JavaScript module specifier, so `./m.js:X` could not be told from
 * a URL; `#` cannot appear in a bare identifier either, so the two forms never
 * collide and a mistyped `bluefors` still gets the known-devices error rather than
 * an import failure.
 *
 * What the imported value must be is a {@link DeviceSpec}, or a builder — for which
 * the operation and name are taken from the specifier.
 */
export async function resolve(
  operation: Operation,
  device: string,
): Promise<DeviceSpec> {
  if (device.includes("#")) {
    return importedSpec(operation, device);
  }
  if (!lookupOperation(operation)) {
    throw new Error(
      `unknown operation '${operation}'; valid operations: ${operationNames().join(", ")}`,
    );
  }

  const available = devices(operation);
  if (available.length === 0) {
    throw new Error(
      `the TypeScript SDK ships no ${operation} devices; run one from the Python ` +
        `SDK instead (pip install "qpi-driver[cli]") or register one of your own`,
    );
  }

  const spec = registry.get(operation)?.get(device);
  if (!spec) {
    throw new Error(
      `unknown ${operation} device '${device}'; known devices: ` +
        available.map((each) => each.name).join(", "),
    );
  }
  return spec;
}

/**
 * Import `./module.js#Export` and adapt what it exports into a device.
 *
 * The error is one line with no stack: an operator gets a stack trace for a bug,
 * not for a typo, and a trace here would carry filesystem paths into the journal
 * (RFC 0003 §10).
 */
async function importedSpec(
  operation: Operation,
  specifier: string,
): Promise<DeviceSpec> {
  const hash = specifier.lastIndexOf("#");
  const modulePath = specifier.slice(0, hash);
  const exportName = specifier.slice(hash + 1);
  if (!modulePath || !exportName) {
    throw new Error(
      `'${specifier}' is not an import path; expected ./module.js#exportName`,
    );
  }

  let imported: Record<string, unknown>;
  try {
    imported = (await import(resolveSpecifier(modulePath))) as Record<
      string,
      unknown
    >;
  } catch (err) {
    throw new Error(
      `could not import device '${specifier}': ${messageOf(err)}`,
    );
  }
  if (!(exportName in imported)) {
    throw new Error(
      `could not import device '${specifier}': ${modulePath} has no export '${exportName}'`,
    );
  }

  const value = imported[exportName];
  if (isDeviceSpec(value)) {
    if (value.operation !== operation) {
      throw new Error(
        `'${specifier}' is a ${value.operation} device, not a ${operation} one`,
      );
    }
    return value;
  }
  if (typeof value === "function") {
    return {
      name: specifier,
      operation,
      build: value as DeviceBuilder,
      summary: `Device builder imported from ${specifier}.`,
      acceptsAnyOption: true,
    };
  }
  throw new Error(
    `'${specifier}' is not usable as a ${operation} device: expected a device ` +
      `builder or a DeviceSpec`,
  );
}

/** A relative path is relative to the working directory, not to this module. */
function resolveSpecifier(modulePath: string): string {
  if (modulePath.startsWith(".")) {
    return new URL(modulePath, `file://${process.cwd()}/`).href;
  }
  return modulePath;
}

function isDeviceSpec(value: unknown): value is DeviceSpec {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as DeviceSpec).name === "string" &&
    typeof (value as DeviceSpec).build === "function"
  );
}

/**
 * One line out of an error, with the stack and any filesystem path it volunteers
 * left off (RFC 0003 §10).
 *
 * Node is generous with paths here — a failed dynamic import reports both the
 * module it could not find and the file that asked for it, as absolute paths. An
 * operator gets a stack trace for a bug, not for a typo, and neither the trace nor
 * the layout of the machine belongs in a driver's journal, so an absolute path is
 * reduced to its last segment: the operator typed that much themselves.
 */
function messageOf(err: unknown): string {
  const message = err instanceof Error ? err.message : String(err);
  return message
    .split("\n")[0]
    .replace(/\s*imported from\s+\S+/g, "")
    .replace(
      /(?:file:\/\/)?[^\s'"`]*[\\/][^\s'"`]*/g,
      (token) => token.split(/[\\/]/).filter(Boolean).pop() ?? token,
    )
    .trim();
}

/**
 * A device's `-o` values, already checked and converted by its own schema. The
 * accessors are lookups, not conversions: whatever a value is going to be, it
 * became that in {@link parseOptions}.
 */
export class Options {
  constructor(private readonly values: Record<string, unknown> = {}) {}

  /** Whether the option is present at all — "not set" versus "set to nothing". */
  has(key: string): boolean {
    return key in this.values;
  }

  /** The stored value, whatever its type. */
  raw(key: string): unknown {
    return this.values[key];
  }

  /** A string option, or `""` if it is absent or not a string. */
  str(key: string): string {
    const value = this.values[key];
    return typeof value === "string" ? value : "";
  }

  /** A numeric option, or 0 if it is absent or not a number. */
  num(key: string): number {
    const value = this.values[key];
    return typeof value === "number" ? value : 0;
  }

  /** An integer option, or 0. The same store as {@link Options.num}. */
  int(key: string): number {
    return this.num(key);
  }

  /** A boolean option, or false if it is absent or not a boolean. */
  bool(key: string): boolean {
    return this.values[key] === true;
  }

  /**
   * A seconds-valued option as milliseconds, which is how every time-valued option
   * in the catalog is declared — seconds on the command line, milliseconds in a JS
   * timer. Undefined when absent, so a driver's own default still applies.
   */
  ms(key: string): number | undefined {
    return this.has(key) ? this.num(key) * 1000 : undefined;
  }

  /** A channel-map option, or `{}` if it is absent or not one. */
  channels(key: string): Record<string, string> {
    const value = this.values[key];
    return typeof value === "object" && value !== null
      ? (value as Record<string, string>)
      : {};
  }

  /** Every stored value, for tests and for passing on wholesale. */
  all(): Record<string, unknown> {
    return { ...this.values };
  }
}

/** The default parser: no conversion at all. */
export function asString(raw: string): unknown {
  return raw;
}

/** Parses a whole number. */
export function asInt(raw: string): unknown {
  const value = Number(raw.trim());
  if (!Number.isInteger(value)) {
    throw new Error(`'${raw}' is not a whole number`);
  }
  return value;
}

/** Parses a decimal number, which is how seconds are given. */
export function asFloat(raw: string): unknown {
  const value = Number(raw.trim());
  if (Number.isNaN(value)) {
    throw new Error(`'${raw}' is not a number`);
  }
  return value;
}

/**
 * Parses a boolean-ish value: `1`, `true`, `yes` and `on` are true, in any case;
 * anything else, including the empty string, is false. Matches the Python and Go
 * SDKs, so the same `-o is_dummy=on` means the same thing in all three.
 */
export function asBool(raw: string): unknown {
  return ["1", "true", "yes", "on"].includes(raw.trim().toLowerCase());
}

/**
 * Check raw `-o` values against a device's schema and convert them.
 *
 * The result is what {@link DeviceSpec.build} is called with, and carries every
 * option that has a value — the ones given, plus the parsed default of each one
 * left out — so a builder reads its keys without repeating their defaults, and
 * conversion happens here rather than in every builder. A key the device does not
 * declare is an error unless {@link DeviceSpec.acceptsAnyOption} says to pass it
 * through.
 */
export function parseOptions(
  spec: DeviceSpec,
  raw: Record<string, string>,
): Options {
  const declared = new Map((spec.options ?? []).map((o) => [o.key, o]));
  const valid = [...declared.keys()].sort();
  const parsed: Record<string, unknown> = {};

  const unknown = Object.keys(raw)
    .filter((key) => !declared.has(key))
    .sort();
  if (unknown.length > 0 && !spec.acceptsAnyOption) {
    const label = unknown.length > 1 ? "options" : "option";
    throw new Error(
      `unknown ${label} ${unknown.map((k) => `'${k}'`).join(", ")} for ` +
        `${spec.operation} device '${spec.name}'; valid options: ` +
        `${valid.length > 0 ? valid.join(", ") : "none"}`,
    );
  }
  // With no schema there is nothing to convert an undeclared option to, and
  // guessing would be worse than leaving it as typed.
  for (const key of unknown) {
    parsed[key] = raw[key];
  }

  for (const option of spec.options ?? []) {
    let value: string;
    if (option.key in raw) {
      value = raw[option.key];
    } else if (option.required) {
      const example = option.example
        ? `, e.g. -o ${option.key}=${option.example}`
        : "";
      throw new Error(
        `${spec.operation} device '${spec.name}' needs a '${option.key}' option${example}`,
      );
    } else if (option.default === undefined || option.default === "") {
      continue;
    } else {
      value = option.default;
    }

    try {
      parsed[option.key] = (option.parse ?? asString)(value);
    } catch (err) {
      throw new Error(`bad value for -o ${option.key}: ${messageOf(err)}`);
    }
  }

  return new Options(parsed);
}
