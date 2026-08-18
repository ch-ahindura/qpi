export interface User {
  id: string;
  email: string;
  username: string;
  qpu_seconds: number;
}

export interface QPU {
  id: string;
  name: string;
  // The same three the server's select allows. `maintenance` was missing here
  // while the column accepted it, so the dashboard could not render a state it
  // was already able to receive.
  status: "online" | "offline" | "maintenance";
  nng_command_port: number;
  nng_result_port: number;
  enabled: boolean;
}

export interface JobResult {
  shots: number;
  backend: string;
  success: boolean;
  counts?: Record<string, number>;
  hex_counts?: Record<string, number>;
  memory?: number[][][];
  circuit_results?: unknown[];
  /** Why the job failed. Present instead of results, never alongside them. */
  error?: string;
}

export interface QuantumJob {
  id: string;
  user_id: string;
  qpu_target: string;
  payload: string; // JSON string
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  results?: JobResult; // Qiskit-compatible result dict
  duration?: number; // execution duration in seconds
  created: string;
  finished_at?: string;
}

export interface TimeSlot {
  id: string;
  start_time: string;
  end_time: string;
  booked_by: string;
  expand?: {
    booked_by?: User;
  };
}

export interface TimeRequest {
  id: string;
  user: string;
  seconds: number;
  requested_reason: string;
  status: "pending" | "approved" | "rejected";
  rejection_reason?: string;
  expand?: {
    user?: User;
  };
}

export interface NotificationRequest {
  title: string;
  description: string;
  start_time?: string;
  end_time?: string;
  target_users?: string[];
}

export interface Notification extends NotificationRequest {
  id: string;
  title: string;
  description: string;
  user_id?: string;
  dismissed_by?: string[];
  created: string;
}

export interface CreateQpuRequest {
  name: string;
  num_qubits?: number;
  enabled?: boolean;
}

export interface CreateQpuResponse {
  id: string;
  name: string;
  status: string;
  enabled: boolean;
}

export type DriverKind =
  | "mock"
  | "qiskit_aer"
  | "quantify"
  | "qblox"
  | "presto"
  | "bluefors_gen1"
  | "quantify_tuner"
  | "qblox_tuner"
  | "custom";

/** The tuner devices, i.e. the drivers that implement the `calibrate`
 * operation (RFC 0004 §6.1). */
export const TUNER_KINDS: DriverKind[] = ["quantify_tuner", "qblox_tuner"];

export type DriverLanguage = "python" | "typescript" | "go";

/**
 * Which SDKs ship each official device, mirroring `drivers.Spec.Languages` on the
 * server (`qpi-ui/internal/drivers/catalog.go`).
 *
 * Only the Python SDK has a `process` device, so a Go or TypeScript driver can only
 * be `bluefors_gen1` — or `custom`, which is code the operator writes against the
 * SDK and so exists in every language. `POST /api/op/drivers/create` rejects any
 * other pairing; this is what keeps the form from offering it in the first place.
 */
export const DEVICE_LANGUAGES: Record<DriverKind, DriverLanguage[]> = {
  mock: ["python"],
  qiskit_aer: ["python"],
  quantify: ["python"],
  qblox: ["python"],
  presto: ["python"],
  bluefors_gen1: ["python", "typescript", "go"],
  // Only the Python SDK ships a tuner, as only it ships a process device.
  quantify_tuner: ["python"],
  qblox_tuner: ["python"],
  custom: ["python", "typescript", "go"],
};

export interface Driver {
  id: string;
  name: string;
  qpu: string;
  kind: DriverKind;
  language: DriverLanguage;
  events: string[];
  status: "offline" | "online" | "maintenance";
  nng_in_port: number;
  nng_out_port: number;
  host?: string;
  version?: string;
  last_seen?: string;
  enabled: boolean;
  created: string;
  expand?: {
    qpu?: QPU;
  };
}

export interface CreateDriverRequest {
  name: string;
  qpu: string;
  kind: DriverKind;
  language: DriverLanguage;
  events?: string[];
}

export interface DriverSnippets {
  systemd?: string;
  manual_cli?: string;
  install?: string;
  stub?: string;
}

export interface ChannelReading {
  value: number | null;
  unit?: string;
  status?: string;
}

/** A row from the `events` trace log (RFC 0001 §7). Phase 3's only writer is
 * the CryostatReading handler, so `payload.readings` is what a monitoring
 * driver read on that tick, keyed by channel path. */
export interface EventRow {
  id: string;
  source: string;
  driver: string;
  qpu: string;
  type: string;
  payload: { readings?: Record<string, ChannelReading> };
  ts: string;
  created: string;
  expand?: {
    driver?: Driver;
  };
}

export interface CreateDriverResponse {
  id: string;
  name: string;
  qpu: string;
  kind: DriverKind;
  language: DriverLanguage;
  events: string[];
  status: string;
  enabled: boolean;
  token: string;
  ca_fingerprint: string;
  qpi_addr: string;
  driver_version: string;
  snippets: DriverSnippets;
}

export interface ThemeRecord {
  id: string;
  name: string;
  is_active: boolean;
  site_name: string;
  tagline: string;
  logo: string;
  favicon: string;
  tokens: {
    colors: {
      light: Record<string, string>;
      dark: Record<string, string>;
    };
    fonts?: Record<string, string>;
    spacing?: Record<string, string>;
    radius?: Record<string, string>;
    shadows?: Record<string, string>;
  } | null;
  custom_css?: string;
  custom_js?: string;
  updated?: string;
}

/** The sweep behind a fit: what was measured, and the fitted curve on the same x
 * (RFC 0006 §7). At most 200 points per trace, so a report stays bounded.
 *
 * `dropped` stands in for the traces when a report's summaries together came to more
 * than the driver's cap, or when retention has stripped them from an older row — a
 * report that will not save is worse than a report with no chart in it. */
export interface FitSummary {
  x?: number[];
  measured?: number[];
  fitted?: number[];
  x_label?: string;
  y_label?: string;
  x_scale?: "linear" | "log";
  dropped?: boolean;
}

/** One routine's outcome on one target, inside a calibration report. */
export interface RoutineResult {
  routine_name: string;
  target: string;
  parameters: Record<string, unknown>;
  timestamp: string;
  duration_s: number;
  /** Absent for a routine whose `analyse` does not produce one yet. */
  fit?: FitSummary;
}

/** A fidelity measurement. Benchmarks calibrate nothing; their number is what
 * the drift check compares against its threshold (RFC 0004 §6.7). */
export interface BenchmarkResult {
  protocol: string;
  target: string;
  fidelity: number | null;
  error_per_gate: number | null;
  raw_data?: Record<string, unknown>;
}

/** A row from the `calibration_results` collection (RFC 0004 §6.8). */
export interface CalibrationResult {
  id: string;
  driver: string;
  qpu: string;
  timestamp: string;
  duration_s: number;
  mode: "full" | "partial" | "fidelity_check";
  backend?: string;
  routine_results: RoutineResult[];
  benchmarks: BenchmarkResult[];
  errors: string[];
  status: "success" | "partial_failure" | "failed";
  created: string;
  expand?: {
    driver?: Driver;
  };
}

/** One routine in a calibration's plan (RFC 0006 §5.1). */
export interface CalibrationPlanNode {
  name: string;
  depends_on: string[];
  /** The targets this run walks it over, after the routine declined the ones whose
   * result would have nowhere to go. Empty means it applies to nothing here. */
  targets: string[];
  kind: "qubits" | "edges";
  /** Whether this run includes it at all. False for a routine disabled in
   * `calibration.yml` or outside a partial's subset — sent anyway, because which
   * part of the graph a partial is *not* touching is most of the value of drawing it. */
  planned: boolean;
  is_benchmark: boolean;
  has_check: boolean;
  /** Device-config paths this routine writes, e.g. `rxy.amp180`. Empty for the
   * twelve routines that measure without tuning. */
  updates: string[];
}

/** The graph a calibration walks, as its driver resolved it. Absent on a drift
 * check, which walks four disconnected benchmarks (RFC 0006 D6), and on requests
 * that predate the field.
 *
 * Rendered as sent and never re-derived from progress events, so a `calibration.yml`
 * edited mid-run cannot make the drawing disagree with the walk (RFC 0006 D7). */
export interface CalibrationPlan {
  nodes: CalibrationPlanNode[];
}

/** What one routine looks like right now, accumulated across progress events
 * (RFC 0006 §5.3). `done` counts the targets that have finished, `failed` how many
 * of those failed, and `skipped` how many never ran for want of a prerequisite. */
export interface CalibrationNodeState {
  state: "running" | "done" | "partial" | "failed" | "blocked";
  done: number;
  total: number;
  failed: number;
  skipped: number;
  /** The targets being measured right now, so the drawing can name the components in
   * flight rather than only the routine (RFC 0009 §7.2). Absent on a walk whose driver
   * predates it, and empty once the last target of a group reports back. */
  running?: string[];
}

/** How a node is drawn. The five a walk reports, plus the three that are properties
 * of the plan rather than of anything that happened: a planned node nothing has
 * reported on, one that applies to no target here, and one this run excludes. */
export type CalibrationNodeStatus =
  | CalibrationNodeState["state"]
  | "pending"
  | "skipped"
  | "not_planned";

/** A queued calibration waiting for, or being run by, its driver's dispatcher
 * (RFC 0004 §6.8). "running" is how the dashboard knows a calibration is in
 * flight — the run itself takes hours and reports back over NNG. */
export interface CalibrationRequest {
  id: string;
  driver: string;
  mode: "full" | "partial" | "fidelity_check";
  target_qubits?: string[];
  target_edges?: string[];
  status: "pending" | "running" | "done" | "failed";
  /** Where the walk has got to, replaced on each CalibrationProgress event. Absent
   * until the first routine finishes, and on a request that predates the field. */
  progress?: CalibrationProgress;
  /** The graph this run walks, published by its driver before the first routine. */
  plan?: CalibrationPlan;
  /** What the driver calls this run. Its own id for a dispatched calibration; a name
   * of the driver's making for one it started itself. */
  job_id?: string;
  /** Who wanted this. `"drift"` is the driver's own doing — a periodic check, or the
   * recalibration it triggers. Empty on requests that predate the field. */
  trigger?: "dispatched" | "drift";
  /** The admin who dispatched it, as their `users` record. Empty for a run the driver
   * started itself, and on requests that predate the field. */
  requested_by?: string;
  created: string;
  expand?: {
    driver?: Driver;
    requested_by?: { id: string; username?: string; email?: string };
  };
}

/** One routine and target of a walk in flight, with its running totals. */
export interface CalibrationProgress {
  mode: string;
  step: number;
  total: number;
  routine: string;
  target: string;
  succeeded: number;
  failed: number;
  skipped: number;
  elapsed_s: number;
  /** Targets this routine is about to measure. Present on the event sent before the
   * work and absent on the one after it, which is what tells a start from a finish. */
  running?: string[];
  /** Per routine, accumulated rather than replaced — this is what colours the graph.
   * Absent on a run whose driver or server predates it. */
  nodes?: Record<string, CalibrationNodeState>;
}
