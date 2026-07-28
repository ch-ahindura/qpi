/**
 * The operations this SDK can run and the devices within them (RFC 0003 §5, §6).
 *
 * Separate from `cli.ts` because it is all decisions and no I/O: which operations
 * exist, which device an `--operation`/`--device` pair means, and what to fall back
 * to when a flag is omitted. `cli.ts` is then only commander wiring, and this can
 * be asserted on directly.
 *
 * Operations are closed — QPI-UI must have a server-side handler for each, so a new
 * one is a coordinated change across the SDKs, never a third-party extension
 * (RFC 0003 §13.1). Devices are the open half.
 */

import type { QpiDriver } from "../driver.js";

/** The universal options `start` shares across every operation. */
export interface CommonOpts {
  operation: string;
  qpiAddr: string;
  token: string;
  name: string;
  device: string;
  caFile: string;
  caFingerprint?: string;
  option: string[];
  recvTimeoutMs: number;
}

/**
 * Builds — but does not run — the driver for one device. Returning it rather than
 * running it is what lets a device be built and asserted on with no server, and
 * makes `driver.run()` the only thing that starts anything (RFC 0003 §7).
 */
export type DeviceRunner = (
  common: CommonOpts,
  opts: Record<string, string>,
) => QpiDriver;

/**
 * What `start --operation <name>` needs to know: the devices it can run, and what
 * to fall back to when --device and --name are omitted.
 */
export interface OperationSpec {
  summary: string;
  defaultDevice: string;
  defaultName: string;
  devices: Record<string, DeviceRunner>;
}

/**
 * Every operation, keyed by the `--operation` value. TypeScript ships no process
 * (QPU) device yet, so that operation's device table is empty — and it says so
 * rather than offering a default device it cannot honour (RFC 0003 §8).
 *
 * Adding a device is one entry in a `devices` map.
 */
export const operations: Record<string, OperationSpec> = {
  process: {
    summary: "Run quantum jobs pushed by QPI-UI and report their results.",
    defaultDevice: "",
    defaultName: "qpu_sim_01",
    devices: {},
  },
  monitor: {
    summary: "Report readings upward on a timer.",
    defaultDevice: "bluefors_gen1",
    defaultName: "qpi-monitor",
    devices: {},
  },
};

/** The operation names, sorted, for help and error messages. */
export function operationNames(): string {
  return Object.keys(operations).sort().join(", ");
}

/**
 * The operations and their devices, generated rather than written down, so a new
 * device shows up in `--help` with no change to the help text.
 */
export function operationHelp(): string {
  const lines = ["Operations (--operation), and the devices each can run:"];
  for (const name of Object.keys(operations).sort()) {
    const spec = operations[name];
    lines.push(`  ${name} — ${spec.summary}`);
    lines.push(
      Object.keys(spec.devices).length === 0
        ? `    (no ${name} devices in the TypeScript SDK; see the Python SDK, pip install "qpi-driver[cli]")`
        : `    ${Object.keys(spec.devices).sort().join(", ")}`,
    );
  }
  return lines.join("\n");
}

/** A device chosen, with the defaults its operation supplies already filled in. */
export interface ResolvedDevice {
  device: string;
  name: string;
  build: DeviceRunner;
}

/**
 * Works out which device to build from the flags, applying the operation's own
 * defaults for an omitted `--device` or `--name`.
 *
 * Throws an `Error` whose message is meant to be shown as-is: an operation with no
 * devices in this SDK says that and where to find one, rather than reporting an
 * unknown device with an empty list of known ones (RFC 0003 §8).
 */
export function resolveDevice(
  operation: string,
  device: string,
  name: string,
): ResolvedDevice {
  if (!operation) {
    throw new Error(
      `--operation is required; one of ${operationNames()} (or set QPI_OPERATION)`,
    );
  }
  const spec = operations[operation];
  if (!spec) {
    throw new Error(
      `unknown operation '${operation}'; valid operations: ${operationNames()}`,
    );
  }
  if (Object.keys(spec.devices).length === 0) {
    throw new Error(
      `the TypeScript SDK ships no ${operation} devices; run one from the Python ` +
        `SDK instead (pip install "qpi-driver[cli]") or add one to this CLI`,
    );
  }

  const chosen = device || spec.defaultDevice;
  const build = spec.devices[chosen];
  if (!build) {
    const known = Object.keys(spec.devices).sort().join(", ");
    throw new Error(
      `unknown ${operation} device '${chosen}'; known devices: ${known}`,
    );
  }
  return { device: chosen, name: name || spec.defaultName, build };
}
