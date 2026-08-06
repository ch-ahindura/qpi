import type {
  CalibrationPlan,
  CalibrationRequest,
  CalibrationResult,
} from "@/types";

/** What one target of one routine produced, drawn from the report (RFC 0006 §6). */
export interface TargetOutcome {
  target: string;
  /** The fitted parameters, once the report has landed. */
  parameters?: Record<string, unknown>;
  durationS?: number;
  /** The report's own entry, which is already keyed `routine[target]`. */
  error?: string;
  fidelity?: number | null;
  errorPerGate?: number | null;
}

/** Everything the report says about *routine*, one row per target it walked.
 *
 * A target with neither a result nor an error has not been reached yet, and its row
 * says so rather than being left out: the walk's own tally is the count of these. */
export function outcomesFor(
  routine: string,
  targets: string[],
  report?: CalibrationResult,
): TargetOutcome[] {
  return targets.map((target) => {
    const result = report?.routine_results?.find(
      (r) => r.routine_name === routine && r.target === target,
    );
    const benchmark = report?.benchmarks?.find(
      (b) => b.protocol === routine && b.target === target,
    );
    const prefix = `${routine}[${target}]:`;
    const error = report?.errors?.find((e) => e.startsWith(prefix));
    return {
      target,
      parameters: result?.parameters,
      durationS: result?.duration_s,
      error: error?.slice(prefix.length).trim(),
      fidelity: benchmark?.fidelity,
      errorPerGate: benchmark?.error_per_gate,
    };
  });
}

/** The routines that depend on *routine*, in the plan's own order. */
export function dependentsOf(routine: string, plan: CalibrationPlan): string[] {
  return plan.nodes
    .filter((node) => node.depends_on.includes(routine))
    .map((node) => node.name);
}

/** The report *request* produced, or undefined while it is still running.
 *
 * Matched by driver, mode and time rather than by id, because a report carries no
 * job id: it is written when the request closes out. Mode matters — a drift check
 * between a full run and now would otherwise supply its benchmarks as though they
 * were that run's parameters. */
export function reportFor(
  request: CalibrationRequest,
  results: CalibrationResult[],
): CalibrationResult | undefined {
  if (request.status === "pending" || request.status === "running") {
    return undefined;
  }
  const queued = asDate(request.created);
  return results.find(
    (result) =>
      result.driver === request.driver &&
      result.mode === request.mode &&
      asDate(result.timestamp) >= queued,
  );
}

/** PocketBase stores `2026-08-06 20:00:00.000Z`; a report's own timestamp is ISO.
 * Only the second parses everywhere, so the first is made to look like it. */
function asDate(value: string): number {
  return new Date(value.replace(" ", "T")).getTime();
}
