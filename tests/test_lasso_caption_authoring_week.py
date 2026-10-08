"""Real week-eight authority packets (4 Summit dated CTAs, 4 FullGym book rows);
fake Responses transport, no provider spend."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest
from agent import caption_ledger, copy_gate, lasso_caption_authoring as author
from agent.image_engine import AstraImageEngine

CASES_PATH = Path("/Users/blakeruff/.codex/reviews/echo-lasso-cadence-20261008/week-eight-authority-cases.json")
CASES = json.loads(CASES_PATH.read_text()) if CASES_PATH.exists() else []
OCT9 = json.loads((Path(__file__).parent / "fixtures/lasso_caption_author_oct9.json").read_text())

pytestmark = pytest.mark.skipif(not CASES, reason="week-eight authority cases input unavailable")

# Authored body wording only; the mock writer appends the packet canonical CTA
# exactly. Numbers and required names mirror each packet's approved context.
TEXTS = {
    # Summit, dated terminal CTA "Get tickets" (on_image.cta declared)
    "c32e4432-78a2-571f-9090-02466514ce44":
        "Nashville is the destination.\nA clearer growth plan is the reason.\n\n"
        "Spend November 7 and 8 at Virgin Hotels Nashville building the next version "
        "of your gym with a clear plan you can take home.",
    # Summit, dated terminal CTA "Register" (on_image.cta declared)
    "a665ad0c-3a27-4b60-83b6-271d565c2626":
        "The next year will arrive either way.\nMeet it with a real plan.\n\n"
        "Build yours alongside 100 serious operators who are working on the same "
        "problems you face every week.",
    "85e4195b-5532-4279-bf15-6c025c19ae6b":
        "Big goals need more than motivation.\nThey need a working sequence.\n\n"
        "Build yours in Nashville this November and leave with the steps in the "
        "right order for your gym.",
    "01bfbfb7-d5b2-44a0-be78-6e51e6dc4e2c":
        "Your gym deserves a weekend of real focus.\nBuild the next version of it.\n\n"
        "Join the LASSO Growth Summit at Virgin Hotels Nashville and spend the "
        "days working on the plan instead of the noise.",
    # Book: Copy That Sells, verbatim lasso_now Body authority, approved
    # editorial CTA "Send this to a gym owner."
    "21b39971-eae1-5ac3-8676-9f8f9e7befd6":
        "Short copy earns the pause.\nLong copy earns the decision.\n\n"
        "If someone doesn't stop scrolling, your copy doesn't matter, so the opening "
        "line carries the work before any other line gets read.",
    # Book: Client Avatars, exact approved book campaign master caption
    "3decbe20-00af-4687-8996-ae3211139d0b":
        "The right member should see themselves in your message.\nSpeak to a real person.\n\n"
        "Start with who you help, name the problem in their words and show the "
        "result they want, then make the next step a simple conversation.",
    # Book: The Three Levers, exact approved book campaign master caption
    "60d7b1fc-3abc-46c0-8c4e-7b9dd4e0db0b":
        "Before you scale your ads, check the rest of the system.\nThe levers move together.\n\n"
        "Retention keeps the members you already serve, conversion helps interested "
        "people commit and lead flow brings more of the right people in.",
    # Book: The Halo Effect, exact approved book campaign master caption
    "93ba1f19-fda4-4739-8e39-4e6530c742ee":
        "Judge your marketing by the whole picture.\nNot only the dashboard.\n\n"
        "A person who sees your ad may later search for your gym, visit your website "
        "or ask a friend, so weigh trends across channels next to direct leads and signups.",
}

OCT9_PACKET_KEYS = {
    "6b2bd923-d2de-53e1-a588-aa4dd50eca48": "cc3d5aef5007e0331a746378cdca0288504545914d4ea8fb6ba5748bbb357d1f",
    "860b73e2-b37c-4ecb-b9fd-0e89028e2e61": "5694f05b7555a064e81fbd9b0b72a96e71a834412a3c6f94b9063b7f621a9af5",
    "f58249ad-2476-4d07-8403-f39f4171866b": "25bc345e267196cef4b67770a880a1a8f75fcca3368502ed1759bc4859d07276",
    "fe2c7f8e-2e5c-47a4-a916-707dd383ffa0": "39b77b0ae679dcf74712758d70b63fbd3d800623930faedc86c41265d13f456b",
}

SUMMIT_ROWS = [row for row in CASES if row["pillar"] == "summit"]
BOOK_ROWS = [row for row in CASES if row["pillar"] == "book"]


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


def responses(row, *, alter_writer=None, alter_review=None):
    calls = []
    def transport(url, headers, payload):
        assert payload["model"] == "gpt-6-astra" and payload["store"] is False
        data = json.loads(payload["input"][1]["content"][0]["text"])
        packet = data["source_packet"]
        calls.append(payload)
        text = data.get("candidate") or candidate(row, packet)
        fact = next(s for s in packet["sources"] if s["text"])
        claim = {"text": text.splitlines()[0], "source_id": fact["source_id"], "quote": fact["text"]}
        if len(calls) == 1:
            result = {"caption": text, "claims": [claim]}
            if alter_writer:
                alter_writer(result, packet)
            ident = "resp_week8_writer_mock"
        else:
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
            ident = "resp_week8_reviewer_mock"
        return 200, json.dumps({"id": ident, "model": "gpt-6-astra", "status": "completed", "output": [
            {"content": [{"type": "output_text", "text": json.dumps(result)}]}]})
    return AstraImageEngine("test-only", transport=transport), calls


@pytest.fixture(autouse=True)
def armed(monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_CAPTION_AUTHOR", "true")
    originals = {r["caption"] for r in CASES}
    monkeypatch.setattr(caption_ledger, "is_blocked_strict", lambda gym, text, day: text in originals)


def test_input_is_exactly_the_eight_actual_pending_rows():
    assert len(CASES) == 8 and len(SUMMIT_ROWS) == 4 and len(BOOK_ROWS) == 4
    assert all(row["status"] == "pending" and row["variant_status"] == "active" for row in CASES)
    assert all(row["published_at"] is None and row["publish_claim_token"] is None for row in CASES)


@pytest.mark.parametrize("row", CASES, ids=lambda row: row["id"][:8])
def test_all_eight_actual_rows_resolve_approved_source_packets_and_receipts(row):
    packet = author._source_packet(row)
    assert packet["sources"] and packet["target"]["pillar"] == row["pillar"]
    assert packet["old_caption_sha256"] == author._sha(row["caption"])
    engine, calls = responses(row)
    cache = Cache()
    result = author.author(row, [row["caption"]], gate_fn=lambda: True, engine=engine, cache=cache, now_fn=lambda: 100)
    assert result["ok"] and result["writer_calls"] == result["reviewer_calls"] == 1
    assert len(calls) == 2 and result["receipt_sha256"]
    record = json.loads(cache.values[result["receipt_key"]])["receipt"]
    assert record["writer_response_id"] != record["reviewer_response_id"]
    assert record["caption_sha256"] == author._sha(result["caption"])
    assert record["source_packet"]["cta_words_and_destination_equivalent"] is True
    assert copy_gate.format_caption(result["caption"]) == result["caption"]
    again = author.author(row, [row["caption"]], gate_fn=lambda: True, engine=engine, cache=cache, now_fn=lambda: 101)
    assert again["ok"] and again["reused"] == 1 and len(calls) == 2


@pytest.mark.parametrize("row", SUMMIT_ROWS, ids=lambda row: row["post_date"])
def test_summit_cta_is_exact_dated_terminal_sentence_with_declared_on_image_cta(row):
    packet = author._source_packet(row)
    entry = next(p for p in json.loads((author.ROOT / "brand_voice/lasso_summit_daily.json").read_text())["posts"]
                 if p["date"] == row["post_date"])
    sentences = [s.strip() for s in __import__("re").split(r"(?<=[.!?])\s+", entry["caption"]) if s.strip()]
    assert entry["on_image"]["cta"].strip() and entry["on_image"]["cta"].lower() in packet["original_cta"].lower()
    assert packet["original_cta"] == sentences[-1]
    assert any(source["source_id"].startswith(f"summit:{row['post_date']}:") for source in packet["sources"])


@pytest.mark.parametrize("row", BOOK_ROWS, ids=lambda row: row["post_date"] + row["id"][:4])
def test_book_cta_and_topic_come_from_approved_authority(row):
    packet = author._source_packet(row)
    original_cta = packet["original_cta"]
    if "lassoframework.com/book" in original_cta:
        # Exact verbatim approved book campaign master caption authorizes the
        # terminal book CTA; the old DB caption is never the source.
        campaign = json.loads((author.ROOT / "brand_voice/lasso_calendar_campaign.json").read_text())["assets"]
        matches = [a for a in campaign if a.get("category") == "book" and not a.get("is_sprint")
                   and author._norm(a.get("caption") or "") == author._norm(row["caption"])]
        assert matches and all(author._norm(a["caption"]).endswith(author._norm(original_cta)) for a in matches)
        assert "brand_voice/lasso_calendar_campaign.json" in packet["source_files"]
        assert any(source["source_id"].startswith("book-campaign:") for source in packet["sources"])
    else:
        assert original_cta == "Send this to a gym owner."
        assert any(source["topic"] == "Book: Copy That Sells" for source in packet["sources"])


@pytest.mark.parametrize("row", CASES, ids=lambda row: row["id"][:8])
def test_original_cta_exact_words_and_url_survive_documented_normalization(row):
    packet = author._source_packet(row)
    assert author._words(packet["original_cta"]) == author._words(packet["canonical_cta"])
    assert author.URL_RE.findall(packet["original_cta"]) == author.URL_RE.findall(packet["canonical_cta"])
    if ": https://" in packet["original_cta"]:
        assert ".\n\nhttps://" in packet["canonical_cta"]


@pytest.mark.parametrize("row", CASES, ids=lambda row: row["id"][:8])
def test_source_file_hashes_are_current_file_bytes(row):
    packet = author._source_packet(row)
    for name, sha in packet["source_files"].items():
        assert hashlib.sha256((author.ROOT / name).read_bytes()).hexdigest() == sha


def test_inline_final_paragraph_yields_single_terminal_cta_not_body():
    row = next(r for r in CASES if r["id"] == "3decbe20-00af-4687-8996-ae3211139d0b")
    packet = author._source_packet(row)
    assert packet["original_cta"] == "Claim your copy of The Full Gym: https://lassoframework.com/book"
    assert "book a conversation" not in packet["original_cta"]


@pytest.mark.parametrize("row", OCT9, ids=lambda row: row["id"][:8])
def test_current_oct9_packets_are_unchanged(row):
    packet = author._source_packet(row)
    assert author._sha(author._canonical(packet)) == OCT9_PACKET_KEYS[row["id"]]


def test_unknown_or_unsupported_topic_refuses_with_zero_calls():
    row = deepcopy(CASES[0])
    row["caption"] = "Our smoothie bar menu is the best in town.\n\nGet tickets at lassoframework.com/summit"
    engine, calls = responses(CASES[0])
    result = author.author(row, [], gate_fn=lambda: True, engine=engine, cache=Cache())
    assert not result["ok"] and calls == []


def test_mixed_or_guessed_summit_action_refuses_with_zero_calls():
    row = deepcopy(SUMMIT_ROWS[0])
    lines = [line for line in row["caption"].splitlines() if line.strip()]
    row["caption"] = "\n".join(lines[:-1]) + "\nReserve your seat at lassoframework.com/summit"
    engine, calls = responses(row)
    result = author.author(row, [], gate_fn=lambda: True, engine=engine, cache=Cache())
    assert not result["ok"] and result["reason"] == "approved same-topic source or original CTA unavailable"
    assert calls == []


def test_book_manifest_match_requires_exact_slot_identity():
    row = deepcopy(next(r for r in BOOK_ROWS if r["id"].startswith("60d7b1fc")))
    row["slot_index"] = 2  # same exact source caption, wrong slot identity
    engine, calls = responses(row)
    result = author.author(row, [], gate_fn=lambda: True, engine=engine, cache=Cache())
    assert not result["ok"] and calls == []


def test_book_manifest_matches_align_category_slot_and_caption():
    campaign = json.loads((author.ROOT / "brand_voice/lasso_calendar_campaign.json").read_text())["assets"]
    expected = {"3decbe20": "09", "60d7b1fc": "11", "93ba1f19": "12"}
    for row in BOOK_ROWS:
        prefix = row["id"][:8]
        if prefix not in expected:
            continue
        asset = next(a for a in campaign if a["id"] == expected[prefix])
        assert asset["category"] == "book" and not asset["is_sprint"]
        assert asset["slot_index"] == row["slot_index"]
        assert author._norm(asset["caption"]) == author._norm(row["caption"])
        assert author._norm(asset["caption"]).endswith(
            author._norm("Claim your copy of The Full Gym: https://lassoframework.com/book"))


def test_unsupported_book_cta_url_refuses_with_zero_calls():
    row = deepcopy(BOOK_ROWS[1])
    row["caption"] = row["caption"].replace("lassoframework.com/book", "lassoframework.com/fullgym")
    engine, calls = responses(row)
    result = author.author(row, [], gate_fn=lambda: True, engine=engine, cache=Cache())
    assert not result["ok"] and calls == []


@pytest.mark.parametrize("bad", ["number", "url", "cta", "unknown_source"])
def test_writer_numeric_url_second_action_and_source_mismatch_hold(bad):
    row = CASES[0]
    def alter(result, packet):
        text = result["caption"]
        if bad == "number": text = "47 new members joined.\n" + text
        elif bad == "url": text = text.replace("lassoframework.com/summit", "bad.example/summit")
        elif bad == "cta": text += "\n\nBook a call."
        elif bad == "unknown_source": result["claims"][0]["source_id"] = "lasso_now.md:Book: Pricing Confidence:hooks:0"
        result["caption"] = text
    engine, calls = responses(row, alter_writer=alter)
    result = author.author(row, [row["caption"]], gate_fn=lambda: True, engine=engine, cache=Cache())
    assert not result["ok"] and len(calls) == 1 and result["reviewer_calls"] == 0


def test_source_change_after_writer_invalidates_before_independent_review(monkeypatch):
    row = CASES[0]; real = author._source_packet; calls = []
    def packet(target):
        value = real(target)
        if calls:
            value["source_files"]["brand_voice/lasso_summit_daily.json"] = "stale"
        return value
    monkeypatch.setattr(author, "_source_packet", packet)
    engine, api_calls = responses(row, alter_writer=lambda result, pkt: calls.append(1))
    result = author.author(row, [], gate_fn=lambda: True, engine=engine, cache=Cache())
    assert not result["ok"] and len(api_calls) == 1


def test_current_receipt_binds_expected_caption_and_fresh_sources(monkeypatch):
    row = CASES[0]; engine, calls = responses(row); cache = Cache()
    result = author.author(row, [], gate_fn=lambda: True, engine=engine, cache=cache)
    assert result["ok"]
    assert author.current_receipt_valid(row, [], result["receipt_key"], expected_caption=result["caption"],
                                        gate_fn=lambda: True, cache=cache)
    assert not author.current_receipt_valid(row, [], result["receipt_key"], expected_caption=result["caption"] + " ",
                                            gate_fn=lambda: True, cache=cache)
    assert len(calls) == 2
