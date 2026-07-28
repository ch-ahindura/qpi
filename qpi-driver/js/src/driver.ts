/**
 * Base SDK for building QPI drivers in TypeScript (RFC 0001 §4).
 *
 * A driver is an external process that exchanges typed events with QPI-UI: it
 * handles the events QPI-UI sends it and emits events of its own. Subclass
 * {@link QpiDriver}, implement {@link QpiDriver.handleEvent} to act on each
 * inbound event (switching on its type), and call {@link QpiDriver.emit} to
 * send an event upward. {@link QpiDriver.every} runs a callback on a timer,
 * for drivers that report on their own schedule rather than in reply to a
 * dispatch.
 *
 * It mirrors the Python SDK (`qpi-driver/py`) and the Go SDK (`qpi-driver/go`):
 * the same envelope, the same `drivers/connect` handshake, and TLS with the
 * pinned root CA.
 */

import { mkdir, writeFile } from "node:fs/promises";
import { dirname } from "node:path";

import { verifyFingerprint } from "./ca.js";
import { Event } from "./events.js";
import { PipelineSocket } from "./nng.js";

/** Options for constructing a {@link QpiDriver}. */
export interface QpiDriverOptions {
  /** Full URL of the QPI-UI server, e.g. `"https://qpi.example.com"`. */
  qpiAddr: string;
  /** The driver's access token; identifies it (and its QPU) to QPI-UI. */
  token: string;
  /** Human-readable name for this driver. */
  name: string;
  /**
   * Expected SHA-256 (hex) of the server root CA, pinned over TLS. Required:
   * there is no path that connects without verifying the pin, because an opt-out
   * reachable by leaving an argument out is one a copy-pasted command hits by
   * accident (RFC 0003 §10).
   */
  caFingerprint: string;
  /**
   * Where to write the downloaded root CA certificate, for an operator to inspect.
   * Omitted means it is kept in memory only. Matches the Python and Go SDKs'
   * `--ca-file`.
   */
  caFilePath?: string;
}

interface Connection {
  host: string;
  inPort: number;
  outPort: number;
  ca: string;
}

interface PeriodicTask {
  intervalMs: number;
  fn: () => void | Promise<void>;
}

/**
 * Base class for a QPI driver: handles inbound events, emits its own. The base
 * owns the transport; subclasses only decide which events they handle and emit.
 */
export abstract class QpiDriver {
  readonly name: string;
  protected readonly qpiAddr: string;
  protected readonly token: string;
  protected readonly caFingerprint: string;
  protected readonly caFilePath?: string;

  private pushSocket?: PipelineSocket;
  private pullSocket?: PipelineSocket;
  private readonly periodic: PeriodicTask[] = [];
  private readonly timers: ReturnType<typeof setInterval>[] = [];
  private stopResolve?: () => void;
  private stopped = false;
  private readonly signalHandler = () => this.stop();

  constructor(options: QpiDriverOptions) {
    this.qpiAddr = normalizeQpiAddr(options.qpiAddr);
    this.token = options.token;
    this.name = options.name;
    this.caFingerprint = options.caFingerprint;
    this.caFilePath = options.caFilePath;
  }

  /**
   * Act on a single inbound event, switching on `event.type`. Implemented per
   * driver. An event a driver does not care about is simply ignored; there is
   * no application-level ACK/NACK (RFC 0001 §4).
   */
  abstract handleEvent(event: Event): void | Promise<void>;

  /**
   * Send an event upward to QPI-UI over the outbound NNG channel. Delivery is
   * best-effort: if nothing is listening the event is dropped rather than
   * buffered (RFC 0001 §5). Throws if called before the driver has connected.
   */
  emit(event: Event): void {
    if (!this.pushSocket) {
      throw new Error("qpi-driver: cannot emit before the driver is running");
    }
    if (!event.driver) {
      event.driver = this.name;
    }
    this.pushSocket.send(Buffer.from(event.toJSON(), "utf8"));
  }

  /**
   * Register a callback to run every `intervalMs` milliseconds while the driver
   * runs. Used by drivers that report on their own schedule — e.g. a monitor
   * that emits a reading on a timer. Register callbacks before calling
   * {@link run}.
   */
  every(intervalMs: number, fn: () => void | Promise<void>): void {
    this.periodic.push({ intervalMs, fn });
  }

  /**
   * Connect to QPI-UI and process events until {@link stop} is called or the
   * process receives SIGINT/SIGTERM. Performs the handshake, opens both NNG
   * channels, starts any periodic callbacks, then resolves once stopped.
   */
  async run(): Promise<void> {
    const conn = await this.connect();

    this.pushSocket = new PipelineSocket("push");
    await this.pushSocket.dial({
      host: conn.host,
      port: conn.outPort,
      ca: conn.ca,
      servername: conn.host,
    });

    this.pullSocket = new PipelineSocket("pull");
    this.pullSocket.onMessage((raw) => {
      void this.deliver(raw);
    });
    await this.pullSocket.dial({
      host: conn.host,
      port: conn.inPort,
      ca: conn.ca,
      servername: conn.host,
    });

    this.startPeriodic();
    process.once("SIGINT", this.signalHandler);
    process.once("SIGTERM", this.signalHandler);

    await new Promise<void>((resolve) => {
      this.stopResolve = resolve;
    });

    this.shutdown();
  }

  /** Signal a running driver to shut down. Safe to call more than once. */
  stop(): void {
    if (this.stopped) {
      return;
    }
    this.stopped = true;
    this.stopResolve?.();
  }

  private async deliver(raw: Buffer): Promise<void> {
    let event: Event;
    try {
      event = Event.fromJSON(raw);
    } catch (err) {
      console.error("[qpi-driver] dropping malformed inbound message:", err);
      return;
    }
    try {
      await this.handleEvent(event);
    } catch (err) {
      console.error(
        `[qpi-driver] dropping event ${event.id} of type ${event.type}: handler failed:`,
        err,
      );
    }
  }

  private startPeriodic(): void {
    for (const task of this.periodic) {
      const timer = setInterval(() => {
        void this.runPeriodic(task.fn);
      }, task.intervalMs);
      this.timers.push(timer);
    }
  }

  private async runPeriodic(fn: () => void | Promise<void>): Promise<void> {
    try {
      await fn();
    } catch (err) {
      console.error("[qpi-driver] periodic callback failed:", err);
    }
  }

  private shutdown(): void {
    for (const timer of this.timers) {
      clearInterval(timer);
    }
    this.timers.length = 0;
    process.removeListener("SIGINT", this.signalHandler);
    process.removeListener("SIGTERM", this.signalHandler);
    this.pullSocket?.close();
    this.pushSocket?.close();
  }

  /**
   * Handshake with QPI-UI over the shared `drivers/connect` endpoint. The token
   * identifies the driver (and, transitively, its QPU); QPI-UI returns the NNG
   * host and ports. Every driver connects the same way (RFC 0001 §3, §8).
   */
  private async connect(): Promise<Connection> {
    const resp = await fetch(`${this.qpiAddr}/api/op/drivers/connect`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: this.token, name: this.name }),
    });
    if (!resp.ok) {
      const body = await resp.text().catch(() => "");
      throw new Error(
        `qpi-driver: connect rejected (${resp.status}): ${body.trim()}`,
      );
    }
    const data = (await resp.json()) as {
      nng_host: string;
      nng_in_port: number;
      nng_out_port: number;
    };
    const ca = await this.downloadRootCa();
    return {
      host: data.nng_host,
      inPort: data.nng_in_port,
      outPort: data.nng_out_port,
      ca,
    };
  }

  /**
   * Download the server root CA and verify its SHA-256 fingerprint against the
   * pinned value. The fingerprint is the hex SHA-256 of the certificate's DER
   * bytes, matching the Python and Go SDKs, and there is no way to skip the
   * check (RFC 0003 §10).
   *
   * The certificate is written to `caFilePath` when one was given — after
   * verification, so a certificate that failed the pin is never left on disk
   * looking legitimate. A write that fails is reported and does not stop the
   * driver: the copy on disk is for an operator to look at, not something the
   * transport reads back.
   */
  private async downloadRootCa(): Promise<string> {
    const resp = await fetch(`${this.qpiAddr}/api/pub/root-ca.pem`);
    if (!resp.ok) {
      throw new Error(`qpi-driver: downloading root CA: status ${resp.status}`);
    }
    const pem = await resp.text();
    verifyFingerprint(pem, this.caFingerprint);

    if (this.caFilePath) {
      try {
        await mkdir(dirname(this.caFilePath), { recursive: true });
        await writeFile(this.caFilePath, pem, "utf8");
      } catch (err) {
        console.error(
          `[qpi-driver] could not write the root CA to ${this.caFilePath}:`,
          err instanceof Error ? err.message : err,
        );
      }
    }
    return pem;
  }
}

/** Ensure the address has a scheme and no trailing slash. */
function normalizeQpiAddr(addr: string): string {
  const withScheme = addr.includes("://") ? addr : `http://${addr}`;
  return withScheme.replace(/\/+$/, "");
}
