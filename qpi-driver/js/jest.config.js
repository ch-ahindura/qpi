/** @type {import('ts-jest').JestConfigWithTsJest} */
module.exports = {
  preset: "ts-jest",
  testEnvironment: "node",
  roots: ["<rootDir>/src"],
  testMatch: ["**/*.test.ts"],
  moduleNameMapper: {
    "^(\\.{1,2}/.*)\\.js$": "$1",
  },
  collectCoverage: true,
  collectCoverageFrom: ["src/**/*.ts"],
  coveragePathIgnorePatterns: ["\\.test\\.ts$"],
  coverageReporters: ["text", "lcov"],
  // The high floors apply to the device registry, the CLI over it, and the CA
  // pinning — all of which need no server.
  //
  // driver.ts and nng.ts are the transport, and their floors are lower because a
  // unit test reaches them only through a fake peer: a loopback TLS server
  // speaking SP by hand, and an HTTP server standing in for QPI-UI. That covers
  // the handshake, the dial and every way either can time out; what it does not
  // cover is the long-running half — the receive loop, the periodic timers and
  // shutdown — which `make test-e2e-driver` exercises against a live server.
  // Both floors are set just under what the suite reaches today: enough to catch
  // a regression, not a number pretending to be 96.
  //
  // `global` then covers only what no path threshold matches — the event
  // envelope and the built-in Bluefors monitor — so its numbers are not the "All
  // files" row jest prints.
  coverageThreshold: {
    "./src/devices.ts": {
      statements: 96,
      lines: 96,
      functions: 96,
      branches: 90,
    },
    "./src/builtins/cli.ts": {
      statements: 90,
      lines: 90,
      functions: 80,
      branches: 90,
    },
    "./src/ca.ts": {
      statements: 100,
      lines: 100,
      functions: 100,
      branches: 100,
    },
    "./src/index.ts": {
      statements: 100,
      lines: 100,
      functions: 100,
      branches: 100,
    },
    "./src/driver.ts": {
      statements: 82,
      lines: 82,
      functions: 84,
      branches: 70,
    },
    "./src/nng.ts": {
      statements: 95,
      lines: 95,
      functions: 95,
      branches: 84,
    },
    global: { statements: 91, lines: 92, functions: 80, branches: 76 },
  },
};
