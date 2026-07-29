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

/**
 * Deadline for each HTTP request the handshake makes. Matches the Python SDK's
 * `requests(..., timeout=10)` and the Go SDK's
 * `http.Client{Timeout: 10 * time.Second}`.
 */
const HTTP_TIMEOUT_MS = 10_000;

interface Connection {
  name: string;
  host: string;
  inPort: number;
  outPort: number;
  ca: string;
}

/** A completed HTTP response, with its body already read. */
interface HttpResult {
  ok: boolean;
  status: number;
  body: string;
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
  /**
   * The display label QPI-UI has this driver registered under, used to tag emitted
   * events. It comes from the `drivers/connect` response, so it is empty until
   * {@link run} has connected — the label belongs to the admin who typed it into
   * the dashboard, not to the driver.
   */
  name = "";
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
    // The server owns the label, so the driver only knows it from here on.
    this.name = conn.name;

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
   *
   * The token is the whole of the identity asserted here. The driver's display
   * label comes back in the response rather than going out in the request: it
   * belongs to the admin who typed it into the dashboard, and a driver sending one
   * meant every restart silently overwrote what they chose.
   */
  private async connect(): Promise<Connection> {
    const resp = await fetchWithDeadline(
      "the drivers/connect handshake",
      `${this.qpiAddr}/api/op/drivers/connect`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: this.token }),
      },
    );
    if (!resp.ok) {
      throw new Error(
        `qpi-driver: connect rejected (${resp.status}): ${resp.body.trim()}`,
      );
    }
    let data: {
      name?: string;
      nng_host: string;
      nng_in_port: number;
      nng_out_port: number;
    };
    try {
      data = JSON.parse(resp.body);
    } catch {
      throw new Error(
        `qpi-driver: connect returned a body that is not JSON: ${resp.body.trim().slice(0, 200)}`,
      );
    }
    const ca = await this.downloadRootCa();
    return {
      name: data.name ?? "",
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
    const resp = await fetchWithDeadline(
      "the root CA download",
      `${this.qpiAddr}/api/pub/root-ca.pem`,
    );
    if (!resp.ok) {
      throw new Error(`qpi-driver: downloading root CA: status ${resp.status}`);
    }
    const pem = resp.body;
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

/**
 * `fetch` under a deadline, with the body read inside it, and a clear error when
 * the deadline expires.
 *
 * Node's `fetch` sets no timeout of its own beyond undici's defaults, which are
 * minutes rather than seconds: a server behind a firewall that drops packets
 * leaves the driver hanging inside `run()` instead of failing, and under
 * systemd's `Restart=on-failure` a unit that never fails never restarts. The
 * body is read under the same deadline as the request, so a server that sends
 * headers and then stalls is caught too.
 *
 * `AbortSignal.timeout` aborts with "This operation was aborted", which says
 * neither what timed out nor against what, so `what` and `url` are put back into
 * the message.
 *
 * `timeoutMs` defaults to the 10s the Python and Go SDKs use; only tests pass a
 * shorter one, to avoid waiting out the real deadline.
 */
export async function fetchWithDeadline(
  what: string,
  url: string,
  // No `signal`: the deadline owns it, and one passed in here would be silently
  // dropped by the spread below.
  init: Omit<RequestInit, "signal"> = {},
  timeoutMs: number = HTTP_TIMEOUT_MS,
): Promise<HttpResult> {
  try {
    const resp = await fetch(url, {
      ...init,
      signal: AbortSignal.timeout(timeoutMs),
    });
    return { ok: resp.ok, status: resp.status, body: await resp.text() };
  } catch (err) {
    if (isTimeout(err)) {
      throw new Error(
        `qpi-driver: ${what} timed out after ${timeoutMs}ms against ${url}`,
      );
    }
    throw err;
  }
}

/**
 * Whether an error is the abort raised by our own deadline. `fetch` may reject
 * with the signal's reason directly (a `TimeoutError` DOMException) or wrap it
 * as the `cause` of a `TypeError: fetch failed`, so the cause chain is walked.
 *
 * Matched on `name` rather than `instanceof`: the abort is constructed inside
 * Node, and under a test runner that loads this module in its own realm it is
 * not an instance of *this* realm's `Error`.
 */
function isTimeout(err: unknown): boolean {
  for (let e = err, depth = 0; isObject(e) && depth < 8; e = e.cause, depth++) {
    if (e.name === "TimeoutError" || e.name === "AbortError") {
      return true;
    }
  }
  return false;
}

function isObject(e: unknown): e is { name?: unknown; cause?: unknown } {
  return typeof e === "object" && e !== null;
}

/** Ensure the address has a scheme and no trailing slash. */
function normalizeQpiAddr(addr: string): string {
  const withScheme = addr.includes("://") ? addr : `http://${addr}`;
  return withScheme.replace(/\/+$/, "");
}
