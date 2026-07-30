export interface User {
  id: string;
  email: string;
  username: string;
  qpu_seconds: number;
}

export interface QPU {
  id: string;
  name: string;
  status: "online" | "offline";
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
  | "custom";

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
