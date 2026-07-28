/**
 * `catalog --json` and the generated help text (RFC 0003 §5, §9).
 *
 * Three SDKs produce this document and QPI-UI reads it, so the shape is the thing
 * under test — a renamed key is a break, not a detail.
 */

import {
  catalog,
  deviceLines,
  renderCatalog,
  SCHEMA_VERSION,
} from "./catalog.js";
import {
  asInt,
  clearDevices,
  Operation,
  registerDevice,
  type DeviceSpec,
} from "./devices.js";
import type { QpiDriver } from "./driver.js";

const spec: DeviceSpec = {
  name: "thermometer",
  operation: Operation.Monitor,
  summary: "Reads a made-up thermometer.",
  build: () => ({}) as QpiDriver,
  options: [
    {
      key: "probes",
      help: "How many probes to read.",
      type: "int",
      parse: asInt,
      default: "1",
      example: "4",
    },
    { key: "label", help: "What to call it.", type: "str" },
  ],
};

beforeEach(() => {
  clearDevices();
  registerDevice(spec);
});
afterAll(() => clearDevices());

describe("catalog", () => {
  it("has exactly the documented keys", () => {
    const document = catalog();

    expect(Object.keys(document).sort()).toEqual([
      "operations",
      "schema_version",
    ]);
    expect(document.schema_version).toBe(SCHEMA_VERSION);

    for (const operation of document.operations) {
      expect(Object.keys(operation).sort()).toEqual([
        "default_device",
        "devices",
        "events",
        "name",
        "summary",
      ]);
      for (const device of operation.devices) {
        expect(Object.keys(device).sort()).toEqual([
          "extra",
          "name",
          "options",
          "summary",
        ]);
        for (const option of device.options) {
          expect(Object.keys(option).sort()).toEqual([
            "default",
            "example",
            "help",
            "key",
            "required",
            "type",
          ]);
        }
      }
    }
  });

  it("lists both operations even when one is empty", () => {
    // Not omitted, and not null: a consumer must be able to tell "no devices here"
    // from "this key was missing".
    const document = catalog();

    expect(document.operations.map((each) => each.name)).toEqual([
      "process",
      "monitor",
    ]);
    expect(
      document.operations.find((each) => each.name === "process")?.devices,
    ).toEqual([]);
  });

  it("reports an option's type, default and requiredness", () => {
    const monitor = catalog().operations.find(
      (each) => each.name === "monitor",
    );
    const options = monitor?.devices[0].options ?? [];

    expect(options[0]).toMatchObject({
      key: "probes",
      type: "int",
      default: "1",
      required: false,
      example: "4",
    });
    // No default is null, as it is in the Python and Go SDKs — not "".
    expect(options[1].default).toBeNull();
  });

  it("round-trips through JSON unchanged", () => {
    expect(JSON.parse(JSON.stringify(catalog()))).toEqual(catalog());
  });
});

describe("renderCatalog", () => {
  it("covers every operation, or narrows to one", () => {
    const everything = renderCatalog();
    expect(everything).toContain("--operation process");
    expect(everything).toContain("--operation monitor");

    const monitorOnly = renderCatalog(Operation.Monitor);
    expect(monitorOnly).not.toContain("--operation process");
    expect(monitorOnly).toContain("probes=<int> (default: 1)");
  });
});

describe("deviceLines", () => {
  it("lists each device and its options", () => {
    const lines = deviceLines(Operation.Monitor).join("\n");

    expect(lines).toContain("• thermometer — Reads a made-up thermometer.");
    expect(lines).toContain(
      "• probes=<int> (default: 1) — How many probes to read.",
    );
    expect(lines).toContain("• label=<str> (optional) — What to call it.");
  });

  it("says an operation is empty rather than showing nothing", () => {
    // Someone reading --help must be able to tell "no devices here" from "devices
    // exist but are not listed".
    expect(deviceLines(Operation.Process).join("\n")).toContain(
      "ships no process devices",
    );
  });

  it("only advertises a default device this build has", () => {
    // The operation's default is `bluefors_gen1`, which this test never registered;
    // advertising it would send an operator after a device that cannot run here.
    expect(deviceLines(Operation.Monitor)[0]).not.toContain("default");
  });

  it("documents the import-path route", () => {
    // --help has to say the catalog is not the limit, and that -o is unchecked there.
    const lines = deviceLines(Operation.Monitor).join("\n");

    expect(lines).toContain("named by import path");
    expect(lines).toContain("unchecked");
  });
});
