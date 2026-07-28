import * as http from "node:http";
import type { AddressInfo } from "node:net";

import { Event, EventType } from "../events.js";
import {
  BlueforsGen1Driver,
  normalizeChannels,
  parseChannels,
} from "./bluefors-gen1.js";

function startBluefors(
  handler: (path: string, res: http.ServerResponse) => void,
): Promise<{ url: string; close: () => void }> {
  const server = http.createServer((req, res) => handler(req.url ?? "", res));
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address() as AddressInfo;
      resolve({
        url: `http://127.0.0.1:${port}`,
        close: () => server.close(),
      });
    });
  });
}

function makeDriver(baseUrl: string, channels: Record<string, string>) {
  const driver = new BlueforsGen1Driver({
    qpiAddr: "http://127.0.0.1:1",
    token: "t",
    caFingerprint: "unused: this driver never connects in these tests",
    blueforsBaseUrl: baseUrl,
    channels,
  });
  const emitted: Event[] = [];
  // Capture what would go on the wire without standing up the transport.
  (driver as unknown as { emit: (e: Event) => void }).emit = (e) =>
    emitted.push(e);
  return { driver, emitted };
}

describe("BlueforsGen1Driver", () => {
  test("polls channels (dots become slashes) and emits a CryostatReading", async () => {
    const server = await startBluefors((path, res) => {
      expect(path).toBe("/values/mapper/bf/tmc");
      res.end(
        JSON.stringify({
          data: {
            content: { latest_valid_value: { value: 0.0123, status: "OK" } },
          },
        }),
      );
    });
    const { driver, emitted } = makeDriver(server.url, {
      "mapper.bf.tmc": "K",
    });

    await driver.poll();

    expect(emitted).toHaveLength(1);
    expect(emitted[0].type).toBe(EventType.CryostatReading);
    const readings = emitted[0].payload.readings as Record<string, unknown>;
    expect(readings["mapper.bf.tmc"]).toEqual({
      value: 0.0123,
      unit: "K",
      status: "OK",
    });
    server.close();
  });

  test("falls back to latest_value and coerces numeric strings", async () => {
    const server = await startBluefors((_path, res) => {
      res.end(
        JSON.stringify({
          data: {
            content: { latest_value: { value: "1.5", status: "STALE" } },
          },
        }),
      );
    });
    const { driver, emitted } = makeDriver(server.url, {
      "mapper.bf.pmc": "mbar",
    });

    await driver.poll();

    const readings = emitted[0].payload.readings as Record<
      string,
      { value: number; status: string }
    >;
    expect(readings["mapper.bf.pmc"].value).toBe(1.5);
    expect(readings["mapper.bf.pmc"].status).toBe("STALE");
    server.close();
  });

  test("skips the emit when every channel fails", async () => {
    const server = await startBluefors((_path, res) => {
      res.statusCode = 500;
      res.end("boom");
    });
    const { driver, emitted } = makeDriver(server.url, { x: "" });

    await driver.poll();

    expect(emitted).toHaveLength(0);
    server.close();
  });

  test("ignores inbound events without throwing", () => {
    const { driver } = makeDriver("http://127.0.0.1:1", { x: "K" });
    expect(() =>
      driver.handleEvent(new Event(EventType.JobDispatch, { job_id: "j1" })),
    ).not.toThrow();
  });
});

describe("channel parsing helpers", () => {
  test("normalizeChannels handles maps, lists, and undefined", () => {
    expect(normalizeChannels({ "a.b": "K" })).toEqual({ "a.b": "K" });
    expect(normalizeChannels(["a.b", "c.d"])).toEqual({ "a.b": "", "c.d": "" });
    expect(normalizeChannels(undefined)).toEqual({});
  });

  test("parseChannels parses the path[:unit],... string form", () => {
    expect(parseChannels("mapper.bf.tmc:K, mapper.bf.pmc:mbar ,bare")).toEqual({
      "mapper.bf.tmc": "K",
      "mapper.bf.pmc": "mbar",
      bare: "",
    });
  });
});
