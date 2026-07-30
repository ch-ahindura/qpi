import React, { useState, useEffect, useCallback } from "react";
import { pb } from "./lib/pb";
import { Sidebar } from "./components/Sidebar";
import { Header } from "./components/Header";
import { OverviewTab } from "./components/tabs/OverviewTab";
import { QpuRegistryTab } from "./components/tabs/QpuRegistryTab";
import { DriversTab } from "./components/tabs/DriversTab";
import { MonitoringTab } from "./components/tabs/MonitoringTab";
import { CalibrationTab } from "./components/tabs/CalibrationTab";
import { JobsConsoleTab } from "./components/tabs/JobsConsoleTab";
import { BookingsTab } from "./components/tabs/BookingsTab";
import { AdminPanelTab } from "./components/tabs/AdminPanelTab";
import { SettingsTab } from "./components/tabs/SettingsTab";
import { LoginModal } from "./components/LoginModal";
import { RequestTimeModal } from "./components/RequestTimeModal";
import type {
  QPU,
  QuantumJob,
  Notification,
  TimeSlot,
  User,
  TimeRequest,
  CreateQpuResponse,
  NotificationRequest,
  Driver,
  CreateDriverRequest,
  CreateDriverResponse,
  EventRow,
  CalibrationResult,
  CalibrationRequest,
} from "./types";

export const App: React.FC = () => {
  const [authValid, setAuthValid] = useState(pb.authStore.isValid);
  const [userEmail, setUserEmail] = useState("");
  const [userId, setUserId] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const [qpuSeconds, setQpuSeconds] = useState(0);
  const [version, setVersion] = useState<string>("");

  // Tab routing
  const [activeTab, setActiveTab] = useState("overview");
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);

  // Data collections
  const [qpus, setQpus] = useState<QPU[]>([]);
  const [drivers, setDrivers] = useState<Driver[]>([]);
  const [events, setEvents] = useState<EventRow[]>([]);
  const [calibrations, setCalibrations] = useState<CalibrationResult[]>([]);
  const [calibrationRequests, setCalibrationRequests] = useState<
    CalibrationRequest[]
  >([]);
  const [jobs, setJobs] = useState<QuantumJob[]>([]);
  const [bookings, setBookings] = useState<TimeSlot[]>([]);
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [usersList, setUsersList] = useState<User[]>([]);
  const [timeRequests, setTimeRequests] = useState<TimeRequest[]>([]);

  // Modal overlays
  const [isRequestTimeOpen, setIsRequestTimeOpen] = useState(false);

  // Synced tab hash router
  useEffect(() => {
    const handleHashChange = () => {
      let hash = window.location.hash.replace("#", "");
      if (!hash) hash = "overview";

      if (
        [
          "overview",
          "qpus",
          "drivers",
          "monitoring",
          "calibration",
          "jobs",
          "bookings",
          "settings",
          "admin",
        ].includes(hash)
      ) {
        setActiveTab(hash as string);
      }
    };
    window.addEventListener("hashchange", handleHashChange);
    window.addEventListener("popstate", handleHashChange);
    handleHashChange(); // Run once on mount

    return () => {
      window.removeEventListener("hashchange", handleHashChange);
      window.removeEventListener("popstate", handleHashChange);
    };
  }, []);

  const changeTab = (tab: string) => {
    setActiveTab(tab);
    window.location.hash = tab;
  };

  // Data Loading Helpers
  const loadUserQuota = useCallback(async () => {
    if (!pb.authStore.isValid || !pb.authStore.model) return;
    const isSuper = pb.authStore.model.collectionName === "_superusers";
    if (isSuper) {
      setQpuSeconds(999999);
      return;
    }
    try {
      const user = await pb.collection("users").getOne(pb.authStore.model.id);
      setQpuSeconds((user as unknown as User).qpu_seconds || 0);
    } catch (err) {
      console.error("Failed to load user quota:", err);
    }
  }, []);

  const loadQpus = useCallback(async () => {
    try {
      const records = await pb
        .collection("qpus")
        .getFullList({ sort: "+name" });
      setQpus(records as unknown as QPU[]);
    } catch (err) {
      console.error("Failed to load QPUs:", err);
    }
  }, []);

  const loadDrivers = useCallback(async () => {
    try {
      const records = await pb.collection("drivers").getFullList({
        sort: "+name",
        expand: "qpu",
      });
      setDrivers(records as unknown as Driver[]);
    } catch (err) {
      console.error("Failed to load drivers:", err);
    }
  }, []);

  const loadEvents = useCallback(async () => {
    try {
      const records = await pb.collection("events").getList(1, 200, {
        filter: 'type = "CryostatReading"',
        sort: "-ts",
        expand: "driver",
      });
      setEvents(records.items as unknown as EventRow[]);
    } catch (err) {
      console.error("Failed to load events:", err);
    }
  }, []);

  const loadCalibrations = useCallback(async () => {
    try {
      const records = await pb
        .collection("calibration_results")
        .getList(1, 100, { sort: "-timestamp", expand: "driver" });
      setCalibrations(records.items as unknown as CalibrationResult[]);
    } catch (err) {
      console.error("Failed to load calibration results:", err);
    }
  }, []);

  const loadCalibrationRequests = useCallback(async () => {
    try {
      // Only what is still in flight: a calibration takes hours, and this is
      // what lets the panel say "in progress" during the gap before a report
      // exists. Finished requests are represented by their report.
      const records = await pb
        .collection("calibration_requests")
        .getList(1, 50, {
          filter: 'status = "pending" || status = "running"',
          sort: "-created",
          expand: "driver",
        });
      setCalibrationRequests(records.items as unknown as CalibrationRequest[]);
    } catch (err) {
      console.error("Failed to load calibration requests:", err);
    }
  }, []);

  const loadJobs = useCallback(async () => {
    try {
      const records = await pb.collection("quantum_jobs").getFullList({
        sort: "-created",
      });
      setJobs(records as unknown as QuantumJob[]);
    } catch (err) {
      console.error("Failed to load quantum jobs:", err);
    }
  }, []);

  const loadBookings = useCallback(async () => {
    try {
      const records = await pb.collection("time_slots").getFullList({
        sort: "start_time",
        expand: "booked_by",
      });
      setBookings(records as unknown as TimeSlot[]);
    } catch (err) {
      console.error("Failed to load bookings:", err);
    }
  }, []);

  const loadNotifications = useCallback(async () => {
    try {
      const records = await pb.collection("notifications").getFullList({
        sort: "-created",
      });

      let filterId = pb.authStore.record?.id;
      const isSuper = pb.authStore.record?.collectionName === "_superusers";
      if (isSuper) {
        try {
          const proxyUser = await pb
            .collection("users")
            .getFirstListItem(`email="${pb.authStore.record?.email}"`);
          if (proxyUser) {
            filterId = proxyUser.id;
          }
        } catch (e) {
          // Proxy user might not exist yet, which is fine
          console.warn(e);
        }
      }

      const filtered = (records as unknown as Notification[]).filter((n) => {
        if (!filterId) return true;
        return !n.dismissed_by?.includes(filterId);
      });

      setNotifications(filtered);
    } catch (err) {
      console.error("Failed to load notifications:", err);
    }
  }, []);

  const loadAdminUsers = useCallback(async () => {
    try {
      const records = await pb
        .collection("users")
        .getFullList({ sort: "+email" });
      setUsersList(records as unknown as User[]);
    } catch (err) {
      console.error("Failed to load users for admin:", err);
    }
  }, []);

  const loadTimeRequests = useCallback(async () => {
    try {
      const records = await pb.collection("qpu_time_requests").getFullList({
        expand: "user",
      });
      console.log("Fetched time requests:", records);
      setTimeRequests(records as unknown as TimeRequest[]);
    } catch (err) {
      console.error("Failed to load time requests:", err);
    }
  }, []);

  const loadVersion = useCallback(async () => {
    try {
      const res = await pb.send<{
        version: string;
      }>("/api/op/version", {
        method: "GET",
      });
      setVersion(res.version);
    } catch (err) {
      console.error("Failed to load version:", err);
    }
  }, []);

  // Main data loader
  const loadAllData = useCallback(async () => {
    if (!pb.authStore.isValid || !pb.authStore.model) return;
    const isSuper = pb.authStore.model.collectionName === "_superusers";

    await Promise.all([
      loadUserQuota(),
      loadQpus(),
      loadJobs(),
      loadBookings(),
      loadNotifications(),
    ]);

    if (isSuper) {
      await Promise.all([
        loadAdminUsers(),
        loadTimeRequests(),
        loadVersion(),
        loadDrivers(),
        loadEvents(),
        loadCalibrations(),
        loadCalibrationRequests(),
      ]);
    }
  }, [
    loadUserQuota,
    loadQpus,
    loadJobs,
    loadBookings,
    loadNotifications,
    loadAdminUsers,
    loadTimeRequests,
    loadVersion,
    loadDrivers,
    loadEvents,
    loadCalibrations,
    loadCalibrationRequests,
  ]);

  // Real-time Subscriptions setup
  useEffect(() => {
    if (!authValid) return;

    // eslint-disable-next-line react-hooks/set-state-in-effect
    loadAllData();

    // Subscribe to database changes
    pb.collection("notifications").subscribe("*", () => {
      loadNotifications();
    });
    pb.collection("quantum_jobs").subscribe("*", () => {
      loadJobs();
      loadUserQuota();
    });
    pb.collection("time_slots").subscribe("*", () => {
      loadBookings();
    });
    pb.collection("qpus").subscribe("*", () => {
      loadQpus();
    });

    const isSuper = pb.authStore.model?.collectionName === "_superusers";
    if (isSuper) {
      pb.collection("users").subscribe("*", () => {
        loadAdminUsers();
      });
      pb.collection("qpu_time_requests").subscribe("*", () => {
        loadTimeRequests();
      });
      pb.collection("drivers").subscribe("*", () => {
        loadDrivers();
      });
      pb.collection("events").subscribe("*", () => {
        loadEvents();
      });
      pb.collection("calibration_results").subscribe("*", () => {
        loadCalibrations();
        // A report closes out the request it answers, so the in-flight banner
        // has to be reread here too.
        loadCalibrationRequests();
      });
      pb.collection("calibration_requests").subscribe("*", () => {
        loadCalibrationRequests();
      });
    }

    return () => {
      pb.collection("notifications").unsubscribe();
      pb.collection("quantum_jobs").unsubscribe();
      pb.collection("time_slots").unsubscribe();
      pb.collection("qpus").unsubscribe();
      if (isSuper) {
        pb.collection("users").unsubscribe();
        pb.collection("qpu_time_requests").unsubscribe();
        pb.collection("drivers").unsubscribe();
        pb.collection("events").unsubscribe();
        pb.collection("calibration_results").unsubscribe();
        pb.collection("calibration_requests").unsubscribe();
      }
    };
  }, [
    authValid,
    loadAllData,
    loadNotifications,
    loadJobs,
    loadUserQuota,
    loadBookings,
    loadQpus,
    loadAdminUsers,
    loadTimeRequests,
    loadDrivers,
    loadEvents,
    loadCalibrations,
    loadCalibrationRequests,
  ]);

  // Synchronize auth sessions across tabs (e.g. from /_/)
  useEffect(() => {
    return pb.authStore.onChange((_token, model) => {
      const isValid = pb.authStore.isValid;
      if (isValid && model) {
        setUserEmail(model.email || "admin@qpi.org");
        setUserId(model.id);
        setIsAdmin(model.collectionName === "_superusers");
        setAuthValid(true);
      } else {
        setUserEmail("");
        setUserId("");
        setIsAdmin(false);
        setAuthValid(false);
        setSelectedJobId(null);
        setQpus([]);
        setDrivers([]);
        setEvents([]);
        setJobs([]);
        setBookings([]);
        setNotifications([]);
        setUsersList([]);
        setTimeRequests([]);
      }
    }, true);
  }, []);

  const handleLoginSuccess = () => {
    if (pb.authStore.isValid && pb.authStore.model) {
      setUserEmail(pb.authStore.model.email || "admin@qpi.org");
      setUserId(pb.authStore.model.id);
      setIsAdmin(pb.authStore.model.collectionName === "_superusers");
      setAuthValid(true);
    }
  };

  const handleLogout = () => {
    pb.authStore.clear(); // This will trigger the onChange listener to clean up the state
  };

  // Actions & Custom Endpoints handlers
  const handleDismissNotification = async (id: string) => {
    if (!userId) return;
    try {
      await pb.send(`/api/notifications/${encodeURIComponent(id)}/dismiss`, {
        method: "POST",
      });
      loadNotifications();
    } catch (err: unknown) {
      alert(`Dismiss failed: ${(err as Error).message}`);
    }
  };

  const handleDismissAllNotifications = async () => {
    if (!userId) return;
    try {
      await Promise.all(
        notifications.map(async (ann) => {
          return pb.send(
            `/api/notifications/${encodeURIComponent(ann.id)}/dismiss`,
            {
              method: "POST",
            },
          );
        }),
      );
      loadNotifications();
    } catch (err: unknown) {
      alert(`Clear failed: ${(err as Error).message}`);
    }
  };

  /** Queues a calibration (RFC 0004 §6.8). Admin-only server side: it takes a
   * QPU out of service for hours and rewrites what every later job runs
   * against. Optimistically reloads the queue so the in-flight banner appears
   * without waiting for the realtime round trip. */
  const handleTriggerCalibration = async (payload: {
    driver_id: string;
    mode: string;
    target_qubits?: string[];
    target_edges?: string[];
  }) => {
    await pb.send("/api/op/calibrate/dispatch", {
      method: "POST",
      body: JSON.stringify(payload),
      headers: { "Content-Type": "application/json" },
    });
    await loadCalibrationRequests();
  };

  const handleToggleQpu = async (id: string, enabled: boolean) => {
    await pb.send("/api/op/qpu/toggle", {
      method: "POST",
      body: JSON.stringify({ id: id, enabled: enabled }),
      headers: { "Content-Type": "application/json" },
    });
    loadQpus();
  };

  const handleDeleteQpu = async (id: string) => {
    try {
      await pb.collection("qpus").delete(id);
      loadQpus();
    } catch (err: unknown) {
      alert(`Failed to delete QPU: ${(err as Error).message}`);
    }
  };

  const handleCreateQpu = async (name: string): Promise<CreateQpuResponse> => {
    const res = await pb.send<CreateQpuResponse>("/api/op/qpus/create", {
      method: "POST",
      body: JSON.stringify({
        name: name,
      }),
      headers: { "Content-Type": "application/json" },
    });
    loadQpus();
    return res;
  };

  const handleToggleDriver = async (id: string, enabled: boolean) => {
    await pb.send("/api/op/drivers/toggle", {
      method: "POST",
      body: JSON.stringify({ id: id, enabled: enabled }),
      headers: { "Content-Type": "application/json" },
    });
    loadDrivers();
  };

  const handleDeleteDriver = async (id: string) => {
    try {
      await pb.collection("drivers").delete(id);
      loadDrivers();
    } catch (err: unknown) {
      alert(`Failed to delete driver: ${(err as Error).message}`);
    }
  };

  const handleCreateDriver = async (
    req: CreateDriverRequest,
  ): Promise<CreateDriverResponse> => {
    const res = await pb.send<CreateDriverResponse>("/api/op/drivers/create", {
      method: "POST",
      body: JSON.stringify(req),
      headers: { "Content-Type": "application/json" },
    });
    loadDrivers();
    return res;
  };

  const handleSubmitQuantumJob = async (
    qpuId: string,
    qasm: string,
    shots: number,
    measLevel: number,
  ) => {
    const res = await pb.send("/api/jobs", {
      method: "POST",
      body: JSON.stringify({
        circuits: [{ circuit: qasm }],
        shots: shots,
        meas_level: measLevel,
        meas_return: "single",
        qpu_target: qpuId,
      }),
      headers: { "Content-Type": "application/json" },
    });
    loadJobs();
    loadUserQuota();
    return res.id;
  };

  const handleBookSlot = async (startTime: string, endTime: string) => {
    await pb.collection("time_slots").create({
      start_time: startTime,
      end_time: endTime,
      booked_by: userId,
    });
    loadBookings();
  };

  const handleCancelSlot = async (id: string) => {
    await pb.collection("time_slots").delete(id);
    loadBookings();
  };

  const handleRequestTime = async (seconds: number, reason: string) => {
    await pb.collection("qpu_time_requests").create({
      user: userId,
      seconds: seconds,
      requested_reason: reason,
      status: "pending",
    } as TimeRequest);
    if (isAdmin) loadTimeRequests();
  };

  const handleAllocateTime = async (targetUserId: string, seconds: number) => {
    await pb.collection("users").update(targetUserId, {
      qpu_seconds: seconds,
    });
    loadAdminUsers();
  };

  const handleBroadcastAnnouncement = async (
    title: string,
    desc: string,
    start: string,
    end: string,
  ) => {
    const payload: NotificationRequest = {
      title: title,
      description: desc,
    };
    if (start) payload.start_time = start;
    if (end) payload.end_time = end;

    await pb.collection("notifications").create(payload);
    loadNotifications();
  };

  const handleApproveRequest = async (id: string) => {
    await pb.collection("qpu_time_requests").update(id, {
      status: "approved",
    });
    loadTimeRequests();
    loadAdminUsers();
  };

  const handleRejectRequest = async (id: string, reason: string) => {
    await pb.collection("qpu_time_requests").update(id, {
      status: "rejected",
      rejection_reason: reason,
    });
    loadTimeRequests();
  };

  const renderActiveTab = () => {
    switch (activeTab) {
      case "qpus":
        return (
          <QpuRegistryTab
            qpus={qpus}
            isAdmin={isAdmin}
            onToggleQpu={handleToggleQpu}
            onRegisterQpu={handleCreateQpu}
            onDeleteQpu={handleDeleteQpu}
          />
        );
      case "drivers":
        return isAdmin ? (
          <DriversTab
            drivers={drivers}
            qpus={qpus}
            isAdmin={isAdmin}
            onToggleDriver={handleToggleDriver}
            onRegisterDriver={handleCreateDriver}
            onDeleteDriver={handleDeleteDriver}
          />
        ) : (
          <div className="text-gray-400 dark:text-zinc-500">Access Denied.</div>
        );
      case "monitoring":
        return isAdmin ? (
          <MonitoringTab events={events} drivers={drivers} />
        ) : (
          <div className="text-gray-400 dark:text-zinc-500">Access Denied.</div>
        );
      case "calibration":
        return isAdmin ? (
          <CalibrationTab
            drivers={drivers}
            results={calibrations}
            requests={calibrationRequests}
            onTrigger={handleTriggerCalibration}
          />
        ) : (
          <div className="text-gray-400 dark:text-zinc-500">Access Denied.</div>
        );
      case "jobs":
        return (
          <JobsConsoleTab
            qpus={qpus}
            selectedJobId={selectedJobId}
            setSelectedJobId={setSelectedJobId}
            onSubmitJob={handleSubmitQuantumJob}
          />
        );
      case "bookings":
        return (
          <BookingsTab
            bookings={bookings}
            currentUserId={userId}
            isAdmin={isAdmin}
            onBookSlot={handleBookSlot}
            onCancelSlot={handleCancelSlot}
          />
        );
      case "admin":
        return isAdmin ? (
          <AdminPanelTab
            users={usersList}
            timeRequests={timeRequests}
            onAllocateTime={handleAllocateTime}
            onBroadcastAnnouncement={handleBroadcastAnnouncement}
            onApproveRequest={handleApproveRequest}
            onRejectRequest={handleRejectRequest}
          />
        ) : (
          <div className="text-gray-400 dark:text-zinc-500">Access Denied.</div>
        );
      case "settings":
        return (
          <SettingsTab
            userId={userId}
            email={userEmail}
            qpuSeconds={qpuSeconds}
            isAdmin={isAdmin}
            onLogout={handleLogout}
          />
        );
      case "overview":
      default:
        return (
          <OverviewTab
            qpus={qpus}
            jobs={jobs}
            qpuSeconds={qpuSeconds}
            bookings={bookings}
            notifications={notifications}
            onDismissNotification={handleDismissNotification}
            switchTab={(tab) => {
              changeTab(tab);
              if (tab !== "jobs") setSelectedJobId(null);
            }}
          />
        );
    }
  };

  return (
    <>
      <LoginModal isOpen={!authValid} onLoginSuccess={handleLoginSuccess} />

      {authValid && (
        <div className="flex w-full h-full">
          <Sidebar
            activeTab={activeTab}
            setActiveTab={changeTab}
            isAdmin={isAdmin}
            qpuSeconds={qpuSeconds}
            onRequestTimeClick={() => setIsRequestTimeOpen(true)}
            version={version}
          />

          <div className="flex-1 flex flex-col min-w-0 h-full">
            <Header
              pageTitle={activeTab}
              userEmail={userEmail}
              notifications={notifications}
              onDismissNotification={handleDismissNotification}
              onDismissAllNotifications={handleDismissAllNotifications}
              onLogout={handleLogout}
              onGoToSettings={() => changeTab("settings")}
            />

            <main className="flex-1 overflow-y-auto p-8 bg-background">
              <div className="max-w-7xl mx-auto">{renderActiveTab()}</div>
            </main>
          </div>
        </div>
      )}

      <RequestTimeModal
        isOpen={isRequestTimeOpen}
        onClose={() => setIsRequestTimeOpen(false)}
        onSubmit={handleRequestTime}
      />
    </>
  );
};
