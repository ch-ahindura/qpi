describe("QPU Registry — Admin: maintenance", () => {
  beforeEach(() => {
    cy.clearCookies();
    cy.clearLocalStorage();
    cy.visit("/");

    cy.contains("button", "Administrator").click();
    cy.get('input[type="text"]').clear().type("admin@example.com");
    cy.get('input[type="password"]').clear().type("supersecretpassword1234");
    cy.get('button[type="submit"]').click();
    cy.contains("h1", "QPI Interface").should("be.visible");

    cy.contains("button", "QPU Registry").click();
    cy.contains("h1", "QPU Registry").should("be.visible");
  });

  afterEach(() => {
    // Leave the QPU in service; every other spec assumes a QPU that takes jobs.
    cy.get('[data-testid="qpu-maintenance-toggle"]')
      .first()
      .then(($button) => {
        if ($button.text().includes("Under maintenance")) {
          cy.wrap($button).click();
        }
      });
  });

  it("puts a QPU under maintenance and takes it back out", () => {
    cy.get('[data-testid="qpu-maintenance-toggle"]')
      .first()
      .should("contain.text", "Maintenance")
      .click();

    cy.get('[data-testid="qpu-maintenance-toggle"]')
      .first()
      .should("contain.text", "Under maintenance");
    cy.contains("maintenance").should("be.visible");

    cy.get('[data-testid="qpu-maintenance-toggle"]').first().click();
    cy.get('[data-testid="qpu-maintenance-toggle"]')
      .first()
      .should("contain.text", "Maintenance")
      .should("not.contain.text", "Under maintenance");
  });

  // The point of the state: an idle queue on a QPU nobody switched off used to
  // explain nothing.
  it("says why the QPU is not taking jobs, and stops saying it afterwards", () => {
    cy.get('[data-testid="qpu-maintenance-toggle"]').first().click();

    cy.get('[data-testid="qpu-unavailable-reason"]')
      .should("be.visible")
      .and("contain.text", "under maintenance");

    cy.get('[data-testid="qpu-maintenance-toggle"]').first().click();
    cy.get('[data-testid="qpu-unavailable-reason"]').should("not.exist");
  });

});
