import React from "react";
import {
  LayoutDashboard,
  Cpu,
  Terminal,
  Calendar,
  ShieldAlert,
  User,
  Workflow,
  Thermometer,
  SlidersHorizontal,
} from "lucide-react";
import { useTheme } from "../lib/ThemeContext";

interface SidebarProps {
  activeTab: string;
  setActiveTab: (tab: string) => void;
  isAdmin: boolean;
  qpuSeconds: number;
  onRequestTimeClick: () => void;
  version?: string;
}

export const Sidebar: React.FC<SidebarProps> = ({
  activeTab,
  setActiveTab,
  isAdmin,
  qpuSeconds,
  onRequestTimeClick,
  version,
}) => {
  const { siteName, tagline, logoUrl } = useTheme();

  const navItems = [
    { id: "overview", label: "Overview", icon: LayoutDashboard },
    { id: "qpus", label: "QPU Registry", icon: Cpu },
    ...(isAdmin
      ? [
          { id: "drivers", label: "Drivers", icon: Workflow },
          { id: "monitoring", label: "Monitoring", icon: Thermometer },
          { id: "calibration", label: "Calibration", icon: SlidersHorizontal },
        ]
      : []),
    { id: "jobs", label: "Jobs Console", icon: Terminal },
    { id: "bookings", label: "Bookings", icon: Calendar },
    ...(isAdmin
      ? [{ id: "admin", label: "Admin Panel", icon: ShieldAlert }]
      : []),
    { id: "settings", label: "Profile Settings", icon: User },
  ];

  return (
    <aside className="w-sidebar-width h-full border-r border-gray-200 dark:border-zinc-800 bg-white dark:bg-zinc-900/40 flex flex-col z-50">
      {/* Brand */}
      <div className="px-6 py-6 border-b border-gray-200 dark:border-zinc-800/50 flex items-center gap-3">
        {logoUrl ? (
          <img
            src={logoUrl}
            alt={siteName}
            className="w-6 h-6 object-contain"
          />
        ) : (
          <Cpu className="w-6 h-6 text-gray-900 dark:text-white" />
        )}
        <div>
          <h1 className="font-geist text-lg font-bold text-gray-900 dark:text-white leading-none tracking-tight">
            {siteName}
          </h1>
          <p className="text-[10px] text-gray-500 dark:text-zinc-400 uppercase tracking-widest mt-1">
            {tagline}
          </p>
        </div>
      </div>

      {/* Navigation Links */}
      <nav className="flex-1 px-3 py-4 space-y-1">
        {navItems.map((item) => {
          const Icon = item.icon;
          const isActive = activeTab === item.id;
          return (
            <button
              key={item.id}
              onClick={() => setActiveTab(item.id)}
              className={`w-full flex items-center gap-3 px-4 py-2.5 rounded text-sm font-medium transition-colors text-left border-l-2 ${
                isActive
                  ? "text-gray-900 dark:text-white bg-gray-100 dark:bg-zinc-800/50 border-white"
                  : "text-gray-500 dark:text-zinc-400 hover:bg-gray-100 dark:bg-zinc-800/30 hover:text-gray-900 dark:text-white border-transparent"
              }`}
            >
              <Icon className="w-5 h-5" />
              {item.label}
            </button>
          );
        })}
      </nav>

      {/* User quota card at bottom */}
      <div className="p-4 border-t border-gray-200 dark:border-zinc-800">
        <div className="bg-gray-50 dark:bg-zinc-950/40 border border-gray-200 dark:border-zinc-800 rounded p-3 space-y-2">
          <div className="flex justify-between items-center text-xs">
            <span className="text-gray-500 dark:text-zinc-400">
              QPU Balance
            </span>
            <span className="font-mono text-gray-900 dark:text-white font-semibold">
              {qpuSeconds}s
            </span>
          </div>
          <div className="h-1 bg-gray-100 dark:bg-zinc-800 rounded-full overflow-hidden">
            <div className="h-full bg-indigo-500 w-full"></div>
          </div>
          <div className="text-[10px] text-gray-400 dark:text-zinc-500 flex justify-between items-center">
            <span>{isAdmin ? "Administrator" : "User Account"}</span>
            {!isAdmin ? (
              <button
                onClick={onRequestTimeClick}
                className="text-indigo-400 hover:underline font-medium focus:outline-none"
              >
                Request Time
              </button>
            ) : (
              version && (
                <span data-testid="admin-footer" className="font-mono">
                  {version}
                </span>
              )
            )}
          </div>
        </div>
      </div>
    </aside>
  );
};
