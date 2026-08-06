/* eslint-disable @typescript-eslint/no-namespace */
/// <reference types="cypress" />

/** The tuner `seedCalibration` runs its calibrations on. Named so `resetDb` can
 * remove it again: the Calibration tab's empty state is only reachable while no
 * tuner is registered, and Cypress runs specs in whatever order it likes. */
const SEEDED_TUNER = "cypress-seeded-tuner";

const PB = "http://127.0.0.1:8090";

interface SeededCalibration {
  driverId: string;
  requestId: string;
}

declare namespace Cypress {
  interface Chainable {
    resetDb(): Chainable<void>;
    /** A `running` calibration on a tuner of its own, as a driver mid-walk would
     * leave it. *fields* is merged over the row, so a spec can supply its own
     * `progress`, `plan` or `mode`. */
    seedCalibration(
      fields?: Record<string, unknown>,
    ): Chainable<SeededCalibration>;
    /** The report a run produced, on *driverId*. Timestamped now unless told
     * otherwise, so it lands after the request it answers — which is how the two are
     * matched, a report carrying no job id. Seed it *after* the request. */
    seedCalibrationReport(
      driverId: string,
      fields?: Record<string, unknown>,
    ): Chainable<string>;
  }
}

/** A superuser token. Every seeded write needs one: the calibration collections
 * are read-only to everyone else. */
function asSuperuser(): Cypress.Chainable<string> {
  return cy
    .request({
      method: "POST",
      url: `${PB}/api/collections/_superusers/auth-with-password`,
      body: {
        identity: "admin@example.com",
        password: "supersecretpassword1234",
      },
      log: false,
    })
    .then((resp) => resp.body.token as string);
}

function deleteAll(token: string, col: string, filter?: string) {
  const query = filter ? `&filter=${encodeURIComponent(filter)}` : "";
  cy.request({
    method: "GET",
    url: `${PB}/api/collections/${col}/records?perPage=500${query}`,
    headers: { Authorization: token },
    log: false,
  }).then((res) => {
    const items = res.body.items || [];
    items.forEach((item: { id: string }) => {
      cy.request({
        method: "DELETE",
        url: `${PB}/api/collections/${col}/records/${item.id}`,
        headers: { Authorization: token },
        log: false,
      });
    });
  });
}

Cypress.Commands.add("resetDb", () => {
  asSuperuser().then((token) => {
    [
      "notifications",
      "qpu_time_requests",
      "calibration_requests",
      "calibration_results",
    ].forEach((col) => deleteAll(token, col));
    deleteAll(token, "drivers", `name = "${SEEDED_TUNER}"`);
  });
});

Cypress.Commands.add("seedCalibration", (fields = {}) => {
  return asSuperuser().then((token) => {
    const headers = { Authorization: token };
    return cy
      .request({
        method: "GET",
        url: `${PB}/api/collections/qpus/records?perPage=1`,
        headers,
        log: false,
      })
      .then((qpus) => qpus.body.items[0].id as string)
      .then((qpuId) =>
        cy
          .request({
            method: "POST",
            url: `${PB}/api/op/drivers/create`,
            headers,
            body: {
              name: SEEDED_TUNER,
              qpu: qpuId,
              kind: "quantify_tuner",
              language: "python",
            },
          })
          .then((driver) => ({ qpuId, driverId: driver.body.id as string })),
      )
      .then(({ qpuId, driverId }) =>
        cy
          .request({
            method: "POST",
            url: `${PB}/api/collections/calibration_requests/records`,
            headers,
            body: {
              driver: driverId,
              qpu: qpuId,
              mode: "full",
              status: "running",
              trigger: "drift",
              job_id: `cypress_${Date.now()}`,
              ...fields,
            },
          })
          .then((request) => ({
            driverId,
            qpuId,
            requestId: request.body.id as string,
          })),
      );
  });
});

Cypress.Commands.add("seedCalibrationReport", (driverId, fields = {}) => {
  return asSuperuser().then((token) =>
    cy
      .request({
        method: "POST",
        url: `${PB}/api/collections/calibration_results/records`,
        headers: { Authorization: token },
        body: {
          driver: driverId,
          timestamp: new Date().toISOString(),
          duration_s: 100,
          mode: "full",
          backend: "cypress",
          status: "success",
          routine_results: [],
          benchmarks: [],
          errors: [],
          ...fields,
        },
      })
      .then((result) => result.body.id as string),
  );
});
