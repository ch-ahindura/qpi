describe("QPU Registry — Regular User View", () => {
  beforeEach(() => {
    cy.clearCookies();
    cy.clearLocalStorage();
    cy.visit("/");

    // Log in as regular user
    cy.get('input[type="text"]').clear().type("user@example.com");
    cy.get('input[type="password"]').clear().type("userpassword1234");
    cy.get('button[type="submit"]').click();
    cy.contains("h1", "QPI Interface").should("be.visible");

    // Navigate to QPU Registry
    cy.contains("button", "QPU Registry").click();
    cy.contains("h1", "QPU Registry").should("be.visible");
  });

  it("does NOT show the 'Register QPU' button", () => {
    cy.contains("button", "Register QPU").should("not.exist");
  });

  it("does NOT show the Service toggle", () => {
    cy.contains("span", "Service").should("not.exist");
    cy.contains("button", "In service").should("not.exist");
    cy.contains("button", "Switched off").should("not.exist");
  });

  it("does NOT show the maintenance toggle", () => {
    cy.get('[data-testid="qpu-maintenance-toggle"]').should("not.exist");
  });

  it("shows QPU cards with status and executor info", () => {
    // QPU cards should be visible with basic info
    cy.get("h3").should("have.length.at.least", 1);

    // Each card shows status badge
    cy.contains("span", "offline").should("exist");

    // Each card shows NNG ports
    cy.contains("span", "NNG Ports (Cmd/Res)").should("exist");
  });
});
