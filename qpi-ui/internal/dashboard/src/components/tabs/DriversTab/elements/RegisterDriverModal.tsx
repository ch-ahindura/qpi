import { useState } from "react";
import { X, Copy, Check } from "lucide-react";
import { DEVICE_LANGUAGES } from "@/types";
import type {
  CreateDriverRequest,
  CreateDriverResponse,
  DriverKind,
  DriverLanguage,
  QPU,
} from "@/types";

const KNOWN_EVENTS = ["JobDispatch", "JobResult", "CryostatReading"];

const LANGUAGES: DriverLanguage[] = ["python", "typescript", "go"];

interface Props {
  qpus: QPU[];
  onClose: () => void;
  onRegister: (req: CreateDriverRequest) => Promise<CreateDriverResponse>;
}

export function RegisterDriverModal({ qpus, onClose, onRegister }: Props) {
  const [name, setName] = useState("");
  const [qpu, setQpu] = useState(qpus[0]?.id ?? "");
  const [kind, setKind] = useState<DriverKind>("mock");
  const [language, setLanguage] = useState<DriverLanguage>("python");
  const [events, setEvents] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const [created, setCreated] = useState<CreateDriverResponse | null>(null);
  const [copiedToken, setCopiedToken] = useState(false);
  const [copiedSnippet, setCopiedSnippet] = useState<string | null>(null);

  const isCustom = kind === "custom";
  const availableLanguages = DEVICE_LANGUAGES[kind];

  // Changing the kind can strip the language out from under the form — pick qblox
  // while go is selected and there is no such Go driver to register. Fall back to
  // the first language that does ship it rather than letting the server say no.
  const chooseKind = (next: DriverKind) => {
    setKind(next);
    if (!DEVICE_LANGUAGES[next].includes(language)) {
      setLanguage(DEVICE_LANGUAGES[next][0]);
    }
  };

  const toggleEvent = (event: string) => {
    setEvents((prev) =>
      prev.includes(event) ? prev.filter((e) => e !== event) : [...prev, event],
    );
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");

    if (!qpu) {
      setError("Select the QPU this driver belongs to.");
      return;
    }
    if (isCustom && events.length === 0) {
      setError("Select at least one event for a custom driver.");
      return;
    }

    setLoading(true);
    try {
      const res = await onRegister({
        name,
        qpu,
        kind,
        language,
        events: isCustom ? events : undefined,
      });
      setCreated(res);
    } catch (err: unknown) {
      const message =
        err instanceof Error
          ? err.message
          : "Registration failed. Check inputs.";
      setError(message);
    } finally {
      setLoading(false);
    }
  };

  const handleCopyToken = () => {
    if (!created) return;
    navigator.clipboard.writeText(created.token);
    setCopiedToken(true);
    setTimeout(() => setCopiedToken(false), 2000);
  };

  const handleCopySnippet = (key: string, value: string) => {
    navigator.clipboard.writeText(value);
    setCopiedSnippet(key);
    setTimeout(() => setCopiedSnippet(null), 2000);
  };

  if (created) {
    const snippets = created.snippets;
    const allSnippets: Array<[string, string, string]> = [
      ["systemd", "Installation Command (Systemd)", snippets.systemd ?? ""],
      ["manual_cli", "Manual CLI Command", snippets.manual_cli ?? ""],
      ["install", "Install Command", snippets.install ?? ""],
      ["stub", "Driver Stub", snippets.stub ?? ""],
    ];
    const snippetEntries = allSnippets.filter(([, , value]) => value !== "");

    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-gray-50 dark:bg-zinc-950/80 backdrop-blur-sm animate-fade-in">
        <div className="w-full max-w-lg bg-white dark:bg-zinc-900 border border-gray-200 dark:border-zinc-800 rounded-lg shadow-2xl p-6 space-y-5 max-h-[90vh] overflow-y-auto">
          <div className="flex justify-between items-center border-b border-gray-200 dark:border-zinc-800 pb-3">
            <h3 className="text-lg font-semibold font-geist text-emerald-400">
              Driver Registered Successfully!
            </h3>
            <button
              onClick={onClose}
              className="text-gray-500 dark:text-zinc-400 hover:text-gray-900 dark:text-white focus:outline-none"
            >
              <X className="w-5 h-5" />
            </button>
          </div>

          <div className="space-y-4 text-sm text-gray-600 dark:text-zinc-300">
            <p>
              Driver <strong>{created.name}</strong> ({created.kind} /{" "}
              {created.language}) has been registered. Copy the credentials
              below to configure it — they are shown only once.
            </p>

            <div className="bg-gray-50 dark:bg-zinc-950 border border-gray-200 dark:border-zinc-800 rounded p-4 space-y-4">
              <div>
                <label className="block text-xs font-semibold text-gray-500 dark:text-zinc-400 uppercase tracking-wider mb-1">
                  One-Time Token
                </label>
                <div className="flex items-center gap-2">
                  <span className="flex-1 font-mono bg-white dark:bg-zinc-900 border border-gray-200 dark:border-zinc-800 px-3 py-1.5 rounded text-gray-900 dark:text-white overflow-x-auto whitespace-nowrap">
                    {created.token}
                  </span>
                  <button
                    type="button"
                    onClick={handleCopyToken}
                    className="p-2 bg-gray-100 dark:bg-zinc-800 hover:bg-gray-200 dark:hover:bg-zinc-700 text-gray-900 dark:text-white rounded transition-colors focus:outline-none flex items-center justify-center min-w-[36px]"
                    title="Copy Token"
                  >
                    {copiedToken ? (
                      <Check className="w-4 h-4 text-emerald-400" />
                    ) : (
                      <Copy className="w-4 h-4" />
                    )}
                  </button>
                </div>
                <span className="text-xs text-amber-400 mt-1 block">
                  ⚠️ This token is only displayed once. Store it securely.
                </span>
              </div>

              {snippetEntries.map(([key, label, value]) => (
                <div key={key}>
                  <label className="block text-xs font-semibold text-gray-500 dark:text-zinc-400 uppercase tracking-wider mb-1">
                    {label}
                  </label>
                  <div className="flex items-start gap-2">
                    <pre className="flex-1 font-mono bg-white dark:bg-zinc-900 border border-gray-200 dark:border-zinc-800 p-3 rounded text-gray-700 dark:text-zinc-200 text-xs overflow-x-auto whitespace-pre-wrap break-all leading-relaxed max-h-40">
                      {value}
                    </pre>
                    <button
                      type="button"
                      onClick={() => handleCopySnippet(key, value)}
                      className="p-2 bg-gray-100 dark:bg-zinc-800 hover:bg-gray-200 dark:hover:bg-zinc-700 text-gray-900 dark:text-white rounded transition-colors focus:outline-none flex items-center justify-center min-w-[36px] mt-1"
                      title={`Copy ${label}`}
                    >
                      {copiedSnippet === key ? (
                        <Check className="w-4 h-4 text-emerald-400" />
                      ) : (
                        <Copy className="w-4 h-4" />
                      )}
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="flex justify-end pt-2">
            <button
              type="button"
              onClick={onClose}
              className="bg-white text-zinc-950 font-semibold py-2 px-6 rounded hover:opacity-90 transition-opacity focus:outline-none"
            >
              Done
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-gray-50 dark:bg-zinc-950/80 backdrop-blur-sm">
      <div className="w-full max-w-md bg-white dark:bg-zinc-900 border border-gray-200 dark:border-zinc-800 rounded-lg shadow-2xl p-6 space-y-4 max-h-[90vh] overflow-y-auto">
        <div className="flex justify-between items-center border-b border-gray-200 dark:border-zinc-800 pb-3">
          <h3 className="text-lg font-semibold font-geist text-gray-900 dark:text-white">
            Register Driver
          </h3>
          <button
            onClick={onClose}
            className="text-gray-500 dark:text-zinc-400 hover:text-gray-900 dark:text-white focus:outline-none"
          >
            <X className="w-5 h-5" />
          </button>
        </div>
        <form onSubmit={handleSubmit} className="space-y-4 text-sm">
          <div>
            <label className="block text-xs font-medium text-gray-500 dark:text-zinc-400 mb-1.5 uppercase tracking-wider">
              Driver Name
            </label>
            <input
              type="text"
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full bg-gray-50 dark:bg-zinc-950 border border-gray-200 dark:border-zinc-800 rounded px-3 py-2 text-gray-900 dark:text-white focus:outline-none focus:border-zinc-500 transition-colors"
              placeholder="cryostat-monitor-1"
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-500 dark:text-zinc-400 mb-1.5 uppercase tracking-wider">
              QPU
            </label>
            <select
              required
              data-testid="driver-qpu-select"
              value={qpu}
              onChange={(e) => setQpu(e.target.value)}
              className="w-full bg-gray-50 dark:bg-zinc-950 border border-gray-200 dark:border-zinc-800 text-gray-900 dark:text-white rounded px-3 py-2 focus:outline-none focus:border-zinc-500 transition-colors"
            >
              {qpus.length === 0 && (
                <option value="">No QPUs registered yet</option>
              )}
              {qpus.map((q) => (
                <option key={q.id} value={q.id}>
                  {q.name}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-500 dark:text-zinc-400 mb-1.5 uppercase tracking-wider">
              Kind
            </label>
            <select
              data-testid="driver-kind-select"
              value={kind}
              onChange={(e) => chooseKind(e.target.value as DriverKind)}
              className="w-full bg-gray-50 dark:bg-zinc-950 border border-gray-200 dark:border-zinc-800 text-gray-900 dark:text-white rounded px-3 py-2 focus:outline-none focus:border-zinc-500 transition-colors"
            >
              <option value="mock">mock (Local Simulator)</option>
              <option value="qiskit_aer">qiskit_aer (Aer Simulator)</option>
              <option value="quantify">quantify (Quantify Driver)</option>
              <option value="qblox">qblox (Qblox Driver)</option>
              <option value="presto">presto (Presto Driver)</option>
              <option value="bluefors_gen1">
                bluefors_gen1 (Bluefors Gen. 1 Cryostat Monitor)
              </option>
              <option value="custom">custom (Bring your own driver)</option>
            </select>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-500 dark:text-zinc-400 mb-1.5 uppercase tracking-wider">
              Language
            </label>
            <select
              data-testid="driver-language-select"
              value={language}
              onChange={(e) => setLanguage(e.target.value as DriverLanguage)}
              className="w-full bg-gray-50 dark:bg-zinc-950 border border-gray-200 dark:border-zinc-800 text-gray-900 dark:text-white rounded px-3 py-2 focus:outline-none focus:border-zinc-500 transition-colors"
            >
              {LANGUAGES.map((l) => (
                <option
                  key={l}
                  value={l}
                  disabled={!availableLanguages.includes(l)}
                >
                  {l}
                  {availableLanguages.includes(l)
                    ? ""
                    : ` (no ${kind} device in this SDK)`}
                </option>
              ))}
            </select>
          </div>

          {isCustom && (
            <div>
              <label className="block text-xs font-medium text-gray-500 dark:text-zinc-400 mb-1.5 uppercase tracking-wider">
                Events
              </label>
              <div className="flex flex-col gap-2">
                {KNOWN_EVENTS.map((event) => (
                  <label
                    key={event}
                    className="flex items-center gap-2 text-gray-700 dark:text-zinc-300"
                  >
                    <input
                      type="checkbox"
                      checked={events.includes(event)}
                      onChange={() => toggleEvent(event)}
                      className="accent-white"
                    />
                    {event}
                  </label>
                ))}
              </div>
            </div>
          )}

          {error && (
            <div className="text-xs text-error font-medium bg-error/10 border border-error/20 p-2.5 rounded">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={loading}
            className="w-full bg-white text-zinc-950 font-semibold py-2.5 rounded hover:opacity-90 transition-opacity disabled:opacity-50"
          >
            {loading ? "Registering..." : "Register Driver"}
          </button>
        </form>
      </div>
    </div>
  );
}
