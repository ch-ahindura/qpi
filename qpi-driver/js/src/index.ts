/**
 * qpi-driver — TypeScript SDK for building QPI drivers (RFC 0001).
 *
 * @example
 * ```typescript
 * import { QpiDriver, Event, EventType } from "qpi-driver";
 *
 * class MyDriver extends QpiDriver {
 *   handleEvent(event: Event): void {
 *     if (event.type === EventType.JobDispatch) {
 *       const results = runMyBackend(event.payload);
 *       this.emit(new Event(EventType.JobResult, {
 *         job_id: event.payload.job_id, status: "completed", results,
 *       }));
 *     }
 *   }
 * }
 *
 * await new MyDriver({
 *   qpiAddr: "https://qpi.example.com",
 *   token: "your-driver-token",
 *   caFingerprint: "sha256-of-the-server-root-ca",
 * }).run();
 * ```
 *
 * Built-in drivers (e.g. the Bluefors Gen. 1 monitor) live in their own
 * sub-modules — `import { BlueforsGen1Driver } from "qpi-driver/builtins/bluefors-gen1"` —
 * so they are only pulled into a bundle when actually used.
 *
 * To make a driver of your own runnable by the `qpi-driver` CLI, register it as a
 * device: `import { registerDevice } from "qpi-driver/devices"` (RFC 0003 §6).
 *
 * @packageDocumentation
 */

export { QpiDriver, type QpiDriverOptions } from "./driver.js";
export { Event, EventType, type EventWire, type EventInit } from "./events.js";
export {
  clearDevices,
  devices,
  hasDevice,
  knownOperation,
  Operation,
  Options,
  operationNames,
  operations,
  registerDevice,
  resolve,
  type DeviceBuilder,
  type DeviceConfig,
  type DeviceSpec,
} from "./devices.js";
