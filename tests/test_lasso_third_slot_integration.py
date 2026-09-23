from types import SimpleNamespace

from agent import calendar_autopublish as autopublish
from agent import config
from agent import real_calendar_mirror
from agent.portal_calendar_store import preserve_and_prune


WINDOW_DAY = "2026-09-23"


def _enable_window(monkeypatch):
    monkeypatch.setattr(
        config,
        "lasso_summit_daily_enabled",
        lambda day_key=None: day_key == WINDOW_DAY,
        raising=False,
    )


def test_lasso_third_feed_has_noon_slot_without_moving_regular_or_story(monkeypatch):
    _enable_window(monkeypatch)
    monkeypatch.setattr(config, "cadence_2x_enabled", lambda: True)
    monkeypatch.setattr(config, "cadence_slot_times", lambda: ("07:30", "18:30"))

    base = {"gym_id": "lasso", "post_date": WINDOW_DAY, "format": "feed"}
    assert autopublish.slot_time_for_row(dict(base, slot_index=0)) == "07:30"
    assert autopublish.slot_time_for_row(dict(base, slot_index=1)) == "18:30"
    assert autopublish.slot_time_for_row(dict(base, slot_index=2)) == "12:00"
    assert autopublish.slot_time_for_row(
        dict(base, format="story", slot_index=2, id="story")
    ) == "12:30"

    # The same ordinal cannot open another tenant or a day outside the campaign.
    assert autopublish.slot_time_for_row(
        dict(base, gym_id="client", slot_index=2, id="client")
    ) != "12:00"
    assert autopublish.slot_time_for_row(
        dict(base, post_date="2026-11-09", slot_index=2, id="late")
    ) != "12:00"


def test_real_calendar_mirror_preserves_third_slot_ordinal():
    draft = SimpleNamespace(
        draft_id="summit-third",
        account_key="lasso_ig",
        platform="instagram",
        caption="Summit",
        hashtags=[],
        creative_public_url="https://cdn.example/summit.png",
        scheduled_for=WINDOW_DAY,
        day_key=WINDOW_DAY,
        category="summit",
        status="pending",
        is_story=False,
        draft_type="feed",
        cadence_slot_index=2,
    )
    store = SimpleNamespace(list_for_account=lambda _: [draft])

    rows = real_calendar_mirror.collect_real_drafts("lasso", store)

    assert len(rows) == 1
    assert rows[0]["slot_index"] == 2


def test_preserve_and_prune_admits_ordinal_two_only_for_lasso_feed_window(monkeypatch):
    _enable_window(monkeypatch)
    monkeypatch.setattr(config, "lasso_editorial_calendar_enabled", lambda: True)
    monkeypatch.setattr(config, "cadence_2x_enabled", lambda: True)

    class Store:
        def gym_posts_per_day(self, _):
            return 2

        def list_month(self, *_):
            return []

    def row(day=WINDOW_DAY, fmt="feed"):
        return {
            "post_date": day,
            "account": "instagram",
            "format": fmt,
            "slot_index": 2,
            "caption": f"{day}-{fmt}",
            "image_url": f"https://cdn.example/{day}-{fmt}.png",
            "status": "pending",
        }

    kept, _ = preserve_and_prune(Store(), "lasso", ["2026-09"], [row()])
    assert [item["slot_index"] for item in kept] == [2]
    assert preserve_and_prune(
        Store(), "lasso", ["2026-11"], [row("2026-11-09")]
    )[0] == []
    assert preserve_and_prune(Store(), "lasso", ["2026-09"], [row(fmt="story")])[0] == []


def test_publish_capacity_uses_actual_local_claim_day_and_stays_feed_only(monkeypatch):
    _enable_window(monkeypatch)
    monkeypatch.setattr(config, "cadence_2x_enabled", lambda: True)

    class Store:
        def gym_posts_per_day(self, _):
            return 2

    feed = {"format": "feed", "post_date": "2026-09-22", "slot_index": 2}
    story = {"format": "story", "post_date": WINDOW_DAY, "slot_index": 2}

    # Capacity follows the refreshed gym-local reservation day, not the stale row
    # date or the worker/server UTC date.
    assert autopublish._publish_capacity("lasso", feed, Store(), WINDOW_DAY) == 3
    assert autopublish._publish_capacity("lasso", feed, Store(), "2026-11-09") == 2
    assert autopublish._publish_capacity("lasso", story, Store(), WINDOW_DAY) == 2
    assert autopublish._publish_capacity("client", feed, Store(), WINDOW_DAY) == 2
