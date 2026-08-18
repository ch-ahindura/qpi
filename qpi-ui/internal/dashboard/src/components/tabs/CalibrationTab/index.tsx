import React, { useMemo, useState } from "react";
import { Activity, Play, Loader2 } from "lucide-react";
import type { CalibrationRequest, CalibrationResult, Driver } from "@/types";
import { TUNER_KINDS } from "@/types";
import { CalibrationGraph } from "./elements/CalibrationGraph";
import { FidelityGrid } from "./elements/FidelityGrid";
import { statusOf } from "./elements/layout";
import { NodeCard } from "./elements/NodeCard";
import { reportFor } from "./elements/nodeDetail";
import { ParameterTable } from "./elements/ParameterTable";
import {
  TriggerCalibrationModal,
  type CalibrationMode,
} from "./elements/TriggerCalibrationModal";

interface CalibrationTabProps {
  drivers: Driver[];
  results: CalibrationResult[];
  requests: CalibrationRequest[];
  onTrigger: (payload: {
    driver_id: string;
    mode: CalibrationMode;
    target_qubits?: string[];
    target_edges?: string[];
  }) => Promise<void>;
}

const STATUS_STYLES: Record<string, string> = {
  success: "text-emerald-700 dark:text-emerald-400",
  partial_failure: "text-amber-700 dark:text-amber-400",
  failed: "text-red-600 dark:text-red-400",
};

/** The admin who dispatched a calibration, however much of them we can see. */
function requesterName(request: CalibrationRequest): string {
  const user = request.expand?.requested_by;
  return user?.username ?? user?.email ?? "";
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  if (seconds < 3600) return `${(seconds / 60).toFixed(1)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

/** Calibration state for the tuner drivers (RFC 0004 §6.8).
 *
 * Reads `calibration_results` for what the chip currently looks like and
 * `calibration_requests` for whether something is running right now. The two are
 * separate because a calibration takes hours: the request is how the dashboard
 * can say "in progress" during the long gap before a report exists. */
export const CalibrationTab: React.FC<CalibrationTabProps> = ({
  drivers,
  results,
  requests,
  onTrigger,
}) => {
  const tuners = useMemo(
    () => drivers.filter((d) => TUNER_KINDS.includes(d.kind)),
    [drivers],
  );

  const [selectedDriver, setSelectedDriver] = useState<string>("all");
  const [triggerFor, setTriggerFor] = useState<Driver | null>(null);

  const visible = useMemo(
    () =>
      selectedDriver === "all"
        ? results
        : results.filter((r) => r.driver === selectedDriver),
    [results, selectedDriver],
  );

  // The report to show parameters from. A drift check calibrates nothing, so
  // the current parameters are the last run that actually wrote some.
  const latest = visible[0];
  const latestCalibration = useMemo(
    () =>
      visible.find((r) => r.mode !== "fidelity_check" && r.status !== "failed"),
    [visible],
  );

  const forDriver = useMemo(
    () =>
      requests.filter(
        (r) => selectedDriver === "all" || r.driver === selectedDriver,
      ),
    [requests, selectedDriver],
  );

  const inFlight = useMemo(
    () =>
      forDriver.filter((r) => r.status === "running" || r.status === "pending"),
    [forDriver],
  );

  // One graph per tuner: the run it is making, or the last one it made. The
  // finished ones matter because a plan is only on the request, while the fitted
  // parameters the card shows are only in the report — so the graph has to outlive
  // the walk for the two to ever be in front of an operator together.
  const graphed = useMemo(() => {
    const newest = new Map<string, CalibrationRequest>();
    for (const request of forDriver) {
      if (!request.plan?.nodes?.length) continue;
      if (!newest.has(request.driver)) newest.set(request.driver, request);
    }
    return [...newest.values()];
  }, [forDriver]);

  return (
    <div className="space-y-8">
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <h1 className="text-3xl font-geist text-gray-900 dark:text-white">
            Calibration
          </h1>
          <p className="text-sm text-gray-500 dark:text-zinc-400 mt-1">
            Current chip parameters, measured fidelities, and calibration
            history.
          </p>
        </div>
        {tuners.length > 0 && (
          <div className="flex items-center gap-3">
            <select
              data-testid="calibration-driver-select"
              value={selectedDriver}
              onChange={(e) => setSelectedDriver(e.target.value)}
              className="bg-gray-50 dark:bg-zinc-950 border border-gray-200 dark:border-zinc-800 text-gray-900 dark:text-white rounded px-3 py-2 text-sm focus:outline-none focus:border-zinc-500 transition-colors"
            >
              <option value="all">All tuners</option>
              {tuners.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name} ({d.status})
                </option>
              ))}
            </select>
            <button
              data-testid="calibration-trigger-button"
              onClick={() =>
                setTriggerFor(
                  tuners.find((d) => d.id === selectedDriver) ?? tuners[0],
                )
              }
              className="flex items-center gap-2 px-4 py-2 text-sm bg-gray-900 dark:bg-white text-white dark:text-zinc-900 rounded hover:opacity-90 transition-opacity"
            >
              <Play className="w-4 h-4" />
              Calibrate
            </button>
          </div>
        )}
      </div>

      {tuners.length === 0 ? (
        <div className="text-sm text-gray-500 dark:text-zinc-400 border border-dashed border-gray-200 dark:border-zinc-800 rounded-lg p-8 text-center">
          No tuner drivers registered yet. Register one from the Drivers page
          with kind <code>quantify_tuner</code> or <code>qblox_tuner</code>.
        </div>
      ) : (
        <>
          {inFlight.length > 0 && (
            <div
              data-testid="calibration-in-flight"
              className="flex items-center gap-3 rounded-lg border border-blue-200 dark:border-blue-500/30 bg-blue-50 dark:bg-blue-500/5 px-4 py-3"
            >
              <Loader2 className="w-4 h-4 animate-spin text-blue-600 dark:text-blue-400" />
              <div className="text-sm text-blue-800 dark:text-blue-300">
                {inFlight.map((request) => (
                  <div key={request.id} className="space-y-1">
                    <div>
                      <span className="font-medium">
                        {request.expand?.driver?.name ?? request.driver}
                      </span>{" "}
                      — {request.mode.replace("_", " ")} calibration{" "}
                      {request.status === "running"
                        ? "in progress"
                        : "queued, waiting for the driver"}
                      {/* Who wanted this. A drift-triggered run was nobody's request,
                          so say that rather than leave an operator looking for who
                          started it. `requested_by` is absent on rows that predate
                          the field, and expands to nothing for a reader who may not
                          view users — hence the fallbacks. */}
                      {request.trigger === "drift"
                        ? ", started by the driver's own drift monitoring"
                        : requesterName(request)
                          ? `, requested by ${requesterName(request)}`
                          : ""}
                      . A full run can take hours.
                    </div>
                    {/* From the first routine's start, not its finish — the driver
                        reports the targets it is about to measure (RFC 0009 §7.1). */}
                    {request.progress && (
                      <div data-testid="calibration-progress">
                        <div className="flex items-center justify-between gap-4 text-xs">
                          <span>
                            step {request.progress.step} of{" "}
                            {request.progress.total} —{" "}
                            <span className="font-medium">
                              {request.progress.routine}
                            </span>{" "}
                            on{" "}
                            {request.progress.running?.length
                              ? request.progress.running.join(", ")
                              : request.progress.target}
                          </span>
                          <span className="tabular-nums whitespace-nowrap">
                            {request.progress.succeeded} ok
                            {request.progress.failed > 0 &&
                              `, ${request.progress.failed} failed`}
                            {request.progress.skipped > 0 &&
                              `, ${request.progress.skipped} skipped`}{" "}
                            · {formatDuration(request.progress.elapsed_s)}
                          </span>
                        </div>
                        <div className="mt-1 h-1 w-full rounded bg-blue-200 dark:bg-blue-500/20">
                          <div
                            className="h-1 rounded bg-blue-600 dark:bg-blue-400 transition-all"
                            style={{
                              width: `${Math.min(100, (request.progress.step / request.progress.total) * 100)}%`,
                            }}
                          />
                        </div>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* One per run that published a plan. A drift check sends none — it walks
              four benchmarks with nothing between them, and the bar above says all
              there is to say about it (RFC 0006 D6). */}
          {graphed.map((request) => (
            <GraphSection
              key={`graph-${request.id}`}
              request={request}
              results={results}
            />
          ))}

          <section>
            <h2 className="text-sm uppercase tracking-wider text-gray-500 dark:text-zinc-500 mb-3">
              Measured fidelity
            </h2>
            {latest ? (
              <FidelityGrid benchmarks={latest.benchmarks ?? []} />
            ) : (
              <p className="text-sm text-gray-500 dark:text-zinc-400">
                No calibration has reported yet.
              </p>
            )}
          </section>

          <section>
            <h2 className="text-sm uppercase tracking-wider text-gray-500 dark:text-zinc-500 mb-3">
              Current parameters
              {latestCalibration && (
                <span className="ml-2 normal-case tracking-normal text-gray-400 dark:text-zinc-600">
                  from {new Date(latestCalibration.timestamp).toLocaleString()}
                </span>
              )}
            </h2>
            {latestCalibration ? (
              <ParameterTable
                results={latestCalibration.routine_results ?? []}
              />
            ) : (
              <p className="text-sm text-gray-500 dark:text-zinc-400">
                No successful calibration yet — a drift check measures fidelity
                but writes no parameters.
              </p>
            )}
          </section>

          <section>
            <h2 className="text-sm uppercase tracking-wider text-gray-500 dark:text-zinc-500 mb-3">
              History
            </h2>
            {visible.length === 0 ? (
              <p className="text-sm text-gray-500 dark:text-zinc-400">
                Nothing yet. Runs appear here as the tuner reports them.
              </p>
            ) : (
              <div className="overflow-x-auto">
                <table
                  data-testid="calibration-history"
                  className="w-full text-sm"
                >
                  <thead>
                    <tr className="text-left text-xs uppercase tracking-wider text-gray-500 dark:text-zinc-500 border-b border-gray-200 dark:border-zinc-800">
                      <th className="py-2 pr-4 font-medium">When</th>
                      <th className="py-2 pr-4 font-medium">Driver</th>
                      <th className="py-2 pr-4 font-medium">Mode</th>
                      <th className="py-2 pr-4 font-medium">Status</th>
                      <th className="py-2 pr-4 font-medium">Took</th>
                      <th className="py-2 pr-4 font-medium">Routines</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100 dark:divide-zinc-800/60">
                    {visible.map((result) => (
                      <tr key={result.id}>
                        <td className="py-2 pr-4 text-gray-700 dark:text-zinc-300 whitespace-nowrap">
                          {new Date(result.timestamp).toLocaleString()}
                        </td>
                        <td className="py-2 pr-4 text-gray-500 dark:text-zinc-400">
                          {result.expand?.driver?.name ?? result.driver}
                        </td>
                        <td className="py-2 pr-4 text-gray-500 dark:text-zinc-400">
                          {result.mode.replace("_", " ")}
                        </td>
                        <td
                          className={`py-2 pr-4 ${STATUS_STYLES[result.status] ?? ""}`}
                        >
                          {result.status.replace("_", " ")}
                          {result.errors?.length > 0 && (
                            <span
                              className="ml-1 text-xs text-gray-400 dark:text-zinc-600"
                              title={result.errors.join("\n")}
                            >
                              ({result.errors.length} error
                              {result.errors.length === 1 ? "" : "s"})
                            </span>
                          )}
                        </td>
                        <td className="py-2 pr-4 text-gray-500 dark:text-zinc-400 tabular-nums">
                          {formatDuration(result.duration_s ?? 0)}
                        </td>
                        <td className="py-2 pr-4 text-gray-500 dark:text-zinc-400 tabular-nums">
                          {result.routine_results?.length ?? 0}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <p className="flex items-start gap-2 text-xs text-gray-400 dark:text-zinc-600">
            <Activity className="w-3.5 h-3.5 shrink-0 mt-0.5" />
            <span>
              A tuner can also watch for drift on its own schedule — start it
              with <code>-o drift_check_interval=1800</code> and it will
              benchmark every half hour, recalibrating the qubits that fall
              below their threshold.
            </span>
          </p>
        </>
      )}

      {triggerFor && (
        <TriggerCalibrationModal
          driver={triggerFor}
          onClose={() => setTriggerFor(null)}
          onSubmit={onTrigger}
        />
      )}
    </div>
  );
};

/** One tuner's graph, beside the card for whichever node is selected.
 *
 * A component of its own because the selection is per graph, and a hook cannot live
 * inside the loop that renders them. */
const GraphSection: React.FC<{
  request: CalibrationRequest;
  results: CalibrationResult[];
}> = ({ request, results }) => {
  const [selected, setSelected] = useState<string | null>(null);
  const plan = request.plan!;
  const report = useMemo(() => reportFor(request, results), [request, results]);

  const node = plan.nodes.find((n) => n.name === selected);
  const reported = selected ? request.progress?.nodes?.[selected] : undefined;
  const running = request.status === "running" || request.status === "pending";

  return (
    <section>
      <h2 className="text-sm uppercase tracking-wider text-gray-500 dark:text-zinc-500 mb-3">
        {request.mode.replace("_", " ")} calibration graph
        <span className="ml-2 normal-case tracking-normal text-gray-400 dark:text-zinc-600">
          on {request.expand?.driver?.name ?? request.driver}
          {!running && ` · finished ${request.status}`}
        </span>
      </h2>
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_20rem] items-start">
        <CalibrationGraph
          plan={plan}
          nodes={request.progress?.nodes}
          selected={selected}
          onSelect={setSelected}
        />
        {node ? (
          <NodeCard
            node={node}
            status={statusOf(node, reported)}
            done={reported?.done ?? 0}
            total={reported?.total || node.targets.length}
            running={reported?.running}
            plan={plan}
            report={report}
            onSelect={setSelected}
          />
        ) : (
          <p className="text-xs text-gray-400 dark:text-zinc-600 lg:pt-2">
            Click a routine for what it measures, what it writes, and how it
            went on each target.
          </p>
        )}
      </div>
    </section>
  );
};
