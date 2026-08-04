/**
 * Legacy end-to-end feature flows.
 *
 * Auth, routing, and role-based navigation are now covered in:
 *   - cypress/e2e/auth/login.cy.ts
 *   - cypress/e2e/auth/role-based-nav.cy.ts
 *   - cypress/e2e/routing/hash-router.cy.ts
 *
 * This file retains the high-level user journeys (job submission, bookings,
 * QPU registration, admin broadcasts) that exercise the full stack.
 */
describe("QPI Dashboard — End-to-End User Journeys", () => {
  beforeEach(() => {
    cy.clearCookies();
    cy.clearLocalStorage();
    cy.visit("/");
  });

  it("regular user: submits a job and books a time slot", () => {
    // Log in
    cy.get('input[type="text"]').clear().type("user@example.com");
    cy.get('input[type="password"]').clear().type("userpassword1234");
    cy.get('button[type="submit"]').click();
    cy.contains("h1", "QPI Interface").should("be.visible");

    // Navigate to Jobs Console & Submit mock job
    cy.contains("button", "Jobs Console").click();
    cy.contains("h1", "Jobs Console").should("be.visible");

    // Verify footer is NOT visible for regular users
    cy.get('[data-testid="admin-footer"]').should("not.exist");

    // Select first online QPU (should be set by default, but let's check its presence)
    cy.get("select", { timeout: 15000 }).should("be.visible").and("not.have.value", "");

    // Execute job
    cy.contains("button", "Execute Job").click();

    // Wait for completion (status becomes completed, results show)
    cy.contains("div", "completed", { timeout: 15000 }).should("be.visible");

    // Verify duration is displayed after completion
    cy.contains("span", "Duration")
      .parent()
      .find("span.font-mono")
      .should("be.visible")
      .and("contain", "s");

    // Navigate to Bookings & Book a slot
    cy.contains("button", "Bookings").click();
    cy.contains("h1", "Bookings").should("be.visible");
    cy.contains("button", "Book Time Slot").click();

    // Fill dates: start in 10 minutes, end in 20 minutes
    const pad = (n: number) => n.toString().padStart(2, "0");
    const start = new Date(Date.now() + 120 * 60000);
    const end = new Date(Date.now() + 130 * 60000);

    const startStr = `${start.getFullYear()}-${pad(start.getMonth() + 1)}-${pad(start.getDate())}T${pad(start.getHours())}:${pad(start.getMinutes())}`;
    const endStr = `${end.getFullYear()}-${pad(end.getMonth() + 1)}-${pad(end.getDate())}T${pad(end.getHours())}:${pad(end.getMinutes())}`;

    cy.get('input[type="datetime-local"]').first().type(startStr);
    cy.get('input[type="datetime-local"]').last().type(endStr);
    cy.get("form").submit();

    // Slot should now show up in the scheduled list
    cy.contains("td", "user@example.com").should("be.visible");

    // Clean up to prevent overlapping booking errors on subsequent local runs
    cy.on("window:confirm", () => true);
    cy.get('button svg.lucide-trash-2').last().parent().click();

    // Logout
    cy.contains("button", "Profile Settings").click();
    cy.contains("button", "Sign Out").click();
    cy.get('[data-testid="login-modal"]').should("be.visible");
  });

  it("admin: registers a QPU, toggles it, and broadcasts an announcement", () => {
    // Log in as administrator
    cy.contains("button", "Administrator").click();
    cy.get('input[type="text"]').clear().type("admin@example.com");
    cy.get('input[type="password"]').clear().type("supersecretpassword1234");
    cy.get('button[type="submit"]').click();

    cy.contains("h1", "QPI Interface").should("be.visible");
    cy.contains("button", "Admin Panel").should("be.visible");

    // Verify footer IS visible and shows version for admins
    cy.get('[data-testid="admin-footer"]')
      .scrollIntoView()
      .should("be.visible")
      .and("contain", "v0.");

    const qpuName = `cypress-test-qpu-${Date.now()}`;

    // Register a new QPU on QPU Registry
    cy.contains("button", "QPU Registry").click();
    cy.contains("button", "Register QPU").click();
    cy.get('input[placeholder="rigetti-aspen-9"]').clear().type(qpuName);
    cy.contains("button", "Register Unit").click();

    // Modal is closed
    cy.contains("h3", "Register QPU").should("not.exist");

    // Verify the QPU is now listed in the grid
    cy.contains("h3", qpuName).should("be.visible");

    // Toggle QPU state on QPU Registry
    cy.contains("span", "Service").should("be.visible");
    
    // The newly created QPU defaults to enabled: true in the API
    cy.contains(".bg-white", qpuName)
      .contains("button", "In service")
      .click();
    // Verify it becomes switched off
    cy.contains(".bg-white", qpuName)
      .contains("button", "Switched off")
      .should("be.visible");
    
    // Toggle it back online
    cy.contains(".bg-white", qpuName)
      .contains("button", "Switched off")
      .click();
    cy.contains(".bg-white", qpuName)
      .contains("button", "In service")
      .should("be.visible");

    // Compose and post broadcast announcement on Admin Panel
    cy.contains("button", "Admin Panel").click();
    cy.contains("button", "Broadcast Announcement").click();
    cy.get('input[placeholder="QPU Maintenance Schedule"]').type(
      "Test Cypress Broadcast Title",
    );
    cy.get(
      'textarea[placeholder="Rigetti Aspen-9 will be offline for calibration tomorrow..."]',
    ).clear().type("Test Cypress Broadcast Description");
    cy.get('form button[type="submit"]').click();
    cy.contains("Announcement broadcasted successfully!").should("be.visible");

    // Verify broadcast is visible in header dropdown
    cy.get("svg.lucide-bell").parent().click();
    cy.contains("Test Cypress Broadcast Title").should("be.visible");

    // Sign Out
    cy.contains("button", "Profile Settings").click();
    cy.contains("button", "Sign Out").click();
    cy.get('[data-testid="login-modal"]').should("be.visible");
  });
});
