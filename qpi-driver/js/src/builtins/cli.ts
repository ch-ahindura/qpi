#!/usr/bin/env node
/**
 * The `qpi-driver` CLI that runs QPI's officially maintained TypeScript built-in
 * drivers, mirroring the Python `qpi-driver` CLI (RFC 0003 §4): one `start` verb,
 * `--operation` saying what the driver does, `--device` selecting the backend
 * within it, universal flags shared, and a device's own settings passed as
 * repeatable `-o key=value`.
 *
 * Installed as the package's `qpi-driver` bin, so:
 *
 *   npm install -g qpi-driver   # or: npx -y qpi-driver …
 *   qpi-driver start --operation monitor --device bluefors_gen1 \
 *     --qpi-addr https://qpi.example.com --token … --ca-fingerprint … \
 *     -o base_url=http://localhost:49099 -o channels=mapper.bf.tmc:K
 *
 * Everything it knows about devices comes from `qpi-driver/devices`, so a device of
 * your own — registered with `registerDevice`, or named by import path — is run
 * exactly as a built-in is. This file is only wiring.
 */

import { Command } from "commander";

import {
  type DeviceConfig,
  devices,
  knownOperation,
  Operation,
  operationNames,
  operations,
  Options,
  registerDevice,
  resolve,
} from "../devices.js";
import { DEVICE_SPEC as BLUEFORS_GEN1 } from "./bluefors-gen1.device.js";

const VERSION = "0.1.2";

// The devices this build ships. One line per device, and the same line a bundle of
// your own writes for a device of its own (RFC 0003 §6).
registerDevice(BLUEFORS_GEN1);

/** The universal options `start` shares across every operation. */
interface CommonOpts {
  operation: string;
  qpiAddr: string;
  token: string;
  device: string;
  caFile: string;
  caFingerprint: string;
  option: string[];
}

/**
 * Resolves the device within the chosen operation, hands it its `-o` options, and
 * runs the driver its builder returns.
 *
 * An option no device read is reported after the build rather than checked against a
 * declared list beforehand: the device's own code is the only description of what it
 * accepts (RFC 0003 §9).
 */
async function runStart(common: CommonOpts): Promise<void> {
  if (!common.operation) {
    fail(
      `--operation is required; one of ${operationNames().join(", ")} (or set QPI_OPERATION)`,
    );
  }
  const operation = common.operation as Operation;
  if (!knownOperation(operation)) {
    fail(
      `unknown operation '${common.operation}'; valid operations: ${operationNames().join(", ")}`,
    );
  }
  if (!common.token) {
    fail(
      "access token is required; set --token/-t or the QPI_ACCESS_TOKEN environment variable",
    );
  }
  // Required, and with no way to opt out: an unpinned CA reachable by omitting an
  // argument is one a copy-pasted command hits by accident (RFC 0003 §10).
  if (!common.caFingerprint) {
    fail(
      "a CA fingerprint is required; set --ca-fingerprint or the QPI_CA_FINGERPRINT " +
        "environment variable. It is shown when the driver is registered in the dashboard",
    );
  }

  // There is no default device: QPI-UI generates the command that launches a driver
  // and it always names one. Say what this build has instead of guessing — unless it
  // has none, in which case resolve's own message is the one worth printing.
  const available = devices(operation).map((each) => each.name);
  if (!common.device && available.length > 0) {
    fail(
      `--device is required: this build's ${operation} devices are ${available.join(", ")}`,
    );
  }

  try {
    const resolved = await resolve(operation, common.device);
    const options = new Options(splitOptions(common.option));
    const config: DeviceConfig = {
      qpiAddr: common.qpiAddr,
      token: common.token,
      caFingerprint: common.caFingerprint,
      caFilePath: common.caFile,
    };
    const driver = resolved.build(config, options);
    const unread = options.unread();
    if (unread.length > 0) {
      const label = unread.length > 1 ? "options" : "option";
      fail(
        `unknown ${label} ${unread.map((k) => `'${k}'`).join(", ")} for ` +
          `${operation} device '${common.device}'`,
      );
    }
    await driver.run();
  } catch (err) {
    fail((err as Error).message);
  }
}

/**
 * Turns repeatable `-o key=value` flags into raw strings. Only the syntax is the
 * CLI's business; which keys mean anything and what type each value has belong to
 * the device that reads them.
 */
export function splitOptions(pairs: string[]): Record<string, string> {
  const opts: Record<string, string> = {};
  for (const pair of pairs) {
    const eq = pair.indexOf("=");
    const key = eq >= 0 ? pair.slice(0, eq).trim() : "";
    if (!key) {
      throw new Error(`invalid option '${pair}'; expected key=value`);
    }
    opts[key] = pair.slice(eq + 1).trim();
  }
  return opts;
}

function fail(message: string): never {
  console.error(`Error: ${message}`);
  process.exit(1);
}

function collect(value: string, previous: string[]): string[] {
  return previous.concat([value]);
}

function envOr(key: string, fallback: string): string {
  return process.env[key] || fallback;
}

/**
 * Adds the one verb that runs a driver. One command rather than a subcommand per
 * operation, because everything about launching a driver is the same whichever
 * operation it is (RFC 0003 §4).
 */
function addStart(program: Command): void {
  program
    .command("start")
    .description(
      "Run a driver: one --operation, on one --device within it (RFC 0001 §4).",
    )
    .addHelpText(
      "after",
      "\nQPI-UI is where a driver is registered and where the command to run it —\n" +
        "this operation, this device, these -o options — is generated. Run\n" +
        "`qpi-driver devices` to see which devices this build has.\n",
    )
    // No short form for --operation: -o is --option, and -O beside it would be a
    // hazard in a command usually written once into a unit file (RFC 0003 §13.7).
    .option(
      "--operation <operation>",
      `What this driver does: ${operationNames().join(" | ")}`,
      process.env.QPI_OPERATION || "",
    )
    .option(
      "-a, --qpi-addr <url>",
      "Full URL of the QPI server",
      envOr("QPI_ADDR", "http://127.0.0.1:8090"),
    )
    .option(
      "-t, --token <token>",
      "Access token identifying this driver",
      process.env.QPI_ACCESS_TOKEN || "",
    )
    .option(
      "-d, --device <device>",
      "Which backend to run within the operation, or an import path; `qpi-driver devices` lists them",
      process.env.QPI_DEVICE || "",
    )
    .option(
      "--ca-file <path>",
      "Where the downloaded server root CA is written",
      envOr("QPI_CA_FILE", "./bin/qpi.ca.pem"),
    )
    .option(
      "--ca-fingerprint <hex>",
      "SHA-256 fingerprint pinning the downloaded root CA (required)",
      process.env.QPI_CA_FINGERPRINT || "",
    )
    .option(
      "-o, --option <keyvalue>",
      "A setting of the chosen device as key=value, repeatable",
      collect,
      [],
    )
    .action((opts: CommonOpts) => runStart(opts));
}

/**
 * Build the whole command tree. Exported so it can be asserted on without spawning
 * a process; the bin below is the only thing that runs it.
 */
export function buildProgram(): Command {
  const program = new Command();
  program
    .name("qpi-driver")
    .description("Quantum Processing Interface (QPI) Driver CLI")
    .version(VERSION)
    .exitOverride();

  addStart(program);

  // Names only: what a device does is documented in QPI-UI, not here.
  program
    .command("devices")
    .description("List the devices this build can run, by operation")
    .option(
      "--operation <operation>",
      "Show only this operation's devices, instead of all of them",
    )
    .action((opts: { operation?: string }) => {
      if (opts.operation && !knownOperation(opts.operation)) {
        fail(
          `unknown operation '${opts.operation}'; valid operations: ${operationNames().join(", ")}`,
        );
      }
      const wanted = opts.operation
        ? [opts.operation as Operation]
        : operations();
      for (const operation of wanted) {
        const names = devices(operation).map((each) => each.name);
        console.log(`${operation}: ${names.join(", ") || "none"}`);
      }
    });

  program
    .command("version")
    .description("Show the version of the QPI driver CLI")
    .action(() => console.log(VERSION));

  return program;
}

/**
 * Whether a rejection out of `parseAsync` is a failure, or commander doing its job.
 *
 * `exitOverride()` above makes commander throw instead of exiting, so that
 * `buildProgram()` can be driven in-process by a test — but that also turns printing
 * `--help` or `--version` into a rejected promise. Treating those as failures made
 * `qpi-driver --help` exit 1 where the Go and Python CLIs exit 0, which breaks any
 * `set -e` wrapper that asks a CLI what it can do before using it.
 *
 * Exported so the classification is testable without spawning a process.
 */
export function isCommanderOutput(err: unknown): boolean {
  const code = (err as { code?: string } | null)?.code;
  return (
    code === "commander.helpDisplayed" ||
    code === "commander.help" ||
    code === "commander.version"
  );
}

// Run only when this file is the process entry point. Importing it — which the
// tests do — must not execute a command; `process.argv[1]` under jest is the test
// runner, not this bin.
if (process.argv[1]?.endsWith("cli.js")) {
  buildProgram()
    .parseAsync(process.argv)
    .catch((err) => {
      if (isCommanderOutput(err)) {
        process.exit(0);
      }
      console.error("Error:", err);
      process.exit(1);
    });
}
