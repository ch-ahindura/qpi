/**
 * The in-flight banner and progress bar (RFC 0004 §6.8, RFC 0006 §10).
 *
 * Both shipped untested, because nothing could put a `running` calibration in
 * front of the dashboard: a real one takes hours. `cy.seedCalibration` writes the
 * row a driver mid-walk would have left.
 */
describe("Calibration in flight", () => {
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

  it("says a run started by the driver is in progress, and who started it", () => {
    cy.seedCalibration();
    cy.visit("/#calibration");
    cy.get('[data-testid="calibration-in-flight"]')
      .should("be.visible")
      .and("contain", "full calibration in progress")
      .and("contain", "the driver's own drift monitoring");
  });

  it("shows nothing but the banner until the first routine finishes", () => {
    cy.seedCalibration();
    cy.visit("/#calibration");
    cy.get('[data-testid="calibration-in-flight"]').should("be.visible");
    cy.get('[data-testid="calibration-progress"]').should("not.exist");
  });

  it("reports the position, the tally and the elapsed time", () => {
    cy.seedCalibration({
      progress: {
        mode: "full",
        step: 7,
        total: 33,
        routine: "rabi",
        target: "q2",
        succeeded: 18,
        failed: 2,
        elapsed_s: 1830,
      },
    });
    cy.visit("/#calibration");
    cy.get('[data-testid="calibration-progress"]')
      .should("contain", "step 7 of 33")
      .and("contain", "rabi")
      .and("contain", "q2")
      .and("contain", "18 ok")
      .and("contain", "2 failed")
      .and("contain", "30.5m");
  });

  it("says queued rather than in progress while the driver has not picked it up", () => {
    cy.seedCalibration({ status: "pending", trigger: "dispatched" });
    cy.visit("/#calibration");
    cy.get('[data-testid="calibration-in-flight"]').should(
      "contain",
      "queued, waiting for the driver",
    );
  });
});
