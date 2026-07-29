/**
 * The Bluefors Gen. 1 monitor as a device the CLI can run (RFC 0003 §6).
 *
 * Separate from the driver so importing the driver does not drag in the registry.
 *
 * A name, an operation and a builder is the whole of it. What the device is and which
 * `-o` keys to fill in belongs to QPI-UI, where the driver is registered; describing
 * it here as well would be a second catalog to keep in step with the first
 * (RFC 0003 §9).
 *
 * @packageDocumentation
 */

import {
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

/**
 * Builds an unstarted driver from its `-o` options.
 *
 * The options this monitor reads are the ones read here. Only the channels are
 * unknowable in advance — which ones a system exposes depends on how its mappers are
 * configured — so they are the one option with no default and the one whose absence
 * throws.
 */
function build(config: DeviceConfig, options: Options): QpiDriver {
  return new BlueforsGen1Driver({
    qpiAddr: config.qpiAddr,
    token: config.token,
    caFingerprint: config.caFingerprint,
    caFilePath: config.caFilePath,
    blueforsBaseUrl: options.str("base_url", DEFAULT_BASE_URL),
    channels: parseChannels(
      options.require("channels", "mapper.bf.tmc:K,mapper.bf.pmc:mbar"),
    ),
    apiKey: options.str("api_key"),
    pollIntervalMs: options.ms("poll_interval"),
    timeoutMs: options.ms("timeout"),
  });
}

/** The device spec: register it with `registerDevice` to make the CLI run it. */
export const DEVICE_SPEC: DeviceSpec = {
  name: "bluefors_gen1",
  operation: Operation.Monitor,
  build,
};
