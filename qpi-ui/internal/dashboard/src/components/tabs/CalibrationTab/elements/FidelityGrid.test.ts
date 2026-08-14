import { describe, expect, it } from "vitest";
import { worstComparable } from "./FidelityGrid";
import type { BenchmarkResult } from "@/types";

function benchmark(
  protocol: string,
  fidelity: number | null,
  target = "q5",
): BenchmarkResult {
  return { protocol, target, fidelity, error_per_gate: null };
}

/** The August 2026 B chip's benchmarks, in the order the driver emitted them. */
const B_CHIP: BenchmarkResult[] = [
  benchmark("readout_fidelity", 0.925),
  benchmark("rb", 0.9999887278138929),
  benchmark("allxy_check", 0.9416347706408066),
];

describe("worstComparable", () => {
  it("shows the gate fidelity, not the lowest number in the payload", () => {
    // Taking the minimum across all three showed readout_fidelity at 92.5% and
    // called it below the 99.9% one-qubit *gate* threshold. An assignment fidelity
    // is a property of the readout chain; it is not in the same units as a gate
    // infidelity and cannot be compared with one.
    const chosen = worstComparable(B_CHIP).get("q5");
    expect(chosen?.protocol).toBe("rb");
    expect(chosen?.fidelity).toBeCloseTo(0.9999887, 6);
  });

  it("still shows the worst of two comparable protocols", () => {
    const chosen = worstComparable([
      benchmark("rb", 0.994),
      benchmark("interleaved_rb", 0.981),
    ]).get("q5");
    expect(chosen?.protocol).toBe("interleaved_rb");
  });

  it("falls back to a diagnostic where nothing measured a gate fidelity", () => {
    // Better a diagnostic score, named as itself, than an empty grid — this is
    // what a `allxy_as_smoke_test` run looks like.
    const chosen = worstComparable([
      benchmark("readout_fidelity", 0.925),
      benchmark("allxy_check", 0.9416),
    ]).get("q5");
    expect(chosen?.protocol).toBe("readout_fidelity");
  });

  it("keeps each target's own evidence apart", () => {
    const worst = worstComparable([
      benchmark("rb", 0.994, "q0"),
      benchmark("allxy_check", 0.88, "q1"),
    ]);
    expect(worst.get("q0")?.protocol).toBe("rb");
    expect(worst.get("q1")?.protocol).toBe("allxy_check");
  });

  it("ignores a benchmark that measured nothing", () => {
    expect(worstComparable([benchmark("rb", null)]).size).toBe(0);
  });
});
