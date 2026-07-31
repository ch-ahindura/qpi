/**
 * What each measurement level actually renders, against the simulated chip.
 *
 * The other job-console specs check that a job submits and that the results
 * panel names the right QPU — they pass whether or not the numbers in it mean
 * anything. These assert the numbers, which is only possible because the e2e
 * driver runs `-o is_simulated=true`: a mock returns a fixed answer and a dummy
 * cluster returns nan, and neither can show one level behaving differently from
 * another.
 *
 * The form's default circuit is a Bell state — `h q[0]; cx q[0], q[1];` — so
 * level 2 is a real end-to-end check of the two-qubit gate: the CZ the CNOT
 * decomposes to has to work through the compiler, the simulator and the readout
 * for the histogram to come out correlated.
 */

const COMPLETED = { timeout: 30000 };

/**
 * Move the Meas Level slider, which is a range input from 0 to 2.
 *
 * Through the native value setter rather than `.invoke("val")`: React keeps its
 * own record of an input's value and ignores a change it did not see, so
 * setting the property directly moves the thumb and leaves the component still
 * believing it is on the old level. Every job would then run at the default,
 * and these tests would all quietly measure the same thing.
 */
function setMeasLevel(level: number) {
  cy.get('input[type="range"]').then(($input) => {
    const element = $input[0] as HTMLInputElement;
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype,
      "value",
    )?.set;
    setter?.call(element, String(level));
    element.dispatchEvent(new Event("input", { bubbles: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
  });
  // The label mirrors the level, so this both waits for React and proves the
  // change landed.
  cy.get('input[type="range"]').should("have.value", String(level));
}

function runJob() {
  cy.contains("button", "Execute Job").click();
  cy.contains("div", "completed", COMPLETED).should("be.visible");
}

/**
 * A one-qubit circuit, for the raw-trace level.
 *
 * Not a stylistic choice: a Qblox module can put only one sequencer into scope
 * mode, so raw trace capture on two qubits at once does not compile —
 * "Only one sequencer per device can trigger raw trace capture". The form's
 * default circuit measures both, so level 0 has to be given something narrower
 * or the job fails before it runs.
 */
const SINGLE_QUBIT =
  'OPENQASM 3.0;\ninclude "stdgates.inc";\nqubit[1] q;\nbit[1] c;\nx q[0];\nc[0] = measure q[0];';

function setCircuit(qasm: string) {
  cy.get("textarea").first().clear().type(qasm, { parseSpecialCharSequences: false });
}

describe("Jobs Console — results at every measurement level", () => {
  beforeEach(() => {
    cy.clearCookies();
    cy.clearLocalStorage();
    cy.visit("/");

    cy.get('input[type="text"]').clear().type("user@example.com");
    cy.get('input[type="password"]').clear().type("userpassword1234");
    cy.get('button[type="submit"]').click();
    cy.contains("h1", "QPI Interface").should("be.visible");

    cy.contains("button", "Jobs Console").click();
    cy.contains("h1", "Jobs Console").should("be.visible");
  });

  it("level 2 shows a Bell state's two correlated outcomes", () => {
    setMeasLevel(2);
    runJob();

    cy.contains("button", "Counts Histogram").click();

    // The default circuit entangles the pair, so the weight has to sit on the
    // two aligned outcomes. On the *weight*, not on which bars exist: a real
    // Bell state still puts a handful of shots in |01> and |10> — readout error
    // and decay during the measurement — and the histogram draws a bar for
    // each. A CZ that did nothing would spread across all four evenly; one with
    // the wrong phase would pile onto the other two.
    cy.get("div.group").then(($bars) => {
      const bins: Record<string, number> = {};
      [...$bars].forEach((bar) => {
        const spans = bar.querySelectorAll("span");
        if (spans.length < 2) return;
        const count = Number(spans[0].textContent?.trim());
        const key = spans[spans.length - 1].textContent?.trim();
        if (key && Number.isFinite(count)) bins[key] = count;
      });

      const total = Object.values(bins).reduce((a, b) => a + b, 0);
      expect(total, `no histogram bins found: ${JSON.stringify(bins)}`).to.be
        .greaterThan(0);
      const aligned = ((bins["00"] ?? 0) + (bins["11"] ?? 0)) / total;
      expect(
        aligned,
        `expected |00> and |11> to dominate, got ${JSON.stringify(bins)}`,
      ).to.be.greaterThan(0.85);
    });
  });

  it("level 1 shows a cluster of IQ points, not a single one", () => {
    setMeasLevel(1);
    runJob();

    cy.contains("button", "IQ Clusters").click();

    // One point per shot. The tab used to be able to render exactly one — and
    // separately, a hardcoded axis range put every point off-screen — so the
    // check is that there are *many* and that they are inside the viewBox.
    cy.get("svg circle").should("have.length.greaterThan", 50);
    cy.get("svg circle").each(($circle) => {
      const cx = Number($circle.attr("cx"));
      const cy_ = Number($circle.attr("cy"));
      expect(cx, "point is inside the plot horizontally").to.be.within(0, 200);
      expect(cy_, "point is inside the plot vertically").to.be.within(0, 200);
    });
  });

  it("level 0 shows a raw trace with many samples", () => {
    setCircuit(SINGLE_QUBIT);
    setMeasLevel(0);
    runJob();

    cy.contains("button", "Raw Trace").click();

    // A Trace is a time series. This rendered a single point for as long as the
    // simulator returned one integrated value for it, which looked like an
    // empty plot rather than a wrong one.
    cy.get("svg path[stroke]")
      .should("have.attr", "d")
      .then((d) => {
        const segments = String(d).trim().split(/(?=[ML])/).length;
        expect(
          segments,
          "the trace should be a curve over many samples, not one point",
        ).to.be.greaterThan(20);
      });
  });

  it("the IQ tab says what to do when the level cannot produce it", () => {
    // Level 2 returns discriminated bits, so there is no IQ memory to plot.
    // Saying so beats an empty box.
    setMeasLevel(2);
    runJob();

    cy.contains("button", "IQ Clusters").click();
    cy.contains("Must submit with Meas Level = 1").should("be.visible");
  });
});
