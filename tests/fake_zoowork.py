"""A fake ZooWork HTTP API for offline tests.

The REAL `zoowork` SDK runs against it through httpx.MockTransport, so our live-agent code is exercised
end to end without an API key. Its response shapes come from the installed SDK source and the ZooWork
docs, so tests using it are "offline-tested", not "live-verified".

The scripted "agent brain" decides what the model does next. `behaviour` makes it misbehave:
  normal                 requirement -> existing -> market -> submit cheapest option that avoids stockout
  bad_option_then_good   first submits an option id that does not exist, then corrects itself
  inject_numbers_then_good  first tries to add its own total cost to the submission, then corrects itself
  no_submit_then_nudge   finishes without submitting; submits only after our nudge message
  never_submit           finishes without submitting, even after the nudge
  run_failed             the run ends with status "failed"
  silent                 starts the run, then says nothing (to test the timeout)
Flags: fail_first_stream, fail_create_session, replay_all (ignore the resume cursor).
"""
from __future__ import annotations

import json
import re

import httpx


class FakeZooWork:
    def __init__(self, behaviour: str = "normal", fail_first_stream: bool = False,
                 fail_create_session: bool = False, replay_all: bool = False):
        self.behaviour = behaviour
        self.fail_first_stream, self.fail_create_session, self.replay_all = (
            fail_first_stream, fail_create_session, replay_all)
        self.requests: list[dict] = []
        self.events: list[dict] = []
        self.started = False
        self.agent_id = "agt_fake"
        self.calls: dict[str, str] = {}        # call_id -> tool name
        self.resolutions: list[dict] = []      # every resolve request body, in order
        self.nudges = 0
        self.stream_requests = 0
        self.run_id = None
        self._n = 0
        self._tool_results: dict[str, dict] = {}

    # ---- plumbing ----
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _emit(self, event_type: str, payload: dict) -> None:
        self.events.append({"seq": len(self.events) + 1, "event_type": event_type, "payload": payload,
                            "run_id": "run_1"})

    def _call(self, name: str, tool_input: dict) -> None:
        self._n += 1
        call_id = f"call_{self._n}"
        self.calls[call_id] = name
        self._emit("agent.custom_tool_use", {"phase": "requested", "callId": call_id,
                                             "toolCallId": f"tc_{self._n}", "name": name, "input": tool_input})

    def _finish(self, text: str, status: str = "succeeded") -> None:
        self._emit("agent.assistant", {"message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
        self._emit("run.finished", {"status": status})

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.split("/service/v1", 1)[-1]
        method = request.method
        body = json.loads(request.content) if request.content else None
        self.requests.append({"method": method, "path": path, "body": body,
                              "headers": dict(request.headers), "query": dict(request.url.params)})
        aid = self.agent_id

        if method == "GET" and path == "/models":
            return httpx.Response(200, json=[
                {"model": "fake/old-model", "selectable": False, "default_for": ["model"]},
                {"model": "fake/chat-1", "selectable": True, "default_for": ["model"]}])
        if method == "POST" and path == "/agents":
            return httpx.Response(200, json={"agent_id": aid, "config_version": 1})
        if method == "GET" and path == f"/agents/{aid}":
            return httpx.Response(200, json={"agent_id": aid, "declared": {}, "status": {
                "desired_state": "running" if self.started else "stopped", "config_version": 1}})
        if method == "POST" and path == f"/agents/{aid}/start":
            self.started = True
            return httpx.Response(200, json={"warnings": []})
        if method == "PUT" and path == f"/agents/{aid}":
            return httpx.Response(200, json={"agent_id": aid})
        if method == "POST" and path == f"/agents/{aid}/sessions":
            if self.fail_create_session:
                return httpx.Response(500, json={"error": {"type": "platform.internal", "message": "boom"}})
            text = body["initial_events"][0]["content"]
            self.run_id = int(re.search(r"Procurement run (\d+)", text).group(1))
            self._emit("run.started", {"trigger": "user"})
            if self.behaviour != "silent":
                self._call("get_procurement_requirement", {"procurement_run_id": self.run_id})
            return httpx.Response(200, json={"session_id": "ses_1"})
        if method == "GET" and path == f"/agents/{aid}/sessions/ses_1/events/stream":
            return self._stream(request)
        if method == "POST" and path == f"/agents/{aid}/sessions/ses_1/events":
            return self._post_events(body)
        m = re.fullmatch(rf"/agents/{aid}/custom_tool_calls/(call_\d+)/result", path)
        if method == "POST" and m:
            return self._resolve(m.group(1), body)
        return httpx.Response(404, json={"error": {"type": "not_found", "message": f"{method} {path}"}})

    # ---- streaming ----
    def _stream(self, request: httpx.Request) -> httpx.Response:
        self.stream_requests += 1
        if self.fail_first_stream and self.stream_requests == 1:
            return httpx.Response(503, json={"error": {"type": "platform.unavailable", "message": "try again"}})
        after = 0
        cursor = request.url.params.get("cursor")
        if cursor and not self.replay_all:
            after = int(cursor.split(":")[1])
        chunks = []
        for ev in self.events:
            if ev["seq"] > after:
                chunks.append(f"id: pse1:{ev['seq']}\nevent: message\ndata: {json.dumps(ev)}\n\n")
        # a finite body: the stream "ends" after the events so far, like an idle close.
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="".join(chunks))

    def _post_events(self, body: dict) -> httpx.Response:
        events = body["events"]
        if any("submit_recommendation" in (e.get("content") or "") for e in events):
            self.nudges += 1
            if self.behaviour == "no_submit_then_nudge":
                self._emit("run.started", {"trigger": "user"})
                self._submit_best()
            elif self.behaviour == "never_submit":
                self._emit("run.started", {"trigger": "user"})
                self._finish("I could not decide.")
        return httpx.Response(202, json={"events": [{"accepted": True, "type": e["type"]} for e in events]})

    # ---- the scripted model ----
    def _resolve(self, call_id: str, body: dict) -> httpx.Response:
        self.resolutions.append({"call_id": call_id, "name": self.calls[call_id], **body})
        value = body["content"][0]["value"]
        name = self.calls[call_id]
        self._tool_results[name] = value
        is_error = bool(body.get("is_error"))

        if name == "get_procurement_requirement":
            self._call("get_existing_supplier_options",
                       {"inventory_item_id": value["requirement"]["item"]["id"]})
        elif name == "get_existing_supplier_options":
            self._call("search_market_prices", {"inventory_item_id": self._item_id()})
        elif name == "search_market_prices":
            if self.behaviour == "run_failed":
                self._emit("agent.error", {"errorMessage": "openai-responses: OpenAI API error (402): 402 insufficient_credits"})
                self._finish("Something went wrong.", status="failed")
            elif self.behaviour == "bad_option_then_good":
                self._call("submit_recommendation", self._submission("opt-that-does-not-exist"))
            elif self.behaviour == "inject_numbers_then_good":
                # a model trying to slip its own (wrong) total into the recommendation
                self._call("submit_recommendation",
                           {**self._submission(self._best_option()["option_id"]), "estimated_total_landed_cost": 1.0})
            elif self.behaviour in ("no_submit_then_nudge", "never_submit"):
                self._finish("Here is my analysis, but no submission yet.")
            else:
                self._submit_best()
        elif name == "submit_recommendation":
            if is_error:
                self._submit_best()  # the agent reads the error and corrects itself
            else:
                self._finish("Recommendation submitted for approval.")
        return httpx.Response(202, json={"call_id": call_id, "status": "pending", "signaled": True})

    def _item_id(self) -> int:
        return self._tool_results["get_procurement_requirement"]["requirement"]["item"]["id"]

    def _all_options(self) -> list[dict]:
        return [*self._tool_results["get_existing_supplier_options"]["options"],
                *self._tool_results["search_market_prices"]["options"]]

    def _best_option(self) -> dict:
        ok = [o for o in self._all_options() if o["lead_time_known"] and o["avoids_stockout"]]
        return min(ok, key=lambda o: o["estimated_total_landed_cost"])

    def _submission(self, option_id: str) -> dict:
        return {"recommended_option_id": option_id,
                "reasons": ["Fake model: cheapest option that arrives before the stockout."],
                "risks_and_uncertainties": ["Fake model: price may not be confirmed."],
                "proposed_next_action": "Draft an RFQ for human approval."}

    def _submit_best(self) -> None:
        self._call("submit_recommendation", self._submission(self._best_option()["option_id"]))
