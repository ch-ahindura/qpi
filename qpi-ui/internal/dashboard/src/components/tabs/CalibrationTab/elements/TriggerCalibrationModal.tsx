import React, { useState } from "react";
import { X, AlertTriangle } from "lucide-react";
import type { Driver } from "@/types";

export type CalibrationMode = "full" | "partial" | "fidelity_check";

interface TriggerCalibrationModalProps {
  driver: Driver;
  onClose: () => void;
  onSubmit: (payload: {
    driver_id: string;
    mode: CalibrationMode;
    target_qubits?: string[];
    target_edges?: string[];
  }) => Promise<void>;
}

const MODES: {
  id: CalibrationMode;
  label: string;
  description: string;
}[] = [
  {
    id: "fidelity_check",
    label: "Check drift",
    description:
      "Runs the benchmarks only. Calibrates nothing and changes no parameters — minutes, not hours.",
  },
  {
    id: "partial",
    label: "Recalibrate qubits",
    description:
      "Re-runs the routines downstream of the qubits you name, and the edges touching them.",
  },
  {
    id: "full",
    label: "Full calibration",
    description:
      "Walks the whole graph from resonator spectroscopy up. Takes the QPU out of service for hours.",
  },
];

/** Queues a calibration on a tuner driver (RFC 0004 §6.8).
 *
 * Defaults to the drift check rather than the full run: it is the one an
 * operator wants most often, it is cheap, and it changes nothing — whereas a
 * mis-clicked full calibration costs hours of QPU time. */
export const TriggerCalibrationModal: React.FC<TriggerCalibrationModalProps> = ({
  driver,
  onClose,
  onSubmit,
}) => {
  const [mode, setMode] = useState<CalibrationMode>("fidelity_check");
  const [qubits, setQubits] = useState("");
  const [edges, setEdges] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const parsed = (raw: string) =>
    raw
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);

  // A partial run with no targets would widen to a full one on the driver,
  // taking the QPU out for hours nobody asked for. The server rejects it too;
  // this is so the operator finds out before they submit.
  const missingTargets = mode === "partial" && parsed(qubits).length === 0;

  const handleSubmit = async () => {
    if (missingTargets) return;
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit({
        driver_id: driver.id,
        mode,
        target_qubits: parsed(qubits),
        target_edges: parsed(edges),
      });
      onClose();
    } catch (err: unknown) {
      setError((err as Error).message || "Failed to queue the calibration.");
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div
        data-testid="trigger-calibration-modal"
        className="bg-white dark:bg-zinc-900 border border-gray-200 dark:border-zinc-800 rounded-lg w-full max-w-lg max-h-[90vh] overflow-y-auto"
      >
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-zinc-800">
          <div>
            <h2 className="text-lg font-geist text-gray-900 dark:text-white">
              Calibrate {driver.name}
            </h2>
            <p className="text-xs text-gray-500 dark:text-zinc-400 mt-0.5">
              Queued for the driver to pick up. It reports back when it finishes.
            </p>
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="text-gray-400 hover:text-gray-600 dark:text-zinc-500 dark:hover:text-zinc-300"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="px-6 py-4 space-y-4">
          <div className="space-y-2">
            {MODES.map((option) => (
              <label
                key={option.id}
                className={`flex gap-3 p-3 rounded border cursor-pointer transition-colors ${
                  mode === option.id
                    ? "border-zinc-400 dark:border-zinc-600 bg-gray-50 dark:bg-zinc-800/50"
                    : "border-gray-200 dark:border-zinc-800 hover:border-gray-300 dark:hover:border-zinc-700"
                }`}
              >
                <input
                  type="radio"
                  name="calibration-mode"
                  value={option.id}
                  checked={mode === option.id}
                  onChange={() => setMode(option.id)}
                  className="mt-1"
                />
                <div>
                  <div className="text-sm text-gray-900 dark:text-white">
                    {option.label}
                  </div>
                  <div className="text-xs text-gray-500 dark:text-zinc-400 mt-0.5">
                    {option.description}
                  </div>
                </div>
              </label>
            ))}
          </div>

          {mode === "partial" && (
            <div className="space-y-3">
              <div>
                <label className="block text-xs text-gray-500 dark:text-zinc-400 mb-1">
                  Target qubits (comma separated)
                </label>
                <input
                  data-testid="calibration-qubits"
                  value={qubits}
                  onChange={(e) => setQubits(e.target.value)}
                  placeholder="q0, q1"
                  className="w-full bg-gray-50 dark:bg-zinc-950 border border-gray-200 dark:border-zinc-800 text-gray-900 dark:text-white rounded px-3 py-2 text-sm font-mono focus:outline-none focus:border-zinc-500"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-500 dark:text-zinc-400 mb-1">
                  Target edges (optional)
                </label>
                <input
                  data-testid="calibration-edges"
                  value={edges}
                  onChange={(e) => setEdges(e.target.value)}
                  placeholder="q0_q1"
                  className="w-full bg-gray-50 dark:bg-zinc-950 border border-gray-200 dark:border-zinc-800 text-gray-900 dark:text-white rounded px-3 py-2 text-sm font-mono focus:outline-none focus:border-zinc-500"
                />
              </div>
            </div>
          )}

          {mode === "full" && (
            <div className="flex gap-2 text-xs text-amber-700 dark:text-amber-400 bg-amber-50 dark:bg-amber-500/5 border border-amber-200 dark:border-amber-500/30 rounded p-3">
              <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
              <span>
                A full calibration walks every enabled routine over every target.
                The QPU will not run jobs until it finishes.
              </span>
            </div>
          )}

          {error && (
            <p
              data-testid="calibration-error"
              className="text-xs text-red-600 dark:text-red-400"
            >
              {error}
            </p>
          )}
        </div>

        <div className="flex justify-end gap-2 px-6 py-4 border-t border-gray-200 dark:border-zinc-800">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm text-gray-600 dark:text-zinc-400 hover:text-gray-900 dark:hover:text-white transition-colors"
          >
            Cancel
          </button>
          <button
            data-testid="calibration-submit"
            onClick={handleSubmit}
            disabled={submitting || missingTargets}
            className="px-4 py-2 text-sm bg-gray-900 dark:bg-white text-white dark:text-zinc-900 rounded hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed transition-opacity"
          >
            {submitting ? "Queueing…" : "Queue calibration"}
          </button>
        </div>
      </div>
    </div>
  );
};
