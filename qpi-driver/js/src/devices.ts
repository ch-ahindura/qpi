/**
 * What `--device` can name in the TypeScript SDK (RFC 0003 §6, §8, §9) — the
 * counterpart of the Python SDK's `qpi_driver.builtins.registry` and the Go SDK's
 * `devices` package.
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
 * What a device *is*, here, is a name and a builder. It has no description and no
 * declared options, because none of that is the driver's to publish: QPI-UI is where
 * a device is chosen and configured, and a driver that also described itself would be
 * a second catalog to keep in step with the first. A driver runs what it is told to
 * run.
 *
 * @example Registering a device of your own
 * ```typescript
 * import { registerDevice, Operation } from "qpi-driver/devices";
 *
 * registerDevice({
 *   name: "thermometer",
 *   operation: Operation.Monitor,
 *   build: (cfg, opts) =>
 *     new ThermometerDriver({ ...cfg, probes: opts.int("probes", 1) }),
 * });
 * ```
 *
 * @packageDocumentation
 */

import type { QpiDriver } from "./driver.js";

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

/** One device: what `--device` names, and what to call when it does. */
export interface DeviceSpec {
  /** How `--device` names it, e.g. `"bluefors_gen1"`. */
  name: string;
  /** Which operation this device implements. */
  operation: Operation;
  /** Returns an unstarted driver; see {@link DeviceBuilder}. */
  build: DeviceBuilder;
}

/**
 * Every operation, in the order help and errors list them. Deliberately the same
 * order as the Python and Go SDKs'.
 */
const allOperations: Operation[] = [Operation.Process, Operation.Monitor];

/** Every operation, in declaration order. */
export function operations(): Operation[] {
  return [...allOperations];
}

/** The operation names, in declaration order, for help and error messages. */
export function operationNames(): string[] {
  return [...allOperations];
}

/** Whether a string is one of the two operations. */
export function knownOperation(operation: string): boolean {
  return (allOperations as string[]).includes(operation);
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
  if (!knownOperation(spec.operation)) {
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
 * registration order so `qpi-driver devices` is stable no matter what was imported
 * first.
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
  if (!knownOperation(operation)) {
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
    return { name: specifier, operation, build: value as DeviceBuilder };
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
 * The `-o key=value` settings a device was launched with.
 *
 * There is no option schema in this SDK. A device's options are whatever keys its
 * builder reads, and a value's type is whichever accessor reads it — so there is no
 * second description of a device to keep in step with the device, and nothing for
 * QPI-UI to be checked against.
 *
 * Every accessor takes the fallback used when the key is absent, written as the value
 * itself rather than as a string to parse: the default belongs to the one piece of
 * code that acts on it. A value the accessor cannot read throws, naming the option.
 *
 * Reads are remembered so {@link Options.unread} can report a key nothing looked at.
 * That is what keeps `-o pol_interval=2` an error rather than a setting silently
 * ignored, without a declared list of keys to check it against.
 */
export class Options {
  private readonly values: Record<string, string>;
  private readonly seen = new Set<string>();

  constructor(values: Record<string, string> = {}) {
    this.values = { ...values };
  }

  /** The option as typed, or `fallback` if it is absent. */
  str(key: string, fallback = ""): string {
    this.seen.add(key);
    return this.values[key] ?? fallback;
  }

  /**
   * The option, or a throw because the device cannot run without it. For the one
   * kind of option nothing can make up a value for.
   */
  require(key: string, example = ""): string {
    this.seen.add(key);
    if (!(key in this.values)) {
      const hint = example ? `, e.g. -o ${key}=${example}` : "";
      throw new Error(`missing required option '${key}'${hint}`);
    }
    return this.values[key];
  }

  /** The option as a whole number, or `fallback` if it is absent. */
  int(key: string, fallback: number): number {
    const raw = this.lookup(key);
    if (raw === undefined) return fallback;
    const value = Number(raw.trim());
    if (!Number.isInteger(value)) {
      throw new Error(`bad value for -o ${key}: '${raw}' is not a whole number`);
    }
    return value;
  }

  /** The option as a decimal number, or `fallback` if it is absent. */
  num(key: string, fallback: number): number {
    const raw = this.lookup(key);
    if (raw === undefined) return fallback;
    const value = Number(raw.trim());
    if (Number.isNaN(value)) {
      throw new Error(`bad value for -o ${key}: '${raw}' is not a number`);
    }
    return value;
  }

  /**
   * The option as a boolean, or `fallback` if it is absent. `1`, `true`, `yes` and
   * `on` are true in any case; every other value, the empty string included, is
   * false. Matches the Python and Go SDKs, so the same `-o is_dummy=on` means the
   * same thing in all three — and nothing here can fail, so a misspelling is false
   * rather than a refusal to start.
   */
  bool(key: string, fallback = false): boolean {
    const raw = this.lookup(key);
    if (raw === undefined) return fallback;
    return ["1", "true", "yes", "on"].includes(raw.trim().toLowerCase());
  }

  /**
   * A seconds-valued option as milliseconds, which is how every time-valued option
   * is written on a command line — seconds there, milliseconds in a JS timer.
   * Undefined when absent, so a driver's own default still applies.
   */
  ms(key: string): number | undefined {
    return this.lookup(key) === undefined ? undefined : this.num(key, 0) * 1000;
  }

  /**
   * Every option not read yet, as typed, and marks them read. For a device passing
   * options on to something this SDK has never seen; a device that reads its own
   * options should not need it.
   */
  remaining(): Record<string, string> {
    const rest: Record<string, string> = {};
    for (const [key, value] of Object.entries(this.values)) {
      if (!this.seen.has(key)) {
        rest[key] = value;
        this.seen.add(key);
      }
    }
    return rest;
  }

  /**
   * The keys given that nothing read, sorted. The caller reports them: the device
   * has finished building by then, so a key left over is one it does not understand.
   */
  unread(): string[] {
    return Object.keys(this.values)
      .filter((key) => !this.seen.has(key))
      .sort();
  }

  private lookup(key: string): string | undefined {
    this.seen.add(key);
    return this.values[key];
  }
}
