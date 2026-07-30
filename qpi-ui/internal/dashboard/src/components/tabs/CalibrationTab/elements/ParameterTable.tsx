import React from "react";
import type { RoutineResult } from "@/types";

interface ParameterTableProps {
  results: RoutineResult[];
}

/** Formats a fitted parameter for reading rather than for precision.
 *
 * Calibration numbers span ten orders of magnitude — a frequency near 5e9, a
 * duration near 2e-8 — so a single fixed format is unreadable for one end or
 * the other. Frequencies become GHz, times become µs or ns, and everything
 * else falls back to a short significant-figure form. */
function formatValue(key: string, value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return Array.isArray(value) ? `[${value.length} values]` : String(value);
  }

  const name = key.toLowerCase();
  if (name.includes("freq") && Math.abs(value) > 1e6) {
    return `${(value / 1e9).toFixed(6)} GHz`;
  }
  if (name.startsWith("t1") || name.startsWith("t2") || name.includes("duration")) {
    return Math.abs(value) < 1e-6
      ? `${(value * 1e9).toFixed(1)} ns`
      : `${(value * 1e6).toFixed(2)} µs`;
  }
  if (Math.abs(value) !== 0 && (Math.abs(value) < 1e-3 || Math.abs(value) >= 1e6)) {
    return value.toExponential(3);
  }
  return value.toFixed(6).replace(/\.?0+$/, "");
}

/** The parameters a calibration produced, grouped by the target they belong to.
 *
 * Grouped by qubit rather than listed by routine because that is the question
 * an operator actually has — "what does q0 look like now?" — and a routine
 * spans every target while a target spans only a handful of routines. */
export const ParameterTable: React.FC<ParameterTableProps> = ({ results }) => {
  const byTarget = new Map<string, RoutineResult[]>();
  for (const result of results) {
    const existing = byTarget.get(result.target) ?? [];
    existing.push(result);
    byTarget.set(result.target, existing);
  }

  if (byTarget.size === 0) {
    return (
      <p className="text-sm text-gray-500 dark:text-zinc-400">
        This run produced no parameters.
      </p>
    );
  }

  return (
    <div className="space-y-6">
      {Array.from(byTarget.entries())
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([target, routines]) => (
          <div key={target} data-testid={`calibration-target-${target}`}>
            <h4 className="text-sm font-medium text-gray-900 dark:text-white mb-2">
              {target}
            </h4>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs uppercase tracking-wider text-gray-500 dark:text-zinc-500 border-b border-gray-200 dark:border-zinc-800">
                    <th className="py-2 pr-4 font-medium">Routine</th>
                    <th className="py-2 pr-4 font-medium">Parameter</th>
                    <th className="py-2 pr-4 font-medium">Value</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100 dark:divide-zinc-800/60">
                  {routines.flatMap((routine) =>
                    Object.entries(routine.parameters ?? {})
                      // Raw sweep axes are echoed back in the report; they are
                      // the input to the fit, not a result of it.
                      .filter(([, value]) => !Array.isArray(value))
                      .map(([key, value]) => (
                        <tr key={`${routine.routine_name}-${key}`}>
                          <td className="py-2 pr-4 text-gray-500 dark:text-zinc-400 font-mono text-xs">
                            {routine.routine_name}
                          </td>
                          <td className="py-2 pr-4 text-gray-700 dark:text-zinc-300 font-mono text-xs">
                            {key}
                          </td>
                          <td className="py-2 pr-4 text-gray-900 dark:text-white font-mono text-xs">
                            {formatValue(key, value)}
                          </td>
                        </tr>
                      )),
                  )}
                </tbody>
              </table>
            </div>
          </div>
        ))}
    </div>
  );
};
