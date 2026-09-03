"""v0.4 autopilot headless agent runner (U A, docs/08 ADR-015).

The real `claude` binary is NEVER invoked here. Every "agent command" in these tests is a small
Python script written into `tmp_path` and run as `[sys.executable, str(script_path)]`, standing in
for whatever `resolve_command`/`agent.command` would otherwise point at.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from leadforge import agent_runner
from leadforge.util import LeadForgeError

# A fake "claude -p" that captures stdin to a file (relative to its cwd) and prints canned NDJSON
# with the exact kind of MCP noise line seen in the 2026-09-03 live proof.
CAPTURE_SCRIPT = """\
import sys
open("stdin_capture.txt", "w", encoding="utf-8").write(sys.stdin.read())
print("```json")
print('{"target": 1, "subject": "hi"}')
print("Client.listTools() called but server does not advertise tools capability - returning empty list")
print('{"target": 2, "abstain": true}')
print("```")
"""

FAIL_SCRIPT = """\
import sys
sys.stderr.write("boom\\n")
sys.exit(3)
"""

EMPTY_SCRIPT = """\
import sys
sys.exit(0)
"""

SLEEP_SCRIPT = """\
import time
time.sleep(5)
print("too late")
"""

ENV_SCRIPT = """\
import os
print(os.environ.get("CLAUDE_SYNC_SKIP", ""))
print(os.environ.get("CLAUDE_LEARN_SKIP", ""))
print(os.environ.get("LEADFORGE_NO_UI", ""))
"""

CWD_SCRIPT = """\
import os
print(os.getcwd())
"""


def _write_script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


# ------------------------------------------------------------------- error classes
def test_error_classes_are_leadforge_errors_with_exit_code_4():
    assert issubclass(agent_runner.AgentUnavailable, LeadForgeError)
    assert issubclass(agent_runner.AgentFailed, LeadForgeError)
    assert agent_runner.AgentUnavailable.exit_code == 4
    assert agent_runner.AgentFailed.exit_code == 4


# ------------------------------------------------------------------- resolve_command / is_available
def test_resolve_command_none_when_command_is_explicit_empty_list(cfg):
    cfg.agent.command = []
    assert agent_runner.resolve_command(cfg) is None
    assert agent_runner.is_available(cfg) is False


def test_resolve_command_uses_explicit_list_verbatim(cfg):
    cfg.agent.command = ["/opt/bin/my-claude", "-p", "--flag"]
    assert agent_runner.resolve_command(cfg) == ["/opt/bin/my-claude", "-p", "--flag"]
    assert agent_runner.is_available(cfg) is True


def test_resolve_command_returns_a_copy_of_the_configured_list(cfg):
    original = ["/bin/x", "-p"]
    cfg.agent.command = original
    got = agent_runner.resolve_command(cfg)
    got.append("mutated")
    assert cfg.agent.command == original == ["/bin/x", "-p"]


def test_resolve_command_auto_detects_claude_on_path(cfg, monkeypatch):
    monkeypatch.setattr(agent_runner.shutil, "which",
                        lambda name: r"C:\bin\claude.exe" if name == "claude" else None)
    cmd = agent_runner.resolve_command(cfg)
    assert cmd == [r"C:\bin\claude.exe", "-p", "--model", "sonnet", "--output-format", "text",
                   "--no-session-persistence"]
    assert agent_runner.is_available(cfg) is True


def test_resolve_command_none_when_claude_not_on_path(cfg, monkeypatch):
    monkeypatch.setattr(agent_runner.shutil, "which", lambda name: None)
    assert agent_runner.resolve_command(cfg) is None
    assert agent_runner.is_available(cfg) is False


def test_resolve_command_auto_detect_honours_configured_model(cfg, monkeypatch):
    cfg.agent.model = "opus"
    monkeypatch.setattr(agent_runner.shutil, "which", lambda name: "/usr/bin/claude")
    cmd = agent_runner.resolve_command(cfg)
    assert cmd[cmd.index("--model") + 1] == "opus"


# ------------------------------------------------------------------- run_agent: happy path
def test_run_agent_returns_stdout_and_pipes_prompt_on_stdin(cfg, tmp_path):
    script = _write_script(tmp_path, "fake_agent.py", CAPTURE_SCRIPT)
    cfg.agent.command = [sys.executable, str(script)]
    out = agent_runner.run_agent(cfg, "hello world prompt")
    assert '"target": 1' in out
    captured = (tmp_path / "stdin_capture.txt").read_text(encoding="utf-8")
    assert captured == "hello world prompt"


def test_run_agent_sets_the_three_env_vars(cfg, tmp_path):
    script = _write_script(tmp_path, "fake_env.py", ENV_SCRIPT)
    cfg.agent.command = [sys.executable, str(script)]
    out = agent_runner.run_agent(cfg, "prompt")
    assert out.strip().splitlines() == ["1", "1", "1"]


def test_run_agent_uses_workspace_as_cwd_by_default(cfg, tmp_path):
    script = _write_script(tmp_path, "fake_cwd.py", CWD_SCRIPT)
    cfg.agent.command = [sys.executable, str(script)]
    out = agent_runner.run_agent(cfg, "prompt")
    assert Path(out.strip()).resolve() == tmp_path.resolve()


def test_run_agent_honours_an_explicit_cwd_override(cfg, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    script = _write_script(tmp_path, "fake_cwd2.py", CWD_SCRIPT)
    cfg.agent.command = [sys.executable, str(script)]
    out = agent_runner.run_agent(cfg, "prompt", cwd=other)
    assert Path(out.strip()).resolve() == other.resolve()


# ------------------------------------------------------------------- run_agent: failure modes
def test_run_agent_raises_agent_unavailable_when_no_command(cfg):
    cfg.agent.command = []
    with pytest.raises(agent_runner.AgentUnavailable):
        agent_runner.run_agent(cfg, "prompt")


def test_run_agent_raises_agent_failed_on_nonzero_exit(cfg, tmp_path):
    script = _write_script(tmp_path, "fake_fail.py", FAIL_SCRIPT)
    cfg.agent.command = [sys.executable, str(script)]
    with pytest.raises(agent_runner.AgentFailed):
        agent_runner.run_agent(cfg, "prompt")


def test_run_agent_raises_agent_failed_on_empty_stdout(cfg, tmp_path):
    script = _write_script(tmp_path, "fake_empty.py", EMPTY_SCRIPT)
    cfg.agent.command = [sys.executable, str(script)]
    with pytest.raises(agent_runner.AgentFailed):
        agent_runner.run_agent(cfg, "prompt")


def test_run_agent_raises_agent_failed_on_timeout(cfg, tmp_path):
    script = _write_script(tmp_path, "fake_sleep.py", SLEEP_SCRIPT)
    cfg.agent.command = [sys.executable, str(script)]
    with pytest.raises(agent_runner.AgentFailed):
        agent_runner.run_agent(cfg, "prompt", timeout_s=1)


# ------------------------------------------------------------------- run_agent: logging
def test_run_agent_writes_one_json_log_line_per_invocation(cfg, tmp_path):
    script = _write_script(tmp_path, "fake_log.py", CAPTURE_SCRIPT)
    cfg.agent.command = [sys.executable, str(script)]
    agent_runner.run_agent(cfg, "hello")

    log_path = cfg.data_path / "logs" / "agent_runner.log"
    assert log_path.is_file()
    lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    entry = json.loads(lines[0])
    for key in ("ts", "cmd", "prompt_chars", "secs", "exit", "out_chars"):
        assert key in entry
    assert entry["prompt_chars"] == len("hello")
    assert entry["exit"] == 0
    assert entry["cmd"] == sys.executable
    assert entry["out_chars"] > 0


def test_run_agent_logs_a_second_line_on_a_second_invocation(cfg, tmp_path):
    script = _write_script(tmp_path, "fake_log2.py", CAPTURE_SCRIPT)
    cfg.agent.command = [sys.executable, str(script)]
    agent_runner.run_agent(cfg, "one")
    agent_runner.run_agent(cfg, "two")
    log_path = cfg.data_path / "logs" / "agent_runner.log"
    lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2


def test_run_agent_logs_even_on_nonzero_exit(cfg, tmp_path):
    script = _write_script(tmp_path, "fake_fail2.py", FAIL_SCRIPT)
    cfg.agent.command = [sys.executable, str(script)]
    with pytest.raises(agent_runner.AgentFailed):
        agent_runner.run_agent(cfg, "prompt")
    log_path = cfg.data_path / "logs" / "agent_runner.log"
    entry = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
    assert entry["exit"] == 3


def test_run_agent_logs_a_null_exit_on_timeout(cfg, tmp_path):
    script = _write_script(tmp_path, "fake_sleep2.py", SLEEP_SCRIPT)
    cfg.agent.command = [sys.executable, str(script)]
    with pytest.raises(agent_runner.AgentFailed):
        agent_runner.run_agent(cfg, "prompt", timeout_s=1)
    log_path = cfg.data_path / "logs" / "agent_runner.log"
    entry = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
    assert entry["exit"] is None


# ------------------------------------------------------------------- parse_ndjson
def test_parse_ndjson_survives_fences_prose_and_mcp_noise():
    text = "\n".join([
        "Here is the output:",
        "```json",
        '{"target": 1, "subject": "hi"}',
        "Client.listTools() called but server does not advertise tools capability - returning empty list",
        '{"target": 2, "abstain": true}',
        "```",
        "Done.",
    ])
    records = agent_runner.parse_ndjson(text)
    assert records == [{"target": 1, "subject": "hi"}, {"target": 2, "abstain": True}]


def test_parse_ndjson_ignores_non_object_json_lines():
    text = '[1,2,3]\n"just a string"\n42\nnull\ntrue\n{"ok": true}\n'
    assert agent_runner.parse_ndjson(text) == [{"ok": True}]


def test_parse_ndjson_ignores_blank_lines_and_whitespace():
    text = "\n\n  {\"a\": 1}  \n\n\n{\"b\": 2}\n\n"
    assert agent_runner.parse_ndjson(text) == [{"a": 1}, {"b": 2}]


def test_parse_ndjson_empty_or_whitespace_only_text_returns_empty_list():
    assert agent_runner.parse_ndjson("") == []
    assert agent_runner.parse_ndjson("   \n  \n") == []


def test_parse_ndjson_ignores_unparseable_lines_without_raising():
    text = '{"a": 1}\nnot json at all {{{\n{"b": 2}\n'
    assert agent_runner.parse_ndjson(text) == [{"a": 1}, {"b": 2}]


# ------------------------------------------------------------------- make_ndjson_runner
def test_make_ndjson_runner_none_when_unavailable(cfg):
    cfg.agent.command = []
    assert agent_runner.make_ndjson_runner(cfg, "instructions") is None


def test_make_ndjson_runner_builds_prompt_and_parses_response(cfg, tmp_path):
    script = _write_script(tmp_path, "fake_runner.py", CAPTURE_SCRIPT)
    cfg.agent.command = [sys.executable, str(script)]
    runner = agent_runner.make_ndjson_runner(cfg, "SYSTEM INSTRUCTIONS")
    assert runner is not None
    records = runner(['{"a": 1}', '{"a": 2}'])
    assert records == [{"target": 1, "subject": "hi"}, {"target": 2, "abstain": True}]
    prompt = (tmp_path / "stdin_capture.txt").read_text(encoding="utf-8")
    assert prompt == 'SYSTEM INSTRUCTIONS\n\n{"a": 1}\n{"a": 2}\n'


def test_make_ndjson_runner_propagates_agent_failed(cfg, tmp_path):
    script = _write_script(tmp_path, "fake_runner_fail.py", FAIL_SCRIPT)
    cfg.agent.command = [sys.executable, str(script)]
    runner = agent_runner.make_ndjson_runner(cfg, "INSTRUCTIONS")
    assert runner is not None
    with pytest.raises(agent_runner.AgentFailed):
        runner(["{}"])
