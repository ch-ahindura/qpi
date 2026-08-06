/** Formats a fitted parameter for reading rather than for precision.
 *
 * Calibration numbers span ten orders of magnitude — a frequency near 5e9, a
 * duration near 2e-8 — so a single fixed format is unreadable for one end or
 * the other. Frequencies become GHz, times become µs or ns, and everything
 * else falls back to a short significant-figure form. */
export function formatParameter(key: string, value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return Array.isArray(value) ? `[${value.length} values]` : String(value);
  }

  const name = key.toLowerCase();
  if (name.includes("freq") && Math.abs(value) > 1e6) {
    return `${(value / 1e9).toFixed(6)} GHz`;
  }
  if (
    name.startsWith("t1") ||
    name.startsWith("t2") ||
    name.includes("duration")
  ) {
    return Math.abs(value) < 1e-6
      ? `${(value * 1e9).toFixed(1)} ns`
      : `${(value * 1e6).toFixed(2)} µs`;
  }
  if (
    Math.abs(value) !== 0 &&
    (Math.abs(value) < 1e-3 || Math.abs(value) >= 1e6)
  ) {
    return value.toExponential(3);
  }
  return value.toFixed(6).replace(/\.?0+$/, "");
}

/** `12.4s`, `3m12s` or `2h14m`, as the driver's own log renders a duration. */
export function formatSeconds(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  if (seconds < 3600) return `${(seconds / 60).toFixed(1)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}
