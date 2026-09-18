from datetime import date

from agent.client_media_sync import (_PierceWeekStore, _existing_feed_count,
                                     pierce_weekly_window)


def test_friday_stages_next_week_and_other_days_hold_current_week():
    assert pierce_weekly_window(date(2026, 9, 18)) == (date(2026, 9, 19), 7)
    assert pierce_weekly_window(date(2026, 9, 21)) == (date(2026, 9, 19), 7)
    assert pierce_weekly_window(date(2026, 9, 25)) == (date(2026, 9, 26), 7)


def test_week_store_preserves_unapproved_rows_outside_week():
    class Store:
        def list_month(self, base, month):
            return [{"post_date": "2026-09-20"},
                    {"post_date": "2026-09-28"}]

        def delete_month(self, base, month, *, preserve_dates):
            assert base == "piercefitness"
            assert month == "2026-09"
            assert set(preserve_dates) == {"2026-09-28", "2026-09-21"}
            return 1

    weekly = _PierceWeekStore(Store(), date(2026, 9, 19), 7)
    assert weekly.delete_month("piercefitness", "2026-09",
                               preserve_dates=("2026-09-21",)) == 1


def test_week_count_excludes_older_published_feeds():
    class Store:
        def list_month(self, base, month):
            return [{"post_date": "2026-09-18", "format": "feed",
                     "account": "instagram", "status": "published"},
                    {"post_date": "2026-09-19", "format": "feed",
                     "account": "instagram", "status": "pending"}]

    first = date(2026, 9, 19)
    assert _existing_feed_count(Store(), "piercefitness", first, 7) == (2, True)
    assert _existing_feed_count(_PierceWeekStore(Store(), first, 7),
                                "piercefitness", first, 7) == (1, True)
