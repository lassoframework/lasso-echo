import pytest
from agent.jobs.lasso_feed_runway import _coverage


@pytest.mark.parametrize("missing", [True, False])
def test_legacy_wildcard_requires_explicit_identity(missing):
    row = dict(gym_id="lasso", account="instagram", post_date="2026-11-01",
               variant_status="active", status="pending", format="story", slot_index=None)
    if not missing:
        row["logical_post_id"] = ""
    with pytest.raises(ValueError, match="ambiguous logical identity"):
        _coverage([row], "2026-10-08", "2026-11-06")
