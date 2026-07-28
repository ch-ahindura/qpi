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
  // The high floors apply to the device catalog, the CLI over it, and the CA
  // pinning — all of which need no server.
  //
  // `global` covers only what no path threshold matches, which is the transport:
  // driver.ts's connect/run/receive loop and nng.ts's TLS sockets. Covering those in
  // a unit test would mean reimplementing an NNG peer, and they are exercised for
  // real by `make test-e2e-driver`, so the floor there is set just under what they
  // reach today — enough to catch a regression, not a number pretending to be 96.
  coverageThreshold: {
    "./src/devices.ts": {
      statements: 96,
      lines: 96,
      functions: 96,
      branches: 90,
    },
    "./src/catalog.ts": {
      statements: 96,
      lines: 96,
      functions: 96,
      branches: 85,
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
    global: { statements: 86, lines: 87, functions: 80, branches: 70 },
  },
};
