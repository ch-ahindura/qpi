/**
 * The `qpi-driver` CLI (RFC 0003 §4, §10) — which had no test file at all.
 *
 * The decisions belong to `devices.ts` and `catalog.ts` and are tested there; what
 * is left here is the wiring, and the two things only the wiring can get wrong: the
 * flags it accepts, and what it refuses to run without.
 */

import { buildProgram, splitOptions } from "./cli.js";
import { DEVICE_SPEC } from "./bluefors-gen1.device.js";

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
      "--token",
      "t",
    );

    expect(exit).toBe(1);
    expect(out).toContain("CA fingerprint is required");
  });

  it("rejects an unknown -o key before connecting to anything", async () => {
    const { out, exit } = await run(
      "start",
      "--operation",
      "monitor",
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
    expect(out).toContain("base_url");
  });

  it("reports a missing required option", async () => {
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
    expect(out).toContain("needs a 'channels' option");
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

  it("defaults --device and --name to nothing, so the operation decides", async () => {
    const start = buildProgram().commands.find(
      (command) => command.name() === "start",
    )!;
    const defaults = Object.fromEntries(
      start.options.map((option) => [option.long, option.defaultValue]),
    );

    expect(defaults["--device"]).toBe("");
    expect(defaults["--name"]).toBe("");
    expect(defaults["--operation"]).toBe("");
  });

  it("no longer has the old per-operation subcommands", async () => {
    // The grammar broke once, deliberately (RFC 0003 §11); a subcommand that still
    // worked would make the migration optional and the docs wrong.
    const names = buildProgram().commands.map((command) => command.name());

    expect(names).toContain("start");
    expect(names).not.toContain("process");
    expect(names).not.toContain("monitor");
  });

  it("lists every operation, device and option in --help", async () => {
    const { out } = await run("start", "--help");

    expect(out).toContain("--operation process");
    expect(out).toContain("--operation monitor");
    expect(out).toContain("bluefors_gen1");
    for (const option of DEVICE_SPEC.options ?? []) {
      expect(out).toContain(`${option.key}=<${option.type}>`);
    }
    expect(out).toContain("channels=<channels> (required");
  });
});

describe("devices", () => {
  it("lists the whole catalog, or one operation", async () => {
    expect((await run("devices")).out).toContain("--operation process");

    const narrowed = await run("devices", "--operation", "monitor");
    expect(narrowed.out).toContain("bluefors_gen1");
    expect(narrowed.out).not.toContain("--operation process");
  });

  it("rejects an unknown operation", async () => {
    const { out, exit } = await run("devices", "--operation", "nope");

    expect(exit).toBe(1);
    expect(out).toContain("unknown operation 'nope'");
  });
});

describe("catalog", () => {
  it("prints JSON by default, and the devices text on --text", async () => {
    const { out } = await run("catalog");
    const document = JSON.parse(out);

    expect(document.schema_version).toBe(1);
    expect(
      document.operations.find(
        (each: { name: string }) => each.name === "monitor",
      ).devices[0].name,
    ).toBe("bluefors_gen1");

    expect((await run("catalog", "--text")).out).toEqual(
      (await run("devices")).out,
    );
  });
});

describe("splitOptions", () => {
  it("reads the syntax only", () => {
    // Splitting is all the CLI does; an unknown key passes through for the device's
    // own schema to reject.
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
