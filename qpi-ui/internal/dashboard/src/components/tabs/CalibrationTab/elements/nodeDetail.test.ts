import { describe, expect, it } from "vitest";
import type { CalibrationRequest, CalibrationResult } from "@/types";
import { dependentsOf, outcomesFor, reportFor } from "./nodeDetail";

const PLAN = {
  nodes: [
    {
      name: "rabi",
      depends_on: ["qubit_spectroscopy"],
      targets: ["q0", "q1"],
      kind: "qubits" as const,
      planned: true,
      is_benchmark: false,
      has_check: true,
      updates: ["rxy.amp180"],
    },
    {
      name: "ramsey",
      depends_on: ["rabi"],
      targets: ["q0"],
      kind: "qubits" as const,
      planned: true,
      is_benchmark: false,
      has_check: false,
      updates: ["clock_freqs.f01"],
    },
    {
      name: "drag",
      depends_on: ["rabi", "ramsey"],
      targets: ["q0"],
      kind: "qubits" as const,
      planned: true,
      is_benchmark: false,
      has_check: false,
      updates: ["rxy.motzoi"],
    },
  ],
};

function report(fields: Partial<CalibrationResult> = {}): CalibrationResult {
  return {
    id: "r1",
    driver: "d1",
    qpu: "qpu1",
    timestamp: "2026-08-06T12:00:00.000Z",
    duration_s: 100,
    mode: "full",
    routine_results: [],
    benchmarks: [],
    errors: [],
    status: "success",
    created: "2026-08-06 12:01:00.000Z",
    ...fields,
  };
}

function request(fields: Partial<CalibrationRequest> = {}): CalibrationRequest {
  return {
    id: "q1",
    driver: "d1",
    mode: "full",
    status: "done",
    created: "2026-08-06 11:59:00.000Z",
    ...fields,
  };
}

describe("dependentsOf", () => {
  it("finds what a routine feeds, in the plan's order", () => {
    expect(dependentsOf("rabi", PLAN)).toEqual(["ramsey", "drag"]);
  });

  it("is empty for a leaf", () => {
    expect(dependentsOf("drag", PLAN)).toEqual([]);
  });
});

describe("outcomesFor", () => {
  it("gives every target a row, reported on or not", () => {
    const rows = outcomesFor("rabi", ["q0", "q1"], report());
    expect(rows.map((r) => r.target)).toEqual(["q0", "q1"]);
    expect(rows[0].parameters).toBeUndefined();
  });

  it("carries the fitted parameters and the duration through", () => {
    const rows = outcomesFor(
      "rabi",
      ["q0"],
      report({
        routine_results: [
          {
            routine_name: "rabi",
            target: "q0",
            parameters: { "rxy.amp180": 0.203 },
            timestamp: "2026-08-06T12:00:10.000Z",
            duration_s: 41.2,
          },
        ],
      }),
    );
    expect(rows[0]).toMatchObject({
      parameters: { "rxy.amp180": 0.203 },
      durationS: 41.2,
    });
  });

  it("strips the `routine[target]:` key off the report's own error", () => {
    const rows = outcomesFor(
      "rabi",
      ["q0", "q1"],
      report({
        errors: ["rabi[q1]: could not fit", "ramsey[q0]: something else"],
      }),
    );
    expect(rows[0].error).toBeUndefined();
    expect(rows[1].error).toBe("could not fit");
  });

  it("reads a benchmark's fidelity off the protocol of the same name", () => {
    const rows = outcomesFor(
      "rb",
      ["q0"],
      report({
        benchmarks: [
          {
            protocol: "rb",
            target: "q0",
            fidelity: 0.9993,
            error_per_gate: 7e-4,
          },
        ],
      }),
    );
    expect(rows[0]).toMatchObject({ fidelity: 0.9993, errorPerGate: 7e-4 });
  });

  it("has nothing to say before a report exists", () => {
    const rows = outcomesFor("rabi", ["q0"], undefined);
    expect(rows).toEqual([
      {
        target: "q0",
        parameters: undefined,
        durationS: undefined,
        error: undefined,
        fidelity: undefined,
        errorPerGate: undefined,
      },
    ]);
  });
});

describe("reportFor", () => {
  it("has none while the run is still going", () => {
    expect(
      reportFor(request({ status: "running" }), [report()]),
    ).toBeUndefined();
  });

  it("takes the report the run produced", () => {
    expect(reportFor(request(), [report()])?.id).toBe("r1");
  });

  it("will not pass a drift check off as a full run's report", () => {
    const drift = report({ id: "r2", mode: "fidelity_check" });
    expect(reportFor(request(), [drift])).toBeUndefined();
  });

  it("will not reach back to the run before this one", () => {
    const earlier = report({ id: "r0", timestamp: "2026-08-06T09:00:00.000Z" });
    expect(reportFor(request(), [earlier])).toBeUndefined();
  });

  it("ignores another tuner's report", () => {
    expect(reportFor(request(), [report({ driver: "d2" })])).toBeUndefined();
  });
});
