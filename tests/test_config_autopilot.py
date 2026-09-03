"""v0.4 autopilot config additions (U A, docs/08 ADR-015): `AgentCfg`, `PipelineCfg`, and the new
`DraftCfg` autopilot fields. Pure config-loading tests - no agent process is ever started here."""

from __future__ import annotations

from leadforge.config import AgentCfg, Config, DraftCfg, PipelineCfg, load_config


def test_config_defaults_include_agent_and_pipeline_sections():
    cfg = Config()
    assert isinstance(cfg.agent, AgentCfg)
    assert isinstance(cfg.pipeline, PipelineCfg)
    assert cfg.agent.command is None
    assert cfg.agent.model == "sonnet"
    assert cfg.agent.timeout_s == 900
    assert cfg.agent.batch == 40
    assert cfg.agent.max_batches == 50
    assert cfg.pipeline.autopilot is True


def test_draft_cfg_autopilot_defaults():
    d = DraftCfg()
    assert d.auto is True
    assert d.auto_purpose == "gainlev_leadgen"
    assert d.auto_tiers == ["A", "B"]
    assert d.auto_max == 500
    assert d.template_fallback is True


def test_draft_cfg_auto_tiers_default_is_not_a_shared_mutable():
    """Field(default_factory=...) discipline - two instances must not share one list object
    (a plain mutable default would leak edits across every Config in the process)."""
    a, b = DraftCfg(), DraftCfg()
    a.auto_tiers.append("C")
    assert b.auto_tiers == ["A", "B"]


def test_yaml_keys_mirror_field_names_for_agent_pipeline_draft(tmp_path):
    (tmp_path / "leadforge.yaml").write_text(
        "agent:\n"
        "  model: opus\n"
        "  timeout_s: 60\n"
        "  batch: 10\n"
        "  max_batches: 3\n"
        "pipeline:\n"
        "  autopilot: false\n"
        "draft:\n"
        "  auto: false\n"
        "  auto_purpose: follow_up\n"
        "  auto_tiers: [A]\n"
        "  auto_max: 25\n"
        "  template_fallback: false\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.agent.model == "opus"
    assert cfg.agent.timeout_s == 60
    assert cfg.agent.batch == 10
    assert cfg.agent.max_batches == 3
    assert cfg.pipeline.autopilot is False
    assert cfg.draft.auto is False
    assert cfg.draft.auto_purpose == "follow_up"
    assert cfg.draft.auto_tiers == ["A"]
    assert cfg.draft.auto_max == 25
    assert cfg.draft.template_fallback is False


def test_agent_command_empty_list_in_yaml_means_disabled_not_unset(tmp_path):
    """`command: []` (explicit disable) must round-trip as an empty list, distinct from the
    `None` default (auto-detect) - `resolve_command` (agent_runner.py) depends on this distinction."""
    (tmp_path / "leadforge.yaml").write_text("agent:\n  command: []\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.agent.command == []
    assert cfg.agent.command is not None


def test_agent_command_explicit_list_in_yaml_is_used_verbatim(tmp_path):
    (tmp_path / "leadforge.yaml").write_text(
        "agent:\n  command: ['/usr/local/bin/my-claude', '-p']\n", encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.agent.command == ["/usr/local/bin/my-claude", "-p"]


def test_env_override_reaches_nested_agent_and_pipeline_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADFORGE_AGENT__MODEL", "haiku")
    monkeypatch.setenv("LEADFORGE_PIPELINE__AUTOPILOT", "false")
    cfg = load_config(tmp_path)
    assert cfg.agent.model == "haiku"
    assert cfg.pipeline.autopilot is False


def test_config_fixture_has_autopilot_sections(cfg):
    """Sanity check against the shared `cfg` fixture (tests/conftest.py) other units build on."""
    assert cfg.agent.command is None
    assert cfg.pipeline.autopilot is True
    assert cfg.draft.auto_tiers == ["A", "B"]
