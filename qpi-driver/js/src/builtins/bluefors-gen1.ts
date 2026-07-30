/**
 * Cryostat monitor for Bluefors Control Software Gen. 1 (RFC 0001 §7), the
 * TypeScript counterpart of the Python `qpi_driver.builtins.bluefors_gen1`
 * driver.
 *
 * Note the Gen. 1: Bluefors also ships a Gen. 2 Control Software with its own
 * API, which is out of scope here and would need its own driver.
 *
 * A report-only driver: it never handles `JobDispatch` (it is not a QPU), it
 * polls the Bluefors Remote Access Control API Gen. 1 "values" endpoint on a
 * timer and emits a `CryostatReading` event with whatever it read. The value
 * tree path for a channel (e.g. `mapper.bf.tmc`) is the endpoint path with dots
 * replaced by slashes: `GET {baseUrl}/values/mapper/bf/tmc`.
 *
 * It lives in its own sub-module so it is only bundled when imported —
 * `import { BlueforsGen1Driver } from "qpi-driver/builtins/bluefors-gen1"` — the
 * bundler equivalent of the Python `qpi-driver[bluefors_gen1]` extra.
 */

import { Event, EventType } from "../events.js";
import { QpiDriver, type QpiDriverOptions } from "../driver.js";

export const DEFAULT_BASE_URL = "http://127.0.0.1:49099";
export const DEFAULT_POLL_INTERVAL_MS = 5000;
export const DEFAULT_TIMEOUT_MS = 5000;

/** Options for the Bluefors Gen. 1 monitor, on top of the base driver options. */
export interface BlueforsGen1Options extends QpiDriverOptions {
  /** Base URL of the Bluefors Control API. Defaults to {@link DEFAULT_BASE_URL}. */
  blueforsBaseUrl?: string;
  /**
   * Channels to poll: either a `{ path: unit }` map (e.g.
   * `{ "mapper.bf.tmc": "K" }`) or a bare list of paths. The Bluefors basic
   * read response does not report units, so they are supplied here.
   */
  channels?: Record<string, string> | string[];
  /** Optional Bluefors access key, sent as the `key` query parameter. */
  apiKey?: string;
  /** Milliseconds between polls. Defaults to {@link DEFAULT_POLL_INTERVAL_MS}. */
  pollIntervalMs?: number;
  /** Per-channel HTTP timeout in ms. Defaults to {@link DEFAULT_TIMEOUT_MS}. */
  timeoutMs?: number;
  /**
   * What reads one channel; see {@link ChannelReader}. Defaults to the Gen. 1
   * Control API read, which is what `blueforsBaseUrl`, `apiKey` and `timeoutMs`
   * configure. A monitor for other control software supplies its own and leaves
   * those alone.
   */
  channelReader?: ChannelReader;
}

/** One channel's latest value in the emitted payload. */
export interface Reading {
  value: number | null;
  unit: string;
  status: string;
}

/**
 * Reads one channel of a cryostat's value tree, given its path and display unit.
 *
 * This is the part of a cryostat monitor that is specific to one control API, and the
 * only part: the timer, one bad channel not losing the rest of the tick, and the
 * `CryostatReading` event are the same whatever is being read. Passing a different
 * reader to {@link BlueforsGen1Driver} is therefore the whole of what a monitor for
 * different control software has to write.
 *
 * It must not reject: a channel it cannot read is a `null` value with an `"ERROR"`
 * status, which is how the driver knows to carry on with the others.
 */
export type ChannelReader = (channel: string, unit: string) => Promise<Reading>;

/** Polls Bluefors Gen. 1 Control API channels and emits readings on a timer. */
export class BlueforsGen1Driver extends QpiDriver {
  private readonly baseUrl: string;
  private readonly channels: Record<string, string>;
  private readonly apiKey: string;
  private readonly timeoutMs: number;
  private readonly read: ChannelReader;

  constructor(options: BlueforsGen1Options) {
    super(options);
    this.baseUrl = (options.blueforsBaseUrl ?? DEFAULT_BASE_URL).replace(
      /\/+$/,
      "",
    );
    this.channels = normalizeChannels(options.channels);
    this.apiKey = options.apiKey ?? "";
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.read =
      options.channelReader ??
      ((channel, unit) => this.readGen1Channel(channel, unit));

    this.every(options.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS, () =>
      this.poll(),
    );
  }

  /**
   * Ignore every inbound event: the monitor only reports upward. It never
   * handles `JobDispatch`; it is a separate driver from the QPU (RFC 0001 §4).
   */
  handleEvent(event: Event): void {
    console.warn(
      `[bluefors] dropping event ${event.id}: monitor does not handle ${event.type}`,
    );
  }

  /**
   * Read every configured channel and emit whatever succeeded. A channel that
   * fails to read is recorded with a null value and an ERROR status rather than
   * aborting the tick; if every channel fails, nothing is emitted.
   */
  async poll(): Promise<void> {
    const readings: Record<string, Reading> = {};
    for (const [channel, unit] of Object.entries(this.channels)) {
      readings[channel] = await this.read(channel, unit);
    }

    const anyOk = Object.values(readings).some((r) => r.status !== "ERROR");
    if (!anyOk) {
      console.warn(
        `[bluefors] all ${Object.keys(readings).length} channel(s) failed this tick; skipping emit`,
      );
      return;
    }

    this.emit(new Event(EventType.CryostatReading, { readings }));
  }

  /**
   * Read one value-tree channel from the Bluefors Gen. 1 Control API — the default
   * {@link ChannelReader}, and the only Gen. 1-specific code here.
   */
  private async readGen1Channel(
    channel: string,
    unit: string,
  ): Promise<Reading> {
    const path = channel.replaceAll(".", "/");
    const url = new URL(`${this.baseUrl}/values/${path}`);
    if (this.apiKey) {
      url.searchParams.set("key", this.apiKey);
    }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const resp = await fetch(url, { signal: controller.signal });
      if (!resp.ok) {
        throw new Error(`status ${resp.status}`);
      }
      const body = (await resp.json()) as BlueforsResponse;
      const content = body?.data?.content ?? {};
      const sample = content.latest_valid_value ?? content.latest_value ?? {};
      return {
        value: parseValue(sample.value),
        unit,
        status: sample.status ?? "UNKNOWN",
      };
    } catch (err) {
      console.error(`[bluefors] failed to read channel ${channel}:`, err);
      return { value: null, unit, status: "ERROR" };
    } finally {
      clearTimeout(timer);
    }
  }
}

interface BlueforsSample {
  value?: unknown;
  status?: string;
}

interface BlueforsResponse {
  data?: {
    content?: {
      latest_valid_value?: BlueforsSample;
      latest_value?: BlueforsSample;
    };
  };
}

/**
 * Coerce a raw Bluefors value (which may be a number or a numeric string) to a
 * number, or null when it is absent or non-numeric.
 */
function parseValue(raw: unknown): number | null {
  if (raw === null || raw === undefined || raw === "") {
    return null;
  }
  const value = typeof raw === "number" ? raw : Number(raw);
  return Number.isFinite(value) ? value : null;
}

/** Turn a channel list/map/undefined into the `{ path: unit }` shape used internally. */
export function normalizeChannels(
  channels: Record<string, string> | string[] | undefined,
): Record<string, string> {
  if (!channels) {
    return {};
  }
  if (Array.isArray(channels)) {
    return Object.fromEntries(channels.map((path) => [path, ""]));
  }
  return { ...channels };
}

/** Parse `"path[:unit],path[:unit],..."` into a channel->unit map. */
export function parseChannels(raw: string): Record<string, string> {
  const channels: Record<string, string> = {};
  for (const part of raw.split(",")) {
    const trimmed = part.trim();
    if (!trimmed) {
      continue;
    }
    const [path, unit = ""] = trimmed.split(":");
    channels[path.trim()] = unit.trim();
  }
  return channels;
}
