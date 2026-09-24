"""v0.4 headless agent runner (ADR-015, U A).

Autopilot (`leadforge run`, labeling/drafting stages) needs an "agent in the loop" without any paid
API (ADR-007/012): this module shells out to the OPERATOR'S OWN Claude Code, in headless print mode
(`claude -p ... < prompt.txt`), auto-detected on PATH. There is no API key anywhere in this file.
Every caller (`enrich.dm.auto_label`, `draft.service.auto_draft`) treats the runner as optional and
has a deterministic fallback, so `agent.command: []` (or `claude` simply not being on PATH) never
blocks a run — it only means the fallback path runs instead.

Live-proven 2026-09-03: `claude -p --model sonnet --output-format text --no-session-persistence <
prompt.txt` exits 0 with the requested NDJSON lines PLUS the odd line of MCP noise ("Client.listTools()
called but server does not advertise tools capability - returning empty list"). `parse_ndjson` is built
to survive that: it only keeps lines that parse as a JSON *object*, so noise, prose and ``` fences are
silently dropped rather than raising.

Dependency-free by design: stdlib only (subprocess/shutil/json/time/os), no third-party import.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from leadforge.util import LeadForgeError, now_iso

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, type checking only
    from leadforge.config import Config

# Bounds simultaneous `claude` subprocesses across all callers in this process (enrich.dm.auto_label
# and draft.service.auto_draft both call run_agent(); today they run sequentially within one autopilot
# run, but this is cheap insurance against a future concurrent caller or a multi-threaded harness).
# Re-created lazily per configured limit so tests can exercise different `agent.max_concurrent` values.
_SEMAPHORE_LOCK = threading.Lock()
_SEMAPHORE: threading.Semaphore | None = None
_SEMAPHORE_LIMIT: int | None = None


def _semaphore_for(limit: int) -> threading.Semaphore:
    global _SEMAPHORE, _SEMAPHORE_LIMIT
    with _SEMAPHORE_LOCK:
        if _SEMAPHORE is None or _SEMAPHORE_LIMIT != limit:
            _SEMAPHORE = threading.Semaphore(max(1, limit))
            _SEMAPHORE_LIMIT = limit
        return _SEMAPHORE


class AgentUnavailable(LeadForgeError):
    """No usable agent command: not configured, and auto-detect found nothing on PATH."""

    exit_code = 4


class AgentFailed(LeadForgeError):
    """The agent command ran but did not produce a usable result: non-zero exit, timeout, or empty stdout."""

    exit_code = 4


def resolve_command(cfg: Config) -> list[str] | None:
    """The argv to run, or None when no agent is available.

    `cfg.agent.command == []` means the operator explicitly disabled the runner - always None,
    regardless of what is on PATH. `None` (the default) means auto-detect: `shutil.which("claude")`,
    and if found, the standard headless print-mode invocation; not found -> None. Any other non-empty
    list is used verbatim (an explicit override, e.g. a wrapper script or a different model flag set).
    """
    command = cfg.agent.command
    if command == []:
        return None
    if command:
        return list(command)  # explicit operator override — used verbatim, no isolation flags injected
    claude = shutil.which("claude")
    if not claude:
        return None
    cmd = [claude, "-p", "--model", cfg.agent.model, "--output-format", "text", "--no-session-persistence"]
    if cfg.agent.isolate:
        # Measured on this machine (3 runs each, trivial prompt): without these flags the call loads
        # every MCP server/hook/plugin from the operator's global config (7-15 extra node/claude
        # processes, 14.5-15.4s/call). --strict-mcp-config plus disabling hooks drops that to 1-3
        # processes and 5.4-6.1s with the same answer. `--bare` is NOT usable: it refuses OAuth/
        # subscription auth. The settings JSON is one argv element (list-form subprocess, no shell).
        cmd += ["--strict-mcp-config", "--settings", json.dumps({"disableAllHooks": True})]
    return cmd


def is_available(cfg: Config) -> bool:
    return resolve_command(cfg) is not None


def _log_invocation(cfg: Config, cmd: list[str], prompt: str, secs: float,
                     exit_code: int | None, out_chars: int) -> None:
    """One JSON line per invocation under `<data_dir>/logs/agent_runner.log` (created on demand)."""
    entry = {
        "ts": now_iso(),
        "cmd": cmd[0] if cmd else None,
        "prompt_chars": len(prompt),
        "secs": round(secs, 3),
        "exit": exit_code,
        "out_chars": out_chars,
    }
    log_path = cfg.logs_dir / "agent_runner.log"  # cfg.logs_dir creates the directory on access
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # logging must never break the run


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Best-effort kill of `proc` and any children it spawned (e.g. the node processes `claude`
    starts for MCP servers). Windows: `taskkill /T /F`. POSIX: kill the process group, since we
    start the child in its own session (`start_new_session=True`)."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=10, check=False,
            )
        else:
            import signal

            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001 — cleanup must never raise over the original timeout error
        pass
    finally:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def run_agent(cfg: Config, prompt: str, *, cwd: Path | None = None, timeout_s: int | None = None) -> str:
    """Run one headless agent invocation with `prompt` on stdin; return stdout.

    Raises `AgentUnavailable` when no command resolves, `AgentFailed` on timeout, non-zero exit, or
    empty stdout. Always logs one line to agent_runner.log first (even on failure), so a run's history
    is visible even when a caller swallows the exception to fall back.

    Bounded by `cfg.agent.max_concurrent` simultaneous invocations (a module-level semaphore), and
    on timeout the whole process tree is killed, not just the immediate `claude` process — it spawns
    node subprocesses for MCP servers that `subprocess.run(timeout=...)`'s default kill() would leak.
    """
    cmd = resolve_command(cfg)
    if cmd is None:
        raise AgentUnavailable("no agent command available: `claude` is not on PATH and agent.command "
                                "is not set (agent.command: [] means disabled on purpose)")

    env = dict(os.environ)
    env.update({"CLAUDE_SYNC_SKIP": "1", "CLAUDE_LEARN_SKIP": "1", "LEADFORGE_NO_UI": "1"})
    run_cwd = str(cwd or cfg.workspace)
    effective_timeout = timeout_s if timeout_s is not None else cfg.agent.timeout_s

    popen_kwargs: dict = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    semaphore = _semaphore_for(cfg.agent.max_concurrent)
    t0 = time.monotonic()
    with semaphore:
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=run_cwd, env=env, encoding="utf-8", errors="replace", **popen_kwargs,
            )
        except OSError as e:
            secs = time.monotonic() - t0
            _log_invocation(cfg, cmd, prompt, secs, None, 0)
            raise AgentFailed(f"agent command could not be started: {' '.join(cmd)} ({e})") from e

        try:
            stdout, stderr = proc.communicate(input=prompt, timeout=effective_timeout)
        except subprocess.TimeoutExpired as e:
            _kill_process_tree(proc)
            proc.communicate()  # reap; discard whatever partial output remains
            secs = time.monotonic() - t0
            _log_invocation(cfg, cmd, prompt, secs, None, 0)
            raise AgentFailed(f"agent command timed out after {secs:.1f}s (limit {effective_timeout}s): "
                              f"{' '.join(cmd)}") from e

        returncode = proc.returncode

    secs = time.monotonic() - t0
    out = stdout or ""
    _log_invocation(cfg, cmd, prompt, secs, returncode, len(out))

    if returncode != 0:
        stderr_tail = (stderr or "")[-2000:]
        raise AgentFailed(f"agent command exited {returncode}: {' '.join(cmd)}\n{stderr_tail}")
    if not out.strip():
        raise AgentFailed(f"agent command produced empty stdout: {' '.join(cmd)}")
    return out


def parse_ndjson(text: str) -> list[dict]:
    """Every line that `json.loads()`s to a JSON object, after stripping ``` fences and whitespace.

    Anything else - a fence line, prose, MCP noise like "Client.listTools() called but server does
    not advertise tools capability - returning empty list", a JSON array/scalar line - is silently
    dropped. This is deliberately permissive: the operator's `claude -p` output is not a strict
    protocol, it is a chat transcript that happens to contain the NDJSON we asked for.
    """
    out: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip().strip("`").strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def make_ndjson_runner(cfg: Config, instructions: str) -> Callable[[list[str]], list[dict]] | None:
    """A `lines -> [dict]` callable for a caller that just wants "send these NDJSON lines, get NDJSON
    back" - or None when no agent is available, so callers can `runner = make_ndjson_runner(...)` and
    branch on `runner is None` without ever touching `resolve_command`/`is_available` themselves.

    The returned callable never raises `AgentUnavailable` (availability was already settled when this
    factory returned non-None); `AgentFailed` from `run_agent` propagates to the caller, which decides
    whether to fall back to a deterministic path.
    """
    if not is_available(cfg):
        return None

    def _runner(lines: Iterable[str]) -> list[dict]:
        prompt = instructions + "\n\n" + "\n".join(lines) + "\n"
        out = run_agent(cfg, prompt)
        return parse_ndjson(out)

    return _runner
