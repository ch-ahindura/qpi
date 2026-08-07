import { useState } from "react";
import { Cpu, Power, Trash2, AlertTriangle, Wrench } from "lucide-react";
import type { QPU } from "@/types";

interface Props {
  qpu: QPU;
  isAdmin: boolean;
  /** Why this QPU is not taking jobs, as the server phrased it. */
  unavailableReason?: string;
  onToggle: (id: string, enabled: boolean) => Promise<void>;
  onMaintenance: (id: string, underMaintenance: boolean) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
}

// A QPU under maintenance is not offline: it is being worked on, and a
// calibration can still be dispatched to it. Drawing both red hid the difference.
const STATUS_PILL: Record<QPU["status"], string> = {
  online: "bg-green-500/10 border-green-500/20 text-green-400",
  maintenance: "bg-amber-500/10 border-amber-500/20 text-amber-400",
  offline: "bg-red-500/10 border-red-500/20 text-red-400",
};

const STATUS_DOT: Record<QPU["status"], string> = {
  online: "bg-green-500",
  maintenance: "bg-amber-500",
  offline: "bg-red-500",
};

export function QpuCard({
  qpu,
  isAdmin,
  unavailableReason,
  onToggle,
  onMaintenance,
  onDelete,
}: Props) {
  const [deleteModalOpen, setDeleteModalOpen] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);

  const handleMaintenance = async () => {
    try {
      await onMaintenance(qpu.id, qpu.status !== "maintenance");
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "Failed";
      alert(`Could not change maintenance: ${message}`);
    }
  };

  const handleToggle = async () => {
    try {
      await onToggle(qpu.id, !qpu.enabled);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "Toggle failed";
      alert(`Toggle failed: ${message}`);
    }
  };

  const handleDelete = async () => {
    setIsDeleting(true);
    try {
      await onDelete(qpu.id);
      setDeleteModalOpen(false);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "Delete failed";
      alert(`Delete failed: ${message}`);
    } finally {
      setIsDeleting(false);
    }
  };

  return (
    <>
      <div className="bg-white dark:bg-zinc-900 border border-gray-200 dark:border-zinc-800 rounded-lg p-6 flex flex-col justify-between hover:border-gray-300 dark:border-zinc-700 transition-colors">
        <div>
          <div className="flex justify-between items-start mb-4">
            <div className="flex items-center gap-3">
              <div className="p-2 rounded bg-gray-50 dark:bg-zinc-950 border border-gray-200 dark:border-zinc-800">
                <Cpu className="w-5 h-5 text-gray-900 dark:text-white" />
              </div>
              <div>
                <h3 className="font-geist font-bold text-gray-900 dark:text-white text-lg leading-tight">
                  {qpu.name}
                </h3>
                <p className="text-xs font-mono text-gray-400 dark:text-zinc-500 mt-0.5">
                  ID: {qpu.id}
                </p>
              </div>
            </div>
            <span
              className={`px-2 py-0.5 border rounded-full text-[10px] uppercase font-semibold flex items-center gap-1 ${
                STATUS_PILL[qpu.status] ?? STATUS_PILL.offline
              }`}
            >
              <span
                className={`w-1.5 h-1.5 rounded-full ${STATUS_DOT[qpu.status] ?? STATUS_DOT.offline}`}
              />
              {qpu.status}
            </span>
          </div>

          {unavailableReason && (
            <p
              data-testid="qpu-unavailable-reason"
              className="text-xs text-amber-600 dark:text-amber-400 mt-2"
            >
              Not taking jobs — {unavailableReason}.
            </p>
          )}

          <div className="grid grid-cols-1 gap-4 py-4 my-2 border-t border-b border-gray-200 dark:border-zinc-800/50 text-xs">
            <div>
              <span className="text-gray-400 dark:text-zinc-500 block uppercase tracking-wider text-[10px] mb-1">
                NNG Ports (Cmd/Res)
              </span>
              <span className="font-mono text-gray-600 dark:text-zinc-300">
                {qpu.nng_command_port > 0
                  ? `${qpu.nng_command_port} / ${qpu.nng_result_port}`
                  : "offline"}
              </span>
            </div>
          </div>
        </div>

        {isAdmin && (
          <div className="flex justify-between items-center mt-4">
            <button
              onClick={() => setDeleteModalOpen(true)}
              className="px-3 py-1.5 rounded text-xs font-semibold flex items-center gap-1.5 border border-red-500/20 text-red-400 bg-red-500/10 hover:bg-red-500/20 transition-all focus:outline-none"
              title="Delete QPU"
            >
              <Trash2 className="w-3.5 h-3.5" />
              Delete
            </button>

            <div className="flex items-center gap-3">
              <span className="text-xs text-gray-500 dark:text-zinc-400">
                Service
              </span>
              <button
                onClick={handleToggle}
                className={`px-4 py-1.5 rounded text-xs font-semibold flex items-center gap-2 border transition-all focus:outline-none ${
                  qpu.enabled
                    ? "bg-green-500/10 border-green-500/20 text-green-400 hover:bg-green-500/20"
                    : "bg-red-500/10 border-red-500/20 text-red-400 hover:bg-red-500/20"
                }`}
              >
                <Power className="w-3.5 h-3.5" />
                {qpu.enabled ? "In service" : "Switched off"}
              </button>
              <button
                onClick={handleMaintenance}
                data-testid="qpu-maintenance-toggle"
                className={`px-4 py-1.5 rounded text-xs font-semibold flex items-center gap-2 border transition-all focus:outline-none ${
                  qpu.status === "maintenance"
                    ? "bg-amber-500/10 border-amber-500/20 text-amber-400 hover:bg-amber-500/20"
                    : "bg-gray-500/10 border-gray-500/20 text-gray-500 dark:text-zinc-400 hover:bg-gray-500/20"
                }`}
              >
                <Wrench className="w-3.5 h-3.5" />
                {qpu.status === "maintenance"
                  ? "Under maintenance"
                  : "Maintenance"}
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Delete Confirmation Modal */}
      {deleteModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-gray-50 dark:bg-zinc-950/80 backdrop-blur-sm animate-fade-in">
          <div className="w-full max-w-sm bg-white dark:bg-zinc-900 border border-gray-200 dark:border-zinc-800 rounded-lg shadow-2xl p-6 space-y-5">
            <div className="flex items-start gap-4">
              <div className="p-3 bg-red-500/10 rounded-full">
                <AlertTriangle className="w-6 h-6 text-red-400" />
              </div>
              <div>
                <h3 className="text-lg font-semibold font-geist text-gray-900 dark:text-white">
                  Delete QPU
                </h3>
                <p className="text-sm text-gray-500 dark:text-zinc-400 mt-1">
                  Are you sure you want to delete{" "}
                  <span className="font-bold text-gray-900 dark:text-white">
                    {qpu.name}
                  </span>
                  ? This action cannot be undone and will terminate any active
                  driver connection.
                </p>
              </div>
            </div>
            <div className="flex justify-end gap-3 pt-2">
              <button
                onClick={() => setDeleteModalOpen(false)}
                disabled={isDeleting}
                className="px-4 py-2 text-sm font-semibold text-gray-600 dark:text-zinc-300 hover:text-gray-900 dark:text-white transition-colors disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                onClick={handleDelete}
                disabled={isDeleting}
                className="px-4 py-2 bg-red-500 text-zinc-950 text-sm font-semibold rounded hover:bg-red-400 transition-colors disabled:opacity-50 flex items-center gap-2"
              >
                {isDeleting ? "Deleting..." : "Delete QPU"}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
