import { useState } from "react";
import type { QPU, QuantumJob } from "@/types";
import { JobError } from "./JobError";
import { ResultsVisualizer } from "./ResultsVisualizer";

interface Props {
  viewedJob: QuantumJob | null;
  qpus: QPU[];
}

export function JobResultsPanel({ viewedJob, qpus }: Props) {
  const [activeTab, setActiveTab] = useState<"counts" | "iq" | "trace">(
    "counts",
  );

  // A failed job has a reason and no results, so the tabs have nothing to show
  // and showing them invites the reader to hunt through three empty ones.
  const failure =
    viewedJob?.results?.error ??
    (viewedJob?.status === "failed"
      ? "The driver reported no reason. Check the driver logs on the node."
      : null);

  return (
    <div className="bg-white dark:bg-zinc-900 border border-gray-200 dark:border-zinc-800 rounded-lg p-6 flex flex-col gap-6 h-[650px] overflow-hidden">
      <div className="flex items-start justify-between border-b border-gray-200 dark:border-zinc-800 pb-4">
        <div>
          <div className="flex items-center gap-3 mb-1">
            <h3 className="text-xl font-bold font-geist text-gray-900 dark:text-white">
              {viewedJob
                ? `#${viewedJob.id.substring(0, 8)}`
                : "Select or run a job"}
            </h3>
            {viewedJob && (
              <div
                className={`flex items-center gap-1.5 px-2 py-0.5 rounded-full border text-[10px] uppercase font-semibold ${
                  viewedJob.status === "completed"
                    ? "border-green-500/30 bg-green-500/10 text-green-400"
                    : viewedJob.status === "pending"
                      ? "border-yellow-500/30 bg-yellow-500/10 text-yellow-400"
                      : viewedJob.status === "running"
                        ? "border-indigo-500/30 bg-indigo-500/10 text-indigo-400"
                        : "border-red-500/30 bg-red-500/10 text-red-400"
                }`}
              >
                {viewedJob.status}
              </div>
            )}
          </div>
          <p className="text-xs text-gray-500 dark:text-zinc-400">
            {viewedJob
              ? `Executed on ${qpus.find((q) => q.id === viewedJob.qpu_target)?.name || viewedJob.qpu_target}`
              : "No active job selected"}
          </p>
        </div>
        {viewedJob && (
          <div className="flex gap-4 text-right">
            <div>
              <span className="block text-[10px] font-semibold text-gray-400 dark:text-zinc-500 uppercase tracking-wider">
                Duration
              </span>
              <span className="font-mono text-sm text-gray-900 dark:text-white font-semibold">
                {viewedJob.duration !== undefined
                  ? `${viewedJob.duration}s`
                  : "--"}
              </span>
            </div>
            <div>
              <span className="block text-[10px] font-semibold text-gray-400 dark:text-zinc-500 uppercase tracking-wider">
                Created
              </span>
              <span className="font-mono text-sm text-gray-900 dark:text-white font-semibold">
                {new Date(viewedJob.created).toLocaleTimeString()}
              </span>
            </div>
          </div>
        )}
      </div>

      {/* Tab view options */}
      <div
        className={`flex border-b border-gray-200 dark:border-zinc-800 ${failure ? "hidden" : ""}`}
      >
        <button
          onClick={() => setActiveTab("counts")}
          className={`px-4 py-2 font-geist text-sm transition-all -mb-[1px] ${
            activeTab === "counts"
              ? "text-gray-900 dark:text-white border-b-2 border-white"
              : "text-gray-400 dark:text-zinc-500 hover:text-gray-600 dark:text-zinc-300"
          }`}
        >
          Counts Histogram
        </button>
        <button
          onClick={() => setActiveTab("iq")}
          className={`px-4 py-2 font-geist text-sm transition-all -mb-[1px] ${
            activeTab === "iq"
              ? "text-gray-900 dark:text-white border-b-2 border-white"
              : "text-gray-400 dark:text-zinc-500 hover:text-gray-600 dark:text-zinc-300"
          }`}
        >
          IQ Clusters
        </button>
        <button
          onClick={() => setActiveTab("trace")}
          className={`px-4 py-2 font-geist text-sm transition-all -mb-[1px] ${
            activeTab === "trace"
              ? "text-gray-900 dark:text-white border-b-2 border-white"
              : "text-gray-400 dark:text-zinc-500 hover:text-gray-600 dark:text-zinc-300"
          }`}
        >
          Raw Trace
        </button>
      </div>

      {/* Chart body */}
      <div className="flex-1 bg-gray-50 dark:bg-zinc-950 border border-gray-200 dark:border-zinc-800 rounded p-6 flex flex-col items-center justify-center relative overflow-hidden">
        {failure ? (
          <JobError message={failure} />
        ) : (
          <ResultsVisualizer viewedJob={viewedJob} activeTab={activeTab} />
        )}
      </div>
    </div>
  );
}
