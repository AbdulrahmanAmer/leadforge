"""U4.8 social/video presence tests — no network, subprocess mocked."""
from types import SimpleNamespace

from leadforge.config import load_config
from leadforge.providers import social


def _cfg(tmp_path, monkeypatch, enabled=True):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    cfg.social.enabled = enabled
    return cfg


def test_filter_networks_drops_linkedin():
    links = {"linkedin": "https://linkedin.com/company/x", "youtube": "https://youtube.com/@x"}
    out = social.filter_networks(links)
    assert "linkedin" not in out and "youtube" in out


def test_linkedin_never_reaches_subprocess(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    calls = []

    def _spy(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="{}")

    monkeypatch.setattr(social.subprocess, "run", _spy)
    monkeypatch.setattr(social.shutil, "which", lambda name: f"/usr/bin/{name}")
    cfg.social.networks = ["youtube", "linkedin", "facebook"]
    social.presence({"linkedin": "https://linkedin.com/company/x",
                     "youtube": "https://youtube.com/@x"}, cfg)
    assert all("linkedin" not in " ".join(argv) for argv in calls)


def test_presence_shape_with_mocked_ytdlp(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    payload = ('{"channel_follower_count": 120, "entries": [{"upload_date": "20240115"}]}')
    monkeypatch.setattr(social.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(social.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout=payload))
    out = social.presence({"youtube": "https://youtube.com/@x"}, cfg)
    yt = out["youtube"]
    assert yt["exists"] is True and yt["status"] == "ok"
    assert yt["followers"] == 120 and yt["last_post_at"] == "2024-01-15"


def test_presence_missing_binary_is_unknown_not_crash(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(social.shutil, "which", lambda name: None)
    out = social.presence({"youtube": "https://youtube.com/@x"}, cfg)
    assert out["youtube"]["status"] == "unknown"


def test_to_signals(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    assert social.to_signals({}, cfg) == ["no_social_presence"]
    stale = {"facebook": {"url": "u", "exists": True, "last_post_at": "2020-01-01",
                          "followers": None, "status": "ok"}}
    sigs = social.to_signals(stale, cfg)
    assert "no_video_presence" in sigs and "stale_social" in sigs


def test_disabled_is_unavailable(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, enabled=False)
    ok, msg = social.is_available(cfg)
    assert ok is False and "disabled" in msg
