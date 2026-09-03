import pytest
from pydantic import ValidationError

from leadforge.models import ICP


def test_icp_hash_deterministic(sample_icp):
    h1 = sample_icp.icp_hash()
    h2 = ICP.model_validate(sample_icp.model_dump()).icp_hash()
    assert h1 == h2 and len(h1) == 12


_GEO = {"areas": ["Austin, TX"], "country": "US"}


def test_bad_hard_qualifier_rejected():
    with pytest.raises(ValidationError):
        ICP.model_validate({
            "campaign": "x", "offer": {"what": "y"},
            "target": {"categories": ["z"], "geography": _GEO},
            "qualify": {"hard": ["not_a_real_qualifier"]},
        })


def test_prefixed_qualifiers_ok():
    icp = ICP.model_validate({
        "campaign": "x", "offer": {"what": "y"},
        "target": {"categories": ["z"], "geography": _GEO},
        "qualify": {"hard": ["competitor:acme", "existing_client:beta"]},
    })
    assert "competitor:acme" in icp.qualify.hard


def test_campaign_must_be_slug():
    with pytest.raises(ValidationError):
        ICP.model_validate({
            "campaign": "has spaces", "offer": {"what": "y"},
            "target": {"categories": ["z"], "geography": _GEO},
        })


def test_categories_bounds():
    with pytest.raises(ValidationError):
        ICP.model_validate({
            "campaign": "x", "offer": {"what": "y"},
            "target": {"categories": [], "geography": _GEO},
        })


# --- country / vague-location guards (a vague area = garbage results) ----------------------
def test_country_is_required():
    with pytest.raises(ValidationError) as e:
        ICP.model_validate({
            "campaign": "x", "offer": {"what": "y"},
            "target": {"categories": ["z"], "geography": {"areas": ["Austin, TX"]}},
        })
    assert "country" in str(e.value)


def test_country_must_be_valid_iso2():
    for bad in ("USA", "u", "ZZ", "12"):
        with pytest.raises(ValidationError):
            ICP.model_validate({
                "campaign": "x", "offer": {"what": "y"},
                "target": {"categories": ["z"], "geography": {"areas": ["Austin, TX"], "country": bad}},
            })


def test_country_normalized_to_upper():
    icp = ICP.model_validate({
        "campaign": "x", "offer": {"what": "y"},
        "target": {"categories": ["z"], "geography": {"areas": ["Cairo"], "country": "eg"}},
    })
    assert icp.target.geography.country == "EG"


def test_too_short_area_rejected():
    with pytest.raises(ValidationError):
        ICP.model_validate({
            "campaign": "x", "offer": {"what": "y"},
            "target": {"categories": ["z"], "geography": {"areas": ["TX"], "country": "US"}},
        })



def test_icp_hash_ignores_caps_and_notes(sample_icp):
    """Raising a cap mid-campaign must resume the same run, not re-plan it (2026-09-03)."""
    base = sample_icp.icp_hash()
    bumped = sample_icp.model_copy(update={"caps": sample_icp.caps.model_copy(update={"max_leads": 99999})})
    noted = sample_icp.model_copy(update={"notes": "anything"})
    assert bumped.icp_hash() == base and noted.icp_hash() == base
    other = sample_icp.model_copy(update={"campaign": "other-campaign"})
    assert other.icp_hash() != base


def test_legacy_hash_includes_caps(sample_icp):
    assert sample_icp.icp_hash_legacy() != sample_icp.icp_hash()
    bumped = sample_icp.model_copy(update={"caps": sample_icp.caps.model_copy(update={"max_leads": 99999})})
    assert bumped.icp_hash_legacy() != sample_icp.icp_hash_legacy()
