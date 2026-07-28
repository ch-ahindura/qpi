/**
 * The `start --operation`/`--device` grammar (RFC 0003 §4, §8).
 *
 * These assert the decisions, which is why they live on `catalog.ts` rather than on
 * the commander wiring: which operations exist, what an omitted flag falls back to,
 * and — the one worth having — that an operation this SDK ships no devices for says
 * so, rather than reporting an unknown device with an empty list of known ones.
 */

import type { QpiDriver } from "../driver.js";
import {
  type CommonOpts,
  operationHelp,
  operationNames,
  operations,
  resolveDevice,
} from "./catalog.js";

/** A device that builds nothing, for asserting on resolution alone. */
const fakeBuild = (_common: CommonOpts, _opts: Record<string, string>) =>
  ({}) as QpiDriver;

/** Registers a monitor device for one test, then puts the table back. */
function withMonitorDevice(name: string, body: () => void): void {
  const devices = operations.monitor.devices;
  const before = { ...devices };
  devices[name] = fakeBuild;
  try {
    body();
  } finally {
    for (const key of Object.keys(devices)) delete devices[key];
    Object.assign(devices, before);
  }
}

describe("operations", () => {
  it("are exactly the two QPI-UI has handlers for", () => {
    // Closed by design: a new operation is a coordinated change across the SDKs
    // and the server, so asserting the whole set is the point (RFC 0003 §13.1).
    expect(Object.keys(operations).sort()).toEqual(["monitor", "process"]);
    expect(operationNames()).toBe("monitor, process");
  });

  it("each describe themselves for generated help", () => {
    for (const spec of Object.values(operations)) {
      expect(spec.summary).toBeTruthy();
      expect(spec.defaultName).toBeTruthy();
    }
  });
});

describe("resolveDevice", () => {
  it("requires an operation, naming the valid ones", () => {
    expect(() => resolveDevice("", "", "")).toThrow(/--operation is required/);
    expect(() => resolveDevice("", "", "")).toThrow(/monitor, process/);
  });

  it("rejects an unknown operation, naming the valid ones", () => {
    expect(() => resolveDevice("procces", "", "")).toThrow(
      /unknown operation 'procces'/,
    );
    expect(() => resolveDevice("procces", "", "")).toThrow(/monitor, process/);
  });

  it("says plainly when this SDK ships no devices for an operation", () => {
    // The failure this replaced read `unknown process device "mock"; known
    // devices: ` — an empty list, and a default device that never existed here.
    let message = "";
    try {
      resolveDevice("process", "", "");
    } catch (err) {
      message = (err as Error).message;
    }

    expect(message).toContain("ships no process devices");
    expect(message).toContain('pip install "qpi-driver[cli]"');
    expect(message).not.toContain("known devices");
  });

  it("falls back to the operation's own device and name", () => {
    withMonitorDevice("bluefors_gen1", () => {
      const resolved = resolveDevice("monitor", "", "");

      expect(resolved.device).toBe("bluefors_gen1");
      expect(resolved.name).toBe("qpi-monitor");
      expect(resolved.build).toBe(fakeBuild);
    });
  });

  it("keeps a device and name that were given", () => {
    withMonitorDevice("bluefors_gen1", () => {
      const resolved = resolveDevice("monitor", "bluefors_gen1", "cryostat-1");

      expect(resolved.name).toBe("cryostat-1");
    });
  });

  it("rejects an unknown device, listing the known ones", () => {
    withMonitorDevice("bluefors_gen1", () => {
      expect(() => resolveDevice("monitor", "bluefors", "")).toThrow(
        /unknown monitor device 'bluefors'; known devices: bluefors_gen1/,
      );
    });
  });
});

describe("operationHelp", () => {
  it("lists every operation and its devices", () => {
    withMonitorDevice("bluefors_gen1", () => {
      const help = operationHelp();

      expect(help).toContain("monitor");
      expect(help).toContain("bluefors_gen1");
      expect(help).toContain("process");
    });
  });

  it("says an operation is empty rather than showing nothing", () => {
    // Someone reading --help must be able to tell "no devices here" from
    // "devices exist but are not listed".
    expect(operationHelp()).toContain(
      "no process devices in the TypeScript SDK",
    );
  });
});
