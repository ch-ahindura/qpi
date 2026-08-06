import React, { useMemo } from "react";
import type {
  CalibrationNodeState,
  CalibrationNodeStatus,
  CalibrationPlan,
} from "@/types";
import { layoutGraph } from "./layout";

interface CalibrationGraphProps {
  plan: CalibrationPlan;
  /** What the walk has reported per routine. Absent before the first routine ends. */
  nodes?: Record<string, CalibrationNodeState>;
}

// Eleven layers at this pitch is ~700px tall and eight nodes wide is ~1200px, so the
// whole graph fits a desktop and scrolls in its container on anything narrower.
const NODE_W = 136;
const NODE_H = 42;
const GAP_X = 14;
const GAP_Y = 26;
const PITCH_X = NODE_W + GAP_X;
const PITCH_Y = NODE_H + GAP_Y;

const NODE_STYLES: Record<
  CalibrationNodeStatus,
  { box: string; label: string; dashed?: boolean }
> = {
  running: {
    box: "fill-blue-500/15 stroke-blue-500 animate-pulse",
    label: "fill-blue-700 dark:fill-blue-300",
  },
  done: {
    box: "fill-emerald-500/15 stroke-emerald-500",
    label: "fill-emerald-700 dark:fill-emerald-300",
  },
  partial: {
    box: "fill-amber-500/15 stroke-amber-500",
    label: "fill-amber-700 dark:fill-amber-300",
  },
  failed: {
    box: "fill-red-500/15 stroke-red-500",
    label: "fill-red-700 dark:fill-red-400",
  },
  pending: {
    box: "fill-transparent stroke-gray-300 dark:stroke-zinc-700",
    label: "fill-gray-500 dark:fill-zinc-400",
  },
  skipped: {
    box: "fill-transparent stroke-gray-300 dark:stroke-zinc-700",
    label: "fill-gray-400 dark:fill-zinc-500",
    dashed: true,
  },
  not_planned: {
    box: "fill-transparent stroke-gray-200 dark:stroke-zinc-800",
    label: "fill-gray-300 dark:fill-zinc-600",
    dashed: true,
  },
};

const LEGEND: { status: CalibrationNodeStatus; label: string }[] = [
  { status: "done", label: "done" },
  { status: "running", label: "running" },
  { status: "partial", label: "some targets failed" },
  { status: "failed", label: "failed" },
  { status: "pending", label: "not yet run" },
  { status: "skipped", label: "applies to nothing here" },
  { status: "not_planned", label: "not in this run" },
];

/** The calibration graph, drawn from the plan its driver published (RFC 0006 §5.4).
 *
 * By hand rather than through a graph library: the shape is fixed, eleven layers by
 * eight, and longest-path layering is a dozen lines. `react-flow` and `dagre` exist to
 * make nodes draggable, pannable and connectable — an interactive editor, which is
 * exactly what RFC 0006 §12 says this must not become. */
export const CalibrationGraph: React.FC<CalibrationGraphProps> = ({
  plan,
  nodes,
}) => {
  const layout = useMemo(() => layoutGraph(plan, nodes ?? {}), [plan, nodes]);

  const placedBy = useMemo(
    () => new Map(layout.nodes.map((placed) => [placed.node.name, placed])),
    [layout],
  );

  if (layout.nodes.length === 0) return null;

  const width = layout.width * PITCH_X - GAP_X;
  const height = layout.depth * PITCH_Y - GAP_Y;
  // Each layer centred against the widest, so the graph reads as the tree it is.
  const leftX = (layer: number, column: number) =>
    ((layout.width - layout.counts[layer]) * PITCH_X) / 2 + column * PITCH_X;
  const topY = (layer: number) => layer * PITCH_Y;

  return (
    <div data-testid="calibration-graph">
      <div className="overflow-auto max-h-[70vh] border border-gray-200 dark:border-zinc-800 rounded-lg bg-white dark:bg-zinc-900 p-4">
        <svg
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          className="max-w-none"
        >
          {layout.nodes.flatMap((placed) =>
            placed.node.depends_on.map((parentName) => {
              const parent = placedBy.get(parentName);
              if (!parent) return null;
              const from = {
                x: leftX(parent.layer, parent.column) + NODE_W / 2,
                y: topY(parent.layer) + NODE_H,
              };
              const to = {
                x: leftX(placed.layer, placed.column) + NODE_W / 2,
                y: topY(placed.layer),
              };
              const bend = (to.y - from.y) / 2;
              return (
                <path
                  key={`${parentName}->${placed.node.name}`}
                  d={`M ${from.x} ${from.y} C ${from.x} ${from.y + bend}, ${to.x} ${to.y - bend}, ${to.x} ${to.y}`}
                  fill="none"
                  strokeWidth={1}
                  className="stroke-gray-300 dark:stroke-zinc-700"
                />
              );
            }),
          )}
          {layout.nodes.map((placed) => {
            const style = NODE_STYLES[placed.status];
            const x = leftX(placed.layer, placed.column);
            const y = topY(placed.layer);
            const lines = wrapName(placed.node.name);
            return (
              <g
                key={placed.node.name}
                data-testid={`calibration-node-${placed.node.name}`}
                data-status={placed.status}
              >
                <title>{`${placed.node.name} — ${placed.status}`}</title>
                <rect
                  x={x}
                  y={y}
                  width={NODE_W}
                  height={NODE_H}
                  rx={4}
                  strokeWidth={1}
                  strokeDasharray={style.dashed ? "3 3" : undefined}
                  className={style.box}
                />
                {lines.map((line, i) => (
                  <text
                    key={i}
                    x={x + 6}
                    y={y + 15 + i * 11}
                    className={`text-[9px] font-mono ${style.label}`}
                  >
                    {line}
                  </text>
                ))}
                {/* Only when there is more than one, or `1/1` on every node is noise. */}
                {placed.total > 1 && (
                  <text
                    x={x + NODE_W - 6}
                    y={y + NODE_H - 6}
                    textAnchor="end"
                    className={`text-[9px] tabular-nums ${style.label}`}
                  >
                    {placed.done}/{placed.total}
                  </text>
                )}
              </g>
            );
          })}
        </svg>
      </div>
      <ul className="flex flex-wrap gap-x-4 gap-y-1 mt-2 text-xs text-gray-500 dark:text-zinc-400">
        {LEGEND.map(({ status, label }) => (
          <li key={status} className="flex items-center gap-1.5">
            <svg width={12} height={12} aria-hidden="true">
              <rect
                x={0.5}
                y={0.5}
                width={11}
                height={11}
                rx={2}
                strokeWidth={1}
                strokeDasharray={NODE_STYLES[status].dashed ? "2 2" : undefined}
                className={NODE_STYLES[status].box}
              />
            </svg>
            {label}
          </li>
        ))}
      </ul>
    </div>
  );
};

/** A routine name over at most two lines, broken at an underscore.
 *
 * `resonator_spectroscopy_second_excited` is thirty-six characters and a node is a
 * hundred and thirty-six pixels wide; one line would run into the next node. */
function wrapName(name: string, perLine = 22): string[] {
  if (name.length <= perLine) return [name];
  const words = name.split("_");
  const lines: string[] = [];
  let line = "";
  for (const word of words) {
    const candidate = line ? `${line}_${word}` : word;
    if (candidate.length > perLine && line) {
      lines.push(line);
      line = word;
    } else {
      line = candidate;
    }
  }
  if (line) lines.push(line);
  return lines.length <= 2 ? lines : [lines[0], `${lines.slice(1).join("_")}`];
}
