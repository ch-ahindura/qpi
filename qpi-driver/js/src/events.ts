/**
 * Event envelope shared with QPI-UI over NNG (RFC 0001 §4, §6).
 *
 * A driver speaks the same typed events QPI-UI understands: it handles the
 * events QPI-UI sends it and emits events of its own. Every event travels in
 * one envelope whose payload shape depends on its type and is validated by
 * whoever handles it.
 */

import { randomBytes } from "node:crypto";

/**
 * The fixed set of event types a QPI-UI version understands. Maintainers grow
 * the framework by adding new types over releases.
 */
export enum EventType {
  JobDispatch = "JobDispatch",
  JobResult = "JobResult",
  CryostatReading = "CryostatReading",
  CalibrateDispatch = "CalibrateDispatch",
  CalibrationResult = "CalibrationResult",
  CalibrationProgress = "CalibrationProgress",
  CalibrationQueued = "CalibrationQueued",
  QPUState = "QPUState",
}

/** The wire shape of an envelope, as sent over NNG. */
export interface EventWire {
  id: string;
  driver: string;
  type: EventType;
  ts: string;
  payload: Record<string, unknown>;
}

/** Optional envelope fields; normally left to the SDK to fill in. */
export interface EventInit {
  driver?: string;
  id?: string;
  ts?: string;
}

/** A single typed message exchanged with QPI-UI in either direction. */
export class Event {
  readonly type: EventType;
  readonly payload: Record<string, unknown>;
  /** Identifier of the driver this event belongs to; stamped on emit. */
  driver: string;
  readonly id: string;
  readonly ts: string;

  constructor(
    type: EventType,
    payload: Record<string, unknown> = {},
    init: EventInit = {},
  ) {
    this.type = type;
    this.payload = payload;
    this.driver = init.driver ?? "";
    this.id = init.id ?? newEventId();
    this.ts = init.ts ?? nowTimestamp();
  }

  /** Return the envelope as a plain object matching the wire shape. */
  toWire(): EventWire {
    return {
      id: this.id,
      driver: this.driver,
      type: this.type,
      ts: this.ts,
      payload: this.payload,
    };
  }

  /** Serialise the envelope to a JSON string for sending over NNG. */
  toJSON(): string {
    return JSON.stringify(this.toWire());
  }

  /** Build an event from a JSON string or bytes received over NNG. */
  static fromJSON(raw: string | Buffer): Event {
    const text = typeof raw === "string" ? raw : raw.toString("utf8");
    const data = JSON.parse(text) as Partial<EventWire>;
    if (!data.type) {
      throw new Error("event envelope is missing a type");
    }
    return new Event(data.type, data.payload ?? {}, {
      driver: data.driver ?? "",
      id: data.id ?? "",
      ts: data.ts ?? "",
    });
  }
}

function newEventId(): string {
  return "evt_" + randomBytes(12).toString("hex");
}

function nowTimestamp(): string {
  return new Date().toISOString().replace(/(\.\d{3})\d*Z$/, "$1Z");
}
