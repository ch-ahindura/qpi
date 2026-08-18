import type {
  CalibrationNodeState,
  CalibrationNodeStatus,
  CalibrationPlan,
  CalibrationPlanNode,
} from "@/types";

/** One node, placed and coloured. `column` is its position within its layer, in the
 * plan's own sequence, so the drawing reads in the order the walk takes. */
export interface PlacedNode {
  node: CalibrationPlanNode;
  layer: number;
  column: number;
  status: CalibrationNodeStatus;
  done: number;
  total: number;
  /** Targets in flight, for a node the walk is on. Empty for every other node. */
  running: string[];
}

export interface GraphLayout {
  nodes: PlacedNode[];
  /** How many nodes each layer holds, indexed by layer. The drawing centres a layer
   * against the widest one from this; left-packed, a graph that narrows towards the
   * bottom reads as though every edge bends left. */
  counts: number[];
  /** How many layers deep, and how many nodes wide the widest layer is. The drawing
   * is sized from these. */
  depth: number;
  width: number;
}

/** Each routine's layer: the longest path to it from a root over `depends_on`.
 *
 * Longest rather than shortest, so a node sits below every one of its dependencies
 * instead of below the nearest of them — otherwise an edge would run upwards.
 */
export function layersOf(nodes: CalibrationPlanNode[]): Map<string, number> {
  const byName = new Map(nodes.map((n) => [n.name, n]));
  const layers = new Map<string, number>();

  const layerOf = (name: string, ancestors: Set<string>): number => {
    const cached = layers.get(name);
    if (cached !== undefined) return cached;
    const node = byName.get(name);
    // A dependency the plan does not name, or a cycle. The driver refuses both
    // outright; neither should hang a browser, so both read as a root.
    if (!node || ancestors.has(name)) return 0;
    const seen = new Set(ancestors).add(name);
    const layer = node.depends_on.length
      ? Math.max(...node.depends_on.map((d) => layerOf(d, seen) + 1))
      : 0;
    layers.set(name, layer);
    return layer;
  };

  for (const node of nodes) layerOf(node.name, new Set());
  return layers;
}

/** How a node is drawn, given what the walk has reported about it.
 *
 * The three statuses no progress event produces are read off the plan: a routine
 * this run excludes, one that applies to none of the targets configured here, and
 * one that is simply not its turn yet.
 */
export function statusOf(
  node: CalibrationPlanNode,
  reported?: CalibrationNodeState,
): CalibrationNodeStatus {
  if (!node.planned) return "not_planned";
  if (node.targets.length === 0) return "skipped";
  return reported?.state ?? "pending";
}

/** The plan as a grid of placed nodes, ready to draw. */
export function layoutGraph(
  plan: CalibrationPlan,
  reported: Record<string, CalibrationNodeState> = {},
): GraphLayout {
  const layers = layersOf(plan.nodes);
  const filled: number[] = [];

  const nodes = plan.nodes.map((node) => {
    const layer = layers.get(node.name) ?? 0;
    const column = filled[layer] ?? 0;
    filled[layer] = column + 1;
    const state = reported[node.name];
    return {
      node,
      layer,
      column,
      status: statusOf(node, state),
      done: state?.done ?? 0,
      total: state?.total || node.targets.length,
      running: state?.running ?? [],
    };
  });

  return {
    nodes,
    counts: filled,
    depth: filled.length,
    width: filled.length ? Math.max(...filled) : 0,
  };
}
