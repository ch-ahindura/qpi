import { describe, expect, it } from "vitest";
import type { CalibrationPlanNode } from "@/types";
import { layersOf, layoutGraph, statusOf } from "./layout";

function node(
  name: string,
  depends_on: string[] = [],
  extra: Partial<CalibrationPlanNode> = {},
): CalibrationPlanNode {
  return {
    name,
    depends_on,
    targets: ["q0"],
    kind: "qubits",
    planned: true,
    is_benchmark: false,
    has_check: false,
    updates: [],
    ...extra,
  };
}

/** The graph the driver actually publishes, as far as its shape goes. Eleven layers
 * deep and at most eight wide, one root (RFC 0006 §2). */
const REAL_GRAPH: CalibrationPlanNode[] = [
  node("resonator_spectroscopy"),
  node("time_of_flight", ["resonator_spectroscopy"]),
  node("resonator_relaxation", ["resonator_spectroscopy"]),
  node("resonator_punchout", ["resonator_spectroscopy"]),
  node("qubit_spectroscopy", ["resonator_punchout"]),
  node("rabi", ["qubit_spectroscopy"]),
  node("ramsey", ["rabi"]),
  node("drag", ["ramsey"]),
  node("fine_amplitude", ["drag"]),
  node("rb", ["fine_amplitude"]),
  // Two parents at different depths: the longest path is what decides.
  node("allxy_check", ["rb", "qubit_spectroscopy"]),
];

describe("layersOf", () => {
  it("puts a root at zero and each node below its deepest dependency", () => {
    const layers = layersOf(REAL_GRAPH);

    expect(layers.get("resonator_spectroscopy")).toBe(0);
    expect(layers.get("resonator_punchout")).toBe(1);
    expect(layers.get("qubit_spectroscopy")).toBe(2);
    expect(layers.get("rb")).toBe(7);
    // Not 3, which is what following the nearest parent would give.
    expect(layers.get("allxy_check")).toBe(8);
  });

  it("orders every edge downwards", () => {
    const layers = layersOf(REAL_GRAPH);
    for (const child of REAL_GRAPH) {
      for (const parent of child.depends_on) {
        expect(layers.get(parent)!).toBeLessThan(layers.get(child.name)!);
      }
    }
  });

  it("treats a dependency the plan does not name as a root", () => {
    const layers = layersOf([
      node("orphan", ["a_routine_this_run_never_heard_of"]),
    ]);
    expect(layers.get("orphan")).toBe(1);
  });

  it("does not recurse forever on a cycle the driver would have refused", () => {
    const layers = layersOf([node("a", ["b"]), node("b", ["a"])]);
    expect(layers.size).toBe(2);
  });
});

describe("statusOf", () => {
  it("reads a routine this run excludes off the plan", () => {
    expect(statusOf(node("rb", [], { planned: false }))).toBe("not_planned");
  });

  it("reads a routine that applies to nothing here off the plan", () => {
    expect(statusOf(node("cz_chevron", [], { targets: [] }))).toBe("skipped");
  });

  it("is pending until the walk has reported on it", () => {
    expect(statusOf(node("rabi"))).toBe("pending");
  });

  it("takes what the walk reported when there is any", () => {
    const reported = {
      state: "partial" as const,
      done: 3,
      total: 5,
      failed: 1,
    };
    expect(statusOf(node("rabi"), reported)).toBe("partial");
  });

  it("prefers the plan's own verdict over a stale report", () => {
    const reported = { state: "done" as const, done: 1, total: 1, failed: 0 };
    expect(statusOf(node("rb", [], { planned: false }), reported)).toBe(
      "not_planned",
    );
  });
});

describe("layoutGraph", () => {
  it("sizes the drawing from the graph it was given", () => {
    const layout = layoutGraph({ nodes: REAL_GRAPH });
    expect(layout.depth).toBe(9);
    expect(layout.width).toBe(3); // time_of_flight, resonator_relaxation, resonator_punchout
  });

  it("columns a layer in the plan's own sequence, so the drawing matches the walk", () => {
    const layout = layoutGraph({ nodes: REAL_GRAPH });
    const firstLayer = layout.nodes
      .filter((placed) => placed.layer === 1)
      .map((placed) => [placed.node.name, placed.column]);

    expect(firstLayer).toEqual([
      ["time_of_flight", 0],
      ["resonator_relaxation", 1],
      ["resonator_punchout", 2],
    ]);
  });

  it("falls back to the plan's target count before the walk has reported one", () => {
    const plan = { nodes: [node("rabi", [], { targets: ["q0", "q1", "q2"] })] };
    expect(layoutGraph(plan).nodes[0]).toMatchObject({
      status: "pending",
      done: 0,
      total: 3,
    });
  });

  it("carries the reported tally through", () => {
    const plan = { nodes: [node("rabi", [], { targets: ["q0", "q1", "q2"] })] };
    const layout = layoutGraph(plan, {
      rabi: { state: "running", done: 2, total: 3, failed: 0 },
    });
    expect(layout.nodes[0]).toMatchObject({
      status: "running",
      done: 2,
      total: 3,
    });
  });
});
