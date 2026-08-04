import React, { useState } from "react";
import { Plus } from "lucide-react";
import type { QPU, CreateQpuResponse } from "@/types";
import { QpuCard } from "./elements/QpuCard";
import { RegisterQpuModal } from "./elements/RegisterQpuModal";

interface QpuRegistryTabProps {
  qpus: QPU[];
  isAdmin: boolean;
  onToggleQpu: (id: string, enabled: boolean) => Promise<void>;
  onMaintenanceQpu: (id: string, underMaintenance: boolean) => Promise<void>;
  /** QPU id to the server's reason it is not taking jobs. */
  unavailable: Record<string, string>;
  onRegisterQpu: (name: string) => Promise<CreateQpuResponse>;
  onDeleteQpu: (id: string) => Promise<void>;
}

export const QpuRegistryTab: React.FC<QpuRegistryTabProps> = ({
  qpus,
  isAdmin,
  onToggleQpu,
  onMaintenanceQpu,
  unavailable,
  onRegisterQpu,
  onDeleteQpu,
}) => {
  const [modalOpen, setModalOpen] = useState(false);

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <h1 className="text-3xl font-geist text-gray-900 dark:text-white">
            QPU Registry
          </h1>
          <p className="text-sm text-gray-500 dark:text-zinc-400 mt-1">
            Manage and monitor physical/simulator processing units.
          </p>
        </div>
        {isAdmin && (
          <button
            onClick={() => setModalOpen(true)}
            className="w-full md:w-auto bg-white text-zinc-950 font-semibold py-2 px-6 rounded flex items-center justify-center gap-2 hover:opacity-90 transition-opacity focus:outline-none"
          >
            <Plus className="w-4.5 h-4.5" />
            Register QPU
          </button>
        )}
      </div>

      {/* QPU Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {qpus.map((qpu) => (
          <QpuCard
            key={qpu.id}
            qpu={qpu}
            isAdmin={isAdmin}
            unavailableReason={unavailable[qpu.id]}
            onToggle={onToggleQpu}
            onMaintenance={onMaintenanceQpu}
            onDelete={onDeleteQpu}
          />
        ))}
      </div>

      {/* Register Modal overlay */}
      {modalOpen && (
        <RegisterQpuModal
          onClose={() => setModalOpen(false)}
          onRegister={onRegisterQpu}
        />
      )}
    </div>
  );
};
