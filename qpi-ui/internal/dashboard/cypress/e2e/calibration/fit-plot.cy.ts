/**
 * The chart behind a node (RFC 0006 §7): the sweep a fit was made from, and the
 * fitted curve over the same setpoints.
 */

const PLAN = {
  nodes: [
    {
      name: "rabi",
      depends_on: [],
      targets: ["q0"],
      kind: "qubits",
      planned: true,
      is_benchmark: false,
      has_check: true,
      updates: ["rxy.amp180"],
    },
    // Not every routine's `analyse` produces a summary yet — one is converted at
    // a time, and the rest simply have no chart.
    {
      name: "cz_chevron",
      depends_on: ["rabi"],
      targets: ["q0_q1"],
      kind: "edges",
      planned: true,
      is_benchmark: false,
      has_check: false,
      updates: ["cz.duration"],
    },
  ],
};

/** A Rabi sweep and its fitted cosine, the shape `fit_summary` ships. */
const FIT = {
  x: Array.from({ length: 41 }, (_, i) => i * 0.0125),
  measured: Array.from({ length: 41 }, (_, i) =>
    Math.cos(2 * Math.PI * 2 * i * 0.0125),
  ),
  fitted: Array.from({ length: 41 }, (_, i) =>
    Math.cos(2 * Math.PI * 2.02 * i * 0.0125),
  ),
  x_label: "pulse amplitude",
  y_label: "signal",
  x_scale: "linear",
};

function seedRun(fit: unknown) {
  return cy
    .seedCalibration({ plan: PLAN, status: "done" })
    .then(({ driverId }) =>
      cy.seedCalibrationReport(driverId, {
        routine_results: [
          {
            routine_name: "rabi",
            target: "q0",
            parameters: { "rxy.amp180": 0.2031 },
            timestamp: "2026-08-06T12:00:10.000Z",
            duration_s: 41.2,
            fit,
          },
          {
            routine_name: "cz_chevron",
            target: "q0_q1",
            parameters: { "cz.duration": 1.2e-7 },
            timestamp: "2026-08-06T12:01:10.000Z",
            duration_s: 90,
          },
        ],
      }),
    );
}

describe("Calibration fit plot", () => {
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

  it("draws both traces with ticked, labelled axes", () => {
    seedRun(FIT);
    cy.visit("/#calibration");

    cy.get('[data-testid="calibration-node-rabi"]').click();
    cy.get('[data-testid="calibration-fit-plot"]')
      .should("be.visible")
      .and("contain", "pulse amplitude")
      .and("contain", "signal")
      // The legend, which is what says which trace is which.
      .and("contain", "measured")
      .and("contain", "fit");
    // A point per setpoint, and one path for the fitted curve.
    cy.get('[data-testid="calibration-fit-plot"] circle').should(
      "have.length.at.least",
      41,
    );
    cy.get(
      '[data-testid="calibration-fit-plot"] path[stroke-width="1.5"]',
    ).should("exist");
  });

  it("reads out the point under the cursor", () => {
    seedRun(FIT);
    cy.visit("/#calibration");

    cy.get('[data-testid="calibration-node-rabi"]').click();
    cy.get('[data-testid="calibration-fit-readout"]').should("not.exist");
    // `mouseover`, not `mouseenter`: React derives enter and leave from the
    // bubbling pair, so a dispatched `mouseenter` reaches no handler.
    cy.get('[data-testid="calibration-fit-plot"] circle')
      .eq(4)
      .trigger("mouseover");
    cy.get('[data-testid="calibration-fit-readout"]').should("be.visible");
  });

  it("says the traces are gone rather than drawing an empty chart", () => {
    seedRun({ dropped: true });
    cy.visit("/#calibration");

    cy.get('[data-testid="calibration-node-rabi"]').click();
    cy.get('[data-testid="calibration-fit-dropped"]').should("be.visible");
    cy.get('[data-testid="calibration-fit-plot"]').should("not.exist");
  });

  it("draws nothing for a routine whose analyse reports no summary", () => {
    seedRun(FIT);
    cy.visit("/#calibration");

    cy.get('[data-testid="calibration-node-cz_chevron"]').click();
    cy.get('[data-testid="calibration-node-card"]').should(
      "contain",
      "cz.duration",
    );
    cy.get('[data-testid="calibration-fit-plot"]').should("not.exist");
    cy.get('[data-testid="calibration-fit-dropped"]').should("not.exist");
  });
});
