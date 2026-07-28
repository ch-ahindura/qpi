/**
 * The package's public surface.
 *
 * `index.ts` is re-exports and nothing else, which is exactly why it is worth a
 * test: a rename or a moved file breaks `import { … } from "qpi-driver"` for every
 * consumer while every other test still passes, because the tests import the modules
 * directly.
 */

import * as sdk from "./index.js";

describe("the package entry point", () => {
  it("exports the driver-authoring surface", () => {
    for (const name of ["QpiDriver", "Event", "EventType"]) {
      expect(sdk).toHaveProperty(name);
    }
  });

  it("exports the device catalog, so a device can be registered from it", () => {
    for (const name of [
      "Operation",
      "Options",
      "registerDevice",
      "devices",
      "resolve",
      "parseOptions",
      "operations",
      "operationNames",
      "lookupOperation",
      "hasDevice",
      "clearDevices",
      "asString",
      "asInt",
      "asFloat",
      "asBool",
    ]) {
      expect(sdk).toHaveProperty(name);
    }
  });

  it("exports the catalog renderers the CLI is built on", () => {
    for (const name of [
      "catalog",
      "renderCatalog",
      "deviceLines",
      "SCHEMA_VERSION",
    ]) {
      expect(sdk).toHaveProperty(name);
    }
  });

  it("re-exports the same objects, not copies", () => {
    // A barrel that rebuilt its exports would give a consumer a second, empty
    // registry — `registerDevice` from the root must reach the same table.
    const direct = require("./devices.js");
    expect(sdk.registerDevice).toBe(direct.registerDevice);
    expect(sdk.Operation).toBe(direct.Operation);
  });
});
