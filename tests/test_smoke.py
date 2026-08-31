"""G0 smoke: package imports, CLI wires up, digest helper is well-formed."""

import json
import subprocess
import sys


def test_package_imports():
    import leadforge
    import leadforge.cli  # noqa: F401
    import leadforge.pipeline  # noqa: F401
    import leadforge.score  # noqa: F401
    assert leadforge.__version__


def test_all_modules_import():
    mods = [
        "leadforge.config", "leadforge.models", "leadforge.db", "leadforge.util", "leadforge.doctor",
        "leadforge.intake", "leadforge.grid", "leadforge.normalize", "leadforge.score", "leadforge.export",
        "leadforge.providers.base", "leadforge.providers.gosom", "leadforge.providers.fallback_rest",
        "leadforge.enrich.crawler", "leadforge.enrich.extract", "leadforge.enrich.validate",
        "leadforge.enrich.dm", "leadforge.enrich.runner",
    ]
    for m in mods:
        __import__(m)


def test_cli_version_runs():
    out = subprocess.run([sys.executable, "-m", "leadforge", "version"],
                         capture_output=True, encoding="utf-8")
    assert "leadforge" in out.stdout


def test_digest_line(capsys):
    from leadforge.util import emit_digest

    emit_digest(True, "test", counts={"a": 1}, warnings=["w"], next_="do x")
    captured = capsys.readouterr().out.strip()
    assert captured.startswith("LF_DIGEST ")
    payload = json.loads(captured[len("LF_DIGEST "):])
    assert payload["ok"] is True and payload["cmd"] == "test" and payload["counts"] == {"a": 1}
