"""U4.8 guardrail tests that apply NOW, before the unit is implemented.

The LinkedIn exclusion and the default-off behavior are boundaries (icm/SCOPE.md), not implementation
details — so they are tested from day one and must keep passing after U4.8 lands.
"""

import pytest

from leadforge.providers.social import EXCLUDED_NETWORKS, filter_networks, is_available


def test_linkedin_is_always_filtered_out():
    links = {
        "youtube": "https://youtube.com/@shop",
        "facebook": "https://facebook.com/shop",
        "linkedin": "https://linkedin.com/company/shop",
        "LinkedIn": "https://linkedin.com/company/shop2",  # case-insensitive
    }
    out = filter_networks(links)
    assert "linkedin" not in {k.lower() for k in out}
    assert set(out) == {"youtube", "facebook"}


def test_linkedin_is_declared_excluded():
    assert "linkedin" in EXCLUDED_NETWORKS


def test_social_is_disabled_by_default(cfg):
    assert cfg.social.enabled is False
    ok, reason = is_available(cfg)
    assert ok is False and reason


def test_social_signals_are_known_qualifiers():
    """The signal names the unit will emit must already be valid ICP soft qualifiers."""
    from leadforge.models import SOFT_QUALIFIERS

    for sig in ("stale_social", "no_social_presence", "no_video_presence"):
        assert sig in SOFT_QUALIFIERS


def test_social_signals_have_hook_templates():
    from pathlib import Path

    import yaml

    rubric = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "scoring.default.yaml").read_text())
    for sig in ("stale_social", "no_social_presence", "no_video_presence"):
        assert sig in rubric["hooks"], f"missing outreach hook template for {sig}"


@pytest.mark.xfail(reason="ICM U4.8: presence() not implemented yet", strict=False)
def test_presence_shape(cfg):
    from leadforge.providers.social import presence

    out = presence({"youtube": "https://youtube.com/@shop"}, cfg)
    assert set(out["youtube"]) >= {"url", "exists", "last_post_at", "status"}
