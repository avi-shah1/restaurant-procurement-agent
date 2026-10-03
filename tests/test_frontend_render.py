"""The dashboard's rendering code (app/static/js/render.js), run under Node on REAL backend data.

The views come from the real API (fake Tavily, demo agent), so these tests check that what the backend sends is
what the page can actually display: the Procurement Action Card in each state, the RFQ draft, the run stages,
sources, known vs unknown fields, and savings. Skipped if Node.js is not installed."""
import copy
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import tavily

from app import create_app
from app.agent import runner
from app.procurement.tavily_market import TavilyMarketSearch
from tests.fake_tavily import FAST, LEAD, SLOW, FakeTavily

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")


def provider(fake):
    return TavilyMarketSearch(api_key="tvly-test", client=fake)


def make_view(fake=None, decide=None, body=None, **env):
    """One real run through the API, optionally followed by a decision. Uses its own MonkeyPatch context so
    the suite-wide safety setup (blank API keys, blocked paid APIs, temp database) is never undone."""
    with pytest.MonkeyPatch.context() as mp:
        for k, v in env.items():
            mp.setenv(k, v)
        if fake is not None:
            mp.setattr(runner, "get_market_provider", lambda: provider(fake))
        client = create_app(run_sync=True).test_client()
        view = client.post("/api/runs", json={"sku": "MOZ-001"}).get_json()
        if decide:
            view = client.post(f"/api/runs/{view['id']}/{decide}", json=body or {}).get_json()
        return client, view


def down_fake():
    return FakeTavily(search_errors=[tavily.InvalidAPIKeyError("Unauthorized")])


@pytest.fixture
def rendered():
    """Render real runs in every state, plus a hostile one."""
    good_client, good = make_view(FakeTavily(results=[FAST, SLOW, LEAD]))
    forecast = good_client.get("/api/forecast?include_projection=0").get_json()
    _, approved_web = make_view(FakeTavily(results=[FAST, SLOW, LEAD]), decide="approve", body={"note": "Go ahead"})
    _, rejected = make_view(FakeTavily(results=[FAST, SLOW, LEAD]), decide="reject", body={"reason": "Too slow"})
    _, down = make_view(down_fake())
    _, approved_known = make_view(down_fake(), decide="approve")     # a supplier we know, so it has a contact email
    _, demo = make_view()                                            # no Tavily key: seeded demo market data
    _, failed = make_view(AGENT_MODE="live", AGENT_FALLBACK="0")     # live requested, no credentials, no fallback

    hostile = copy.deepcopy(approved_web)
    bad = "<img src=x onerror=alert(1)>"
    hostile["item"]["name"] = hostile["requirement"]["item"]["name"] = bad
    rec = hostile["recommendation"]
    rec["recommended_supplier"]["name"] = bad
    rec["incumbent_cost"]["supplier"] = bad
    rec["reasons"] = ["<script>alert('reason')</script>"]
    rec["risks_and_uncertainties"] = ['"><svg onload=alert(2)>']
    rec["proposed_next_action"] = "<b onmouseover=alert(3)>do it</b>"
    rec["potential_savings"]["note"] = "<u>note</u>"
    rec["sources"][0].update(url="javascript:alert(4)", label=bad)
    for o in rec["compared_options"]:
        o.update(supplier_name=bad, source_url="javascript:alert(5)", caveats=["<script>alert(6)</script>"],
                 unknown_fields=['"><script>'], why="<u>why</u>")
    hostile["market_search"]["reason"] = "<script>alert(7)</script>"
    hostile["market_search"]["status"] = "partial"
    hostile["market_results"][0].update(source_url="javascript:alert(8)", source_domain=bad, caveats=["<i>c</i>"],
                                        normalised_unit_price=None, raw_price="<s>$1</s>")
    hostile["events"][0].update(title=bad, detail="<script>alert(9)</script>")
    hostile["stages"][0].update(label=bad, detail="<script>alert(10)</script>")
    hostile["decision"].update(decided_by=bad, note="<svg onload=alert(11)>")
    hostile["rfq"].update(supplier_name=bad, subject='"><img src=x onerror=alert(12)>',
                          body="<script>alert(13)</script>\nHello", to_email="victim@example.com",
                          send_note="<b>sent</b>")
    hostile["rfq"]["benchmark"] = {"used": True, "price_per_unit": 5, "confidence": "<i>0.9</i>", "reason": "<u>r</u>"}

    payload = {"views": {"good": good, "approved_web": approved_web, "approved_known": approved_known,
                         "rejected": rejected, "down": down, "demo": demo, "failed": failed},
               "forecast": forecast, "hostile": hostile}
    done = subprocess.run([NODE, str(ROOT / "tests" / "render_check.js")], input=json.dumps(payload),
                          capture_output=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    out["raw"] = payload
    return out


def card(rendered, name="good"):
    return rendered["views"][name]["card"]


# ---------- the Procurement Action Card (awaiting approval) ----------
def test_card_header_and_the_requirement(rendered):
    c = card(rendered)
    req = rendered["raw"]["views"]["good"]["requirement"]
    assert "Procurement action" in c and "Mozzarella (low-moisture)" in c and "MOZ-001" in c
    assert 'class="ac-status ac-status--awaiting"' in c and "Awaiting approval" in c
    assert "Required quantity" in c and f"{req['required_quantity']:g} kg" in c
    assert "Required by" in c and "stock runs out" in c


def test_card_compares_the_current_supplier_with_the_recommended_one(rendered):
    c = card(rendered)
    current = c[c.index("Current supplier"):c.index("Recommended supplier")]
    assert "Alpine Dairy Direct" in current and "$7.20/kg" in current and "$396.80" in current
    assert "3-day lead time" in current and "too slow to avoid the stockout" in current
    rec = c[c.index("Recommended supplier"):]
    assert "fastcheese-supply.example" in rec and "$7.50/kg" in rec and "public price, not a quote" in rec
    assert "$415.00" in rec and "$405.00 goods + $10.00 delivery" in rec
    assert "1 day" in rec and "arrives " in rec
    assert "Minimum order (MOQ)" in rec and 'class="ac-unknown">not stated' in rec         # MOQ not stated: shown, not hidden
    assert "Reliability" in rec and 'class="ac-unknown">not known' in rec                  # a web result: reliability unknown
    assert "54 kg" in rec and "Web · unconfirmed" in rec


def test_card_shows_dollar_and_percent_saving_honestly(rendered):
    c = card(rendered)
    assert "ac-saving ac-saving--cost" in c and "Extra cost versus the current supplier" in c
    assert "$18.20" in c and "(4.6%)" in c and "Negative: costs more than the incumbent" in c
    s = rendered["helpers"]["saving"]
    assert "ac-saving--save" in s["save"] and "Potential saving" in s["save"] and "$20.00" in s["save"] and "(5%)" in s["save"]
    assert "ac-saving--cost" in s["cost"] and "$18.20" in s["cost"] and "(4.6%)" in s["cost"]
    assert "ac-saving--even" in s["even"] and s["none"] == ""
    assert "ac-saving__pct\"></span>" in s["nopct"]                                        # no percent given: none shown


def test_card_has_two_to_four_reasons_risks_unknowns_and_sources(rendered):
    c = card(rendered)
    reasons = c[c.index("ac-list--reasons"):]
    reasons = reasons[:reasons.index("</ul>")]
    assert "ZooWork reasoning" in c and 2 <= reasons.count("<li>") <= 4
    risks = c[c.index("Risks / unknowns"):c.index("Sources")]
    assert "unconfirmed web search result" in risks and "Stock availability has not been confirmed" in risks
    assert "minimum order quantity is not stated" in risks                                    # unknown MOQ is called out
    assert 'field-chip--unknown" title="Not stated by the source">Minimum order unknown' in risks
    assert 'field-chip--known">Price' in risks
    sources = c[c.index("Sources"):]
    assert '<a href="https://www.fastcheese-supply.example/mozzarella-6x1kg" target="_blank" rel="noopener noreferrer">' in sources
    assert "public listing, not a quote" in sources and "confidence 0.85" in sources
    # a web page that was weighed but not chosen is still listed as a source, labelled as such
    assert 'href="https://slowbulk.example/mozzarella"' in sources and "considered · public listing, not a quote" in sources
    assert "Proposed next action:" in c and "Backup:" in c and "QuickStock Express" in c


def test_card_buttons_and_the_no_purchase_order_promise(rendered):
    c = card(rendered)
    run_id = rendered["raw"]["views"]["good"]["id"]
    assert f'id="btn-approve" data-action="approve" data-run-id="{run_id}">Approve RFQ</button>' in c
    assert f'id="btn-reject" data-action="reject" data-run-id="{run_id}">Reject</button>' in c
    assert "Approving does not place a purchase order." in c and "RFQ (request for quote) draft" in c
    assert "Sending is a separate button" in c and "rfq__body" not in c               # no draft until a person approves


# ---------- after the decision ----------
def test_an_approved_card_shows_the_rfq_draft_and_no_approve_buttons(rendered):
    c = card(rendered, "approved_web")
    assert "ac-status--approved" in c and ">RFQ approved</span>" in c
    assert 'id="btn-approve"' not in c and 'id="btn-reject"' not in c and "Approving does not place" not in c
    assert "Not sent yet" in c and "No purchase order was placed." in c and "demo user" in c and "Note: Go ahead" in c
    assert 'class="rfq"' in c and "RFQ draft" in c and "Draft · not sent" in c
    assert "Request for quote:" in c and "This is a request for a quote only. It is not a purchase order." in c
    assert 'data-action="copy-rfq">Copy draft</button>' in c
    assert 'data-action="send-rfq"' in c and "Send RFQ" in c
    assert "Not sent yet" in c
    assert "mailto:" not in c and "no email address on file" in c                          # a web supplier has no address
    assert "Market evidence: not used." in c


def test_an_rfq_to_a_known_supplier_offers_a_mailto_link_and_send_button(rendered):
    c = card(rendered, "approved_known")
    m = re.search(r'href="(mailto:[^"]+)"', c)
    assert m and m.group(1).startswith("mailto:orders@") and "?subject=Request%20for%20quote" in m.group(1)
    assert "&amp;body=Hello" in m.group(1) and "%0A" in m.group(1)                         # newlines are encoded, not raw
    assert "Open in your email app" in c and "Draft · not sent" in c and 'data-action="send-rfq"' in c


def test_a_rejected_card_says_so_and_has_no_draft(rendered):
    c = card(rendered, "rejected")
    assert "ac-status--rejected" in c and ">Rejected</span>" in c
    assert "No RFQ was created and nothing was sent." in c and "Note: Too slow" in c
    assert 'class="rfq"' not in c and 'id="btn-approve"' not in c


def test_no_card_without_a_recommendation(rendered):
    assert rendered["views"]["failed"]["card"] == "" and rendered["helpers"]["noCard"] == ["", "", ""]
    assert rendered["views"]["failed"]["rows"] == "" and rendered["views"]["failed"]["phase"] == "idle"
    assert rendered["raw"]["views"]["failed"]["status"] == "failed" and rendered["raw"]["views"]["failed"]["error"]


def test_the_demo_run_still_renders_and_says_its_market_data_is_demo(rendered):
    demo = rendered["views"]["demo"]
    assert "Procurement action" in demo["card"] and "ac-saving" in demo["card"]
    assert demo["banner"].count("Demo market data (not the live web)") == 1


# ---------- the stage timeline ----------
def test_stages_show_the_nine_steps_with_the_right_states(rendered):
    stages = rendered["views"]["good"]["stages"]
    keys = re.findall(r'data-stage="(\w+)"', stages)
    assert keys == ["analysed", "shortage", "triggered", "incumbent", "search", "options", "recommendation", "awaiting", "decision"]
    assert stages.count("stage--done") == 7 and stages.count("stage--active") == 1 and stages.count("stage--pending") == 1
    for label in ("Inventory analysed", "Shortage predicted", "Procurement triggered", "Incumbent checked",
                  "Tavily search started", "Supplier options found", "Recommendation created", "Awaiting approval"):
        assert label in stages
    assert "3 sources" in stages and "T:" in stages


def test_approving_turns_every_stage_green(rendered):
    stages = rendered["views"]["approved_web"]["stages"]
    assert stages.count("stage--done") == 9 and "RFQ approved" in stages and "Not sent yet" in stages


def test_stage_timeline_for_rejection_outage_and_failure(rendered):
    assert 'stage--warning" data-stage="decision"' in rendered["views"]["rejected"]["stages"]
    assert "Rejected by a person" in rendered["views"]["rejected"]["stages"]
    down = rendered["views"]["down"]["stages"]
    assert down.count("stage--warning") == 2 and "Live search unavailable" in down and "Existing suppliers only" in down
    failed = rendered["views"]["failed"]["stages"]
    assert 'stage--error" data-stage="incumbent"' in failed and "ZOOWORK_API_KEY" in failed
    assert rendered["helpers"]["stagesEmpty"] == ["", ""]


def test_the_detailed_log_still_renders_server_events(rendered):
    tl = rendered["views"]["good"]["timeline"]
    assert "timeline-item--agent" in tl and "timeline-item--success" in tl and "Recommendation ready" in tl
    assert "Tavily search complete" in tl and tl.count("T:") == len(rendered["raw"]["views"]["good"]["events"])
    approved = rendered["views"]["approved_web"]["timeline"]
    assert "RFQ approved by a person" in approved and "RFQ draft created" in approved
    assert "timeline-item--error" in rendered["views"]["down"]["timeline"]
    assert "Recommendation rejected by a person" in rendered["views"]["rejected"]["timeline"]


# ---------- the market search summary and the options ----------
def test_banner_shows_how_many_live_sources_were_searched(rendered):
    banner = rendered["views"]["good"]["banner"]
    assert "Live web search · Tavily" in banner and "status-chip--live" in banner
    assert "<strong>3</strong> sources searched" in banner and "<strong>4</strong> queries" in banner
    assert "<strong>2</strong> priced options" in banner and "<strong>1</strong> pages with no usable price" in banner


def test_banner_says_clearly_when_live_search_was_unavailable(rendered):
    banner = rendered["views"]["down"]["banner"]
    assert "Live market search was unavailable." in banner and "API key" in banner
    assert "Showing existing suppliers only" in banner and 'data-market-status="unavailable"' in banner
    assert "sources searched" not in banner                                    # no fake numbers
    rows = rendered["views"]["down"]["rows"]
    assert "Web · unconfirmed" not in rows and "On file" in rows          # existing suppliers still shown
    assert rendered["raw"]["views"]["down"]["status"] == "awaiting_approval"   # and the run still finished
    assert "Live market search was unavailable" in card(rendered, "down")      # and the card says so in its risks


def test_every_compared_option_has_a_row_with_exactly_one_recommended(rendered):
    rows = rendered["views"]["good"]["rows"]
    compared = rendered["raw"]["views"]["good"]["recommendation"]["compared_options"]
    assert rows.count('class="supplier-row') == len(compared) == rows.count("data-option-id=")
    assert rows.count("supplier-row--best") == 1 and rows.count('data-recommended="true"') == 1
    assert rows.count("Web · unconfirmed") == 2 and "On file" in rows
    assert "Incumbent" in rows and "Backup" in rows and "Recommended" in rows


def test_rows_show_source_links_unknowns_and_why_not(rendered):
    rows = rendered["views"]["good"]["rows"]
    assert 'href="https://slowbulk.example/mozzarella"' in rows and ">slowbulk.example</a>" in rows
    assert "too slow" in rows
    assert "Minimum order unknown" in rows and "Delivery fee unknown" in rows
    assert "$<span>7.50</span><span>/kg</span>" in rows


def test_an_option_with_no_stated_lead_time_says_so(rendered):
    row = rendered["helpers"]["unknownLeadRow"]
    assert "lead time unknown" in row and "Lead time unknown" in row and "Web · unconfirmed" in row


def test_pages_with_no_usable_price_are_listed_not_hidden(rendered):
    leads = rendered["views"]["good"]["leads"]
    assert "1 more pages found, but none gave a usable price" in leads
    assert 'href="https://cheesedepot.example/mozzarella"' in leads and "no price stated" in leads


# ---------- inventory, KPIs, phases, helpers ----------
def test_inventory_rows_come_from_the_forecast(rendered):
    html = rendered["forecast"]
    assert html.count("<tr") == 10 and 'data-sku="MOZ-001"' in html
    assert 'badge badge--critical">Critical' in html and 'badge--approaching">Approaching' in html
    assert rendered["kpis"] == {"stock_risks": 2, "pending_approvals": 1}      # only the run still awaiting approval counts


def test_pipeline_phase_follows_what_the_run_has_done(rendered):
    assert rendered["helpers"]["phases"] == [
        "procurement_search", "procurement_search", "procurement_search", "tavily_search", "zoowork_analysing",
        "zoowork_analysing", "recommendation_ready", "recommendation_ready", "recommendation_ready", "idle"]
    assert rendered["views"]["good"]["phase"] == rendered["views"]["approved_web"]["phase"] == "recommendation_ready"


def test_small_helpers(rendered):
    h = rendered["helpers"]
    assert h["money"] == ["$12.50", "-$29.40", "$1,234.50", "n/a", "n/a"]
    assert 'field-chip--known">Price' in h["chips"] and h["chips"].count("unknown</span>") == 2 and h["noChips"] == ""
    assert h["dates"] == ["Sun 4 Oct 2026", "Thu 1 Jan 2026", "garbage", "n/a", "2026-10-03 22:41 UTC", ""]
    assert h["emails"] == [True, True, False, False, False, False, False, False]
    assert ">Awaiting approval<" in h["chip"][0] and ">RFQ approved<" in h["chip"][1] and ">Rejected<" in h["chip"][2]


# ---------- untrusted web content can never run as code ----------
def test_hostile_content_is_escaped_in_every_surface(rendered):
    h = rendered["hostile"]
    everything = "".join(h[k] for k in ("card", "rows", "leads", "banner", "timeline", "stages", "rfq"))
    for tag in ("<img", "<script", "<svg", "<u>", "<i>", "<s>", "<b "):
        assert tag not in everything, tag
    assert not re.search(r"<[^>]*\bon\w+\s*=", everything), "an HTML tag carries an event handler"
    assert "&lt;img src=x onerror=alert(1)&gt;" in everything and "&lt;script&gt;" in everything
    assert "&quot;&gt;&lt;svg onload=alert(2)&gt;" in everything                  # a quote-breakout attempt stays text
    assert "&lt;script&gt;alert(13)&lt;/script&gt;" in h["rfq"]                    # the RFQ body is text, never markup


def test_only_http_and_https_links_are_ever_rendered(rendered):
    h = rendered["hostile"]
    everything = h["card"] + h["rows"] + h["leads"] + h["rfq"]
    hrefs = re.findall(r'href="([^"]*)"', everything)
    assert hrefs and not any(href.lower().startswith(("javascript:", "data:")) for href in hrefs)
    safe = rendered["helpers"]["safe"]
    assert safe == ["https://a.example/x?y=1", "http://a.example/", None, None, None, None, None, None]
    assert rendered["helpers"]["link"] == "&lt;b&gt;click&lt;/b&gt;"                # unsafe URL: plain escaped text, no <a>


def test_a_hostile_rfq_cannot_break_out_of_the_mailto_link(rendered):
    m = re.search(r'href="(mailto:[^"]*)"', rendered["hostile"]["rfq"])
    assert m and "<" not in m.group(1) and '"' not in m.group(1)
    assert "%3Cscript%3E" in m.group(1) and "%22%3E%3Cimg" in m.group(1)           # encoded, so it stays inside the URL


# ---------- the page itself ----------
def test_the_page_loads_the_render_code_before_the_app_code():
    page = create_app().test_client().get("/").get_data(as_text=True)
    assert page.index("js/render.js") < page.index("js/app.js")
    client = create_app().test_client()
    for path in ("/static/js/render.js", "/static/js/app.js", "/static/css/style.css"):
        assert client.get(path).status_code == 200


@pytest.mark.parametrize("name", ["render.js", "app.js"])
def test_the_javascript_has_no_syntax_errors(name):
    done = subprocess.run([NODE, "--check", str(ROOT / "app" / "static" / "js" / name)], capture_output=True, encoding="utf-8")
    assert done.returncode == 0, done.stderr


def test_the_page_script_has_no_way_to_contact_a_supplier():
    """The browser code may only call our own /api routes. It must not post anywhere else or send mail itself."""
    source = (ROOT / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert re.findall(r'fetch\(\s*"(?!/api/)', source) == [] and "XMLHttpRequest" not in source and "sendBeacon" not in source
    assert "mailto" not in source          # the only mailto is a link a person clicks, built in render.js


# ---------- Run / View buttons on the forecast rows ----------
def _rows(html):
    """[(status, item_id, sku, full_row_html)] for each <tr> in the forecast table."""
    out = []
    for chunk in html.split("<tr ")[1:]:
        out.append((re.search(r'data-status="(\w+)"', chunk).group(1), re.search(r'data-item-id="(\d+)"', chunk).group(1),
                    re.search(r'data-sku="([\w-]+)"', chunk).group(1), chunk))
    return out


def test_only_items_that_need_ordering_get_a_run_button(rendered):
    rows = _rows(rendered["forecast"])
    assert len(rows) == 10
    for status, item_id, sku, html in rows:
        has_run = 'data-action="run-item"' in html
        assert has_run == (status != "healthy"), sku                                  # at risk -> Run; healthy -> none
        if has_run:
            assert f'data-action="run-item" data-item-id="{item_id}"' in html          # the button knows which item it runs
    assert sum('data-action="run-item"' in r[3] for r in rows) == rendered["kpis"]["stock_risks"] == 2
    assert "<th>" not in rendered["forecast"] and rendered["forecast"].count("col-action") == 10


def test_rows_with_an_existing_run_get_a_view_button_and_a_status_tag(rendered):
    rows = _rows(rendered["inventoryWithRuns"])
    first, last = rows[0], rows[-1]
    assert 'data-action="view-run" data-run-id="7"' in first[3] and "run-tag--awaiting_approval" in first[3]
    assert ">awaiting approval<" in first[3] and 'data-action="run-item"' in first[3]       # at risk: Run AND View
    assert 'data-action="view-run" data-run-id="8"' in last[3] and ">approved<" in last[3]
    assert last[0] == "healthy" and 'data-action="run-item"' not in last[3]                 # healthy: View only
    assert sum('data-action="view-run"' in r[3] for r in rows) == 2                         # no run -> no View button


def test_the_row_whose_card_is_on_screen_is_highlighted(rendered):
    rows = _rows(rendered["inventoryWithRuns"])
    assert [r[2] for r in rows if "is-viewing" in r[3]] == [rows[0][2]]                      # exactly that one row
    assert "is-viewing" not in rendered["forecast"]                                         # none when nothing is open


def test_the_newest_run_per_item_wins(rendered):
    assert rendered["latestRuns"] == {"1": {"id": 9, "status": "rejected"}, "2": {"id": 7, "status": "failed"}}
