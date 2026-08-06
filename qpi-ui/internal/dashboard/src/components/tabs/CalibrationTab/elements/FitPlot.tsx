import React, { useMemo, useState } from "react";
import { AxisBottom, AxisLeft } from "@visx/axis";
import { scaleLinear, scaleLog } from "@visx/scale";
import { LinePath } from "@visx/shape";
import type { FitSummary } from "@/types";
import { siPrefixed } from "./format";

interface FitPlotProps {
  fit: FitSummary;
}

const WIDTH = 300;
const HEIGHT = 170;
const MARGIN = { top: 8, right: 8, bottom: 18, left: 34 };
const INNER_W = WIDTH - MARGIN.left - MARGIN.right;
const INNER_H = HEIGHT - MARGIN.top - MARGIN.bottom;

/** The sweep behind a fit: the measured points, and the fitted curve over them
 * (RFC 0006 §7).
 *
 * Through visx rather than by hand. `ReadingsChart` is 119 lines for one line with no
 * ticks, no legend and one series, and it is the easy case; two traces with ticked
 * axes, SI-prefixed units and an optional log x, thirty-three routines over, is a
 * charting library whether or not one is imported (RFC 0006 D4b). */
export const FitPlot: React.FC<FitPlotProps> = ({ fit }) => {
  const [hovered, setHovered] = useState<number | null>(null);

  const points = useMemo(() => {
    const x = fit.x ?? [];
    const measured = fit.measured ?? [];
    const fitted = fit.fitted ?? [];
    return x
      .map((value, i) => ({ x: value, y: measured[i], curve: fitted[i] }))
      .filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y));
  }, [fit]);

  if (fit.dropped) {
    return (
      <p
        data-testid="calibration-fit-dropped"
        className="text-xs text-gray-400 dark:text-zinc-500"
      >
        The traces behind this fit are not in this report — they were over the
        size cap, or have since aged out.
      </p>
    );
  }
  if (points.length < 2) return null;

  // A log sweep — RB's depths, a punchout's power — is unreadable linearly, and a
  // zero or negative setpoint has no place on a log axis.
  const logX =
    fit.x_scale === "log" && points.every((p) => p.x > 0)
      ? scaleLog<number>({
          domain: [points[0].x, points[points.length - 1].x],
          range: [0, INNER_W],
        })
      : null;
  const xScale =
    logX ??
    scaleLinear<number>({
      domain: [
        Math.min(...points.map((p) => p.x)),
        Math.max(...points.map((p) => p.x)),
      ],
      range: [0, INNER_W],
    });

  const ys = points.flatMap((p) =>
    Number.isFinite(p.curve) ? [p.y, p.curve] : [p.y],
  );
  const yScale = scaleLinear<number>({
    domain: [Math.min(...ys), Math.max(...ys)],
    range: [INNER_H, 0],
    nice: true,
  });

  const active = hovered === null ? null : points[hovered];

  return (
    <div data-testid="calibration-fit-plot">
      {/* The axes are labelled here rather than on themselves: a rotated label in a
          card this narrow lands on its own tick values. */}
      {(fit.y_label || fit.x_label) && (
        <p className="text-[10px] text-gray-400 dark:text-zinc-500">
          {fit.y_label} vs {fit.x_label}
        </p>
      )}
      <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} className="w-full">
        <g transform={`translate(${MARGIN.left},${MARGIN.top})`}>
          <AxisLeft
            scale={yScale}
            numTicks={4}
            tickFormat={(v) => siPrefixed(Number(v))}
            stroke="currentColor"
            tickStroke="currentColor"
            axisClassName="text-gray-300 dark:text-zinc-700"
            tickLabelProps={() => ({
              className: "fill-gray-500 dark:fill-zinc-400 text-[8px]",
              textAnchor: "end",
              dx: -2,
              dy: 3,
            })}
          />
          <AxisBottom
            top={INNER_H}
            scale={xScale}
            numTicks={4}
            tickFormat={(v) => siPrefixed(Number(v))}
            stroke="currentColor"
            tickStroke="currentColor"
            axisClassName="text-gray-300 dark:text-zinc-700"
            tickLabelProps={() => ({
              className: "fill-gray-500 dark:fill-zinc-400 text-[8px]",
              textAnchor: "middle",
              dy: 1,
            })}
          />
          <LinePath
            data={points.filter((p) => Number.isFinite(p.curve))}
            x={(p) => xScale(p.x) ?? 0}
            y={(p) => yScale(p.curve) ?? 0}
            className="stroke-indigo-500"
            strokeWidth={1.5}
            fill="none"
          />
          {points.map((p, i) => (
            <circle
              key={i}
              cx={xScale(p.x) ?? 0}
              cy={yScale(p.y) ?? 0}
              r={hovered === i ? 3 : 1.6}
              className="fill-gray-500 dark:fill-zinc-300"
              onMouseEnter={() => setHovered(i)}
              onMouseLeave={() => setHovered(null)}
            />
          ))}
        </g>
      </svg>
      <p className="flex items-center justify-between gap-2 text-[10px] text-gray-400 dark:text-zinc-500">
        <span className="flex items-center gap-2">
          <span className="flex items-center gap-1">
            <svg width={10} height={4} aria-hidden="true">
              <line
                x1={0}
                y1={2}
                x2={10}
                y2={2}
                strokeWidth={1.5}
                className="stroke-indigo-500"
              />
            </svg>
            fit
          </span>
          <span className="flex items-center gap-1">
            <svg width={6} height={6} aria-hidden="true">
              <circle
                cx={3}
                cy={3}
                r={1.6}
                className="fill-gray-500 dark:fill-zinc-300"
              />
            </svg>
            measured
          </span>
        </span>
        {active && (
          <span
            data-testid="calibration-fit-readout"
            className="font-mono tabular-nums"
          >
            {siPrefixed(active.x)}, {siPrefixed(active.y)}
          </span>
        )}
      </p>
    </div>
  );
};
