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

const SI = [
  { limit: 1e9, suffix: "G", scale: 1e9 },
  { limit: 1e6, suffix: "M", scale: 1e6 },
  { limit: 1e3, suffix: "k", scale: 1e3 },
  { limit: 1, suffix: "", scale: 1 },
  { limit: 1e-3, suffix: "m", scale: 1e-3 },
  { limit: 1e-6, suffix: "µ", scale: 1e-6 },
  { limit: 1e-9, suffix: "n", scale: 1e-9 },
];

/** A number with an SI prefix, for an axis tick or a hover readout.
 *
 * A sweep axis is a frequency near 5e9 or a delay near 2e-8, and neither reads as a
 * tick label: `5.02G` and `20n` do. */
export function siPrefixed(value: number): string {
  if (!Number.isFinite(value)) return "";
  if (value === 0) return "0";
  const magnitude = Math.abs(value);
  const unit = SI.find((u) => magnitude >= u.limit) ?? SI[SI.length - 1];
  const scaled = value / unit.scale;
  const digits = Math.abs(scaled) < 10 ? 2 : Math.abs(scaled) < 100 ? 1 : 0;
  const text = scaled.toFixed(digits);
  // Only past the decimal point: stripping unconditionally turns 500 into 5.
  return `${digits ? text.replace(/\.?0+$/, "") : text}${unit.suffix}`;
}

/** `12.4s`, `3m12s` or `2h14m`, as the driver's own log renders a duration. */
export function formatSeconds(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  if (seconds < 3600) return `${(seconds / 60).toFixed(1)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}
