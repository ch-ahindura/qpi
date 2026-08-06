import { describe, expect, it } from "vitest";
import { formatParameter, formatSeconds, siPrefixed } from "./format";

describe("siPrefixed", () => {
  it("prefixes across the ten orders of magnitude a sweep axis spans", () => {
    expect(siPrefixed(5.02e9)).toBe("5.02G");
    expect(siPrefixed(4.5e6)).toBe("4.5M");
    expect(siPrefixed(1200)).toBe("1.2k");
    expect(siPrefixed(0.5)).toBe("500m");
    expect(siPrefixed(2e-8)).toBe("20n");
  });

  it("keeps zero and refuses to invent a prefix for it", () => {
    expect(siPrefixed(0)).toBe("0");
  });

  it("has nothing to say about a non-number", () => {
    expect(siPrefixed(NaN)).toBe("");
    expect(siPrefixed(Infinity)).toBe("");
  });

  it("keeps a sign", () => {
    expect(siPrefixed(-2.5e-6)).toBe("-2.5µ");
  });
});

describe("formatParameter", () => {
  it("reads a frequency in GHz rather than as ten digits", () => {
    expect(formatParameter("clock_freqs.f01", 5.02e9)).toBe("5.020000 GHz");
  });

  it("reads a coherence time in µs and a short one in ns", () => {
    expect(formatParameter("t1", 4.2e-5)).toBe("42.00 µs");
    expect(formatParameter("duration", 2e-8)).toBe("20.0 ns");
  });

  it("says how many values an echoed sweep axis had rather than listing them", () => {
    expect(formatParameter("depths", [1, 2, 4, 8])).toBe("[4 values]");
  });
});

describe("formatSeconds", () => {
  it("scales from seconds to hours", () => {
    expect(formatSeconds(41.2)).toBe("41.2s");
    expect(formatSeconds(1830)).toBe("30.5m");
    expect(formatSeconds(8040)).toBe("2.2h");
  });
});
