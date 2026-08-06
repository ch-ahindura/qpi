/**
 * The node card (RFC 0006 §6). Clicking a routine in the graph says what it is, what
 * it writes, where it sits, and how the run went on each of its targets.
 */

const PLAN = {
  nodes: [
    {
      name: "qubit_spectroscopy",
      depends_on: [],
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
    // Twelve routines measure without tuning; the card has to say so rather than
    // showing an empty list.
    {
      name: "t1",
      depends_on: ["rabi"],
      targets: ["q0", "q1"],
      kind: "qubits",
      planned: true,
      is_benchmark: false,
      has_check: false,
      updates: [],
    },
    {
      name: "rb",
      depends_on: ["rabi"],
      targets: ["q0"],
      kind: "qubits",
      planned: true,
      is_benchmark: true,
      has_check: false,
      updates: [],
    },
  ],
};

const PROGRESS = {
  mode: "full",
  step: 4,
  total: 4,
  routine: "rb",
  target: "q0",
  succeeded: 6,
  failed: 1,
  elapsed_s: 3600,
  nodes: {
    qubit_spectroscopy: { state: "done", done: 2, total: 2, failed: 0 },
    rabi: { state: "partial", done: 2, total: 2, failed: 1 },
    t1: { state: "done", done: 2, total: 2, failed: 0 },
    rb: { state: "done", done: 1, total: 1, failed: 0 },
  },
};

describe("Calibration node card", () => {
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

  it("opens on a click and closes again", () => {
    cy.seedCalibration({ plan: PLAN });
    cy.visit("/#calibration");

    cy.get('[data-testid="calibration-node-card"]').should("not.exist");
    cy.get('[data-testid="calibration-node-rabi"]').click();
    cy.get('[data-testid="calibration-node-card"]')
      .should("be.visible")
      .and("have.attr", "data-routine", "rabi");
    cy.get('[data-testid="calibration-node-rabi"]').should(
      "have.attr",
      "data-selected",
      "true",
    );

    cy.get('[data-testid="calibration-node-card-close"]').click();
    cy.get('[data-testid="calibration-node-card"]').should("not.exist");
  });

  it("says what the routine is and what it writes", () => {
    cy.seedCalibration({ plan: PLAN });
    cy.visit("/#calibration");

    cy.get('[data-testid="calibration-node-rabi"]').click();
    cy.get('[data-testid="calibration-node-card"]')
      .should("contain", "per qubit")
      .and("contain", "has a drift check")
      .and("contain", "rxy.amp180");
  });

  it("says so for a routine that measures without tuning", () => {
    cy.seedCalibration({ plan: PLAN });
    cy.visit("/#calibration");

    cy.get('[data-testid="calibration-node-t1"]').click();
    cy.get('[data-testid="calibration-node-card"]').should(
      "contain",
      "Measures only",
    );
  });

  it("navigates from a node to its neighbours", () => {
    cy.seedCalibration({ plan: PLAN });
    cy.visit("/#calibration");

    cy.get('[data-testid="calibration-node-rabi"]').click();
    // Upstream, then back downstream by a different route.
    cy.get('[data-testid="calibration-node-link-qubit_spectroscopy"]').click();
    cy.get('[data-testid="calibration-node-card"]').should(
      "have.attr",
      "data-routine",
      "qubit_spectroscopy",
    );
    cy.get('[data-testid="calibration-node-link-rabi"]').click();
    cy.get('[data-testid="calibration-node-card"]').should(
      "have.attr",
      "data-routine",
      "rabi",
    );
    // Which is where the two dependents of rabi are reachable from.
    cy.get('[data-testid="calibration-node-link-t1"]').should("exist");
    cy.get('[data-testid="calibration-node-link-rb"]').should("exist");
  });

  it("shows only the tally while the run is still going", () => {
    cy.seedCalibration({ plan: PLAN, progress: PROGRESS });
    cy.visit("/#calibration");

    cy.get('[data-testid="calibration-node-rabi"]').click();
    cy.get('[data-testid="calibration-node-card"]')
      .should("contain", "some targets failed")
      .and("contain", "2/2 qubits")
      .and("contain", "parameters appear when the report lands");
  });

  it("shows the fitted parameters and the failure once the report has landed", () => {
    // The report after the request, since that is the order they happen in and how
    // the two are matched — a report carries no job id.
    cy.seedCalibration({ plan: PLAN, progress: PROGRESS, status: "done" }).then(
      ({ driverId }) =>
        cy.seedCalibrationReport(driverId, {
          status: "partial_failure",
          routine_results: [
            {
              routine_name: "rabi",
              target: "q0",
              parameters: { "rxy.amp180": 0.2031 },
              timestamp: "2026-08-06T12:00:10.000Z",
              duration_s: 41.2,
            },
          ],
          errors: ["rabi[q1]: could not fit: the sweep never turned over"],
          benchmarks: [
            {
              protocol: "rb",
              target: "q0",
              fidelity: 0.99932,
              error_per_gate: 6.8e-4,
            },
          ],
        }),
    );
    cy.visit("/#calibration");

    cy.get('[data-testid="calibration-node-rabi"]').click();
    cy.get('[data-testid="calibration-node-target-q0"]')
      .should("contain", "rxy.amp180")
      .and("contain", "0.2031")
      .and("contain", "41.2s");
    cy.get('[data-testid="calibration-node-target-q1"]').should(
      "contain",
      "could not fit",
    );

    // A benchmark's number, which lives in `benchmarks` rather than in parameters.
    cy.get('[data-testid="calibration-node-rb"]').click();
    cy.get('[data-testid="calibration-node-target-q0"]')
      .should("contain", "fidelity 0.99932")
      .and("contain", "per gate");
  });
});
