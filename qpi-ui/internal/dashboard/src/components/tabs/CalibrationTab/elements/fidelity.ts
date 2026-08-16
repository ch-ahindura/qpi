import type { BenchmarkResult } from "@/types";

/** Protocols whose `fidelity` is an average gate fidelity, and so comparable with
 * each other's and with the thresholds below.
 *
 * The driver's `GATE_FIDELITY_PROTOCOLS` written a second time, and it has to stay
 * in step with it — see `report.py`, whose `fidelities()` applies the same rule to
 * decide what the drift check compares against.
 *
 * Everything else here reports a number that is *called* a fidelity and is not one.
 * `allxy_check` reports one minus the rms deviation of a normalised population
 * response; `readout_fidelity` reports an assignment fidelity, which is a property
 * of the readout chain and not of a gate. Taking the minimum across all three let
 * the incommensurable ones win by construction: on the August 2026 B chip this card
 * showed `readout_fidelity` at 92.5% and called it below the 99.9% one-qubit *gate*
 * threshold, while randomised benchmarking sat in the same payload unread. */
const GATE_FIDELITY_PROTOCOLS = new Set(["rb", "interleaved_rb"]);

/** Whether a target is an edge, by the same rule the driver uses: an edge is
 * named `<parent>_<child>`, a qubit is not. */
export function isEdge(target: string): boolean {
  return target.includes("_");
}

/** The worst *comparable* benchmark per target, falling back to a diagnostic one
 * where nothing measured a gate fidelity at all.
 *
 * Worst rather than best among the comparable ones, as the drift check does: a
 * fidelity panel should show the worst evidence it has, not the most flattering.
 * The fallback exists so a run with only `allxy_check` shows that rather than an
 * empty grid — better a diagnostic score, named as itself, than nothing. */
export function worstComparable(
  benchmarks: BenchmarkResult[],
): Map<string, BenchmarkResult> {
  const gates = new Map<string, BenchmarkResult>();
  const diagnostics = new Map<string, BenchmarkResult>();
  for (const benchmark of benchmarks) {
    if (benchmark.fidelity === null || benchmark.fidelity === undefined)
      continue;
    const into = GATE_FIDELITY_PROTOCOLS.has(benchmark.protocol)
      ? gates
      : diagnostics;
    const current = into.get(benchmark.target);
    if (!current || benchmark.fidelity < (current.fidelity ?? 1)) {
      into.set(benchmark.target, benchmark);
    }
  }
  for (const [target, benchmark] of diagnostics) {
    if (!gates.has(target)) gates.set(target, benchmark);
  }
  return gates;
}
