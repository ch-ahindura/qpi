/**
 * The device registry and the options a device reads (RFC 0003 §6, §8, §9).
 *
 * The decisions live here rather than in the commander wiring, so this is where they
 * are asserted: which operations exist, which device an `--operation`/`--device` pair
 * means, and what happens when an import path or an option value is wrong.
 */

import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
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
  type DeviceSpec,
} from "./devices.js";
import type { QpiDriver } from "./driver.js";

/** A builder that builds nothing, for asserting on resolution alone. */
const fakeBuild = () => ({}) as QpiDriver;

function fakeSpec(
  name: string,
  operation: Operation,
  extra: Partial<DeviceSpec> = {},
): DeviceSpec {
  return { name, operation, build: fakeBuild, ...extra };
}

// The registry is module-global, so a test that registered into it would change what
// every later test — and `--help` — sees.
beforeEach(() => clearDevices());
afterAll(() => clearDevices());

describe("operations", () => {
  it("are exactly the two QPI-UI has handlers for", () => {
    // Devices are the extensible half; operations are not, so asserting the whole
    // set is the point here rather than a brittleness (RFC 0003 §13.1).
    expect(operationNames()).toEqual(["process", "monitor"]);
  });

  it("cannot be mutated through the accessor", () => {
    operations()[0] = "tampered" as Operation;
    expect(operations()[0]).not.toBe("tampered");
  });

  it("are recognised by name, and only a real one is", () => {
    expect(knownOperation("monitor")).toBe(true);
    expect(knownOperation("telemetry")).toBe(false);
  });
});

describe("DeviceSpec", () => {
  it("is a name, an operation and a builder — and nothing else", () => {
    // A device that also described itself — a summary, a declared option schema, an
    // install target — would be a second catalog beside QPI-UI's, kept in step by
    // hand (RFC 0003 §9).
    expect(Object.keys(fakeSpec("mine", Operation.Monitor)).sort()).toEqual([
      "build",
      "name",
      "operation",
    ]);
  });
});

describe("registerDevice", () => {
  it("makes a device resolvable, like a built-in", async () => {
    registerDevice(fakeSpec("mylab_qpu", Operation.Process));

    await expect(
      resolve(Operation.Process, "mylab_qpu"),
    ).resolves.toMatchObject({
      name: "mylab_qpu",
    });
    expect(hasDevice(Operation.Process, "mylab_qpu")).toBe(true);
  });

  it("refuses a duplicate name", () => {
    // Two devices of one name would make the winner depend on import order.
    registerDevice(fakeSpec("mine", Operation.Monitor));

    expect(() => registerDevice(fakeSpec("mine", Operation.Monitor))).toThrow(
      /already registered/,
    );
  });

  it("scopes names per operation", () => {
    // `--device` is always read in the context of an operation, so nothing needs to
    // stop a process and a monitor device sharing a name.
    registerDevice(fakeSpec("shared", Operation.Process));
    registerDevice(fakeSpec("shared", Operation.Monitor));

    expect(hasDevice(Operation.Process, "shared")).toBe(true);
    expect(hasDevice(Operation.Monitor, "shared")).toBe(true);
  });

  it("refuses an incomplete spec or an invented operation", () => {
    expect(() =>
      registerDevice({
        name: "",
        operation: Operation.Monitor,
        build: fakeBuild,
      }),
    ).toThrow(/needs a name and a builder/);
    expect(() =>
      registerDevice({
        name: "x",
        operation: "telemetry" as Operation,
        build: fakeBuild,
      }),
    ).toThrow(/unknown operation/);
  });

  it("sorts devices by name, not by registration order", () => {
    for (const name of ["zeta", "alpha", "mu"]) {
      registerDevice(fakeSpec(name, Operation.Monitor));
    }

    expect(devices(Operation.Monitor).map((spec) => spec.name)).toEqual([
      "alpha",
      "mu",
      "zeta",
    ]);
  });
});

describe("resolve", () => {
  it("reports the known devices for an unknown name", async () => {
    registerDevice(fakeSpec("mu", Operation.Monitor));
    registerDevice(fakeSpec("alpha", Operation.Monitor));

    await expect(resolve(Operation.Monitor, "mockk")).rejects.toThrow(
      /unknown monitor device 'mockk'; known devices: alpha, mu/,
    );
  });

  it("says plainly when this SDK ships no devices for an operation", async () => {
    // The failure this replaced read `unknown process device 'mock'; known devices: `
    // — an empty list, and a default device that never existed here (RFC 0003 §8).
    registerDevice(fakeSpec("mine", Operation.Monitor));

    const attempt = resolve(Operation.Process, "mock");
    await expect(attempt).rejects.toThrow(/ships no process devices/);
    await expect(attempt).rejects.not.toThrow(/known devices/);
  });

  it("refuses an invented operation", async () => {
    await expect(resolve("telemetry" as Operation, "x")).rejects.toThrow(
      /unknown operation/,
    );
  });
});

describe("import-path devices", () => {
  /**
   * Write a module to a temp dir and return its absolute path.
   *
   * CommonJS, because ts-jest compiles these tests to CJS and `import()` becomes
   * `require()` there — a `.mjs` fixture would fail to parse for that reason and
   * not for any reason a user would hit. `import()` of a `.cjs` module works the
   * same under both, so what is asserted below — the adaptation and the error
   * messages — is the real behaviour. That `import()` itself reaches an ESM device
   * is covered by running the built CLI in the driver e2e.
   */
  async function moduleWith(exports: string): Promise<string> {
    const dir = await mkdtemp(join(tmpdir(), "qpi-device-"));
    const path = join(dir, "device.cjs");
    await writeFile(path, exports, "utf8");
    return path;
  }

  it("imports a builder, taking the operation from the specifier", async () => {
    const path = await moduleWith("module.exports.MyMonitor = () => ({});\n");

    const spec = await resolve(Operation.Monitor, `${path}#MyMonitor`);

    expect(spec.operation).toBe(Operation.Monitor);
    expect(typeof spec.build).toBe("function");
    expect(spec.name).toBe(`${path}#MyMonitor`);
  });

  it("imports a DeviceSpec directly, which is how a name is chosen", async () => {
    const path = await moduleWith(
      `module.exports.SPEC = { name: "mine", operation: "monitor",
         build: () => ({}) };\n`,
    );

    const spec = await resolve(Operation.Monitor, `${path}#SPEC`);

    expect(spec.name).toBe("mine");
  });

  it("refuses a spec of another operation", async () => {
    const path = await moduleWith(
      `module.exports.SPEC = { name: "mine", operation: "process", build: () => ({}) };\n`,
    );

    await expect(resolve(Operation.Monitor, `${path}#SPEC`)).rejects.toThrow(
      /is a process device, not a monitor one/,
    );
  });

  it("refuses an export that is not a device", async () => {
    const path = await moduleWith(`module.exports.NOT_A_DEVICE = 42;\n`);

    await expect(
      resolve(Operation.Monitor, `${path}#NOT_A_DEVICE`),
    ).rejects.toThrow(/not usable as a monitor device/);
  });

  it("reports a missing export by name", async () => {
    const path = await moduleWith("module.exports.Other = () => ({});\n");

    await expect(resolve(Operation.Monitor, `${path}#Nope`)).rejects.toThrow(
      /has no export 'Nope'/,
    );
  });

  it("reports a module that will not import, without leaking paths", async () => {
    // An operator gets a stack trace for a bug, not for a typo, and Node names both
    // the missing module and the importing file as absolute paths (RFC 0003 §10).
    let message = "";
    try {
      await resolve(Operation.Monitor, "./no-such-module.cjs#Thing");
    } catch (err) {
      message = (err as Error).message;
    }

    expect(message).toContain(
      "could not import device './no-such-module.cjs#Thing'",
    );
    expect(message).not.toContain("\n");
    expect(message).not.toContain("imported from");
    // The specifier the operator typed is echoed; no other path is.
    expect(message.replace("./no-such-module.cjs#Thing", "")).not.toMatch(/\//);
  });

  it("rejects a specifier with nothing either side of the hash", async () => {
    await expect(resolve(Operation.Monitor, "#Thing")).rejects.toThrow(
      /is not an import path/,
    );
  });

  it("treats a name without a hash as a registered name", async () => {
    // A mistyped `bluefors` must not become an import error. `#` cannot appear in a
    // bare identifier, so the two forms never collide.
    registerDevice(fakeSpec("bluefors_gen1", Operation.Monitor));

    await expect(resolve(Operation.Monitor, "bluefors")).rejects.toThrow(
      /unknown monitor device/,
    );
  });
});

describe("Options", () => {
  it("falls back when a key is absent", () => {
    // Every accessor takes the fallback, so a device states its default once, in the
    // one piece of code that acts on it.
    const options = new Options();

    expect(options.str("base_url", "http://localhost")).toBe(
      "http://localhost",
    );
    expect(options.int("probes", 3)).toBe(3);
    expect(options.num("interval", 2.5)).toBe(2.5);
    expect(options.bool("fast", true)).toBe(true);
    expect(options.ms("timeout")).toBeUndefined();
  });

  it("reads a given value as the accessor says", () => {
    const options = new Options({
      probes: "9",
      interval: "0.5",
      fast: "on",
      poll_interval: "2.5",
    });

    expect(options.int("probes", 3)).toBe(9);
    expect(options.num("interval", 5)).toBe(0.5);
    expect(options.bool("fast")).toBe(true);
    // Seconds on the command line, milliseconds in a JS timer.
    expect(options.ms("poll_interval")).toBe(2500);
  });

  it("reads booleans the way the other SDKs do", () => {
    for (const raw of ["1", "true", "TRUE", " yes ", "On"]) {
      expect(new Options({ k: raw }).bool("k")).toBe(true);
    }
    for (const raw of ["0", "false", "no", "off", "", "  ", "maybe"]) {
      expect(new Options({ k: raw }).bool("k", true)).toBe(false);
    }
  });

  it("names the option a bad value belongs to", () => {
    expect(() => new Options({ probes: "many" }).int("probes", 1)).toThrow(
      /bad value for -o probes: 'many' is not a whole number/,
    );
    expect(() => new Options({ interval: "soon" }).num("interval", 1)).toThrow(
      /bad value for -o interval: 'soon' is not a number/,
    );
  });

  it("reports the -o pair a required option was missing", () => {
    expect(() => new Options().require("channels", "a:K")).toThrow(
      /missing required option 'channels', e.g. -o channels=a:K/,
    );
    expect(new Options({ channels: "a:K" }).require("channels")).toBe("a:K");
  });

  it("reports what nothing looked at", () => {
    // The device's own code is the schema: a key it never read is a typo.
    const options = new Options({ probes: "9", probez: "9", elephant: "1" });
    options.int("probes", 1);

    expect(options.unread()).toEqual(["elephant", "probez"]);
  });

  it("counts reading an absent key as reading it", () => {
    // Asking about a key is what says the device understands it, given or not.
    const options = new Options({ probes: "9" });
    options.str("label", "");
    options.int("probes", 1);

    expect(options.unread()).toEqual([]);
  });

  it("hands over everything left, for a device that forwards options", () => {
    const options = new Options({ probes: "9", qubits: "5" });
    options.int("probes", 1);

    expect(options.remaining()).toEqual({ qubits: "5" });
    expect(options.unread()).toEqual([]);
  });

  it("copies what it is given", () => {
    // A caller mutating its own object afterwards must not change what a driver reads.
    const raw = { probes: "9" };
    const options = new Options(raw);
    raw.probes = "1";

    expect(options.int("probes", 0)).toBe(9);
  });
});
