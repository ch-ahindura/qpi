/**
 * The Calibration tab (RFC 0004 §6.8). Admin-only because queueing a calibration
 * takes a QPU out of service for hours (RFC 0004 §10).
 *
 * Order within the file matters: the seeded environment has no tuner, and once
 * the admin context registers one the empty state is gone for the rest of the run.
 */
describe("Calibration Tab", () => {
  beforeEach(() => {
    cy.clearCookies();
    cy.clearLocalStorage();
  });

  context("as an admin", () => {
    beforeEach(() => {
      cy.visit("/");
      cy.contains("button", "Administrator").click();
      cy.get('input[type="text"]').clear().type("admin@example.com");
      cy.get('input[type="password"]').clear().type("supersecretpassword1234");
      cy.get('button[type="submit"]').click();
      cy.contains("h1", "QPI Interface").should("be.visible");
    });

    it("is reachable from the sidebar", () => {
      cy.contains("button", "Calibration").click();
      cy.contains("h1", "Calibration").should("be.visible");
      cy.contains("button", "Calibration").should("have.class", "border-white");
    });

    it("navigates via hash", () => {
      cy.visit("/#calibration");
      cy.contains("h1", "Calibration").should("be.visible");
    });

    it("explains what to do when no tuner is registered", () => {
      cy.visit("/#calibration");
      cy.contains("No tuner drivers registered yet").should("be.visible");
      cy.contains("quantify_tuner").should("be.visible");
      // With no tuner there is nothing to calibrate, so the button is absent
      // rather than present and broken.
      cy.get('[data-testid="calibration-trigger-button"]').should("not.exist");
    });

    it("offers the tuner kinds when registering a driver", () => {
      cy.visit("/#drivers");
      cy.contains("button", "Register Driver").click();
      cy.get('[data-testid="driver-kind-select"]')
        .find("option")
        .then((options) => {
          const values = [...options].map((o) => o.getAttribute("value"));
          expect(values).to.include("quantify_tuner");
          expect(values).to.include("qblox_tuner");
        });
    });

    // Offering the kind and accepting it are different claims — the spec above
    // only reads the dropdown.
    it("registers a tuner, and the tab then offers to calibrate", () => {
      cy.visit("/#drivers");
      cy.contains("button", "Register Driver").click();

      const tunerName = `cypress-tuner-${Date.now()}`;
      cy.get('input[placeholder="cryostat-monitor-1"]').type(tunerName);
      cy.get('[data-testid="driver-qpu-select"]').select("qpu_sim_01");
      cy.get('[data-testid="driver-kind-select"]').select("quantify_tuner");
      cy.get('[data-testid="driver-language-select"]').select("python");
      cy.get("form").contains("button", "Register Driver").click();

      cy.contains("h3", "Driver Registered Successfully!").should("be.visible");

      // Registering is enough; a tuner need not have connected to be queued for.
      cy.visit("/#calibration");
      cy.contains("No tuner drivers registered yet").should("not.exist");
      cy.get('[data-testid="calibration-trigger-button"]').should("be.visible");
      cy.get('[data-testid="calibration-driver-select"]').should(
        "contain",
        tunerName,
      );
    });
  });

  context("as a regular user", () => {
    beforeEach(() => {
      cy.visit("/");
      cy.get('input[type="text"]').clear().type("user@example.com");
      cy.get('input[type="password"]').clear().type("userpassword1234");
      cy.get('button[type="submit"]').click();
      cy.contains("h1", "QPI Interface").should("be.visible");
    });

    it("is not in the sidebar", () => {
      cy.contains("button", "Calibration").should("not.exist");
    });

    it("denies access via hash", () => {
      cy.visit("/#calibration");
      cy.contains("Access Denied").should("be.visible");
    });
  });
});
