/**
 * The calibration graph (RFC 0006 §5.4). Drawn from the plan the driver published,
 * coloured by the per-node state that progress events accumulate.
 */

/** A slice of the real graph: the readout fan-out, then the qubit chain off it. */
const PLAN = {
  nodes: [
    {
      name: "resonator_spectroscopy",
      depends_on: [],
      targets: ["q0", "q1"],
      kind: "qubits",
      planned: true,
      is_benchmark: false,
      has_check: true,
      updates: ["clock_freqs.readout"],
    },
    {
      name: "time_of_flight",
      depends_on: ["resonator_spectroscopy"],
      targets: ["q0", "q1"],
      kind: "qubits",
      planned: true,
      is_benchmark: false,
      has_check: false,
      updates: ["measure.acq_delay"],
    },
    {
      name: "resonator_punchout",
      depends_on: ["resonator_spectroscopy"],
      targets: ["q0", "q1"],
      kind: "qubits",
      planned: true,
      is_benchmark: false,
      has_check: false,
      updates: ["measure.pulse_amp"],
    },
    {
      name: "qubit_spectroscopy",
      depends_on: ["resonator_punchout"],
      targets: ["q0", "q1"],
      kind: "qubits",
      planned: true,
      is_benchmark: false,
      has_check: true,
      updates: ["clock_freqs.f01"],
    },
    {
      name: "rabi",
      depends_on: ["qubit_spectroscopy"],
      targets: ["q0", "q1"],
      kind: "qubits",
      planned: true,
      is_benchmark: false,
      has_check: true,
      updates: ["rxy.amp180"],
    },
    // Excluded from this run, and sent anyway: seeing what a partial is not
    // touching is most of the value of drawing it.
    {
      name: "rb",
      depends_on: ["rabi"],
      targets: ["q0", "q1"],
      kind: "qubits",
      planned: false,
      is_benchmark: true,
      has_check: false,
      updates: [],
    },
    // Planned, but the chip has no edges configured, so it applies to nothing.
    {
      name: "cz_chevron",
      depends_on: ["rabi"],
      targets: [],
      kind: "edges",
      planned: true,
      is_benchmark: false,
      has_check: false,
      updates: ["cz.duration"],
    },
  ],
};

describe("Calibration graph", () => {
  beforeEach(() => {
    cy.clearCookies();
    cy.clearLocalStorage();
    cy.visit("/");
    cy.contains("button", "Administrator").click();
    cy.get('input[type="text"]').clear().type("admin@example.com");
    cy.get('input[type="password"]').clear().type("supersecretpassword1234");
    cy.get('button[type="submit"]').click();
    cy.contains("h1", "QPI Interface").should("be.visible");
  });

  it("draws every node the plan carries, one box each", () => {
    cy.seedCalibration({ plan: PLAN });
    cy.visit("/#calibration");

    cy.get('[data-testid="calibration-graph"]').should("be.visible");
    PLAN.nodes.forEach((node) => {
      cy.get(`[data-testid="calibration-node-${node.name}"]`).should("exist");
    });
  });

  it("reads the states no progress event produces off the plan alone", () => {
    cy.seedCalibration({ plan: PLAN });
    cy.visit("/#calibration");

    // Nothing has reported yet, so a planned node with targets is pending...
    cy.get('[data-testid="calibration-node-rabi"]').should(
      "have.attr",
      "data-status",
      "pending",
    );
    // ...one this run excludes says so...
    cy.get('[data-testid="calibration-node-rb"]').should(
      "have.attr",
      "data-status",
      "not_planned",
    );
    // ...and one that applies to no configured target is skipped, not failed.
    cy.get('[data-testid="calibration-node-cz_chevron"]').should(
      "have.attr",
      "data-status",
      "skipped",
    );
  });

  it("colours each node from what the walk reported about it", () => {
    cy.seedCalibration({
      plan: PLAN,
      progress: {
        mode: "full",
        step: 5,
        total: 6,
        routine: "rabi",
        target: "q0",
        succeeded: 7,
        failed: 1,
        elapsed_s: 900,
        nodes: {
          resonator_spectroscopy: {
            state: "done",
            done: 2,
            total: 2,
            failed: 0,
          },
          time_of_flight: { state: "partial", done: 2, total: 2, failed: 1 },
          resonator_punchout: { state: "done", done: 2, total: 2, failed: 0 },
          qubit_spectroscopy: { state: "failed", done: 2, total: 2, failed: 2 },
          rabi: { state: "running", done: 1, total: 2, failed: 0 },
        },
      },
    });
    cy.visit("/#calibration");

    Object.entries({
      resonator_spectroscopy: "done",
      time_of_flight: "partial",
      qubit_spectroscopy: "failed",
      rabi: "running",
    }).forEach(([name, status]) => {
      cy.get(`[data-testid="calibration-node-${name}"]`).should(
        "have.attr",
        "data-status",
        status,
      );
    });
    // The tally beside the name, so a node mid-sweep says how far in it is.
    cy.get('[data-testid="calibration-node-rabi"]').should("contain", "1/2");
  });

  it("draws no graph for a drift check, which sends no plan", () => {
    cy.seedCalibration({ mode: "fidelity_check" });
    cy.visit("/#calibration");

    cy.get('[data-testid="calibration-in-flight"]').should("be.visible");
    cy.get('[data-testid="calibration-graph"]').should("not.exist");
  });
});
