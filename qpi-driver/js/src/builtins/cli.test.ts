/**
 * The `qpi-driver` CLI (RFC 0003 §4, §10) — which had no test file at all.
 *
 * The decisions belong to `devices.ts` and are tested there; what is left here is the
 * wiring, and the two things only the wiring can get wrong: the flags it accepts, and
 * what it refuses to run without.
 */

import { buildProgram, isCommanderOutput, splitOptions } from "./cli.js";
import { DEVICE_SPEC } from "./bluefors-gen1.device.js";
import {
  clearDevices,
  type DeviceConfig,
  Operation,
  registerDevice,
} from "../devices.js";
import type { QpiDriver } from "../driver.js";

// cli.ts registers bluefors_gen1 when it is imported, and some tests below replace
// the registry wholesale — so it is put back for the ones that need it.
afterEach(() => {
  clearDevices();
  registerDevice(DEVICE_SPEC);
});

/** Run the CLI, capturing what it wrote and any exit it attempted. */
async function run(...argv: string[]): Promise<{ out: string; exit?: number }> {
  const written: string[] = [];
  const log = jest
    .spyOn(console, "log")
    .mockImplementation((...args) => written.push(args.join(" ")));
  const error = jest
    .spyOn(console, "error")
    .mockImplementation((...args) => written.push(args.join(" ")));
  let exit: number | undefined;
  const exiter = jest.spyOn(process, "exit").mockImplementation(((
    code?: number,
  ) => {
    exit = code;
    // `fail()` is typed as never-returning, and the code after it assumes so, so
    // stop the command here rather than letting it run on with bad state.
    throw new Error(`process.exit(${code})`);
  }) as never);

  // Commander writes help and its own errors through this hook rather than
  // console, so it has to be captured too — and on the subcommands, which do not
  // inherit it once they exist.
  const program = buildProgram();
  const capture = {
    writeOut: (text: string) => void written.push(text),
    writeErr: (text: string) => void written.push(text),
  };
  program.configureOutput(capture);
  for (const command of program.commands) {
    command.configureOutput(capture);
  }

  try {
    await program.parseAsync(["node", "cli.js", ...argv]);
  } catch {
    // An attempted exit, or commander's own exitOverride: the output is the subject.
  } finally {
    log.mockRestore();
    error.mockRestore();
    exiter.mockRestore();
  }
  return { out: written.join("\n"), exit };
}

describe("start", () => {
  it("requires an operation, naming the valid ones", async () => {
    const { out, exit } = await run("start", "--token", "t");

    expect(exit).toBe(1);
    expect(out).toContain("--operation is required");
    expect(out).toContain("process, monitor");
  });

  it("rejects an unknown operation", async () => {
    const { out, exit } = await run(
      "start",
      "--operation",
      "procces",
      "--token",
      "t",
    );

    expect(exit).toBe(1);
    expect(out).toContain("unknown operation 'procces'");
  });

  it("requires an access token", async () => {
    const { out, exit } = await run("start", "--operation", "monitor");

    expect(exit).toBe(1);
    expect(out).toContain("access token is required");
  });

  it("requires a CA fingerprint, with no way to opt out", async () => {
    // The gap this closed: the SDK used to skip verification entirely when the
    // fingerprint was absent, so an unpinned connection was reachable by leaving an
    // argument out — which a copy-pasted command hits by accident (RFC 0003 §10).
    const { out, exit } = await run(
      "start",
      "--operation",
      "monitor",
      "--device",
      "bluefors_gen1",
      "--token",
      "t",
    );

    expect(exit).toBe(1);
    expect(out).toContain("CA fingerprint is required");
  });

  it("rejects an unknown -o key before connecting to anything", async () => {
    // With no declared schema, the check is what the device read: the builder never
    // looks at base_urll, so the leftover key is the whole evidence.
    const { out, exit } = await run(
      "start",
      "--operation",
      "monitor",
      "--device",
      "bluefors_gen1",
      "--token",
      "t",
      "--ca-fingerprint",
      "fp",
      "-o",
      "channels=mapper.bf.tmc:K",
      "-o",
      "base_urll=http://x",
    );

    expect(exit).toBe(1);
    expect(out).toContain("unknown option 'base_urll'");
  });

  it("reports a missing required option", async () => {
    const { out, exit } = await run(
      "start",
      "--operation",
      "monitor",
      "--device",
      "bluefors_gen1",
      "--token",
      "t",
      "--ca-fingerprint",
      "fp",
    );

    expect(exit).toBe(1);
    expect(out).toContain("missing required option 'channels'");
  });

  it("has no short form for --operation, and no -O beside -o", async () => {
    // -o is --option; -O next to it would be a hazard in a command usually written
    // once into a unit file (RFC 0003 §13.7).
    const start = buildProgram().commands.find(
      (command) => command.name() === "start",
    )!;
    const flags = start.options.map((option) => option.flags).join(" ");

    expect(flags).toContain("--operation <operation>");
    expect(flags).not.toContain("-O");
    expect(flags).not.toMatch(/-\w, --operation/);
  });

  it("defaults --device to nothing, and has no fallback for it", async () => {
    const start = buildProgram().commands.find(
      (command) => command.name() === "start",
    )!;
    const defaults = Object.fromEntries(
      start.options.map((option) => [option.long, option.defaultValue]),
    );

    expect(defaults["--device"]).toBe("");
    expect(defaults["--operation"]).toBe("");
  });

  it("has no --name: a driver does not name itself", async () => {
    // The display label belongs to the admin who registered the driver, and the
    // drivers/connect response hands it over. A flag that no longer reached
    // anything would be worse than one that is rejected.
    const start = buildProgram().commands.find(
      (command) => command.name() === "start",
    )!;
    const flags = start.options.map((option) => option.flags).join(" ");

    expect(flags).not.toContain("--name");
    expect(flags).not.toMatch(/-n\b/);
  });

  it("no longer has the old per-operation subcommands", async () => {
    // The grammar broke once, deliberately (RFC 0003 §11); a subcommand that still
    // worked would make the migration optional and the docs wrong.
    const names = buildProgram().commands.map((command) => command.name());

    expect(names).toContain("start");
    expect(names).not.toContain("process");
    expect(names).not.toContain("monitor");
  });

  it("points at the dashboard in --help rather than describing devices", async () => {
    // --help used to render every device and every -o key from a declared schema,
    // which was a second catalog beside QPI-UI's (RFC 0003 §9).
    const { out } = await run("start", "--help");

    expect(out).toContain("QPI-UI");
    expect(out).toContain("qpi-driver devices");
  });

  it("asks for a device, naming the ones this build has", async () => {
    const { out, exit } = await run(
      "start",
      "--operation",
      "monitor",
      "--token",
      "t",
      "--ca-fingerprint",
      "fp",
    );

    expect(exit).toBe(1);
    expect(out).toContain("--device is required");
    expect(out).toContain("bluefors_gen1");
  });

  it("says plainly when this build ships no device for the operation", async () => {
    const { out, exit } = await run(
      "start",
      "--operation",
      "process",
      "--token",
      "t",
      "--ca-fingerprint",
      "fp",
    );

    expect(exit).toBe(1);
    expect(out).toContain("ships no process devices");
  });
});

describe("start, as far as it goes without a server", () => {
  it("builds the resolved device and runs it", async () => {
    // The happy path: resolve, parse, build, run. `run()` is where it would connect,
    // so the device under test is one whose driver does nothing — which is only
    // possible because a builder returns a driver rather than running one.
    const built: DeviceConfig[] = [];
    const ran: string[] = [];
    clearDevices();
    registerDevice({
      name: "recorder",
      operation: Operation.Monitor,
      build: (config) => {
        built.push(config);
        return {
          run: async () => void ran.push("ran"),
        } as unknown as QpiDriver;
      },
    });

    const { exit } = await run(
      "start",
      "--operation",
      "monitor",
      "--device",
      "recorder",
      "--token",
      "tok",
      "--ca-fingerprint",
      "fp",
      "--qpi-addr",
      "https://qpi.example.com",
    );

    expect(exit).toBeUndefined();
    expect(ran).toEqual(["ran"]);
    expect(built[0]).toEqual({
      qpiAddr: "https://qpi.example.com",
      token: "tok",
      caFingerprint: "fp",
      caFilePath: "./bin/qpi.ca.pem",
    });
  });

  it("reports an error from the builder as one line", async () => {
    clearDevices();
    registerDevice({
      name: "refuses",
      operation: Operation.Monitor,
      build: () => {
        throw new Error("the cryostat is warm");
      },
    });

    const { out, exit } = await run(
      "start",
      "--operation",
      "monitor",
      "--device",
      "refuses",
      "--token",
      "t",
      "--ca-fingerprint",
      "fp",
    );

    expect(exit).toBe(1);
    expect(out).toContain("Error: the cryostat is warm");
  });

  it("reports an option the device never read, after building it", async () => {
    clearDevices();
    registerDevice({
      name: "reads_nothing",
      operation: Operation.Monitor,
      build: () => ({ run: async () => {} }) as unknown as QpiDriver,
    });

    const { out, exit } = await run(
      "start",
      "--operation",
      "monitor",
      "--device",
      "reads_nothing",
      "--token",
      "t",
      "--ca-fingerprint",
      "fp",
      "-o",
      "probes=4",
    );

    expect(exit).toBe(1);
    expect(out).toContain("unknown option 'probes' for monitor device");
  });
});

describe("devices", () => {
  it("lists names by operation, or one operation", async () => {
    const all = await run("devices");
    expect(all.out).toContain("monitor: bluefors_gen1");
    // This build ships no process device, and must say so rather than print nothing.
    expect(all.out).toContain("process: none");

    const narrowed = await run("devices", "--operation", "monitor");
    expect(narrowed.out).toContain("bluefors_gen1");
    expect(narrowed.out).not.toContain("process:");
  });

  it("rejects an unknown operation", async () => {
    const { out, exit } = await run("devices", "--operation", "nope");

    expect(exit).toBe(1);
    expect(out).toContain("unknown operation 'nope'");
  });
});

describe("catalog", () => {
  it("is gone; nothing consumed it but a sync script", async () => {
    const names = buildProgram().commands.map((command) => command.name());

    expect(names).not.toContain("catalog");
  });
});

describe("splitOptions", () => {
  it("reads the syntax only", () => {
    // Splitting is all the CLI does; an unknown key passes through for the
    // unread-option check to catch after the build.
    expect(splitOptions(["a=1", " b = two ", "nonsense=x"])).toEqual({
      a: "1",
      b: "two",
      nonsense: "x",
    });
  });

  it("rejects a pair without '='", () => {
    expect(() => splitOptions(["not-a-pair"])).toThrow(/expected key=value/);
  });
});

describe("isCommanderOutput", () => {
  // `--help` exiting non-zero is not a cosmetic complaint: a `set -e` script that
  // asks a CLI what it can do before using it dies on the answer, and the Go and
  // Python CLIs both exit 0. exitOverride() turns help into a rejected promise, so
  // the bin has to tell that apart from a real failure.
  it("treats commander printing help or the version as success", () => {
    for (const code of [
      "commander.helpDisplayed",
      "commander.help",
      "commander.version",
    ]) {
      expect(isCommanderOutput({ code })).toBe(true);
    }
  });

  it("treats anything else as a failure", () => {
    expect(isCommanderOutput({ code: "commander.unknownOption" })).toBe(false);
    expect(isCommanderOutput(new Error("the cryostat is warm"))).toBe(false);
    expect(isCommanderOutput(undefined)).toBe(false);
    expect(isCommanderOutput(null)).toBe(false);
  });
});
