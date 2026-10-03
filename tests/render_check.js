// Test helper (run by tests/test_frontend_render.py). Reads {views, forecast, hostile} as JSON on stdin,
// runs the REAL app/static/js/render.js over it, and prints the HTML it produces as JSON.
const path = require("path");
const R = require(path.join(__dirname, "..", "app", "static", "js", "render.js"));

let input = "";
process.stdin.on("data", (c) => (input += c));
process.stdin.on("end", () => {
  const { views, forecast, hostile } = JSON.parse(input);
  const fmt = (iso) => "T:" + iso.slice(11, 16);
  const out = {
    views: {},
    forecast: R.inventoryRowsHtml(forecast.items),
    inventoryWithRuns: R.inventoryRowsHtml(
      forecast.items,
      { [forecast.items[0].item.id]: { id: 7, status: "awaiting_approval" }, [forecast.items[forecast.items.length - 1].item.id]: { id: 8, status: "approved" } },
      forecast.items[0].item.id),
    latestRuns: R.latestRunsByItem([
      { id: 9, status: "rejected", item: { id: 1 } }, { id: 8, status: "approved", item: { id: 1 } },
      { id: 7, status: "failed", item: { id: 2 } }, { id: 6, status: "approved", item: null }, null]),
    kpis: R.kpiValues(forecast.items, [{ status: "awaiting_approval" }, { status: "approved" }, { status: "failed" }]),
  };
  for (const [name, v] of Object.entries(views)) {
    const rec = v.recommendation;
    out.views[name] = {
      card: R.actionCardHtml(v),
      rows: rec ? R.supplierRowsHtml(rec.compared_options, v.item.unit) : "",
      banner: R.marketBannerHtml(v.market_search),
      leads: R.leadsHtml(v.market_results),
      timeline: R.timelineHtml(v.events, fmt),
      stages: R.stagesHtml(v.stages, fmt),
      phase: R.phaseFromRun(v),
    };
  }
  const h = hostile;
  out.hostile = {
    card: R.actionCardHtml(h),
    rows: R.supplierRowsHtml(h.recommendation.compared_options, "kg"),
    leads: R.leadsHtml(h.market_results),
    banner: R.marketBannerHtml(h.market_search),
    timeline: R.timelineHtml(h.events, fmt),
    stages: R.stagesHtml(h.stages, fmt),
    rfq: R.rfqHtml(h.rfq, h),
  };
  const sav = (amount, percent) => R.savingBannerHtml({ amount, percent, note: "n" });
  out.helpers = {
    money: [R.money(12.5), R.money(-29.4), R.money(1234.5), R.money(null), R.money("x")],
    safe: ["https://a.example/x?y=1", "http://a.example", "javascript:alert(1)", "data:text/html,<b>", "//evil.example", "not a url", null, "ftp://a.example"].map((u) => R.safeUrl(u)),
    link: R.link("javascript:alert(1)", "<b>click</b>"),
    unknownLeadRow: R.supplierRowsHtml([{ option_id: "x", supplier_name: "S", origin: "market_search", status: "viable", why: "w",
      price_per_unit: 5, estimated_total_landed_cost: 100, order_quantity: 1, lead_time_days: null, known_fields: [],
      unknown_fields: ["lead_time"], caveats: [], is_incumbent: false }], "kg"),
    chips: R.fieldChips(["price"], ["moq", "lead_time"]),
    noChips: R.fieldChips([], []),
    dates: [R.prettyDate("2026-10-04"), R.prettyDate("2026-01-01"), R.prettyDate("garbage"), R.prettyDate(null),
      R.prettyDateTime("2026-10-03T22:41:07+00:00"), R.prettyDateTime(null)],
    emails: ["a@b.example", "orders@quickstock.example", "no-at-sign", "a b@c.example", 'x"y@c.example', "<s>@c.example", null, "a@b"].map((e) => R.validEmail(e)),
    saving: { save: sav(20, 5), cost: sav(-18.2, -4.6), even: sav(0, 0), none: R.savingBannerHtml(null), nopct: R.savingBannerHtml({ amount: 5, percent: null, note: "n" }) },
    chip: ["awaiting_approval", "approved", "rejected", "weird"].map((s) => R.statusChipHtml(s)),
    noCard: [R.actionCardHtml({}), R.actionCardHtml(null), R.actionCardHtml({ recommendation: null, requirement: {} })],
    stagesEmpty: [R.stagesHtml([], fmt), R.stagesHtml(null, fmt)],
    phases: [
      R.phaseFromRun(null),
      R.phaseFromRun({ status: "running", events: [] }),
      R.phaseFromRun({ status: "running", events: [{ title: "ZooWork session started" }] }),
      R.phaseFromRun({ status: "running", events: [{ title: "Agent: get_existing_supplier_options" }] }),
      R.phaseFromRun({ status: "running", events: [{ title: "Agent: get_existing_supplier_options" }, { title: "Tavily search complete" }] }),
      R.phaseFromRun({ status: "running", events: [{ title: "Live market search unavailable" }] }),
      R.phaseFromRun({ status: "awaiting_approval", events: [] }),
      R.phaseFromRun({ status: "approved", events: [] }),
      R.phaseFromRun({ status: "rejected", events: [] }),
      R.phaseFromRun({ status: "failed", events: [] }),
    ],
  };
  process.stdout.write(JSON.stringify(out));
});
