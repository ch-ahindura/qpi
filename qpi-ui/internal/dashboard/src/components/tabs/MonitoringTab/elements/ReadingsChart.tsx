import React from "react";

interface Point {
  ts: number;
  value: number;
}

interface ReadingsChartProps {
  channel: string;
  unit?: string;
  points: Point[];
}

const WIDTH = 600;
const HEIGHT = 160;
const PADDING = 24;

/** A minimal SVG line chart for one channel's readings over time.
 *
 * Hand-rolled because one line with no ticks and no legend does not need more, not
 * for the reason this comment used to give — that the build cannot reach a registry.
 * `npm ci` already installs from one, so a package is a choice rather than a
 * constraint (RFC 0006 §2). Where the job outgrows this, `FitPlot` uses visx. */
export const ReadingsChart: React.FC<ReadingsChartProps> = ({
  channel,
  unit,
  points,
}) => {
  const latest = points[points.length - 1];

  if (points.length < 2) {
    return (
      <div className="bg-white dark:bg-zinc-900 border border-gray-200 dark:border-zinc-800 rounded-lg p-4">
        <ChartHeader channel={channel} unit={unit} latest={latest} />
        <div className="h-40 flex items-center justify-center text-xs text-gray-400 dark:text-zinc-500">
          Waiting for more readings…
        </div>
      </div>
    );
  }

  const values = points.map((p) => p.value);
  const minValue = Math.min(...values);
  const maxValue = Math.max(...values);
  const range = maxValue - minValue || 1;

  const minTs = points[0].ts;
  const maxTs = points[points.length - 1].ts;
  const tsRange = maxTs - minTs || 1;

  const toX = (ts: number) =>
    PADDING + ((ts - minTs) / tsRange) * (WIDTH - 2 * PADDING);
  const toY = (value: number) =>
    HEIGHT - PADDING - ((value - minValue) / range) * (HEIGHT - 2 * PADDING);

  const linePath = points
    .map(
      (p, i) =>
        `${i === 0 ? "M" : "L"} ${toX(p.ts).toFixed(1)} ${toY(p.value).toFixed(1)}`,
    )
    .join(" ");

  return (
    <div className="bg-white dark:bg-zinc-900 border border-gray-200 dark:border-zinc-800 rounded-lg p-4">
      <ChartHeader channel={channel} unit={unit} latest={latest} />
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="w-full h-40"
        preserveAspectRatio="none"
      >
        <line
          x1={PADDING}
          y1={HEIGHT - PADDING}
          x2={WIDTH - PADDING}
          y2={HEIGHT - PADDING}
          className="stroke-gray-200 dark:stroke-zinc-800"
          strokeWidth={1}
        />
        <path
          d={linePath}
          fill="none"
          className="stroke-indigo-500"
          strokeWidth={2}
          strokeLinejoin="round"
          strokeLinecap="round"
        />
        {points.map((p, i) => (
          <circle
            key={i}
            cx={toX(p.ts)}
            cy={toY(p.value)}
            r={i === points.length - 1 ? 3 : 1.5}
            className="fill-indigo-500"
          />
        ))}
      </svg>
      <div className="flex justify-between text-[10px] text-gray-400 dark:text-zinc-500 mt-1">
        <span>{new Date(minTs).toLocaleTimeString()}</span>
        <span>{new Date(maxTs).toLocaleTimeString()}</span>
      </div>
    </div>
  );
};

const ChartHeader: React.FC<{
  channel: string;
  unit?: string;
  latest?: Point;
}> = ({ channel, unit, latest }) => (
  <div className="flex justify-between items-baseline mb-2">
    <span className="text-xs font-semibold text-gray-500 dark:text-zinc-400 uppercase tracking-wider">
      {channel}
    </span>
    {latest && (
      <span className="font-mono text-sm text-gray-900 dark:text-white">
        {latest.value.toFixed(4)}
        {unit ? ` ${unit}` : ""}
      </span>
    )}
  </div>
);
