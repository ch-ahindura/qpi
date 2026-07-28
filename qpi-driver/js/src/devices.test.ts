/**
 * The operation and device catalog (RFC 0003 §5, §6, §8).
 *
 * The decisions live here rather than in the commander wiring, so this is where they
 * are asserted: which operations exist, which device an `--operation`/`--device` pair
 * means, what an omitted flag falls back to, and what happens when an import path or
 * an option value is wrong.
 */

import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  asBool,
  asFloat,
  asInt,
  asString,
  clearDevices,
  devices,
  hasDevice,
  lookupOperation,
  Operation,
  Options,
  operationNames,
  operations,
  parseOptions,
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
  return {
    name,
    operation,
    summary: "A device from outside the SDK.",
    build: fakeBuild,
    options: [
      {
        key: "probes",
        help: "How many.",
        type: "int",
        parse: asInt,
        default: "1",
      },
      { key: "label", help: "What to call it.", type: "str" },
      {
        key: "fast",
        help: "Whether to hurry.",
        type: "bool",
        parse: asBool,
        default: "no",
      },
    ],
    ...extra,
  };
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

  it("each describe themselves for generated help", () => {
    for (const spec of operations()) {
      expect(spec.summary).toBeTruthy();
      expect(spec.events.length).toBeGreaterThan(0);
    }
  });

  it("cannot be mutated through the accessor", () => {
    operations()[0].summary = "tampered";
    expect(operations()[0].summary).not.toBe("tampered");
  });

  it("can be looked up by name, and only by a real one", () => {
    expect(lookupOperation("monitor")?.defaultDevice).toBe("bluefors_gen1");
    expect(lookupOperation("telemetry")).toBeUndefined();
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
    // With no declared schema, every option passes through as the string it was
    // typed as — there is nothing to convert it to.
    expect(spec.acceptsAnyOption).toBe(true);
    expect(parseOptions(spec, { anything: "1" }).str("anything")).toBe("1");
  });

  it("imports a DeviceSpec directly, which is how a schema is declared", async () => {
    const path = await moduleWith(
      `module.exports.SPEC = { name: "mine", operation: "monitor",
         summary: "Mine.", build: () => ({}),
         options: [{ key: "probes", help: "How many.", type: "int" }] };\n`,
    );

    const spec = await resolve(Operation.Monitor, `${path}#SPEC`);

    expect(spec.name).toBe("mine");
    expect(spec.options?.[0].key).toBe("probes");
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

describe("parseOptions", () => {
  it("converts what is given and fills in defaults", () => {
    const parsed = parseOptions(fakeSpec("mine", Operation.Monitor), {
      probes: "9",
    });

    expect(parsed.int("probes")).toBe(9);
    expect(parsed.bool("fast")).toBe(false);
    // `label` has neither a value nor a default, so it is absent rather than "" —
    // which lets a builder tell "not set" from "set to nothing".
    expect(parsed.has("label")).toBe(false);
  });

  it("rejects an unknown key, listing the valid ones", () => {
    // Silently ignoring it is what this replaced: a typo in a unit file used to mean
    // a driver running with a default nobody chose.
    expect(() =>
      parseOptions(fakeSpec("mine", Operation.Monitor), { probez: "9" }),
    ).toThrow(/unknown option 'probez'.*valid options: fast, label, probes/);
  });

  it("reports every unknown key at once", () => {
    expect(() =>
      parseOptions(fakeSpec("mine", Operation.Monitor), { y: "1", x: "2" }),
    ).toThrow(/unknown options 'x', 'y'/);
  });

  it("passes anything through for a device with no schema", () => {
    const spec = fakeSpec("mine", Operation.Monitor, {
      acceptsAnyOption: true,
    });

    expect(parseOptions(spec, { whatever: "1" }).str("whatever")).toBe("1");
  });

  it("reports a missing required key with its example", () => {
    const spec = fakeSpec("mine", Operation.Monitor, {
      options: [
        {
          key: "channels",
          help: "What to poll.",
          required: true,
          example: "a:K",
        },
      ],
    });

    // The error is the fix: it shows the -o line that should have been typed.
    expect(() => parseOptions(spec, {})).toThrow(
      /needs a 'channels' option, e.g. -o channels=a:K/,
    );
  });

  it("names the option a bad value belongs to", () => {
    expect(() =>
      parseOptions(fakeSpec("mine", Operation.Monitor), { probes: "many" }),
    ).toThrow(/bad value for -o probes/);
  });

  it("accepts a device that reads nothing", () => {
    const spec: DeviceSpec = {
      name: "bare",
      operation: Operation.Monitor,
      build: fakeBuild,
    };

    expect(parseOptions(spec, {}).all()).toEqual({});
    expect(() => parseOptions(spec, { anything: "1" })).toThrow(
      /valid options: none/,
    );
  });
});

describe("parsers", () => {
  it("read booleans the way the other SDKs do", () => {
    for (const raw of ["1", "true", "TRUE", " yes ", "On"]) {
      expect(asBool(raw)).toBe(true);
    }
    for (const raw of ["0", "false", "no", "off", "", "  ", "maybe"]) {
      expect(asBool(raw)).toBe(false);
    }
  });

  it("read numbers, and refuse what is not one", () => {
    expect(asFloat(" 2.5 ")).toBe(2.5);
    expect(() => asFloat("soon")).toThrow(/not a number/);
    expect(asInt(" 7 ")).toBe(7);
    expect(() => asInt("7.5")).toThrow(/not a whole number/);
  });

  it("leave a string alone by default", () => {
    expect(asString(" kept ")).toBe(" kept ");
  });
});

describe("Options", () => {
  it("reads a missing or wrongly-typed option as its zero value", () => {
    // A builder asking for a key its own spec declares cannot be wrong; one asking
    // for anything else should not crash a driver.
    const options = new Options();

    expect(options.str("x")).toBe("");
    expect(options.num("x")).toBe(0);
    expect(options.bool("x")).toBe(false);
    expect(options.channels("x")).toEqual({});
    expect(options.ms("x")).toBeUndefined();
    expect(options.has("x")).toBe(false);
  });

  it("converts a seconds option to milliseconds", () => {
    // Seconds on the command line, milliseconds in a JS timer — declared as "float"
    // in the catalog so every SDK reports the same type for the same option.
    expect(new Options({ poll_interval: 2.5 }).ms("poll_interval")).toBe(2500);
  });
});
