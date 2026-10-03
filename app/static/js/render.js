/**
 * Pure rendering helpers for the procurement dashboard.
 *
 * Data in, HTML string out. No DOM access, no network, so every function is testable in Node
 * (tests/test_frontend_render.py). app.js owns the DOM and the API calls and uses these to build markup.
 *
 * Security: supplier names, page titles, caveats and links come from the open web and are untrusted.
 * Every dynamic value goes through esc(), and a link is only ever rendered for http(s) URLs.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.ProcurementRender = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const FIELD_LABELS = {
    price: "Price",
    pack_size: "Pack size",
    moq: "Minimum order",
    delivery_fee: "Delivery fee",
    lead_time: "Lead time",
    delivery_information: "Delivery info",
  };

  const STATUS_LABELS = { healthy: "Healthy", approaching: "Approaching", at_risk: "At Risk", critical: "Critical" };
  const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  /** Only http(s) URLs may become links. javascript:, data: and the rest are refused. */
  function safeUrl(url) {
    try {
      const u = new URL(String(url));
      return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
    } catch (e) {
      return null;
    }
  }

  function link(url, label) {
    const safe = safeUrl(url);
    if (!safe) return esc(label);
    return '<a href="' + esc(safe) + '" target="_blank" rel="noopener noreferrer">' + esc(label) + "</a>";
  }

  function money(amount) {
    if (amount == null || Number.isNaN(Number(amount))) return "n/a";
    const n = Number(amount);
    const text = Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return (n < 0 ? "-$" : "$") + text;
  }

  function num(value, digits) {
    return Number(value).toLocaleString("en-US", { maximumFractionDigits: digits == null ? 2 : digits });
  }

  /** "2026-10-04" -> "Sun 4 Oct 2026". Done by hand so it never depends on the browser's locale or timezone. */
  function prettyDate(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ""));
    if (!m) return iso == null ? "n/a" : String(iso);
    const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
    return DAYS[d.getUTCDay()] + " " + +m[3] + " " + MONTHS[+m[2] - 1] + " " + m[1];
  }

  /** "2026-10-03T22:41:07+00:00" -> "2026-10-03 22:41 UTC". */
  function prettyDateTime(iso) {
    const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(String(iso || ""));
    return m ? m[1] + " " + m[2] + " UTC" : iso == null ? "" : String(iso);
  }

  function fieldLabel(field) {
    return FIELD_LABELS[field] || String(field).replace(/_/g, " ");
  }

  /** Known fields as green chips; unknown fields as grey "? unknown" chips. Nothing is hidden. */
  function fieldChips(known, unknown) {
    const k = (known || []).map(
      (f) => '<span class="field-chip field-chip--known">' + esc(fieldLabel(f)) + "</span>"
    );
    const u = (unknown || []).map(
      (f) =>
        '<span class="field-chip field-chip--unknown" title="Not stated by the source">' +
        esc(fieldLabel(f)) +
        " unknown</span>"
    );
    if (!k.length && !u.length) return "";
    return '<span class="field-chips">' + k.join("") + u.join("") + "</span>";
  }

  // ---------- live market search ----------
  function marketBannerHtml(ms) {
    if (!ms) return "";
    if (ms.status === "unavailable") {
      return (
        '<div class="notice notice--warn" role="status" data-market-status="unavailable">' +
        "<strong>Live market search was unavailable.</strong> " +
        esc(ms.reason || "Unknown reason") +
        ". Showing existing suppliers only; no live market evidence was checked.</div>"
      );
    }
    const live = Boolean(ms.is_live);
    const provider = ms.provider === "tavily" ? "Tavily" : ms.provider;
    const title = live ? "Live web search · " + provider : "Demo market data (not the live web)";
    const stats = [
      [ms.sources_searched, "sources searched"],
      [(ms.queries || []).length, "queries"],
      [(ms.extracted_urls || []).length, "pages extracted"],
      [ms.priced_options, "priced options"],
      [ms.unpriced_leads, "pages with no usable price"],
    ]
      .filter(([v]) => v != null)
      .map(([v, label]) => "<li><strong>" + esc(v) + "</strong> " + esc(label) + "</li>")
      .join("");
    const warn =
      ms.status === "partial"
        ? '<div class="notice notice--warn" role="status" data-market-status="partial"><strong>Search only partly succeeded.</strong> ' +
          esc(ms.reason || "") +
          "</div>"
        : "";
    return (
      warn +
      '<div class="market-summary" data-market-status="' + esc(ms.status) + '">' +
      '<p class="market-summary__title">' + esc(title) +
      ' <span class="status-chip status-chip--' + (live ? "live" : "demo") + '">' + (live ? "live" : "demo") + "</span></p>" +
      '<ul class="market-stats">' + stats + "</ul></div>"
    );
  }

  // ---------- the Procurement Action Card ----------
  function list(items, cls) {
    return '<ul class="' + cls + '">' + (items || []).map((x) => "<li>" + esc(x) + "</li>").join("") + "</ul>";
  }

  function agentLabel(mode) {
    return { live: "Live ZooWork agent", mock: "Demo agent (rules)", fallback: "Demo agent (live agent failed)" }[mode] || mode || "n/a";
  }

  const STATUS_CHIPS = {
    awaiting_approval: ["awaiting", "Awaiting approval"],
    approved: ["approved", "RFQ approved"],
    rejected: ["rejected", "Rejected"],
  };

  function statusChipHtml(status) {
    const [cls, label] = STATUS_CHIPS[status] || ["other", status || ""];
    return '<span class="ac-status ac-status--' + cls + '" data-bind="card.status">' + esc(label) + "</span>";
  }

  /** The savings banner. A negative saving is shown plainly as an extra cost, never dressed up. */
  function savingBannerHtml(sav) {
    if (!sav || sav.amount == null) return "";
    const a = Number(sav.amount);
    const pct = sav.percent == null ? "" : " (" + num(Math.abs(sav.percent), 1) + "%)";
    let cls = "even";
    let label = "Same cost as the current supplier";
    let big = money(0);
    if (a > 0) {
      cls = "save";
      label = "Potential saving";
      big = money(a);
    } else if (a < 0) {
      cls = "cost";
      label = "Extra cost versus the current supplier";
      big = money(-a);
    }
    return (
      '<div class="ac-saving ac-saving--' + cls + '"><div><p class="ac-label">' + esc(label) + '</p>' +
      '<p class="ac-saving__amount">' + esc(big) + '<span class="ac-saving__pct">' + esc(pct) + "</span></p></div>" +
      '<p class="ac-saving__note">' + esc(sav.note || "") + "</p></div>"
    );
  }

  function row(label, valueHtml) {
    return "<div><dt>" + esc(label) + "</dt><dd>" + valueHtml + "</dd></div>";
  }

  function unknownText(text) {
    return '<span class="ac-unknown">' + esc(text) + "</span>";
  }

  function validEmail(s) {
    return typeof s === "string" && /^[^\s@<>"'()]+@[^\s@<>"'()]+\.[^\s@<>"'()]+$/.test(s);
  }

  /** RFQ draft / sent record. Send is a separate human step after approve (never a purchase order). */
  function rfqHtml(rfq, run) {
    if (!rfq) return "";
    const sent = rfq.status === "sent";
    const record = rfq.send_record || {};
    const reply = rfq.supplier_reply || record.supplier_reply || null;
    const mailto = !sent && validEmail(rfq.to_email)
      ? '<a class="btn btn-ghost btn--small" href="mailto:' + esc(rfq.to_email) + "?subject=" + esc(encodeURIComponent(rfq.subject)) +
        "&amp;body=" + esc(encodeURIComponent(rfq.body)) + '">Open in your email app</a>'
      : "";
    const bench = rfq.benchmark || {};
    const benchLine = bench.used
      ? "Market evidence used: a public price of about " + money(bench.price_per_unit) + " per unit (confidence " + esc(bench.confidence) +
        "). It is described as a public listing, never as a quote."
      : "Market evidence: not used. " + (bench.reason || "");
    const chip = sent
      ? '<span class="status-chip status-chip--live">Sent · ' + esc(record.delivery_mode || "mock") +
        (record.is_test_redirect || record.delivery_mode === "mock" ? " · TEST/DEMO" : "") + "</span>"
      : '<span class="status-chip status-chip--demo">Draft · not sent</span>';
    const sendBtn = !sent
      ? '<button type="button" class="btn btn-primary btn--small" id="btn-send-rfq" data-action="send-rfq" data-run-id="' +
        esc(run && run.id) + '">Send RFQ</button>'
      : "";
    const recipientLine = sent && record.recipient
      ? row("Sent to", esc(record.recipient) +
          (record.is_test_redirect
            ? " " + unknownText("(TEST redirect; intended " + esc(record.intended_recipient || "none") + ")")
            : ""))
      : row("To", esc(rfq.supplier_name) + (rfq.to_email ? " &lt;" + esc(rfq.to_email) + "&gt;" : " " + unknownText("(no email address on file)")));
    const replyHtml = reply
      ? '<aside class="rfq__reply" data-simulated="1">' +
        '<div class="rfq__reply-head"><strong>' + esc(reply.label || "SIMULATED supplier reply") + "</strong>" +
        '<span class="status-chip status-chip--demo">Simulated</span></div>' +
        "<p>Offered " + esc(money(reply.offered_unit_price)) + "/" + esc(reply.unit) +
        " · total ~" + esc(money(reply.offered_total)) +
        " · savings vs incumbent ~" + esc(money(reply.savings_vs_incumbent)) + "</p>" +
        '<pre class="rfq__reply-body">' + esc(reply.body || "") + "</pre></aside>"
      : "";
    return (
      '<section class="rfq" data-rfq-id="' + esc(rfq.id) + '" data-rfq-status="' + esc(rfq.status) + '">' +
      '<div class="rfq__head"><h4>RFQ ' + (sent ? "sent" : "draft") + "</h4>" + chip + "</div>" +
      '<dl class="rfq__meta">' + recipientLine +
      row("Subject", esc(sent && record.subject ? record.subject : rfq.subject)) +
      (sent && record.timestamp ? row("Sent at", esc(prettyDateTime(record.timestamp))) : "") +
      "</dl>" +
      '<pre class="rfq__body" tabindex="0">' + esc(sent && record.body ? record.body : rfq.body) + "</pre>" +
      '<p class="rfq__bench">' + esc(benchLine) + "</p>" +
      '<div class="rfq__actions">' + sendBtn +
      '<button type="button" class="btn btn-ghost btn--small" data-action="copy-rfq">Copy draft</button>' + mailto + "</div>" +
      '<p class="rfq__note">' + esc(rfq.send_note || "Not sent yet.") + "</p>" + replyHtml + "</section>"
    );
  }

  function decisionAreaHtml(run) {
    const d = run.decision || {};
    if (run.status === "awaiting_approval") {
      return (
        '<p class="ac-guard"><strong>Approving does not place a purchase order.</strong> It approves the next step only: ' +
        "an RFQ (request for quote) draft. Sending is a separate button after you approve.</p>" +
        '<div class="opp-actions">' +
        '<button type="button" class="btn btn-primary" id="btn-approve" data-action="approve" data-run-id="' + esc(run.id) + '">Approve RFQ</button>' +
        '<button type="button" class="btn btn-danger" id="btn-reject" data-action="reject" data-run-id="' + esc(run.id) + '">Reject</button></div>'
      );
    }
    const note = d.note ? " Note: " + esc(d.note) : "";
    if (run.status === "approved") {
      const sent = run.rfq && run.rfq.status === "sent";
      return (
        '<div class="ac-decision ac-decision--approved"><strong>RFQ approved</strong> by ' + esc(d.decided_by || "a person") + " on " +
        esc(prettyDateTime(d.decided_at)) + ". " +
        (sent
          ? "<strong>RFQ was sent</strong> (see record below). <strong>No purchase order was placed.</strong>"
          : "An RFQ draft was created. <strong>Not sent yet — use Send RFQ. No purchase order was placed.</strong>") +
        note + "</div>" + rfqHtml(run.rfq, run)
      );
    }
    if (run.status === "rejected") {
      return (
        '<div class="ac-decision ac-decision--rejected"><strong>Rejected</strong> by ' + esc(d.decided_by || "a person") + " on " +
        esc(prettyDateTime(d.decided_at)) + ". No RFQ was created and nothing was sent." + note + "</div>"
      );
    }
    return "";
  }

  function actionCardHtml(run) {
    const rec = run && run.recommendation;
    const req = run && run.requirement;
    if (!rec || !req) return "";
    const unit = rec.quantity.unit;
    const price = rec.unit_price;
    const tot = rec.estimated_total_landed_cost;
    const t = rec.delivery_timing;
    const inc = rec.incumbent_cost;
    const sup = rec.recommended_supplier;
    const moq = rec.moq || {};

    const current = inc
      ? row("Unit price", esc(money(inc.price_per_unit)) + "/" + esc(unit)) +
        row("Estimated total", esc(money(inc.estimated_total_landed_cost))) +
        row("Delivery", esc(inc.lead_time_days) + "-day lead time" + (inc.avoids_stockout === false ? ' <span class="ac-warn">too slow to avoid the stockout</span>' : ""))
      : row("Current supplier", unknownText("none on file"));

    const totalNote = tot.delivery_fee_known
      ? '<span class="ac-small">' + esc(money(tot.goods)) + " goods + " + esc(money(tot.delivery_fee)) + " delivery</span>"
      : '<span class="ac-small ac-warn">delivery fee not stated, so this excludes it</span>';

    const recommended =
      row("Unit price", esc(money(price.per_unit)) + "/" + esc(unit) + " " +
        (price.price_confirmed
          ? '<span class="price-status price-status--ok">confirmed (on file)</span>'
          : '<span class="price-status price-status--warn">public price, not a quote</span>')) +
      row("Total landed cost", esc(money(tot.total)) + " " + totalNote) +
      row("Estimated delivery", t.lead_time_days != null
        ? esc(t.lead_time_days) + " day" + (t.lead_time_days === 1 ? "" : "s") + (t.arrives_by ? " · arrives " + esc(prettyDate(t.arrives_by)) : "")
        : unknownText("not stated")) +
      row("Minimum order (MOQ)", moq.known ? esc(num(moq.packs)) + " × " + esc(moq.selling_unit) : unknownText("not stated")) +
      row("Reliability", sup.reliability != null ? esc(Math.round(sup.reliability * 100)) + "%" : unknownText("not known")) +
      row("Order", esc(num(rec.quantity.order_quantity)) + " " + esc(unit) + ' <span class="ac-small">' + esc(rec.quantity.packs_to_order) + " × " + esc(rec.quantity.pack) + "</span>");

    const tags = [
      sup.is_incumbent ? '<span class="best-tag best-tag--incumbent">Incumbent</span>' : "",
      sup.origin === "market_search" ? '<span class="best-tag best-tag--web">Web · unconfirmed</span>' : '<span class="best-tag best-tag--file">On file</span>',
    ].join("");

    const sources = (rec.sources || [])
      .map((s) => {
        const tag = s.confirmed
          ? ' <span class="src-tag src-tag--ok">confirmed</span>'
          : ' <span class="src-tag src-tag--warn">public listing, not a quote</span>';
        const conf = s.confidence != null ? ' <span class="meta-aside">confidence ' + esc(s.confidence) + "</span>" : "";
        return "<li>" + link(s.url, s.label) + tag + conf + "</li>";
      })
      .join("");

    // Web pages that were weighed but not chosen are sources too: a person approving should see them.
    const listed = new Set((rec.sources || []).map((x) => x.url).filter(Boolean));
    const considered = (rec.compared_options || [])
      .filter((o) => o.origin === "market_search" && o.source_url && !listed.has(o.source_url))
      .slice(0, 4)
      .map((o) =>
        "<li>" + link(o.source_url, o.source_domain || o.source_url) +
        ' <span class="src-tag src-tag--warn">considered · public listing, not a quote</span> <span class="meta-aside">' +
        esc(money(o.price_per_unit)) + "/" + esc(unit) + " · confidence " + esc(o.confidence) + "</span></li>"
      )
      .join("");

    const ev = rec.evidence || {};
    const backup = rec.backup_option
      ? '<p class="next-action"><strong>Backup:</strong> ' + esc(rec.backup_option.supplier_name) + " · " +
        esc(money(rec.backup_option.estimated_total_landed_cost)) + " · " + esc(rec.backup_option.lead_time_days) +
        "d lead" + (rec.backup_option.price_confirmed ? "" : " · unconfirmed") + "</p>"
      : "";

    return (
      '<article class="action-card" data-run-id="' + esc(run.id) + '" data-status="' + esc(run.status) + '">' +
      '<header class="ac-head"><div><p class="ac-eyebrow">Procurement action</p>' +
      '<h3 class="ac-item"><span class="opp-sku">' + esc(req.item.sku) + "</span><span>" + esc(req.item.name) + "</span></h3></div>" +
      statusChipHtml(run.status) + "</header>" +
      '<div class="ac-need"><div><p class="ac-label">Required quantity</p><p class="ac-big">' + esc(num(req.required_quantity)) + " " + esc(unit) + "</p></div>" +
      '<div><p class="ac-label">Required by</p><p class="ac-big">' + esc(prettyDate(req.required_by)) + "</p>" +
      (req.predicted_stockout_date ? '<p class="ac-small">stock runs out ' + esc(prettyDate(req.predicted_stockout_date)) + "</p>" : "") + "</div></div>" +
      '<div class="ac-compare">' +
      '<section class="ac-supplier"><p class="ac-label">Current supplier</p><h4>' + esc(inc ? inc.supplier : "None on file") + "</h4><dl>" + current + "</dl></section>" +
      '<section class="ac-supplier ac-supplier--recommended"><p class="ac-label">Recommended supplier</p><h4>' + esc(sup.name) + tags + "</h4><dl>" + recommended + "</dl></section></div>" +
      savingBannerHtml(rec.potential_savings) +
      '<section class="ac-block"><p class="ac-label">ZooWork reasoning · ' + esc(agentLabel(rec.meta && rec.meta.agent_mode)) + "</p>" +
      list(rec.reasons, "ac-list ac-list--reasons") + "</section>" +
      '<section class="ac-block ac-block--risks"><p class="ac-label">Risks / unknowns</p>' +
      ((rec.risks_and_uncertainties || []).length ? list(rec.risks_and_uncertainties, "ac-list ac-list--risks") : '<p class="risks__empty">None reported</p>') +
      (fieldChips(ev.known_fields, ev.unknown_fields) ? '<div class="ac-chips">' + fieldChips(ev.known_fields, ev.unknown_fields) + "</div>" : "") + "</section>" +
      '<section class="ac-block"><p class="ac-label">Sources</p>' +
      (sources || considered ? '<ul class="sources__list">' + sources + considered + "</ul>" : '<p class="sources__empty">No sources attached</p>') + "</section>" +
      backup +
      '<p class="next-action"><strong>Proposed next action:</strong> ' + esc(rec.proposed_next_action) + "</p>" +
      decisionAreaHtml(run) + "</article>"
    );
  }

  // ---------- supplier comparison ----------
  function statusTags(o) {
    const tags = [];
    if (o.status === "recommended") tags.push('<span class="best-tag">Recommended</span>');
    else if (o.status === "backup") tags.push('<span class="best-tag best-tag--backup">Backup</span>');
    if (o.is_incumbent) tags.push('<span class="best-tag best-tag--incumbent">Incumbent</span>');
    tags.push(
      o.origin === "market_search"
        ? '<span class="best-tag best-tag--web">Web · unconfirmed</span>'
        : '<span class="best-tag best-tag--file">On file</span>'
    );
    return tags.join("");
  }

  function supplierRowsHtml(compared, unit) {
    const rows = compared || [];
    const maxPrice = Math.max(1e-9, ...rows.map((o) => Number(o.price_per_unit) || 0));
    return rows
      .map((o) => {
        const best = o.status === "recommended";
        const meta = [
          o.lead_time_days != null ? o.lead_time_days + "d lead" : "lead time unknown",
          money(o.estimated_total_landed_cost) + " landed",
          o.origin === "market_search" && o.confidence != null ? "confidence " + o.confidence : null,
        ].filter(Boolean).join(" · ");
        const src = o.source_url ? '<p class="supplier-source">' + link(o.source_url, o.source_domain || o.source_url) + "</p>" : "";
        const why = best ? "" : '<p class="supplier-why">' + esc(o.why) + "</p>";
        const caveats = (o.caveats || []).length
          ? '<p class="supplier-caveat">' + esc(o.caveats.slice(0, 2).join(" ")) + "</p>"
          : "";
        const bar = Math.round(((Number(o.price_per_unit) || 0) / maxPrice) * 100);
        return (
          '<article class="supplier-row' + (best ? " supplier-row--best" : "") + '" data-option-id="' + esc(o.option_id) +
          '" data-recommended="' + (best ? "true" : "false") + '" data-origin="' + esc(o.origin) + '">' +
          '<div class="supplier-main"><p class="supplier-name"><span>' + esc(o.supplier_name) + "</span>" + statusTags(o) + "</p>" +
          '<p class="supplier-meta">' + esc(meta) + "</p>" + why + src + caveats +
          fieldChips(o.known_fields, o.unknown_fields) + "</div>" +
          '<p class="supplier-price">$<span>' + esc(Number(o.price_per_unit).toFixed(2)) + "</span><span>/" + esc(unit) + "</span></p>" +
          '<div class="price-bar" aria-hidden="true"><span style="width: ' + bar + '%"></span></div></article>'
        );
      })
      .join("");
  }

  /** Search results that could not be turned into a priced option. Shown so nothing found is hidden. */
  function leadsHtml(results) {
    const leads = (results || []).filter((r) => r.normalised_unit_price == null);
    if (!leads.length) return "";
    const items = leads
      .map((r) => {
        const note = (r.caveats && r.caveats[0]) || "No usable price in the search result";
        const price = r.raw_price ? esc(r.raw_price) : "no price stated";
        return "<li>" + link(r.source_url, r.source_domain || r.source_url) + ' <span class="meta-aside">' + price +
          " · " + esc(note) + "</span></li>";
      })
      .join("");
    return '<details class="leads"><summary>' + leads.length + " more pages found, but none gave a usable price</summary><ul>" + items + "</ul></details>";
  }

  // ---------- stage timeline + detailed log ----------
  /** The nine stages of a run. State comes from the server (derived from stored data). */
  function stagesHtml(stages, formatTime) {
    const fmt = formatTime || ((x) => x);
    if (!stages || !stages.length) return "";
    return (
      '<ol class="stages" aria-label="Run stages">' +
      stages
        .map(
          (s) =>
            '<li class="stage stage--' + esc(s.state) + '" data-stage="' + esc(s.key) + '">' +
            '<span class="stage__dot" aria-hidden="true"></span><div class="stage__body"><p class="stage__label">' + esc(s.label) +
            "</p>" + (s.detail ? '<p class="stage__detail">' + esc(s.detail) + "</p>" : "") + "</div>" +
            (s.at ? '<time class="stage__time">' + esc(fmt(s.at)) + "</time>" : "") + "</li>"
        )
        .join("") +
      "</ol>"
    );
  }

  function timelineHtml(events, formatTime) {
    const fmt = formatTime || ((x) => x);
    return (events || [])
      .map((e) =>
        '<li class="timeline-item timeline-item--' + esc(e.tone || "info") + '">' +
        '<time class="timeline-time">' + esc(fmt(e.created_at)) + "</time>" +
        '<div class="timeline-body"><p class="timeline-title">' + esc(e.title) + "</p>" +
        '<p class="timeline-detail">' + esc(e.detail || "") + "</p></div></li>"
      )
      .join("");
  }

  // ---------- inventory forecast ----------
  const RUN_TAGS = { awaiting_approval: "awaiting approval", approved: "approved", rejected: "rejected", failed: "failed", running: "running", pending: "running" };

  /** The newest run for each item, from a newest-first list of run summaries. Keyed by inventory item id. */
  function latestRunsByItem(runs) {
    const out = {};
    (runs || []).forEach((r) => {
      const id = r && r.item && r.item.id;
      if (id != null && !(id in out)) out[id] = { id: r.id, status: r.status };
    });
    return out;
  }

  /** Run (only for items that need ordering) and View (only if the item already has a run). */
  function rowActionsHtml(f, run) {
    const parts = [];
    if (f.requires_procurement) {
      parts.push('<button type="button" class="btn btn-primary btn--small" data-action="run-item" data-item-id="' + esc(f.item.id) +
        '" title="Search suppliers and get a recommendation for this item">Run</button>');
    }
    if (run) {
      parts.push('<button type="button" class="btn btn-ghost btn--small" data-action="view-run" data-run-id="' + esc(run.id) +
        '" title="Open the existing recommendation (no new search)">View</button><span class="run-tag run-tag--' + esc(run.status) +
        '">' + esc(RUN_TAGS[run.status] || run.status) + "</span>");
    }
    return parts.length ? '<div class="row-actions">' + parts.join("") + "</div>" : '<span class="ac-unknown">\u2014</span>';
  }

  function inventoryRowsHtml(items, runsByItem, viewingItemId) {
    const runs = runsByItem || {};
    return (items || [])
      .map((f) => {
        const unit = f.item.unit;
        const daysLeft = f.days_of_cover == null ? "14+d" : Number(f.days_of_cover).toFixed(1) + "d";
        const qty = f.required_quantity > 0 ? esc(num(f.required_quantity)) + " " + esc(unit) : "\u2014";
        return (
          '<tr class="status-row status-row--' + esc(f.status) + (f.item.id === viewingItemId ? " is-viewing" : "") +
          '" data-sku="' + esc(f.item.sku) + '" data-item-id="' +
          esc(f.item.id) + '" data-status="' + esc(f.status) + '"><td><span class="item-name">' + esc(f.item.name) +
          '</span><span class="item-sku">' + esc(f.item.sku) + "</span></td><td>" + esc(num(f.current_stock)) + " " + esc(unit) +
          "</td><td>" + esc(num(f.seven_day_forecast.average_per_day)) + " " + esc(unit) + '</td><td><span class="days-pill days-pill--' +
          esc(f.status) + '">' + esc(daysLeft) + "</span></td><td>" + qty + '</td><td><span class="badge badge--' + esc(f.status) + '">' +
          esc(STATUS_LABELS[f.status] || f.status) + '</span></td><td class="col-action">' + rowActionsHtml(f, runs[f.item.id]) + "</td></tr>"
        );
      })
      .join("");
  }

  // ---------- pipeline phase ----------
  /** Which pipeline step to show, from what the run has done so far. */
  function phaseFromRun(view) {
    if (!view) return "procurement_search";
    if (view.status === "awaiting_approval" || view.status === "approved" || view.status === "rejected") return "recommendation_ready";
    if (view.status === "failed") return "idle";
    const titles = (view.events || []).map((e) => e.title);
    const has = (t) => titles.some((x) => x.indexOf(t) !== -1);
    if (has("Agent: search_market_prices") || has("Tavily search complete") || has("Live market search unavailable"))
      return "zoowork_analysing";
    if (has("Agent: get_existing_supplier_options")) return "tavily_search";
    return "procurement_search";
  }

  function kpiValues(items, runs) {
    const risks = (items || []).filter((f) => f.status !== "healthy").length;
    const pending = (runs || []).filter((r) => r.status === "awaiting_approval").length;
    return { stock_risks: risks, pending_approvals: pending };
  }

  return {
    esc, safeUrl, link, money, prettyDate, prettyDateTime, fieldLabel, fieldChips, marketBannerHtml, savingBannerHtml,
    statusChipHtml, actionCardHtml, recommendationHtml: actionCardHtml, rfqHtml, supplierRowsHtml, leadsHtml, stagesHtml,
    timelineHtml, inventoryRowsHtml, latestRunsByItem, phaseFromRun, kpiValues, validEmail,
  };
});
