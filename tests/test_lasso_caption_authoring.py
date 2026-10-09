"""Real Oct9 authority packets; fake Responses transport, no provider spend."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
from agent import caption_ledger, copy_gate, lasso_caption_authoring as author
from agent.image_engine import AstraImageEngine

CASES = json.loads((Path(__file__).parent / "fixtures/lasso_caption_author_oct9.json").read_text())
TEXTS = {
    "6b2bd923-d2de-53e1-a588-aa4dd50eca48": "Bring your growth questions to Nashville.\nSpend two days in one focused room.\n\nAt the LASSO Growth Summit, work on a complete 2027 growth playbook you can take home and use to guide your gym's next steps.",
    "860b73e2-b37c-4ecb-b9fd-0e89028e2e61": "When did your gym site last change?\nKeep your visitor's next step clear.\n\nAn update within 60 days is one of the six behaviors in the LASSO website audit. LASSO builds fast sites with StoryBrand copy and local SEO.",
    "f58249ad-2476-4d07-8403-f39f4171866b": "Does your first screen make your audience clear?\nHelp the right visitor recognize your gym.\n\nClear audience identification is one of the six behaviors in the LASSO website audit. LASSO builds StoryBrand written sites for gyms.",
    "fe2c7f8e-2e5c-47a4-a916-707dd383ffa0": "A full gym takes deliberate growth.\nUse word of mouth as a starting point.\n\nIt will not double your business on its own. Buyers want solutions to problems, and organic reach needs to be built or bought. Think like a business owner when planning growth.",
}


class Cache:
    def __init__(self):
        self.values = {}
    def kv_is_durable(self):
        return True
    def kv_get(self, key, default=""):
        return self.values.get(key, default)
    def kv_set(self, key, value):
        self.values[key] = value
    def kv_update(self, key, callback):
        result = callback(self.values.get(key, ""))
        if result is None:
            return False
        self.values[key] = result
        return True


def candidate(row, packet):
    text = TEXTS[row["id"]] + "\n\n" + packet["canonical_cta"]
    if packet["allowed_hashtags"]:
        text += "\n\n" + " ".join(packet["allowed_hashtags"])
    return text


def responses(row, *, alter_writer=None, alter_review=None, mutation=None, identical_ids=False):
    calls = []
    def transport(url, headers, payload):
        assert payload["model"] == "gpt-6-astra" and payload["text"]["format"]["type"] == "json_object"
        assert "tools" not in payload and "previous_response_id" not in payload
        assert payload["max_output_tokens"] == 3600 and payload["store"] is False
        assert all(c["type"] == "input_text" for item in payload["input"] for c in item["content"])
        data = json.loads(payload["input"][1]["content"][0]["text"])
        packet = data["source_packet"]
        calls.append(payload)
        text = data.get("candidate") or candidate(row, packet)
        # A controlled mock reviewer supplies its own claim mapping. Production
        # reviewers receive no writer mapping or self-grade/rationale.
        fact = next(s for s in packet["sources"] if s["text"])
        claim = {"text": text.splitlines()[0], "source_id": fact["source_id"], "quote": fact["text"]}
        if len(calls) == 1:
            assert "candidate" not in data
            result = {"caption": text, "claims": [claim], "self_grade": "never used"}
            if alter_writer:
                alter_writer(result, packet)
            ident = "resp_writer_actual_mock"
        else:
            assert "avoid_wording_context_NOT_AUTHORITY" not in data
            assert set(data) == {"source_packet", "candidate"}
            coverage = []
            for line in text.splitlines():
                if not line.strip():
                    continue
                if line in packet["canonical_cta"].splitlines():
                    coverage.append({"text": line, "kind": "action"})
                elif line.startswith("#"):
                    coverage.append({"text": line, "kind": "hashtag"})
                else:
                    source = max(packet["sources"], key=lambda s:
                        len(set(author._words(line)) & set(author._words(s["text"]))))
                    coverage.append({"text": line, "kind": "factual", "sources": [
                        {"source_id": source["source_id"], "quote": source["text"]}]})
            result = {"status": "PASS", "criteria": {k: True for k in author.CRITERIA},
                      "claims": [claim], "coverage": coverage, "issues": []}
            if alter_review:
                alter_review(result, packet)
            ident = "resp_writer_actual_mock" if identical_ids else "resp_reviewer_actual_mock"
        if mutation:
            mutation(len(calls))
        return 200, json.dumps({"id": ident, "model": "gpt-6-astra", "status": "completed", "output": [
            {"content": [{"type": "output_text", "text": json.dumps(result)}]}]})
    return AstraImageEngine("test-only", transport=transport), calls


@pytest.fixture(autouse=True)
def armed(monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_CAPTION_AUTHOR", "true")
    originals = {r["caption"] for r in CASES}
    monkeypatch.setattr(caption_ledger, "is_blocked_strict", lambda gym, text, day: text in originals)


@pytest.mark.parametrize("row", CASES, ids=lambda row: row["id"])
def test_all_four_actual_exhausted_oct9_cases_have_approved_authority_and_independent_receipts(row):
    packet = author._source_packet(row)
    assert packet["sources"] and packet["target"]["pillar"] == row["pillar"]
    assert packet["old_caption_sha256"] == author._sha(row["caption"])
    assert all("pending" not in source["file"].lower() for source in packet["sources"])
    engine, calls = responses(row)
    cache = Cache()
    result = author.author(row, [row["caption"]], gate_fn=lambda: True, engine=engine, cache=cache, now_fn=lambda: 100)
    assert result["ok"] and result["writer_calls"] == result["reviewer_calls"] == 1
    assert len(calls) == 2 and result["receipt_sha256"]
    record = json.loads(cache.values[result["receipt_key"]])["receipt"]
    assert record["writer_response_id"] != record["reviewer_response_id"]
    assert record["caption_sha256"] == author._sha(result["caption"])
    assert record["source_packet"]["cta_words_and_destination_equivalent"] is True
    # Accepted formatter preserves both grouping levels in the exact reviewed
    # and cached string; formatting does not alter words or URLs.
    assert "\n" in result["caption"].split("\n\n")[0] and "\n\n" in result["caption"]
    assert copy_gate.format_caption(result["caption"]) == result["caption"]
    reviewer_input = json.loads(calls[1]["input"][1]["content"][0]["text"])
    assert reviewer_input["candidate"] == result["caption"] and "writer_claims" not in reviewer_input
    again = author.author(row, [row["caption"]], gate_fn=lambda: True, engine=engine, cache=cache, now_fn=lambda: 101)
    assert again["ok"] and again["reused"] == 1 and len(calls) == 2


def test_website_legacy_cta_colon_normalization_preserves_every_word_and_url():
    row = next(r for r in CASES if r["pillar"] == "website")
    packet = author._source_packet(row)
    assert ": https://" in packet["original_cta"]
    assert "website.\n\nhttps://" in packet["canonical_cta"]
    assert author._words(packet["original_cta"]) == author._words(packet["canonical_cta"])
    assert author.URL_RE.findall(packet["original_cta"]) == author.URL_RE.findall(packet["canonical_cta"])


@pytest.mark.parametrize("bad", ["number", "price", "scarcity", "url", "cta", "mention", "unknown_source", "unquoted", "fuzzy", "words_number"])
def test_writer_false_claims_and_source_mismatch_hold_before_reviewer(bad):
    row = CASES[0]
    def alter(result, packet):
        text = result["caption"]
        if bad == "number": text = "99 new members.\n" + text
        elif bad == "words_number": text = "Twelve new members.\n" + text
        elif bad == "price": text = "$50 tickets.\n" + text
        elif bad == "scarcity": text = "Limited seats are selling fast.\n" + text
        elif bad == "url": text = text.replace("lassoframework.com/summit", "bad.example/summit")
        elif bad == "cta": text += "\n\nBook a call."
        elif bad == "mention": text += "\n\n@UnapprovedGym achieved record growth."
        elif bad == "unknown_source": result["claims"][0]["source_id"] = "03_social_proof_pending.md"
        elif bad == "unquoted": result["claims"][0]["quote"] = "No such approved sentence"
        elif bad == "fuzzy": text = row["caption"]
        result["caption"] = text
    engine, calls = responses(row, alter_writer=alter)
    result = author.author(row, [row["caption"]], gate_fn=lambda: True, engine=engine, cache=Cache())
    assert not result["ok"] and len(calls) == 1 and result["reviewer_calls"] == 0


@pytest.mark.parametrize("bad", ["criterion", "missing", "string_bool", "issues", "status", "claims", "same_id"])
def test_independent_review_must_be_literal_complete_and_separate(bad):
    row = CASES[0]
    def alter(review, packet):
        if bad == "criterion": review["criteria"]["same_topic_and_pillar"] = False
        elif bad == "missing": review["criteria"].pop("all_facts_supported")
        elif bad == "string_bool": review["criteria"]["all_facts_supported"] = "true"
        elif bad == "issues": review["issues"] = [{"severity": "critical", "reason": "unsupported"}]
        elif bad == "status": review["status"] = "FAIL"
        elif bad == "claims": review["claims"] = []
    engine, calls = responses(row, alter_review=alter, identical_ids=bad == "same_id")
    cache = Cache()
    result = author.author(row, [row["caption"]], gate_fn=lambda: True, engine=engine, cache=cache)
    assert not result["ok"] and len(calls) == 2
    assert all(json.loads(value).get("receipt") is None for value in cache.values.values())


def test_failed_attempt_cooldown_prevents_extra_calls():
    row = CASES[0]
    engine, calls = responses(row, alter_review=lambda result, packet: result.update(status="FAIL"))
    cache = Cache()
    first = author.author(row, [row["caption"]], gate_fn=lambda: True, engine=engine, cache=cache, now_fn=lambda: 100)
    second = author.author(row, [row["caption"]], gate_fn=lambda: True, engine=engine, cache=cache, now_fn=lambda: 101)
    assert not first["ok"] and second["reason"] == "caption author lease or cooldown" and len(calls) == 2


def test_gate_change_after_writer_stops_before_review(monkeypatch):
    row = CASES[0]
    engine, calls = responses(row, mutation=lambda count: monkeypatch.setenv("AGENT_LASSO_CAPTION_AUTHOR", "false"))
    result = author.author(row, [row["caption"]], gate_fn=lambda: True, engine=engine, cache=Cache())
    assert not result["ok"] and len(calls) == 1


def test_unknown_authority_and_manual_default_off_are_zero_calls(monkeypatch):
    row = deepcopy(CASES[0]); row["pillar"] = "unknown"
    engine, calls = responses(CASES[0])
    assert not author.author(row, [], gate_fn=lambda: True, engine=engine, cache=Cache())["ok"]
    assert not author.author(CASES[0], [], gate_fn=lambda: False, engine=engine, cache=Cache())["ok"]
    monkeypatch.delenv("AGENT_LASSO_CAPTION_AUTHOR")
    assert not author.author(CASES[0], [], gate_fn=lambda: True, engine=engine, cache=Cache())["ok"]
    assert calls == []


def test_unreadable_durable_store_and_claim_refusal_are_zero_calls():
    row = CASES[0]; engine, calls = responses(row)
    cache = Cache(); cache.kv_is_durable = lambda: False
    assert not author.author(row, [], gate_fn=lambda: True, engine=engine, cache=cache)["ok"]
    cache = Cache(); cache.kv_update = lambda *a: False
    assert not author.author(row, [], gate_fn=lambda: True, engine=engine, cache=cache)["ok"]
    assert calls == []


def test_source_change_after_writer_invalidates_before_independent_review(monkeypatch):
    row = CASES[0]; real = author._source_packet; calls = []
    def packet(target):
        value = real(target)
        if calls:
            value["source_files"]["changed"] = "different"
        return value
    monkeypatch.setattr(author, "_source_packet", packet)
    engine, api_calls = responses(row, mutation=lambda count: calls.append(count))
    result = author.author(row, [], gate_fn=lambda: True, engine=engine, cache=Cache())
    assert not result["ok"] and len(api_calls) == 1


def test_corrupt_or_now_blocked_pass_receipt_never_causes_more_requests(monkeypatch):
    row = CASES[0]; engine, calls = responses(row); cache = Cache()
    result = author.author(row, [], gate_fn=lambda: True, engine=engine, cache=cache)
    assert result["ok"]
    state = json.loads(cache.values[result["receipt_key"]]); record = state["receipt"]; record["caption"] += " changed"
    cache.values[result["receipt_key"]] = json.dumps(state)
    assert not author.author(row, [], gate_fn=lambda: True, engine=engine, cache=cache)["ok"]
    assert len(calls) == 2
    state["receipt"] = dict(record, caption=result["caption"])
    cache.values[result["receipt_key"]] = json.dumps(state)
    monkeypatch.setattr(caption_ledger, "is_blocked_strict", lambda *a: True)
    assert not author.author(row, [], gate_fn=lambda: True, engine=engine, cache=cache)["ok"]
    assert len(calls) == 2


@pytest.mark.parametrize("bad", ["http", "refusal", "missing_id", "wrong_model", "malformed"])
def test_actual_response_proof_failure_is_closed(bad):
    calls = []
    def transport(url, headers, payload):
        calls.append(payload)
        value = {"id": "resp_actual_mock", "model": author.MODEL, "status": "completed", "output": []}
        if bad == "http": return 429, "provider refusal"
        if bad == "missing_id": value.pop("id")
        if bad == "wrong_model": value["model"] = "different-model"
        if bad == "malformed": return 200, "not-json"
        return 200, json.dumps(value)
    result = author.author(CASES[0], [], gate_fn=lambda: True,
        engine=AstraImageEngine("test-only", transport=transport), cache=Cache())
    assert not result["ok"] and len(calls) == 1


def test_durable_receipt_readback_failure_does_not_accept_candidate():
    row = CASES[0]; engine, calls = responses(row); cache = Cache()
    update = cache.kv_update
    def omit_receipt(key, callback):
        result = update(key, callback)
        state = json.loads(cache.values[key])
        state.pop("receipt", None)
        cache.values[key] = json.dumps(state)
        return result
    cache.kv_update = omit_receipt
    result = author.author(row, [], gate_fn=lambda: True, engine=engine, cache=cache)
    assert not result["ok"] and result["reason"] == "caption author receipt readback unavailable"
    assert len(calls) == 2


def test_actual_sqlite_attempt_arbitration_prevents_duplicate_text_calls(monkeypatch, tmp_path):
    import threading
    from agent import db
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "author.db"))
    row = CASES[0]; entered = threading.Event(); release = threading.Event()
    engine, calls = responses(row, mutation=lambda count: (entered.set(), release.wait(5)) if count == 1 else None)
    results = []
    thread = threading.Thread(target=lambda: results.append(author.author(row, [], gate_fn=lambda: True,
        engine=engine, cache=db, now_fn=lambda: 100)))
    thread.start(); assert entered.wait(5)
    second = author.author(row, [], gate_fn=lambda: True, engine=engine, cache=db, now_fn=lambda: 101)
    assert not second["ok"] and second["reason"] == "caption author lease or cooldown"
    release.set(); thread.join(5)
    assert not thread.is_alive() and results[0]["ok"] and len(calls) == 2


@pytest.mark.parametrize("case", CASES, ids=lambda row: row["id"])
def test_real_oct9_exhaustion_enters_author_and_actual_guarded_repair(case, monkeypatch):
    from datetime import datetime
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo
    from agent import build_lock, config
    from agent.jobs import lasso_caption_recovery as recovery, grade_fix
    from test_lasso_caption_recovery import Store, row as complete_row
    book = [complete_row(**entry) for entry in CASES]
    store = Store(book)
    # Target only this lane/slot so each of the four original acceptance cases
    # independently proves the same real guarded repair path.
    for entry in store.rows:
        if entry["id"] != case["id"]:
            entry["status"] = "approved"
    monkeypatch.setenv("AGENT_LASSO_CAPTION_RECOVERY", "true")
    monkeypatch.setattr(recovery, "_gates", lambda *a: True)
    monkeypatch.setattr(config, "lasso_three_feed_enabled", lambda: True)
    monkeypatch.setattr(build_lock, "acquire", lambda *a, **kw: True)
    monkeypatch.setattr(build_lock, "release", lambda *a, **kw: None)
    monkeypatch.setattr(build_lock, "start_heartbeat", lambda *a, **kw: SimpleNamespace(stop=lambda: None))
    monkeypatch.setattr(grade_fix, "_lasso_caption_regen", lambda log: lambda *a: None)
    monkeypatch.setattr(recovery.real_month_run, "plan_and_build", lambda *a, **kw: [])
    stamps = []
    monkeypatch.setattr(caption_ledger, "record_staged_strict", lambda *a: stamps.append(a))
    engine, calls = responses(case); cache = Cache(); original_author = author.author
    original_valid = author.current_receipt_valid
    monkeypatch.setattr(author, "current_receipt_valid", lambda target, avoid, key, **kw:
        original_valid(target, avoid, key, **kw, cache=cache))
    monkeypatch.setattr(author, "author", lambda target, avoid, **kw:
        original_author(target, avoid, **kw, engine=engine, cache=cache))
    outcome = recovery.run(account_key="lasso_ig" if case["account"] == "instagram" else "lasso_fb",
                           now=datetime(2026, 10, 8, 9, tzinfo=ZoneInfo("America/New_York")), store=store)
    assert outcome["ok"] and outcome["repaired"] == 1 and len(calls) == 2
    updated = store.get_row("lasso", case["id"])
    assert updated["caption"] != case["caption"] and updated["logical_post_id"] is None
    assert updated["scheduled_at"] == case["scheduled_at"]
    assert updated["media_not_ready_reason"] == "caption_changed_needs_new_visual"
    assert len(stamps) == 1 and stamps[0][0] == "lasso"
    assert outcome["authoring"]["receipt_sha256"] and "caption" not in outcome["authoring"]
    assert all(store.get_row("lasso", entry["id"])["caption"] == entry["caption"] for entry in book if entry["id"] != case["id"])


@pytest.mark.parametrize("bad", ["missing", "first_hook_only", "duplicate", "wrong_order", "missing_factual_source", "numeric_rhetorical"])
def test_independent_coverage_must_exhaust_every_exact_line(bad):
    row = CASES[0]
    def alter(review, packet):
        if bad == "missing": review.pop("coverage")
        elif bad == "first_hook_only": review["coverage"] = review["coverage"][:1]
        elif bad == "duplicate": review["coverage"][1] = review["coverage"][0]
        elif bad == "wrong_order": review["coverage"] = list(reversed(review["coverage"]))
        elif bad == "missing_factual_source": review["coverage"][0]["sources"] = []
        elif bad == "numeric_rhetorical": review["coverage"][1]["kind"] = "rhetorical"
    engine, calls = responses(row, alter_review=alter)
    result = author.author(row, [], gate_fn=lambda: True, engine=engine, cache=Cache())
    assert not result["ok"] and len(calls) == 2


def test_cached_pass_later_blocked_preserves_receipt_and_gets_one_new_attempt_after_cooldown(monkeypatch):
    row = CASES[0]; cache = Cache(); first_engine, first_calls = responses(row)
    first = author.author(row, [], gate_fn=lambda: True, engine=first_engine, cache=cache, now_fn=lambda: 100)
    assert first["ok"]
    original_receipt = json.loads(cache.values[first["receipt_key"]])["receipt"]
    old_text = first["caption"]
    monkeypatch.setattr(caption_ledger, "is_blocked_strict", lambda gym, text, day:
                        text == row["caption"] or text == old_text)
    def changed(result, packet):
        result["caption"] = result["caption"].replace("Bring your growth questions to Nashville.",
                                                     "Take your growth planning to Nashville.")
        result["claims"][0]["text"] = result["caption"].splitlines()[0]
    engine, calls = responses(row, alter_writer=changed)
    early = author.author(row, [], gate_fn=lambda: True, engine=engine, cache=cache, now_fn=lambda: 101)
    assert not early["ok"] and calls == []
    later = author.author(row, [], gate_fn=lambda: True, engine=engine, cache=cache, now_fn=lambda: 1001)
    assert later["ok"] and later["caption"] != old_text and len(calls) == 2
    archived = json.loads(cache.values[first["receipt_key"]])["ineligible_receipts"]
    assert list(archived.values()) == [original_receipt]
    again = author.author(row, [], gate_fn=lambda: True, engine=engine, cache=cache, now_fn=lambda: 1002)
    assert again["ok"] and again["reused"] == 1 and len(calls) == 2


def test_current_receipt_source_and_runtime_validation_has_no_api_or_write(monkeypatch):
    row = CASES[0]; engine, calls = responses(row); cache = Cache()
    result = author.author(row, [], gate_fn=lambda: True, engine=engine, cache=cache)
    frozen = deepcopy(cache.values)
    assert author.current_receipt_valid(row, [], result["receipt_key"], expected_caption=result["caption"], gate_fn=lambda: True, cache=cache)
    monkeypatch.setenv("AGENT_LASSO_CAPTION_AUTHOR", "false")
    assert not author.current_receipt_valid(row, [], result["receipt_key"], expected_caption=result["caption"], gate_fn=lambda: True, cache=cache)
    assert cache.values == frozen and len(calls) == 2


@pytest.mark.parametrize("phase", [1, 2])
def test_expired_writer_cannot_review_or_overwrite_successor_receipt(phase):
    row = CASES[0]; cache = Cache(); clock = [100.0]; successor = {}
    def expire(count):
        if count != phase:
            return
        clock[0] = 1000.0
        key = next(iter(cache.values))
        state = json.loads(cache.values[key])
        state.update(owner="successor", until=2000, receipt={"successor_proof": "preserve"})
        cache.values[key] = json.dumps(state)
        successor.update(cache.values)
    engine, calls = responses(row, mutation=expire)
    result = author.author(row, [], gate_fn=lambda: True, engine=engine, cache=cache, now_fn=lambda: clock[0])
    assert not result["ok"] and result["reason"] == "caption author lease ownership expired"
    assert len(calls) == phase and cache.values == successor


def test_atomic_receipt_commit_rejects_owner_change_after_preflight():
    row = CASES[0]; cache = Cache(); update = cache.kv_update; successor = {}
    def race(key, callback):
        current = cache.values.get(key, "")
        proposal = callback(current)
        if proposal and json.loads(proposal).get("receipt"):
            state = json.loads(current); state.update(owner="successor", until=9999)
            cache.values[key] = json.dumps(state); successor.update(cache.values)
            return update(key, callback)
        return update(key, callback)
    cache.kv_update = race
    engine, calls = responses(row)
    result = author.author(row, [], gate_fn=lambda: True, engine=engine, cache=cache, now_fn=lambda: 100)
    assert not result["ok"] and result["reason"] == "caption author receipt ownership commit refused"
    assert len(calls) == 2 and cache.values == successor


def test_switched_valid_receipt_cannot_authorize_old_pending_caption():
    row = CASES[0]; cache = Cache(); engine, _ = responses(row)
    first = author.author(row, [], gate_fn=lambda: True, engine=engine, cache=cache)
    def changed(result, packet):
        result["caption"] = result["caption"].replace("Bring your growth questions to Nashville.",
                                                     "Take your growth planning to Nashville.")
        result["claims"][0]["text"] = result["caption"].splitlines()[0]
    alternate_cache = Cache(); alternate_engine, _ = responses(row, alter_writer=changed)
    second = author.author(row, [], gate_fn=lambda: True, engine=alternate_engine, cache=alternate_cache)
    assert first["ok"] and second["ok"] and first["caption"] != second["caption"]
    cache.values[first["receipt_key"]] = alternate_cache.values[second["receipt_key"]]
    assert author.current_receipt_valid(row, [], first["receipt_key"], expected_caption=second["caption"],
                                        gate_fn=lambda: True, cache=cache)
    assert not author.current_receipt_valid(row, [], first["receipt_key"], expected_caption=first["caption"],
                                            gate_fn=lambda: True, cache=cache)
