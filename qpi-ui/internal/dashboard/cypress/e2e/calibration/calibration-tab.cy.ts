/**
 * The Calibration tab (RFC 0004 §6.8).
 *
 * The seeded environment has no tuner driver, so what is asserted here is the
 * empty state and the access rule. Both matter: the tab is admin-only because
 * queueing a calibration takes a QPU out of service for hours (RFC 0004 §10),
 * and the empty state is what an operator sees before they have registered a
 * tuner at all.
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
      cy.get('input[type="password"]')
        .clear()
        .type("supersecretpassword1234");
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
