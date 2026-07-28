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
 */

import { Command } from "commander";

import type { QpiDriver } from "../driver.js";
import { BlueforsGen1Driver, parseChannels } from "./bluefors-gen1.js";
import {
  type CommonOpts,
  operationHelp,
  operationNames,
  operations,
  resolveDevice,
} from "./catalog.js";

const VERSION = "0.1.2";

// The devices this SDK ships, registered into the catalog. Declared here rather
// than in the catalog itself so that module stays free of driver imports.
operations.monitor.devices.bluefors_gen1 = buildBlueforsGen1;

/**
 * Builds the Bluefors Gen. 1 monitor from the -o options. Recognised keys
 * mirror the Python and Go drivers: channels (required), base_url, api_key,
 * poll_interval (seconds), timeout (seconds).
 */
function buildBlueforsGen1(
  common: CommonOpts,
  opts: Record<string, string>,
): QpiDriver {
  const channels = opts.channels;
  if (!channels) {
    throw new Error(
      "bluefors_gen1 needs a 'channels' option, e.g. " +
        "-o channels=mapper.bf.tmc:K,mapper.bf.pmc:mbar",
    );
  }
  return new BlueforsGen1Driver({
    qpiAddr: common.qpiAddr,
    token: common.token,
    name: common.name,
    caFingerprint: common.caFingerprint,
    blueforsBaseUrl: opts.base_url,
    channels: parseChannels(channels),
    apiKey: opts.api_key,
    pollIntervalMs: opts.poll_interval
      ? Number(opts.poll_interval) * 1000
      : undefined,
    timeoutMs: opts.timeout ? Number(opts.timeout) * 1000 : undefined,
  });
}

/**
 * Runs the chosen device within the chosen operation, mirroring the Python CLI's
 * shared handler: the catalog decides which device it is and fills in the
 * operation's defaults, the device's builder returns an unstarted driver, and this
 * is what starts it.
 */
async function runOperation(common: CommonOpts): Promise<void> {
  if (!common.token) {
    fail(
      "access token is required; set --token/-t or the QPI_ACCESS_TOKEN environment variable",
    );
  }

  let resolved;
  try {
    resolved = resolveDevice(common.operation, common.device, common.name);
  } catch (err) {
    fail((err as Error).message);
  }
  common.device = resolved.device;
  common.name = resolved.name;

  let driver: QpiDriver;
  try {
    driver = resolved.build(common, parseOptions(common.option));
  } catch (err) {
    fail((err as Error).message);
  }
  await driver.run();
}

/** Turns repeatable `-o key=value` flags into a dict. */
function parseOptions(pairs: string[]): Record<string, string> {
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
    .addHelpText("after", `\n${operationHelp()}`)
    // No short form for --operation: -o is --option, and -O beside it would be a
    // hazard in a command usually written once into a unit file (RFC 0003 §13.7).
    .option(
      "--operation <operation>",
      `What this driver does: ${operationNames()}`,
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
      "-n, --name <name>",
      "Human-readable name for this driver; defaults to the operation's own",
      process.env.QPI_DRIVER_NAME || "",
    )
    .option(
      "-d, --device <device>",
      "Which backend to run within the operation; defaults to the operation's own",
      process.env.QPI_DEVICE || "",
    )
    .option(
      "--ca-file <path>",
      "Where the downloaded server root CA is written",
      envOr("QPI_CA_FILE", "./bin/qpi.ca.pem"),
    )
    .option(
      "--ca-fingerprint <hex>",
      "SHA-256 fingerprint pinning the downloaded root CA",
      process.env.QPI_CA_FINGERPRINT,
    )
    .option(
      "-o, --option <keyvalue>",
      "Operation config as key=value, repeatable",
      collect,
      [],
    )
    .option(
      "--recv-timeout-ms <ms>",
      "Receive loop timeout in ms",
      (v) => parseInt(v, 10),
      Number(process.env.QPI_RECV_TIMEOUT_MS) || 200,
    )
    .action((opts: CommonOpts) => runOperation(opts));
}

const program = new Command();
program
  .name("qpi-driver")
  .description("Quantum Processing Interface (QPI) Driver CLI")
  .version(VERSION);

addStart(program);

program
  .command("version")
  .description("Show the version of the QPI driver CLI")
  .action(() => console.log(VERSION));

program.parseAsync(process.argv).catch((err) => {
  console.error("Error:", err);
  process.exit(1);
});
