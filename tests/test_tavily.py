"""Tavily market search: the provider, its failure handling, the tool, discovery, and a full run.

Tavily itself is replaced by tests/fake_tavily.py, which records every call, so these tests check how
we USE the API (parameters, number of calls, credits) as well as what we do with the answers."""
import json
from datetime import date
from pathlib import Path

import pytest
import requests
import tavily
from tavily import errors as tavily_errors

from app import create_app
from app.agent import runner
from app.agent.mock_service import MockProcurementAgent
from app.agent.runner import execute_run
from app.agent.tools import ToolContext, execute_tool
from app.data.local_repo import LocalRepository
from app.data.scenario_repo import ScenarioRepository
from app.procurement import tavily_market
from app.procurement.discovery import persist_discoveries, worth_saving
from app.procurement.market import MockMarketSearch, get_market_provider
from app.procurement.requirement import create_run
from app.procurement.tavily_market import NOISE_DOMAINS, TavilyMarketSearch, build_queries
from tests.fake_tavily import FAST, LEAD, SLOW, FakeTavily, result

TODAY = date(2026, 10, 3)
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "tavily_mozzarella_sample.json").read_text())
REAL_RESULTS = list({r["url"]: r for q in FIXTURE for r in q["results"]}.values())  # 12 real results

WEBSTAURANT = "https://www.webstaurantstore.com/54657/cheese.html"
WEBSTAURANT_PAGE = ("Bulk Cheese. Galbani Whole Milk Low Moisture Mozzarella Cheese 5 lb. bag - 6/Case "
                    "$52.99 / Case. Free shipping on orders over $99. Add to cart.")



@pytest.fixture
def base(tmp_path):
    return LocalRepository(tmp_path / "db.json", today=TODAY)


def moz_item(base):
    return next(i for i in base.list_inventory_items() if i["sku"] == "MOZ-001")


def new_run(base):
    return create_run(ScenarioRepository(base, lambda: "default"), moz_item(base)["id"], TODAY)


def ctx_for(base, provider):
    run = new_run(base)
    return run, ToolContext(ScenarioRepository(base, lambda: "default"), run["id"], provider, agent_mode="mock")


def provider(fake, **kw):
    return TavilyMarketSearch(api_key="tvly-test", client=fake, **kw)


def real_fake(**kw):
    return FakeTavily(results=REAL_RESULTS, pages={WEBSTAURANT: WEBSTAURANT_PAGE}, **kw)


def search(base, fake, **kw):
    item = moz_item(base)
    run = new_run(base)
    return provider(fake, **kw).search(item, run["requirement"])


# ---------- queries ----------
def test_queries_are_several_and_focused(base):
    queries = build_queries(moz_item(base), "Austin, Texas")
    assert len(queries) == 4 and len(set(queries)) == 4
    assert all("mozzarella" in q and len(q) < 200 for q in queries)
    assert any("wholesale" in q for q in queries) and any("foodservice" in q for q in queries)
    assert any("Austin, Texas" in q for q in queries)


# ---------- how we use the Tavily API ----------
def test_search_uses_targeted_basic_searches_then_one_extract(base):
    fake = real_fake()
    res = search(base, fake)

    assert len(fake.search_calls) == 4 and res.queries == [c["query"] for c in fake.search_calls]
    for call in fake.search_calls:
        assert call["search_depth"] == "basic" and call["topic"] == "general" and call["max_results"] == 6
        assert set(NOISE_DOMAINS) <= set(call["exclude_domains"]) and call["country"] == "united states"
        assert call["timeout"] == 20.0 and "include_raw_content" not in call  # no crawling, no bulk page fetch
    assert len(fake.extract_calls) == 1                                     # one batched call, not one per URL
    extract = fake.extract_calls[0]
    assert len(extract["urls"]) <= 3 and extract["extract_depth"] == "basic" and "mozzarella" in extract["query"]
    assert WEBSTAURANT in extract["urls"]
    assert not any("tomthumb" in u or "amazon" in u for u in extract["urls"])  # retail pages are not worth extracting


def test_results_are_filtered_parsed_and_extracted(base):
    res = search(base, real_fake())
    assert res.status == "ok" and res.is_live and res.sources_searched == 12 and len(res.extracted_urls) >= 1
    urls = {r["source_url"] for r in res.rows}
    assert not any(x in " ".join(urls) for x in ("tiktok", "marketreportsworld", "a1cashandcarry", ".pdf", "boxncase"))

    priced = [r for r in res.rows if r["normalised_unit_price"] is not None]
    assert {r["source_domain"] for r in priced} == {"tomthumb.com", "webstaurantstore.com"}
    web = next(r for r in res.rows if r["source_url"] == WEBSTAURANT)
    assert web["raw_result"]["extracted"] is True and web["pack_size"] == pytest.approx(30 * 0.45359237)
    assert web["normalised_unit_price"] == pytest.approx(52.99 / (30 * 0.45359237), rel=1e-3)  # about $3.89 per kg
    assert web["raw_result"]["delivery_fee"] is None and web["minimum_order_quantity"] is None   # not invented
    assert any("conditional" in c for c in web["raw_result"]["caveats"])
    unpriced = [r for r in res.rows if r["normalised_unit_price"] is None]
    assert unpriced and all(r["raw_result"]["confirmed"] is False for r in res.rows)
    assert res.rows[0]["normalised_unit_price"] is not None  # priced results are listed first


def test_every_row_has_the_documented_fields_and_never_a_made_up_value(base):
    for row in search(base, real_fake()).rows:
        assert {"supplier_name", "product_name", "raw_price", "normalised_unit_price", "pack_size",
                "minimum_order_quantity", "delivery_information", "source_url", "source_domain", "confidence",
                "raw_result"} <= set(row)
        assert row["supplier_name"] == row["source_domain"] and 0 < row["confidence"] <= 0.95
        raw = row["raw_result"]
        assert sorted(raw["known_fields"] + raw["unknown_fields"]) == sorted(
            ["price", "pack_size", "moq", "delivery_fee", "lead_time", "delivery_information"])
        if "delivery_fee" in raw["unknown_fields"]:
            assert raw["delivery_fee"] is None
        if "moq" in raw["unknown_fields"]:
            assert row["minimum_order_quantity"] is None


def test_extract_is_skipped_when_every_snippet_is_already_priced(base):
    fake = FakeTavily(results=[FAST, SLOW])
    res = search(base, fake)
    assert fake.extract_calls == [] and res.extracted_urls == [] and len(res.rows) == 2


# ---------- a price that cannot be right is never offered as an option ----------
GFS = json.loads((Path(__file__).parent / "fixtures" / "tavily_gfs_sample.json").read_text())
CHEAP = result("Mozzarella - TooGoodSupply", "https://toogoodsupply.example/mozzarella",
               "Low-moisture mozzarella cheese $1.00 per lb. Next-day delivery available.", 0.8)   # $2.20/kg: 0.31x ours


def test_an_implausibly_cheap_price_is_kept_as_a_lead_not_an_option(base):
    run, ctx = ctx_for(base, provider(FakeTavily(results=[CHEAP, FAST])))
    out, _ = execute_tool(ctx, "search_market_prices", {"inventory_item_id": run["requirement"]["item"]["id"]})
    assert [o["supplier_name"] for o in out["options"]] == ["fastcheese-supply.example"]  # the sane one only
    lead = next(x for x in out["unpriced_leads"] if "toogood" in x["source_domain"])
    assert "implausible" in lead["why_not_an_option"] and any("Implausible price" in c for c in lead["caveats"])
    stored = next(r for r in base.list_market_search_results(run["id"]) if "toogood" in r["source_domain"])
    assert stored["raw_result"]["price_suspect"] is True and stored["normalised_unit_price"] == pytest.approx(1 / 0.45359237, rel=1e-3)
    assert stored["confidence"] < 0.5                                                      # and its confidence is halved
    assert "toogoodsupply.example" not in {s["name"] for s in base.list_suppliers()}      # not saved as a supplier


def test_the_real_gfs_pages_now_give_a_believable_price(base):
    res = search(base, FakeTavily(results=GFS))
    priced = [r for r in res.rows if r["normalised_unit_price"] is not None]
    assert len(priced) == 3 and all(r["raw_result"]["price_suspect"] is False for r in priced)
    assert all(4.5 < r["normalised_unit_price"] < 6.0 for r in priced)   # about $5 per kg, not $0.84


def test_every_result_keeps_the_text_its_numbers_were_read_from(base):
    res = search(base, FakeTavily(results=[FAST]))
    source = res.rows[0]["raw_result"]["source_text"]
    assert "$45.00" in source and len(source) <= 600   # so any number can be checked against its source


# ---------- Tavily fails: never raise, always say why ----------
@pytest.mark.parametrize("error, reason_part", [
    (tavily.InvalidAPIKeyError("Unauthorized"), "API key"),
    (tavily.UsageLimitExceededError("limit"), "usage limit"),
    (tavily_errors.ForbiddenError("no"), "forbidden"),
])
def test_fatal_errors_stop_immediately_and_report_unavailable(base, error, reason_part):
    fake = FakeTavily(results=REAL_RESULTS, search_errors=[error])
    res = search(base, fake)
    assert res.status == "unavailable" and reason_part in res.reason and res.rows == []
    assert len(fake.search_calls) == 1  # no point hammering an API that rejected us


def test_timeouts_everywhere_mean_unavailable(base):
    fake = FakeTavily(search_errors=[tavily_errors.TimeoutError(20)] * 4)
    res = search(base, fake)
    assert res.status == "unavailable" and "timed out" in res.reason and len(fake.search_calls) == 4


def test_network_errors_mean_unavailable(base):
    fake = FakeTavily(search_errors=[requests.exceptions.ConnectionError("dns failure")] * 4)
    res = search(base, fake)
    assert res.status == "unavailable" and "ConnectionError" in res.reason


def test_one_failed_query_gives_a_partial_result_with_the_rest_kept(base):
    fake = FakeTavily(results=[FAST, SLOW], search_errors=[tavily_errors.TimeoutError(20)])
    res = search(base, fake)
    assert res.status == "partial" and len(res.rows) == 2 and "timed out" in res.reason and len(res.queries) == 3


def test_an_unsupported_country_is_dropped_and_the_search_continues(base):
    fake = FakeTavily(results=[FAST], search_errors=[tavily.BadRequestError("bad country")])
    res = search(base, fake)
    assert res.status == "ok" and len(res.rows) == 1
    assert "country" in fake.search_calls[0] and all("country" not in c for c in fake.search_calls[1:])
    assert len(fake.search_calls) == 5  # the failed first query was retried without the country


def test_a_failed_extract_keeps_the_snippet_results(base):
    fake = FakeTavily(results=REAL_RESULTS, extract_error=tavily_errors.TimeoutError(30))
    res = search(base, fake)
    assert res.status == "partial" and "Extract" in res.reason and len(res.rows) >= 5


def test_pages_that_fail_to_extract_are_reported(base):
    fake = real_fake(failed_urls={"https://slowbulk.example/mozzarella": "blocked"})
    fake.results = [*REAL_RESULTS, SLOW]
    fake.failed_urls = {WEBSTAURANT: "robots.txt"}
    fake.pages = {}
    res = search(base, fake)
    assert any("webstaurantstore.com" in e and "robots.txt" in e for e in res.errors) and res.status == "partial"


def test_a_missing_key_is_unavailable_without_calling_anything(base):
    fake = FakeTavily(results=REAL_RESULTS)
    res = TavilyMarketSearch(api_key="", client=None).search(moz_item(base), new_run(base)["requirement"])
    assert res.status == "unavailable" and res.reason == "TAVILY_API_KEY is not set" and fake.search_calls == []


def test_the_provider_never_raises_even_on_a_bug(base, monkeypatch):
    monkeypatch.setattr(tavily_market, "parse_listing", lambda *a, **k: 1 / 0)
    res = search(base, FakeTavily(results=[FAST]))
    assert res.status == "unavailable" and "Unexpected error" in res.reason


def test_the_time_budget_stops_further_queries(base):
    ticks = iter(range(0, 1000, 40))
    fake = FakeTavily(results=[FAST])
    # the clock reads 0 at the start, 40 before query 1 (inside the 75s budget), 80 before query 2 (outside)
    res = search(base, fake, budget_s=75, clock=lambda: next(ticks))
    assert len(fake.search_calls) == 1 and res.status == "partial" and "budget" in res.reason


def test_provider_selection(monkeypatch):
    assert isinstance(get_market_provider(), MockMarketSearch)               # auto, no key: demo data
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    assert isinstance(get_market_provider(), TavilyMarketSearch)             # auto, key set: live
    monkeypatch.setenv("MARKET_SEARCH_PROVIDER", "mock")
    assert isinstance(get_market_provider(), MockMarketSearch)               # explicit mock
    monkeypatch.delenv("TAVILY_API_KEY")
    monkeypatch.setenv("MARKET_SEARCH_PROVIDER", "tavily")
    assert isinstance(get_market_provider(), TavilyMarketSearch)             # explicit tavily, no key: still Tavily
    assert get_market_provider().search(
        {"sku": "MOZ-001", "unit": "kg", "name": "Mozzarella"}, {}).status == "unavailable"  # ...and says so


# ---------- the tool: saving, reuse, honesty ----------
def test_the_tool_saves_every_result_and_summarises_the_search(base):
    fake = real_fake()
    run, ctx = ctx_for(base, provider(fake))
    out, is_error = execute_tool(ctx, "search_market_prices", {"inventory_item_id": run["requirement"]["item"]["id"]})
    assert not is_error and out["provider"] == "tavily" and out["live_search"]["status"] == "ok"
    assert out["live_search"]["sources_searched"] == 12 and len(out["live_search"]["queries"]) == 4
    assert len(out["options"]) == 2 and len(out["unpriced_leads"]) >= 3
    assert all(o["price_confirmed"] is False and o["evidence"] == "web_search_result_unconfirmed" for o in out["options"])
    assert all(o["source_url"] and o["source_domain"] and o["unknown_fields"] for o in out["options"])
    assert all(lead["why_not_an_option"] and lead["source_url"] for lead in out["unpriced_leads"])

    saved = base.list_market_search_results(run["id"])
    assert len(saved) == len(out["options"]) + len(out["unpriced_leads"]) >= 5
    assert base.get_procurement_run(run["id"])["market_search"]["priced_options"] == 2


def test_a_second_search_is_free_and_identical(base):
    fake = real_fake()
    run, ctx = ctx_for(base, provider(fake))
    item_id = run["requirement"]["item"]["id"]
    first, _ = execute_tool(ctx, "search_market_prices", {"inventory_item_id": item_id})
    calls = (len(fake.search_calls), len(fake.extract_calls))
    second, _ = execute_tool(ctx, "search_market_prices", {"inventory_item_id": item_id})
    assert (len(fake.search_calls), len(fake.extract_calls)) == calls  # no repeat spend of Tavily credits
    assert [o["option_id"] for o in first["options"]] == [o["option_id"] for o in second["options"]]
    assert len(base.list_market_search_results(run["id"])) == len(first["options"]) + len(first["unpriced_leads"])


def test_quantity_and_deadline_cannot_be_overridden_by_the_agent(base):
    run, ctx = ctx_for(base, provider(FakeTavily(results=[FAST])))
    out, is_error = execute_tool(ctx, "search_market_prices", {
        "inventory_item_id": run["requirement"]["item"]["id"], "quantity": 5, "required_by": "2030-01-01",
        "location": "Austin, Texas"})
    assert not is_error and len(out["notes"]) == 2 and "ignored" in out["notes"][0]
    assert out["options"][0]["order_quantity"] >= 51  # priced for Python's 51 kg, not the agent's 5
    assert execute_tool(ctx, "search_market_prices", {"inventory_item_id": 1, "location": "x" * 101})[1] is True


# ---------- discovery: what gets saved as a supplier ----------
def test_good_results_become_tavily_live_suppliers_and_offers(base):
    run, ctx = ctx_for(base, provider(real_fake()))
    suppliers_before = {s["name"] for s in base.list_suppliers()}
    products_before = base.list_supplier_products()
    execute_tool(ctx, "search_market_prices", {"inventory_item_id": run["requirement"]["item"]["id"]})

    new_suppliers = {s["name"] for s in base.list_suppliers()} - suppliers_before
    assert new_suppliers == {"webstaurantstore.com"}  # the retail listing, ranges and leads are NOT saved
    supplier = next(s for s in base.list_suppliers() if s["name"] == "webstaurantstore.com")
    assert supplier["website"] == "https://webstaurantstore.com" and "unverified" in supplier["notes"]
    assert supplier["reliability_score"] is None and supplier["minimum_order_value"] is None  # unknown stays unknown

    new_products = [p for p in base.list_supplier_products() if p not in products_before]
    assert len(new_products) == 1 and new_products[0]["source_type"] == "tavily_live"
    assert new_products[0]["unit_price"] == 52.99 and new_products[0]["minimum_order_quantity"] is None
    assert new_products[0]["delivery_fee"] is None
    assert all(p in base.list_supplier_products() for p in products_before)  # our own records untouched


def test_discovered_offers_are_not_listed_as_existing_suppliers(base):
    run, ctx = ctx_for(base, provider(real_fake()))
    item_id = run["requirement"]["item"]["id"]
    execute_tool(ctx, "search_market_prices", {"inventory_item_id": item_id})
    existing, _ = execute_tool(ctx, "get_existing_supplier_options", {"inventory_item_id": item_id})
    assert len(existing["options"]) == 5 and all(o["origin"] == "database" and o["price_confirmed"] for o in existing["options"])
    assert not any(o["supplier_name"] == "webstaurantstore.com" for o in existing["options"])  # no double listing


def test_discovery_never_overwrites_our_own_records_and_refreshes_its_own(base):
    item_id = moz_item(base)["id"]
    row = {"supplier_name": "wholesale.example", "product_name": "Mozzarella 6 x 1kg", "source_domain": "wholesale.example",
           "source_url": "https://wholesale.example/m", "normalised_unit_price": 7.0, "pack_size": 6.0,
           "minimum_order_quantity": None, "confidence": 0.8,
           "raw_result": {"provider": "tavily", "pack_price": 42.0, "selling_unit": "case", "delivery_fee": None,
                          "lead_time_days": None, "is_retail": False, "price_is_range": False}}
    first = persist_discoveries(base, item_id, [row])
    assert first["suppliers_created"] == 1 and first["offers_created"] == 1

    cheaper = {**row, "normalised_unit_price": 6.5, "raw_result": {**row["raw_result"], "pack_price": 39.0}}
    second = persist_discoveries(base, item_id, [cheaper])
    assert second["suppliers_created"] == 0 and second["offers_updated"] == 1
    offer = next(p for p in base.list_supplier_products() if p["supplier_product_name"] == "Mozzarella 6 x 1kg")
    assert offer["unit_price"] == 39.0 and offer["source_type"] == "tavily_live"

    # pretend a person has since bought from them: now it is OUR record (change the stored row, not a copy)
    next(p for p in base._rows("supplier_products") if p["supplier_product_name"] == "Mozzarella 6 x 1kg")["source_type"] = "historical"
    third = persist_discoveries(base, item_id, [{**row, "raw_result": {**row["raw_result"], "pack_price": 1.0}}])
    assert third["offers_skipped"] == 1
    assert next(p for p in base.list_supplier_products() if p["supplier_product_name"] == "Mozzarella 6 x 1kg")["unit_price"] == 39.0


@pytest.mark.parametrize("change", [
    {"raw_result": {"is_retail": True}}, {"raw_result": {"price_is_range": True}},
    {"raw_result": {"price_suspect": True}}, {"confidence": 0.2},
    {"normalised_unit_price": None}, {"pack_size": None}, {"raw_result": {"provider": "mock"}},
])
def test_only_sensible_results_are_saved_as_suppliers(change):
    row = {"normalised_unit_price": 7.0, "pack_size": 6.0, "confidence": 0.8,
           "raw_result": {"provider": "tavily", "pack_price": 42.0, "is_retail": False, "price_is_range": False}}
    assert worth_saving(row) is True
    merged = {**row, **{k: v for k, v in change.items() if k != "raw_result"}}
    if "raw_result" in change:
        merged["raw_result"] = {**row["raw_result"], **change["raw_result"]}
    assert worth_saving(merged) is False


# ---------- Tavily fails mid-run: carry on with existing suppliers and say so ----------
def test_a_failed_search_continues_with_existing_suppliers_and_says_so(base):
    fake = FakeTavily(search_errors=[tavily.InvalidAPIKeyError("Unauthorized")])
    run, ctx = ctx_for(base, provider(fake))
    out, is_error = execute_tool(ctx, "search_market_prices", {"inventory_item_id": run["requirement"]["item"]["id"]})
    assert not is_error and out["live_search"]["status"] == "unavailable"
    assert out["options"] == [] and "UNAVAILABLE" in out["note"] and "Do not invent" in out["note"]
    assert any(e["title"] == "Live market search unavailable" and e["tone"] == "error"
               for e in base.list_agent_events(run["id"]))

    again, _ = execute_tool(ctx, "search_market_prices", {"inventory_item_id": run["requirement"]["item"]["id"]})
    assert len(fake.search_calls) == 1 and again["live_search"]["status"] == "unavailable"  # no retry loop

    final = execute_run(base, run["id"], agent=MockProcurementAgent(), market=provider(FakeTavily(
        search_errors=[tavily.InvalidAPIKeyError("Unauthorized")])))
    rec = final["recommendation"]
    assert final["status"] == "awaiting_approval" and rec["recommended_supplier"]["origin"] == "database"
    assert all(o["origin"] == "database" for o in rec["compared_options"])
    assert any("Live market search was unavailable" in r and "API key" in r for r in rec["risks_and_uncertainties"])
    assert rec["meta"]["market_search"]["status"] == "unavailable"
    assert final["market_search"]["reason"].startswith("Tavily rejected the API key")


# ---------- a complete mozzarella run on live-shaped results ----------
def test_a_full_mozzarella_run_with_live_results(base):
    fake = FakeTavily(results=[FAST, SLOW, LEAD])
    run = new_run(base)
    final = execute_run(base, run["id"], agent=MockProcurementAgent(), market=provider(fake))
    rec = final["recommendation"]

    # FastCheese: 6 x 1kg case at $45.00 -> 9 cases (54 kg) = $405.00 + $10.00 delivery = $415.00, next-day
    assert final["status"] == "awaiting_approval"
    assert rec["recommended_supplier"] == {"name": "fastcheese-supply.example", "supplier_id": None,
                                           "origin": "market_search", "is_incumbent": False,
                                           "reliability": None}   # a web result: reliability is not known
    assert rec["estimated_total_landed_cost"]["total"] == 415.00 and rec["quantity"]["order_quantity"] == 54
    assert rec["delivery_timing"]["lead_time_days"] == 1 and rec["delivery_timing"]["avoids_stockout"] is True
    assert rec["potential_savings"]["amount"] == pytest.approx(396.80 - 415.00)   # costs $18.20 more than Alpine
    assert rec["backup_option"]["supplier_name"] == "QuickStock Express"           # the confirmed fallback

    # what is known and what is not
    assert rec["unit_price"]["price_confirmed"] is False
    assert rec["evidence"]["unknown_fields"] == ["moq"] and "price" in rec["evidence"]["known_fields"]
    risks = " ".join(rec["risks_and_uncertainties"])
    assert "unconfirmed web search result" in risks and "minimum order quantity is not stated" in risks
    assert "not a commitment" in risks
    assert rec["sources"][0]["url"] == FAST["url"] and rec["sources"][0]["confirmed"] is False

    by_name = {o["supplier_name"]: o for o in rec["compared_options"]}
    assert by_name["fastcheese-supply.example"]["status"] == "recommended"
    assert by_name["slowbulk.example"]["status"] == "not_viable" and "too slow" in by_name["slowbulk.example"]["why"]
    assert by_name["Alpine Dairy Direct"]["is_incumbent"] and by_name["Alpine Dairy Direct"]["status"] == "not_viable"
    assert {o["origin"] for o in rec["compared_options"]} == {"database", "market_search"}

    # the search itself is on the record
    ms = final["market_search"]
    assert ms["provider"] == "tavily" and ms["is_live"] and ms["status"] == "ok" and ms["sources_searched"] == 3
    assert ms["priced_options"] == 2 and ms["unpriced_leads"] == 1 and len(ms["queries"]) == 4
    assert rec["meta"]["market_search"]["priced_options"] == 2
    assert len(base.list_market_search_results(run["id"])) == 3
    assert {s["name"] for s in base.list_suppliers()} >= {"fastcheese-supply.example", "slowbulk.example"}

    titles = [e["title"] for e in base.list_agent_events(run["id"])]
    assert "Tavily search complete" in titles and "Saved discovered suppliers" in titles


# ---------- API: what the UI receives ----------
@pytest.fixture
def client():
    return create_app(run_sync=True).test_client()


def test_the_run_api_exposes_the_search_and_the_sources(client, monkeypatch):
    monkeypatch.setattr(runner, "get_market_provider", lambda: provider(FakeTavily(results=[FAST, SLOW, LEAD])))
    body = client.post("/api/runs", json={"sku": "MOZ-001"}).get_json()
    assert body["market_search"]["sources_searched"] == 3 and body["market_search"]["status"] == "ok"
    assert len(body["market_results"]) == 3
    lead = next(r for r in body["market_results"] if "cheesedepot" in r["source_url"])
    assert lead["normalised_unit_price"] is None and lead["raw_price"] is None and lead["source_domain"] == "cheesedepot.example"
    assert "price" in lead["unknown_fields"] and lead["confidence"] > 0
    rec = body["recommendation"]
    assert rec["compared_options"] and rec["evidence"] and rec["meta"]["market_search"]["provider"] == "tavily"


def test_the_run_api_still_works_when_tavily_is_down(client, monkeypatch):
    monkeypatch.setattr(runner, "get_market_provider", lambda: provider(FakeTavily(
        search_errors=[tavily.UsageLimitExceededError("plan limit")])))
    body = client.post("/api/runs", json={"sku": "MOZ-001"}).get_json()
    assert body["status"] == "awaiting_approval" and body["market_search"]["status"] == "unavailable"
    assert body["market_results"] == [] and "usage limit" in body["market_search"]["reason"]
    assert any("unavailable" in r for r in body["recommendation"]["risks_and_uncertainties"])
    assert any(e["tone"] == "error" and "unavailable" in e["title"] for e in body["events"])
