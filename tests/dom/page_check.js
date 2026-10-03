// Loads the REAL page from a running server in jsdom (a simulated browser), runs the page's own JavaScript,
// then does what a person would: look for the Approve button, click it, click Run procurement, change scenario.
// Prints what was visible at each step as JSON. Used by tests/test_page_in_browser.py.
//
// jsdom does not do layout or CSS, so this proves the page's logic and DOM state (what is shown or hidden,
// what the buttons do). It cannot show how the page LOOKS.
const { JSDOM, VirtualConsole } = require("./node_modules/jsdom");

const base = process.argv[2];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(fn, what, ms = 15000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    try {
      if (fn()) return;
    } catch (e) {
      /* keep waiting */
    }
    await sleep(50);
  }
  throw new Error("Timed out waiting for: " + what);
}

(async () => {
  const errors = [];
  const html = await (await fetch(base + "/")).text();
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (e) => errors.push("jsdomError: " + (e.detail || e.message || e)));
  virtualConsole.on("error", (e) => errors.push("console.error: " + e));
  const dom = new JSDOM(html, { url: base + "/", runScripts: "outside-only", pretendToBeVisual: true, virtualConsole });
  const w = dom.window;
  const d = w.document;
  w.fetch = (u, o) => fetch(new URL(u, base), o);
  w.Element.prototype.scrollIntoView = function () {};   // jsdom has no layout
  w.addEventListener("error", (e) => errors.push("window error: " + e.message));
  w.addEventListener("unhandledrejection", (e) => errors.push("unhandled rejection: " + e.reason));

  // run the page's scripts in order, exactly as the browser would
  for (const m of html.matchAll(/<script src="([^"]+)"><\/script>/g)) {
    const src = await (await fetch(new URL(m[1], base))).text();
    w.eval(src);
  }

  // an element is visible only if neither it nor any ancestor carries the `hidden` attribute
  const visible = (el) => {
    for (let n = el; n; n = n.parentElement) if (n.hidden) return false;
    return Boolean(el);
  };
  const q = (s) => d.querySelector(s);
  const text = (s) => (q(s) ? q(s).textContent.replace(/\s+/g, " ").trim() : null);

  const snap = (label) => ({
    label,
    phase: q("#pipeline-status").dataset.phase,
    inventoryRows: d.querySelectorAll('[data-field="inventory"] tbody tr').length,
    inventoryVisible: visible(q('[data-bind="inventory_table"]')),
    firstRow: text('[data-field="inventory"] tbody tr'),
    runButtonSkus: [...d.querySelectorAll('[data-action="run-item"]')].map((b) => b.closest("tr").dataset.sku),
    atRiskSkus: [...d.querySelectorAll('[data-field="inventory"] tbody tr')].filter((r) => r.dataset.status !== "healthy").map((r) => r.dataset.sku),
    viewButtonSkus: [...d.querySelectorAll('[data-action="view-run"]')].map((b) => b.closest("tr").dataset.sku),
    viewingSku: q("tr.is-viewing") ? q("tr.is-viewing").dataset.sku : null,
    runButtonsDisabled: [...d.querySelectorAll('[data-action="run-item"], #btn-run')].some((b) => b.disabled),
    cardItem: text(".ac-item"),
    cardPresent: Boolean(q(".action-card")),
    cardVisible: visible(q(".action-card")),
    emptyRecommendationVisible: visible(q('.opportunity [data-state="recommendation_empty"]')),
    approveVisible: visible(q("#btn-approve")),
    approveDisabled: q("#btn-approve") ? q("#btn-approve").disabled : null,
    rejectVisible: visible(q("#btn-reject")),
    cardStatus: text(".ac-status"),
    panelBadge: text('[data-bind="recommendation.status_badge"]'),
    panelBadgeVisible: visible(q('[data-bind="recommendation.status_badge"]')),
    stages: d.querySelectorAll(".stage").length,
    stagesDone: d.querySelectorAll(".stage--done").length,
    stagesVisible: visible(q(".stages")),
    rfqVisible: visible(q(".rfq")),
    rfqBodyStart: q(".rfq__body") ? q(".rfq__body").textContent.slice(0, 40) : null,
    supplierRows: d.querySelectorAll(".supplier-row").length,
    supplierListVisible: visible(q(".supplier-list")),
    marketBanner: text('[data-slot="market-banner"]'),
    kpiRisks: text('[data-bind="kpis.stock_risks"]'),
    kpiPending: text('[data-bind="kpis.pending_approvals"]'),
    kpiSavings: text('[data-bind="kpis.potential_savings"]'),
    scenarioLabel: text('[data-bind="scenario_name"]'),
    timelineItems: d.querySelectorAll(".timeline-item").length,
    toasts: [...d.querySelectorAll(".toast")].map((t) => t.textContent),
  });

  const out = { steps: [], errors };

  // 1. the page loads and shows the run that already exists
  await waitFor(() => q("#pipeline-status").dataset.phase !== "forecast_loading" && q(".action-card"), "the page to load a run");
  out.steps.push(snap("loaded"));

  // 2. click Approve
  q("#btn-approve").click();
  await waitFor(() => q(".ac-status--approved") && text('[data-bind="kpis.pending_approvals"]') === "0", "the card to show RFQ approved");
  out.steps.push(snap("approved"));

  // 3. click Run procurement: a new run starts and its card appears with buttons again
  q("#btn-run").click();
  await waitFor(() => q(".ac-status--awaiting") && !q("#btn-run").disabled, "a new run to finish", 30000);
  out.steps.push(snap("new run"));

  // 4. Reject that new run
  q("#btn-reject").click();
  await waitFor(() => q(".ac-status--rejected") && text('[data-bind="kpis.pending_approvals"]') === "0", "the card to show Rejected");
  out.steps.push(snap("rejected"));

  // 5. change scenario
  q('[data-scenario="avocado_shortage"]').click();
  await waitFor(() => q('[data-bind="scenario_name"]').textContent === "Avocado shortage" && q('[data-field="inventory"] tbody tr'), "the scenario to apply");
  await sleep(300);
  out.steps.push(snap("scenario"));

  // 6. reset
  q('[data-scenario="reset"]').click();
  await waitFor(() => q('[data-bind="scenario_name"]').textContent === "Default", "the scenario reset");
  await sleep(300);
  out.steps.push(snap("reset"));

  // 7. a scenario with several at-risk items: every at-risk row has a Run button, healthy rows have none
  q('[data-scenario="supplier_delay"]').click();
  await waitFor(() => q('[data-bind="scenario_name"]').textContent === "Supplier delay" && q('[data-action="run-item"]'), "supplier_delay to apply");
  await sleep(300);
  out.steps.push(snap("many at risk"));

  // 8. run the agent for a row that is NOT the most urgent one
  q('tr[data-sku="AVO-001"] [data-action="run-item"]').click();
  await waitFor(() => (text(".ac-item") || "").includes("Avocados") && q(".ac-status--awaiting") && !q("#btn-run").disabled, "the avocado run to finish", 30000);
  out.steps.push(snap("ran avocados"));

  // 9. View a different item's earlier run: no new run is created
  const before = d.querySelectorAll('[data-action="view-run"]').length;
  q('tr[data-sku="MOZ-001"] [data-action="view-run"]').click();
  await waitFor(() => (text(".ac-item") || "").includes("Mozzarella") && q(".ac-status--rejected"), "the mozzarella card to be shown again");
  out.steps.push({ ...snap("viewed mozzarella"), viewButtonsBefore: before });

  // 10. put the scenario back, so a later test does not start from supplier_delay
  q('[data-scenario="reset"]').click();
  await waitFor(() => q('[data-bind="scenario_name"]').textContent === "Default", "the final scenario reset");
  await sleep(200);

  process.stdout.write(JSON.stringify(out));
  process.exit(0);
})().catch((e) => {
  process.stdout.write(JSON.stringify({ fatal: String(e && e.stack ? e.stack : e), steps: [], errors: [] }));
  process.exit(0);
});
