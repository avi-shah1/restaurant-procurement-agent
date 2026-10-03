/**
 * Dashboard (vanilla JS). Markup is built by render.js (pure functions); this file owns the DOM and the API.
 *
 * Backend routes used:
 *   GET  /api/scenarios, POST /api/scenarios/active {key}, POST /api/scenarios/reset
 *   GET  /api/forecast
 *   POST /api/runs {inventory_item_id}        start the Procurement Manager for one item
 *   GET  /api/runs, GET /api/runs/<id>?after_event_id=N
 *
 *   POST /api/runs/<id>/approve, POST /api/runs/<id>/reject   (a person's decision; see ARCHITECTURE.md)
 *   POST /api/runs/<id>/send                                  (send approved RFQ draft; mock/test by default)
 *
 * Approving does NOT place an order and does NOT send: it records the decision and shows an RFQ
 * draft. Nothing in this page can contact a supplier.
 */
(function () {
  const R = window.ProcurementRender;

  const PHASES = [
    "idle",
    "forecast_loading",
    "procurement_search",
    "zoowork_analysing",
    "tavily_search",
    "recommendation_ready",
  ];

  const PHASE_LABELS = {
    idle: "Waiting for data",
    forecast_loading: "Inventory forecast loading",
    procurement_search: "Procurement search running",
    zoowork_analysing: "ZooWork agent analysing",
    tavily_search: "Tavily market search running",
    recommendation_ready: "Recommendation ready",
  };

  const SCENARIO_LABELS = {
    default: "Default",
    weekend_rush: "Weekend rush",
    coffee_spike: "Coffee spike",
    supplier_delay: "Supplier delay",
    avocado_shortage: "Avocado shortage",
  };

  const toastHost = document.getElementById("toast-host");
  const opportunity = document.querySelector(".opportunity");
  const runBtn = document.getElementById("btn-run");
  const refreshBtn = document.getElementById("btn-refresh");
  const pipeline = document.getElementById("pipeline-status");
  const scenarioSelector = document.getElementById("scenario-selector");
  const scenarioNameEl = document.querySelector('[data-bind="scenario_name"]');
  const demoChip = document.getElementById("demo-chip");

  let activeScenario = "default";
  let running = false;
  let currentPhase = "idle";
  let shownEvents = [];
  let lastForecast = null;     // the forecast the table is drawn from
  let runsByItem = {};         // newest run per item, for the View buttons
  let viewingItemId = null;    // the item whose card is on screen

  function toast(message, tone) {
    if (!toastHost) return;
    const el = document.createElement("div");
    el.className = "toast" + (tone ? " toast--" + tone : "");
    el.textContent = message;
    toastHost.appendChild(el);
    window.setTimeout(() => {
      el.remove();
    }, tone === "error" ? 6000 : 3200);
  }

  function pad(n) {
    return String(n).padStart(2, "0");
  }

  function nowClock() {
    const d = new Date();
    return pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  function timeLabel(iso) {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? "" : pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  function sleep(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
  }

  function setDecisionState(state) {
    if (!opportunity) return;
    opportunity.classList.remove("is-approved", "is-rejected");
    if (state) opportunity.classList.add("is-" + state);
    const decided = Boolean(state);
    document.querySelectorAll("#btn-approve, #btn-reject").forEach((b) => {
      b.disabled = decided;
    });
  }

  function runIdFrom(btn) {
    return (btn && btn.dataset.runId) || (opportunity && opportunity.dataset.runId) || "";
  }

  // ---------- timeline ----------
  function timelineEls() {
    return {
      list: document.querySelector(".timeline"),
      empty: document.querySelector('[data-state="timeline_empty"]'),
    };
  }

  /** A message the page itself wants to show (not from the server). */
  function appendTimelineEvent(event) {
    const { list, empty } = timelineEls();
    if (!list) return;
    list.hidden = false;
    if (empty) empty.hidden = true;

    const li = document.createElement("li");
    li.className = "timeline-item timeline-item--" + (event.tone || "info");
    li.innerHTML =
      '<time class="timeline-time"></time>' +
      '<div class="timeline-body">' +
      '<p class="timeline-title"></p>' +
      '<p class="timeline-detail"></p>' +
      "</div>";
    li.querySelector(".timeline-time").textContent = event.time;
    li.querySelector(".timeline-title").textContent = event.title;
    li.querySelector(".timeline-detail").textContent = event.detail;
    list.appendChild(li);
    li.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function clearTimeline() {
    shownEvents = [];
    const stageSlot = document.querySelector('[data-slot="stages"]');
    if (stageSlot) stageSlot.innerHTML = "";
    const { list, empty } = timelineEls();
    if (list) {
      list.innerHTML = "";
      list.hidden = true;
    }
    if (empty) empty.hidden = false;
  }

  /** The nine run stages (derived by the server from stored data), above the detailed agent log. */
  function renderStages(view) {
    const list = document.querySelector(".timeline");
    const empty = document.querySelector('[data-state="timeline_empty"]');
    if (!list) return;
    const slot = ensureSlot("stages", list, "beforebegin");
    if (!slot) return;
    slot.innerHTML = R.stagesHtml(view.stages, timeLabel) + (view.stages && view.stages.length ? '<p class="log-label">Detailed agent log</p>' : "");
    if (empty && view.stages && view.stages.length) empty.hidden = true;
  }

  /** Server events from the agent run. Appends only the ones not shown yet. */
  function addServerEvents(events) {
    if (!events || !events.length) return;
    const { list, empty } = timelineEls();
    if (!list) return;
    shownEvents = shownEvents.concat(events);
    list.hidden = false;
    if (empty) empty.hidden = true;
    list.insertAdjacentHTML("beforeend", R.timelineHtml(events, timeLabel));
    list.lastElementChild && list.lastElementChild.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  /**
   * Drive empty/loading visibility from ui_phase.
   * Does not invent data: only toggles existing state blocks.
   */
  function setUiPhase(phase) {
    if (!PHASES.includes(phase)) phase = "idle";
    currentPhase = phase;

    if (pipeline) {
      pipeline.dataset.phase = phase;
      const label = pipeline.querySelector('[data-bind="ui_phase_label"]');
      if (label) label.textContent = PHASE_LABELS[phase] || phase;

      const order = PHASES.filter((p) => p !== "idle");
      const activeIdx = order.indexOf(phase);
      pipeline.querySelectorAll("[data-phase-step]").forEach((step) => {
        const key = step.getAttribute("data-phase-step");
        const idx = order.indexOf(key);
        step.classList.remove("is-active", "is-complete");
        if (phase === "recommendation_ready" && idx !== -1) {
          step.classList.add("is-complete");
        } else if (idx === activeIdx) {
          step.classList.add("is-active");
        } else if (activeIdx !== -1 && idx < activeIdx) {
          step.classList.add("is-complete");
        }
      });
    }

    if (opportunity) opportunity.dataset.phase = phase;

    // Inventory panel
    const forecastLoading = document.querySelector('[data-state="forecast_loading"]');
    const inventoryEmpty = document.querySelector('[data-state="inventory_empty"]');
    const inventoryTable = document.querySelector('[data-bind="inventory_table"]');
    const hasInventoryRows = inventoryTable && inventoryTable.querySelector("tbody tr");

    if (forecastLoading) forecastLoading.hidden = phase !== "forecast_loading";
    if (inventoryTable) {
      inventoryTable.hidden = phase === "forecast_loading" || !hasInventoryRows;
    }
    if (inventoryEmpty) {
      inventoryEmpty.hidden =
        phase === "forecast_loading" || Boolean(hasInventoryRows);
    }

    // Recommendation panel state blocks
    const recStates = [
      "procurement_search",
      "zoowork_analysing",
      "tavily_search",
      "recommendation_empty",
      "recommendation_ready",
    ];
    const readyBody = document.querySelector(
      '.opportunity [data-state="recommendation_ready"]'
    );
    const hasOpportunity =
      readyBody && readyBody.querySelector(".action-card, .opp-item, [data-bind='opportunity.item']");

    recStates.forEach((key) => {
      const el = document.querySelector('.opportunity [data-state="' + key + '"]');
      if (!el) return;
      if (key === "recommendation_empty") {
        // Show empty whenever we are not mid-pipeline and have no bound recommendation
        const busy =
          phase === "procurement_search" ||
          phase === "zoowork_analysing" ||
          phase === "tavily_search";
        el.hidden = busy || (phase === "recommendation_ready" && Boolean(hasOpportunity));
        return;
      }
      if (key === "recommendation_ready") {
        el.hidden = phase !== "recommendation_ready" || !hasOpportunity;
        return;
      }
      el.hidden = phase !== key;
    });

    const statusBadge = document.querySelector('[data-bind="recommendation.status_badge"]');
    if (statusBadge) {
      statusBadge.hidden = phase !== "recommendation_ready" || !hasOpportunity;
    }

    // Supplier panel
    const suppliersLoading = document.querySelector('[data-state="suppliers_loading"]');
    const suppliersEmpty = document.querySelector('[data-state="suppliers_empty"]');
    const supplierList = document.querySelector(".supplier-list");
    const hasSuppliers = supplierList && supplierList.querySelector(".supplier-row");
    const suppliersBusy =
      phase === "procurement_search" ||
      phase === "zoowork_analysing" ||
      phase === "tavily_search";

    if (suppliersLoading) suppliersLoading.hidden = !suppliersBusy;
    if (supplierList) supplierList.hidden = suppliersBusy || !hasSuppliers;
    if (suppliersEmpty) suppliersEmpty.hidden = suppliersBusy || Boolean(hasSuppliers);
  }

  /** The phase that matches what is on screen: a card is showing -> keep it showing, else idle. */
  function restPhase() {
    return document.querySelector(".action-card") ? "recommendation_ready" : "idle";
  }

  // ---------- API ----------
  async function api(path, options) {
    const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, options || {}));
    let body = null;
    try {
      body = await res.json();
    } catch (e) {
      /* not JSON */
    }
    if (!res.ok) {
      const err = new Error((body && body.error) || "Request failed (" + res.status + ")");
      err.status = res.status;
      throw err;
    }
    return body;
  }

  function bind(name) {
    return document.querySelector('[data-bind="' + name + '"]');
  }

  function setKpi(name, text) {
    const el = bind(name);
    if (el) el.textContent = text;
  }

  // ---------- rendering real data ----------
  /** Redraw the forecast table (Run / View buttons, highlight of the row whose card is showing). */
  function paintInventory() {
    const tbody = document.querySelector('[data-field="inventory"] tbody');
    if (!tbody || !lastForecast) return;
    tbody.innerHTML = R.inventoryRowsHtml(lastForecast.items, runsByItem, viewingItemId);
    if (running) setRunButtonsDisabled(true);
  }

  /** While a run is in progress nothing else may start, and the card must not be swapped under it. */
  function setRunButtonsDisabled(flag) {
    document.querySelectorAll('[data-action="run-item"], [data-action="view-run"]').forEach((b) => {
      b.disabled = flag;
    });
    if (runBtn) runBtn.disabled = flag;
  }

  function renderForecast(forecast) {
    lastForecast = forecast;
    paintInventory();
    const k = R.kpiValues(forecast.items, []);
    setKpi("kpis.stock_risks", String(k.stock_risks));
    const asOf = bind("as_of_label");
    if (asOf) {
      asOf.textContent =
        "Forecast as of " + forecast.today + " · " + (SCENARIO_LABELS[forecast.scenario] || forecast.scenario);
    }
  }

  async function loadForecast() {
    const forecast = await api("/api/forecast?include_projection=0");
    renderForecast(forecast);
    showScenario(forecast.scenario);
    return forecast;
  }

  async function loadPending() {
    const data = await api("/api/runs");
    const k = R.kpiValues([], data.runs);
    setKpi("kpis.pending_approvals", String(k.pending_approvals));
    runsByItem = R.latestRunsByItem(data.runs);
    paintInventory();
    return data.runs;
  }

  function ensureSlot(name, referenceEl, where) {
    let slot = document.querySelector('[data-slot="' + name + '"]');
    if (!slot && referenceEl) {
      slot = document.createElement("div");
      slot.dataset.slot = name;
      referenceEl.insertAdjacentElement(where, slot);
    }
    return slot;
  }

  /** Paint one run (recommendation card, supplier comparison, market search, KPIs). */
  function renderRun(view) {
    const rec = view.recommendation;
    viewingItemId = view.item ? view.item.id : null;
    paintInventory();
    if (opportunity) {
      opportunity.dataset.runId = view.id;
      setDecisionState(null);
      if (view.status === "approved") opportunity.classList.add("is-approved");
      if (view.status === "rejected") opportunity.classList.add("is-rejected");
    }
    const panelBadge = bind("recommendation.status_badge");
    const badgeText = { awaiting_approval: "Needs approval", approved: "RFQ approved", rejected: "Rejected" }[view.status];
    if (panelBadge && badgeText) panelBadge.textContent = badgeText;
    renderStages(view);
    const body = document.querySelector('.opportunity [data-state="recommendation_ready"]');
    if (body) body.innerHTML = rec ? R.recommendationHtml(view) : "";

    const supplierList = document.querySelector(".supplier-list");
    const unit = view.item ? view.item.unit : "unit";
    if (supplierList) {
      supplierList.innerHTML = rec ? R.supplierRowsHtml(rec.compared_options, unit) : "";
      const banner = ensureSlot("market-banner", supplierList, "beforebegin");
      const leads = ensureSlot("market-leads", supplierList, "afterend");
      if (banner) banner.innerHTML = R.marketBannerHtml(view.market_search);
      if (leads) leads.innerHTML = R.leadsHtml(view.market_results);
    }
    const subtitle = bind("suppliers.subtitle");
    if (subtitle && view.item) {
      subtitle.textContent =
        "Every option weighed for " + view.item.sku + ": normalised $/" + unit + ", landed cost for the required quantity";
    }

    if (rec && rec.potential_savings) {
      setKpi("kpis.potential_savings", R.money(rec.potential_savings.amount));
    }
    if (demoChip) {
      const ms = view.market_search;
      const search = !ms ? "no market search" : ms.status === "unavailable" ? "live search unavailable"
        : ms.is_live ? "live web search" : "demo market data";
      demoChip.textContent = (view.agent_mode ? "Agent: " + view.agent_mode : "Agent: n/a") + " · " + search;
    }
  }

  async function loadLastRun() {
    const runs = await loadPending();
    if (!runs.length) return false;
    const view = await api("/api/runs/" + runs[0].id);
    clearTimeline();
    addServerEvents(view.events);
    renderRun(view);
    setUiPhase(["awaiting_approval", "approved", "rejected"].includes(view.status) ? "recommendation_ready" : "idle");
    return true;
  }

  // ---------- scenarios ----------
  function showScenario(key) {
    activeScenario = SCENARIO_LABELS[key] ? key : "default";
    if (scenarioSelector) {
      scenarioSelector.querySelectorAll(".scenario-pill[data-scenario]").forEach((btn) => {
        const on = btn.dataset.scenario === activeScenario;
        if (btn.dataset.scenario === "reset") return;
        btn.classList.toggle("is-active", on);
        if (btn.hasAttribute("aria-pressed")) btn.setAttribute("aria-pressed", on ? "true" : "false");
      });
    }
    if (scenarioNameEl) scenarioNameEl.textContent = SCENARIO_LABELS[activeScenario];
  }

  async function selectScenario(key) {
    if (running) {
      toast("Wait for the current run to finish before changing scenario", "warn");
      return;
    }
    try {
      if (key === "reset") {
        await api("/api/scenarios/reset", { method: "POST" });
      } else if (SCENARIO_LABELS[key]) {
        await api("/api/scenarios/active", { method: "POST", body: JSON.stringify({ key }) });
      } else {
        return;
      }
      await loadForecast();
      setUiPhase(restPhase());
      toast(key === "reset" ? "Scenario reset to Default" : "Scenario: " + SCENARIO_LABELS[key], "ok");
    } catch (e) {
      setUiPhase(restPhase());
      toast("Could not change scenario: " + e.message, "error");
    }
  }

  // ---------- running the agent ----------
  async function pollRun(view) {
    let after = 0;
    const started = Date.now();
    for (;;) {
      addServerEvents(view.events);
      renderStages(view);
      after = view.last_event_id;
      setUiPhase(R.phaseFromRun(Object.assign({}, view, { events: shownEvents })));
      if (view.status === "awaiting_approval" || view.status === "failed") return view;
      if (Date.now() - started > 6 * 60 * 1000) throw new Error("Timed out waiting for the agent to finish");
      await sleep(900);
      view = await api("/api/runs/" + view.id + "?after_event_id=" + after);
    }
  }

  /** Run the agent for one item and show its card. Assumes `running` is already true. */
  async function execute(itemId) {
    clearTimeline();
    setUiPhase("procurement_search");
    let view = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify({ inventory_item_id: itemId }),
    });
    view = await pollRun(view);
    renderRun(view);
    await loadPending();
    if (view.status === "awaiting_approval") {
      setUiPhase("recommendation_ready");
      toast("Recommendation ready for " + view.item.name, "ok");
    } else {
      setUiPhase("idle");
      toast("The run failed: " + (view.error || "unknown error"), "error");
    }
  }

  /** One run at a time. Disables every run/view button while it works. */
  async function guarded(work) {
    if (running) return;
    running = true;
    setRunButtonsDisabled(true);
    try {
      await work();
    } catch (e) {
      setUiPhase(restPhase());
      toast(e.message, "error");
    } finally {
      running = false;
      setRunButtonsDisabled(false);
    }
  }

  /** The top button: run the most urgent item. */
  function onRun() {
    return guarded(async () => {
      setUiPhase("forecast_loading");
      const forecast = await loadForecast();
      const target = forecast.items.find((f) => f.requires_procurement);
      if (!target) {
        setUiPhase(restPhase());
        toast("Nothing needs ordering right now", "ok");
        return;
      }
      await execute(target.item.id);
    });
  }

  /** A row's Run button: run the agent for that item. */
  function runForItem(itemId) {
    return guarded(() => execute(itemId));
  }

  /** A row's View button: show the existing recommendation. No new search, no spend. */
  async function viewRun(runId) {
    if (running) {
      toast("Wait for the current run to finish", "warn");
      return;
    }
    try {
      const view = await api("/api/runs/" + runId);
      clearTimeline();
      addServerEvents(view.events);
      renderRun(view);
      setUiPhase(["awaiting_approval", "approved", "rejected"].includes(view.status) ? "recommendation_ready" : "idle");
      if (opportunity) opportunity.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (e) {
      toast("Could not open that run: " + e.message, "error");
    }
  }

  async function onRefresh() {
    if (running) return;
    try {
      await loadForecast();
      const hadRun = await loadLastRun();
      if (!hadRun) setUiPhase(restPhase());
      toast("Refreshed", "ok");
    } catch (e) {
      setUiPhase(restPhase());
      toast("Refresh failed: " + e.message, "error");
    }
  }

  // Approve / Reject: a person's decision. Approving creates an RFQ DRAFT; Send is a separate step.
  async function decide(btn, action) {
    const id = runIdFrom(btn);
    if (!id) return;
    document.querySelectorAll("#btn-approve, #btn-reject").forEach((b) => {
      b.disabled = true;
    });
    try {
      const view = await api("/api/runs/" + id + "/" + action, { method: "POST", body: JSON.stringify({}) });
      const seen = new Set(shownEvents.map((e) => e.id));
      addServerEvents((view.events || []).filter((e) => !seen.has(e.id)));
      renderRun(view);
      setUiPhase("recommendation_ready");
      await loadPending();
      toast(
        action === "approve"
          ? "RFQ approved. Draft ready — use Send RFQ when you want. No order was placed."
          : "Recommendation rejected. Nothing was sent.",
        action === "approve" ? "ok" : "warn"
      );
    } catch (e) {
      document.querySelectorAll("#btn-approve, #btn-reject").forEach((b) => {
        b.disabled = false;
      });
      toast(e.message, "error");
    }
  }

  async function sendRfq(btn) {
    const id = runIdFrom(btn);
    if (!id) return;
    btn.disabled = true;
    try {
      const view = await api("/api/runs/" + id + "/send", { method: "POST", body: JSON.stringify({}) });
      const seen = new Set(shownEvents.map((e) => e.id));
      addServerEvents((view.events || []).filter((e) => !seen.has(e.id)));
      renderRun(view);
      setUiPhase("recommendation_ready");
      const mode = (view.rfq && view.rfq.send_record && view.rfq.send_record.delivery_mode) || "mock";
      toast("RFQ sent via " + mode + " (TEST/DEMO unless SMTP is configured). No purchase order.", "ok");
    } catch (e) {
      btn.disabled = false;
      toast(e.message, "error");
    }
  }

  async function copyRfq(btn) {
    const pre = document.querySelector(".rfq__body");
    if (!pre) return;
    const text = pre.textContent;
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      const area = document.createElement("textarea");
      area.value = text;
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
    }
    const old = btn.textContent;
    btn.textContent = "Copied";
    window.setTimeout(() => {
      btn.textContent = old;
    }, 1500);
  }

  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    if (btn.dataset.action === "approve") decide(btn, "approve");
    else if (btn.dataset.action === "reject") decide(btn, "reject");
    else if (btn.dataset.action === "send-rfq") sendRfq(btn);
    else if (btn.dataset.action === "copy-rfq") copyRfq(btn);
    else if (btn.dataset.action === "run-item") runForItem(Number(btn.dataset.itemId));
    else if (btn.dataset.action === "view-run") viewRun(Number(btn.dataset.runId));
  });

  if (runBtn) runBtn.addEventListener("click", onRun);
  if (refreshBtn) refreshBtn.addEventListener("click", onRefresh);

  if (scenarioSelector) {
    scenarioSelector.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-scenario]");
      if (!btn) return;
      selectScenario(btn.dataset.scenario);
    });
  }

  async function init() {
    setUiPhase("forecast_loading");
    try {
      await loadForecast();
      const hadRun = await loadLastRun();
      if (!hadRun) setUiPhase("idle");
    } catch (e) {
      setUiPhase("idle");
      toast("Could not load the dashboard: " + e.message, "error");
    }
    if (demoChip && currentPhase === "idle" && demoChip.textContent === "Demo data") {
      demoChip.textContent = "Ready";
    }
  }

  init();
})();
