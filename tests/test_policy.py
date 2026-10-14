from leadforge.enrich.policy import policy_for


def test_jurisdiction_policy_is_conservative_for_known_markets():
    policy = policy_for("GB")
    assert policy.public_business_data_allowed is True
    assert policy.personal_email_requires_review is True
    assert policy.inferred_personal_email_allowed is False
    assert "public_registry" in policy.permitted_sources


def test_unknown_jurisdiction_fails_closed():
    policy = policy_for("ZZ")
    assert policy.public_business_data_allowed is False
    assert policy.lawful_basis_review == "manual_review_required"
    assert policy.permitted_sources == ()
