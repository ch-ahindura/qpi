import React from "react";
import { X } from "lucide-react";
import type {
  CalibrationNodeStatus,
  CalibrationPlan,
  CalibrationPlanNode,
  CalibrationResult,
} from "@/types";
import { FitPlot } from "./FitPlot";
import { formatParameter, formatSeconds } from "./format";
import { dependentsOf, outcomesFor } from "./nodeDetail";

interface NodeCardProps {
  node: CalibrationPlanNode;
  status: CalibrationNodeStatus;
  done: number;
  total: number;
  plan: CalibrationPlan;
  /** The report this run produced, once it has landed. */
  report?: CalibrationResult;
  onSelect: (routine: string | null) => void;
}

const STATUS_LABELS: Record<CalibrationNodeStatus, string> = {
  running: "running",
  done: "done",
  partial: "some targets failed",
  failed: "failed",
  pending: "not yet run",
  skipped: "applies to nothing configured here",
  not_planned: "not in this run",
};

/** What is known about one routine of a calibration (RFC 0006 §6).
 *
 * Nothing here is fetched: the plan carries what the routine is, what it writes and
 * where it sits, and the report carries how the run went. Before the report lands,
 * the per-target rows show the tally and nothing else — which is the honest state. */
export const NodeCard: React.FC<NodeCardProps> = ({
  node,
  status,
  done,
  total,
  plan,
  report,
  onSelect,
}) => {
  const dependents = dependentsOf(node.name, plan);
  const outcomes = outcomesFor(node.name, node.targets, report);

  return (
    <aside
      data-testid="calibration-node-card"
      data-routine={node.name}
      className="border border-gray-200 dark:border-zinc-800 rounded-lg bg-white dark:bg-zinc-900 p-4 space-y-4 text-sm"
    >
      <header className="flex items-start justify-between gap-2">
        <div>
          <h3 className="font-mono text-gray-900 dark:text-white break-all">
            {node.name}
          </h3>
          <p className="text-xs text-gray-500 dark:text-zinc-400 mt-0.5">
            {STATUS_LABELS[status]}
            {total > 1 && ` · ${done}/${total} ${node.kind}`}
          </p>
        </div>
        <button
          data-testid="calibration-node-card-close"
          onClick={() => onSelect(null)}
          aria-label="Close"
          className="text-gray-400 hover:text-gray-700 dark:text-zinc-500 dark:hover:text-zinc-200"
        >
          <X className="w-4 h-4" />
        </button>
      </header>

      <p className="flex flex-wrap gap-1.5 text-xs">
        <Tag>per {node.kind === "edges" ? "edge" : "qubit"}</Tag>
        {node.is_benchmark && <Tag>benchmark</Tag>}
        {node.has_check && <Tag>has a drift check</Tag>}
      </p>

      <Section title="Writes">
        {node.updates.length === 0 ? (
          <p className="text-gray-500 dark:text-zinc-400 text-xs">
            Measures only — writes nothing to the device.
          </p>
        ) : (
          <ul className="font-mono text-xs text-gray-700 dark:text-zinc-300 space-y-0.5">
            {node.updates.map((path) => (
              <li key={path}>{path}</li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="Depends on">
        <Neighbours names={node.depends_on} onSelect={onSelect} />
      </Section>

      <Section title="Feeds">
        <Neighbours names={dependents} onSelect={onSelect} />
      </Section>

      <Section title="This run">
        {node.targets.length === 0 ? (
          <p className="text-gray-500 dark:text-zinc-400 text-xs">
            No {node.kind} it applies to are configured.
          </p>
        ) : (
          <ul className="space-y-2">
            {outcomes.map((outcome) => (
              <li
                key={outcome.target}
                data-testid={`calibration-node-target-${outcome.target}`}
                className="text-xs"
              >
                <div className="flex items-baseline justify-between gap-2">
                  <span className="font-mono text-gray-700 dark:text-zinc-300">
                    {outcome.target}
                  </span>
                  {outcome.durationS !== undefined && (
                    <span className="tabular-nums text-gray-400 dark:text-zinc-500">
                      {formatSeconds(outcome.durationS)}
                    </span>
                  )}
                </div>
                {outcome.error && (
                  <p className="text-red-600 dark:text-red-400 break-words">
                    {outcome.error}
                  </p>
                )}
                {outcome.fidelity != null && (
                  <p className="font-mono text-gray-900 dark:text-white">
                    fidelity {outcome.fidelity.toFixed(5)}
                    {outcome.errorPerGate != null &&
                      ` · ${outcome.errorPerGate.toExponential(2)} per gate`}
                  </p>
                )}
                {outcome.parameters &&
                  Object.entries(outcome.parameters)
                    // The sweep axes are echoed back into the report; they are the
                    // fit's input, not its result.
                    .filter(([, value]) => !Array.isArray(value))
                    .map(([key, value]) => (
                      <p
                        key={key}
                        className="font-mono text-gray-900 dark:text-white"
                      >
                        <span className="text-gray-500 dark:text-zinc-400">
                          {key}
                        </span>{" "}
                        {formatParameter(key, value)}
                      </p>
                    ))}
                {!outcome.parameters && !outcome.error && (
                  <p className="text-gray-400 dark:text-zinc-500">
                    {report
                      ? "nothing reported"
                      : "parameters appear when the report lands"}
                  </p>
                )}
                {/* A routine whose `analyse` does not produce a summary yet simply
                    has no chart — one is converted at a time (RFC 0006 §7). */}
                {outcome.fit && <FitPlot fit={outcome.fit} />}
              </li>
            ))}
          </ul>
        )}
      </Section>
    </aside>
  );
};

const Tag: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <span className="px-1.5 py-0.5 rounded bg-gray-100 dark:bg-zinc-800 text-gray-600 dark:text-zinc-300">
    {children}
  </span>
);

const Section: React.FC<{ title: string; children: React.ReactNode }> = ({
  title,
  children,
}) => (
  <div>
    <h4 className="text-xs uppercase tracking-wider text-gray-500 dark:text-zinc-500 mb-1">
      {title}
    </h4>
    {children}
  </div>
);

/** Neighbouring routines, each a way into its own card. */
const Neighbours: React.FC<{
  names: string[];
  onSelect: (routine: string) => void;
}> = ({ names, onSelect }) =>
  names.length === 0 ? (
    <p className="text-gray-500 dark:text-zinc-400 text-xs">Nothing.</p>
  ) : (
    <ul className="flex flex-wrap gap-1.5">
      {names.map((name) => (
        <li key={name}>
          <button
            data-testid={`calibration-node-link-${name}`}
            onClick={() => onSelect(name)}
            className="font-mono text-xs text-blue-700 dark:text-blue-400 hover:underline break-all"
          >
            {name}
          </button>
        </li>
      ))}
    </ul>
  );
