/**
 * The Bluefors Gen. 1 monitor as a device the CLI can run (RFC 0003 §5).
 *
 * Separate from the driver so importing the driver does not drag in the catalog,
 * and beside it rather than in a table somewhere central — so the device and its
 * description cannot drift apart.
 *
 * The option keys, types and defaults match the Python and Go SDKs' `bluefors_gen1`
 * exactly; a test compares the catalogs, since QPI-UI renders setup snippets from
 * one of them and an operator may well run another (RFC 0003 §9).
 *
 * @packageDocumentation
 */

import {
  asFloat,
  type DeviceConfig,
  type DeviceSpec,
  Operation,
  type Options,
} from "../devices.js";
import type { QpiDriver } from "../driver.js";
import {
  BlueforsGen1Driver,
  DEFAULT_BASE_URL,
  parseChannels,
} from "./bluefors-gen1.js";

/** Adapts {@link parseChannels} to an option parser. */
function asChannels(raw: string): unknown {
  return parseChannels(raw);
}

/**
 * Builds an unstarted driver from the parsed options. There is nothing to validate
 * or convert here: the spec's own schema did that, so a missing or malformed option
 * has already become a clean CLI error by the time this runs.
 */
function build(config: DeviceConfig, options: Options): QpiDriver {
  return new BlueforsGen1Driver({
    qpiAddr: config.qpiAddr,
    token: config.token,
    caFingerprint: config.caFingerprint,
    caFilePath: config.caFilePath,
    blueforsBaseUrl: options.str("base_url"),
    channels: options.channels("channels"),
    apiKey: options.str("api_key"),
    pollIntervalMs: options.ms("poll_interval"),
    timeoutMs: options.ms("timeout"),
  });
}

/** The device spec: register it with `registerDevice` to make the CLI run it. */
export const DEVICE_SPEC: DeviceSpec = {
  name: "bluefors_gen1",
  operation: Operation.Monitor,
  summary: "Cryostat monitor for Bluefors Control Software Gen. 1.",
  build,
  options: [
    {
      key: "channels",
      help: "Value-tree channels to poll, as path[:unit] pairs.",
      type: "channels",
      parse: asChannels,
      required: true,
      example: "mapper.bf.tmc:K,mapper.bf.pmc:mbar",
    },
    {
      key: "base_url",
      help: "Base URL of the Bluefors Control API.",
      type: "str",
      default: DEFAULT_BASE_URL,
      example: "http://localhost:49099",
    },
    {
      key: "api_key",
      help: "Bluefors API access key, if the API requires one.",
      type: "str",
      example: "<bluefors-api-key>",
    },
    {
      key: "poll_interval",
      help: "Seconds between polls of every channel.",
      type: "float",
      parse: asFloat,
      default: "5.0",
      example: "5",
    },
    {
      key: "timeout",
      help: "HTTP timeout per channel read, in seconds.",
      type: "float",
      parse: asFloat,
      default: "5.0",
      example: "5",
    },
  ],
};
