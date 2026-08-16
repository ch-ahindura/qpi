import React from "react";
import type { BenchmarkResult } from "@/types";
import { isEdge, worstComparable } from "./fidelity";

interface FidelityGridProps {
  benchmarks: BenchmarkResult[];
  /** 1Q fidelity below which the driver triggers a recalibration. */
  threshold?: number;
  /** 2Q fidelity below which the driver triggers a recalibration. */
  threshold2q?: number;
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
