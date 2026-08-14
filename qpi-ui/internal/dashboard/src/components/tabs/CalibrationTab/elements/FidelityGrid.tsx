import React from "react";
import type { BenchmarkResult } from "@/types";

interface FidelityGridProps {
  benchmarks: BenchmarkResult[];
  /** 1Q fidelity below which the driver triggers a recalibration. */
  threshold?: number;
  /** 2Q fidelity below which the driver triggers a recalibration. */
  threshold2q?: number;
}

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
function isEdge(target: string): boolean {
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

/** The measured fidelities, one card per target.
 *
 * A target is shown against the threshold that actually governs it — the
 * two-qubit one for an edge, the one-qubit one for a qubit. A CZ an order of
 * magnitude worse than a single-qubit gate is normal, and colouring it red
 * against the 1Q threshold would make the whole grid cry wolf. */
export const FidelityGrid: React.FC<FidelityGridProps> = ({
  benchmarks,
  threshold = 0.999,
  threshold2q = 0.99,
}) => {
  const worst = worstComparable(benchmarks);

  if (worst.size === 0) {
    return (
      <p className="text-sm text-gray-500 dark:text-zinc-400">
        No benchmarks in this run. Fidelities appear once <code>rb</code> or{" "}
        <code>allxy_check</code> has run.
      </p>
    );
  }

  return (
    <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
      {Array.from(worst.entries())
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([target, benchmark]) => {
          const limit = isEdge(target) ? threshold2q : threshold;
          const drifted = (benchmark.fidelity ?? 0) < limit;
          return (
            <div
              key={target}
              data-testid={`fidelity-${target}`}
              className={`rounded-lg border p-4 ${
                drifted
                  ? "border-amber-300 dark:border-amber-500/40 bg-amber-50 dark:bg-amber-500/5"
                  : "border-gray-200 dark:border-zinc-800 bg-white dark:bg-zinc-900/40"
              }`}
            >
              <div className="flex items-baseline justify-between gap-2">
                <span className="font-mono text-sm text-gray-900 dark:text-white">
                  {target}
                </span>
                <span className="text-[10px] uppercase tracking-wider text-gray-400 dark:text-zinc-500">
                  {benchmark.protocol}
                </span>
              </div>
              <div
                className={`mt-2 text-2xl font-geist tabular-nums ${
                  drifted
                    ? "text-amber-700 dark:text-amber-400"
                    : "text-gray-900 dark:text-white"
                }`}
              >
                {((benchmark.fidelity ?? 0) * 100).toFixed(3)}%
              </div>
              <div className="mt-1 text-xs text-gray-500 dark:text-zinc-400">
                {drifted ? "below" : "above"} {(limit * 100).toFixed(1)}%
                {isEdge(target) ? " (2Q)" : " (1Q)"}
              </div>
            </div>
          );
        })}
    </div>
  );
};
