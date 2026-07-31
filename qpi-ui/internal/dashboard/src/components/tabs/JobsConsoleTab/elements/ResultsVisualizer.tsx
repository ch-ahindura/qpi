import type { JobResult, QuantumJob } from "@/types";

interface Props {
  viewedJob: QuantumJob | null;
  activeTab: "counts" | "iq" | "trace";
}

export function ResultsVisualizer({ viewedJob, activeTab }: Props) {
  if (!viewedJob) {
    return (
      <div className="text-gray-400 dark:text-zinc-500 text-sm">
        Select a completed job to view results visualization.
      </div>
    );
  }

  if (viewedJob.status !== "completed") {
    return (
      <div className="text-gray-400 dark:text-zinc-500 text-sm">
        Job status is currently{" "}
        <span className="text-warning capitalize font-semibold">
          {viewedJob.status}
        </span>
        . Waiting for completion...
      </div>
    );
  }

  const results = viewedJob.results;
  const firstCircuit = results?.circuit_results?.[0] as JobResult | undefined;

  if (activeTab === "counts") {
    const counts =
      results?.counts ||
      results?.hex_counts ||
      firstCircuit?.counts ||
      firstCircuit?.hex_counts ||
      {};
    const keys = Object.keys(counts);
    if (keys.length === 0) {
      return (
        <div className="text-gray-400 dark:text-zinc-500 text-sm">
          No counts data available.
        </div>
      );
    }

    const maxVal = Math.max(...(Object.values(counts) as number[]));
    return (
      <div className="w-full h-full flex flex-col justify-between pt-8 pb-6 px-4 gap-4 relative">
        <div className="flex-1 flex items-end justify-around gap-4 w-full h-56">
          {keys.map((k) => {
            const count = counts[k] as number;
            const percent = maxVal > 0 ? (count / maxVal) * 100 : 0;
            return (
              <div
                key={k}
                className="w-full max-w-[60px] flex flex-col items-center gap-2 group h-full justify-end"
              >
                <span className="font-mono text-gray-500 dark:text-zinc-400 opacity-0 group-hover:opacity-100 transition-opacity text-[10px]">
                  {count}
                </span>
                <div
                  className="w-full bg-indigo-500 hover:bg-indigo-400 transition-colors rounded-t-sm"
                  style={{ height: `${percent * 0.8}%` }}
                />
                <span className="font-mono text-gray-900 dark:text-white text-xs mt-2">
                  {k}
                </span>
              </div>
            );
          })}
        </div>
        <div
          className="absolute inset-0 pointer-events-none"
          style={{
            backgroundImage:
              "linear-gradient(to bottom, #27272a 1px, transparent 1px)",
            backgroundSize: "100% 20%",
            opacity: 0.1,
          }}
        />
      </div>
    );
  }

  if (activeTab === "iq") {
    const memory = results?.memory || firstCircuit?.memory || [];
    if (memory.length === 0) {
      return (
        <div className="text-gray-400 dark:text-zinc-500 text-sm text-center">
          No IQ memory data available. <br />
          <span className="text-xs text-zinc-600 mt-1 block">
            (Must submit with Meas Level = 1)
          </span>
        </div>
      );
    }

    // Plot first qubit points (limited to 200)
    const shown: number[][] = [];
    memory.forEach((shot: number[][], idx: number) => {
      if (idx > 200) return;
      const qPoint = shot[0]; // first qubit [I, Q]
      if (qPoint) shown.push(qPoint);
    });

    // Scale to the data rather than to a fixed window. IQ values come back in
    // whatever units the readout chain produces — they are not normalised to
    // 0..1 by anything — so a hardcoded range puts the clusters off-screen for
    // most devices and shows an empty plot instead of the answer.
    //
    // One square window over both axes, so the IQ plane is not stretched: the
    // distance between the two blobs is the readout fidelity, and squashing one
    // axis misrepresents it.
    const values = shown.flat();
    const low = values.length ? Math.min(...values) : 0;
    const high = values.length ? Math.max(...values) : 1;
    const pad = (high - low || 1) * 0.15;
    const min = low - pad;
    const max = high + pad;
    const mapVal = (v: number) => ((v - min) / (max - min)) * 200;

    // Colour by which side of the two clusters a shot fell, taken from the
    // spread of this acquisition rather than from an assumed threshold.
    const iValues = shown.map((point) => point[0]);
    const midpoint = iValues.length
      ? (Math.min(...iValues) + Math.max(...iValues)) / 2
      : 0;

    const points: React.ReactNode[] = shown.map((qPoint, idx) => (
      <circle
        key={idx}
        cx={mapVal(qPoint[0])}
        cy={200 - mapVal(qPoint[1])}
        r="3"
        fill={qPoint[0] > midpoint ? "#6366f1" : "#22c55e"}
        opacity="0.7"
      />
    ));

    return (
      <div className="flex flex-col items-center justify-center">
        <svg
          viewBox="0 0 200 200"
          className="w-64 h-64 border border-gray-200 dark:border-zinc-800 rounded bg-gray-50 dark:bg-zinc-950/50"
        >
          <line
            x1="0"
            y1="100"
            x2="200"
            y2="100"
            stroke="#27272a"
            strokeDasharray="2"
          />
          <line
            x1="100"
            y1="0"
            x2="100"
            y2="200"
            stroke="#27272a"
            strokeDasharray="2"
          />
          {points}
        </svg>
        <div className="text-[10px] text-gray-400 dark:text-zinc-500 mt-2 font-mono">
          I component vs Q component scatter plot
        </div>
      </div>
    );
  }

  if (activeTab === "trace") {
    const memory = results?.memory || firstCircuit?.memory || [];
    if (memory.length === 0) {
      return (
        <div className="text-gray-400 dark:text-zinc-500 text-sm text-center">
          No raw trace data available. <br />
          <span className="text-xs text-zinc-600 mt-1 block">
            (Must submit with Meas Level = 0)
          </span>
        </div>
      );
    }

    // Plot first qubit trace
    const qubitTrace = memory[0] || [];
    if (qubitTrace.length === 0) {
      return (
        <div className="text-gray-400 dark:text-zinc-500 text-sm">
          No trace points.
        </div>
      );
    }

    let pathPoints = "";
    const maxLen = Math.min(qubitTrace.length, 100);
    for (let i = 0; i < maxLen; i++) {
      const val = qubitTrace[i][0]; // real component
      const x = (i / (maxLen - 1)) * 200;
      const y = 100 - val * 40; // scale factor
      pathPoints += `${i === 0 ? "M" : "L"} ${x} ${y} `;
    }

    return (
      <div className="w-full flex flex-col items-center px-4">
        <svg
          viewBox="0 0 200 100"
          className="w-full h-48 border border-gray-200 dark:border-zinc-800 rounded bg-gray-50 dark:bg-zinc-950/50 px-2"
        >
          <line
            x1="0"
            y1="50"
            x2="200"
            y2="50"
            stroke="#27272a"
            strokeDasharray="2"
          />
          <path d={pathPoints} fill="none" stroke="#6366f1" strokeWidth="1.5" />
        </svg>
        <div className="text-[10px] text-gray-400 dark:text-zinc-500 mt-2 font-mono">
          Voltage signal trace vs time
        </div>
      </div>
    );
  }
}
