"""Provider adapter tests. gosom parsing is fixture-based (no binary/network).

The fallback_rest test is a TODO stub tied to ICM U3.6 — see docs/05.
"""
import json

from leadforge.config import load_config
from leadforge.providers.gosom import GosomProvider


def test_gosom_parse_ndjson(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    prov = GosomProvider(cfg)
    out = tmp_path / "out.json"
    rows = [
        {"title": "A Shop", "phone": "713-555-0100", "web_site": "http://a.com", "place_id": "P1"},
        {"title": "B Shop", "phone": "713-555-0111", "place_id": "P2"},
    ]
    out.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    listings = list(prov._parse(out))
    assert len(listings) == 2
    assert listings[0].provider == "gosom"
    assert listings[0].data["title"] == "A Shop"


def test_gosom_parse_json_array(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    prov = GosomProvider(cfg)
    out = tmp_path / "out.json"
    out.write_text(json.dumps([{"title": "Solo"}]), encoding="utf-8")
    assert len(list(prov._parse(out))) == 1


def test_gosom_available_without_binary(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    ok, reason = GosomProvider(cfg).available()
    assert ok is False and "doctor" in reason


# --- U3.6 fallback REST provider ------------------------------------------------------------
import httpx  # noqa: E402
import pytest  # noqa: E402

from leadforge.grid import PlannedQuery  # noqa: E402
from leadforge.providers.fallback_rest import FallbackRestProvider  # noqa: E402
from leadforge.util import ProviderDegraded  # noqa: E402


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_fallback_rest_maps_fields(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    prov = FallbackRestProvider(cfg)
    fake_rows = [{"name": "A Shop", "phone_number": "713-555-0100",
                  "website": "http://a.com", "reviews": 12, "lat": 29.7, "lng": -95.3}]
    monkeypatch.setattr("leadforge.providers.fallback_rest.httpx.get",
                        lambda *a, **k: _FakeResp(fake_rows))
    out = prov.fetch(PlannedQuery(text="auto repair in Houston", category="", area=""))
    assert len(out) == 1
    assert out[0].data["title"] == "A Shop"
    assert out[0].data["web_site"] == "http://a.com"
    assert out[0].data["latitude"] == 29.7
    assert out[0].data["name"] == "A Shop"  # original keys preserved


def test_fallback_rest_unreachable_is_degraded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    prov = FallbackRestProvider(cfg)

    def _boom(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr("leadforge.providers.fallback_rest.httpx.get", _boom)
    with pytest.raises(ProviderDegraded):
        prov.fetch(PlannedQuery(text="x", category="", area=""))
    ok, reason = prov.available()
    assert ok is False and "not reachable" in reason


def test_fallback_rest_non_list_is_degraded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    prov = FallbackRestProvider(cfg)
    monkeypatch.setattr("leadforge.providers.fallback_rest.httpx.get",
                        lambda *a, **k: _FakeResp({"detail": "error"}))
    with pytest.raises(ProviderDegraded):
        prov.fetch(PlannedQuery(text="x", category="", area=""))


def test_gosom_timeout_salvages_partial_output(tmp_path, monkeypatch):
    """A 30m timeout must not discard the listings gosom already wrote to disk."""

    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    prov = GosomProvider(cfg)
    from leadforge.grid import PlannedQuery
    from leadforge.providers import gosom as gosom_mod
    q = PlannedQuery(text="x in y", category="", area="")
    from leadforge.util import sha1_hex
    out_path = cfg.cache_dir / f"gosom_{sha1_hex(q.text + str(q.tile), 8)}.json"
    out_path.write_text('{"title": "Partial Shop", "place_id": "P9"}\n', encoding="utf-8")
    monkeypatch.setattr(gosom_mod, "gosom_path", lambda cfg: "gosom-fake")
    monkeypatch.setattr(GosomProvider, "_run_with_watchdog",
                        staticmethod(lambda args, out, t, **kw: ({"returncode": -1, "stderr": ""}, True)))
    listings = prov.fetch(q)
    assert len(listings) == 1 and listings[0].data["title"] == "Partial Shop"


def test_gosom_grid_bbox_is_lat_first(tmp_path, monkeypatch):
    """v0.1.4: Tile.bbox is GeoJSON (minLng,minLat,maxLng,maxLat) but gosom -grid-bbox wants
    'minLat,minLon,maxLat,maxLon' (per -h). Passing it verbatim scraped a box on the wrong
    continent — the exact drift U8.2 predicted for grid mode."""
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path)
    cfg.discovery.grid_mode = "auto"
    prov = GosomProvider(cfg)
    from leadforge.grid import PlannedQuery, Tile
    from leadforge.providers import gosom as gosom_mod
    q = PlannedQuery(text="x in y", category="", area="",
                     tile=Tile(bbox=(-3.80, 40.30, -3.60, 40.50), cell_km=1.0))
    captured: dict = {}

    def fake_run(args, out, t, **kw):
        captured["args"] = args
        out.write_text("", encoding="utf-8")
        return {"returncode": 0, "stderr": ""}, False

    monkeypatch.setattr(gosom_mod, "gosom_path", lambda cfg: "gosom-fake")
    monkeypatch.setattr(GosomProvider, "_run_with_watchdog", staticmethod(fake_run))
    prov.fetch(q)
    args = captured["args"]
    bbox = args[args.index("-grid-bbox") + 1]
    assert bbox == "40.3,-3.8,40.5,-3.6"  # lat first, exactly as gosom -h documents


def test_gosom_real_fixture_maps_to_business(tmp_path, monkeypatch):
    """U8.2: real bytes from a live Guildford run must map cleanly through to_business()."""
    from pathlib import Path

    from leadforge.models import ICP, RawListing
    from leadforge.normalize import to_business
    from leadforge.util import now_iso
    fixture = Path(__file__).parent / "fixtures" / "gosom_sample.ndjson"
    icp = ICP.model_validate({
        "campaign": "t", "offer": {"what": "x"},
        "target": {"categories": ["accounting firm"],
                   "geography": {"areas": ["Guildford"], "country": "GB"}}})
    rows = [json.loads(ln) for ln in fixture.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(rows) == 3
    for row in rows:
        biz = to_business(RawListing(provider="gosom", fetched_at=now_iso(), data=row), "run_x", icp, "GB")
        assert biz is not None
        assert biz.name and biz.place_id
        assert biz.phone_e164 and biz.phone_e164.startswith("+44")
        assert biz.website and biz.website.startswith("http")
        assert biz.address_city == "Guildford"


def test_watchdog_terminates_stalled_process(tmp_path):
    """A process that wrote output then hangs is killed after the stall window."""
    import sys
    out = tmp_path / "results.json"
    out.write_text('{"title": "X"}\n', encoding="utf-8")
    args = [sys.executable, "-c", "import time; time.sleep(600)"]
    t0 = __import__("time").monotonic()
    proc, timed_out = GosomProvider._run_with_watchdog(args, out, hard_timeout_s=600,
                                                       stall_s=3, poll_s=0.5)
    elapsed = __import__("time").monotonic() - t0
    assert timed_out is True
    assert elapsed < 15, f"watchdog took {elapsed:.0f}s to fire"


def test_watchdog_returns_normally_on_clean_exit(tmp_path):
    import sys
    out = tmp_path / "results.json"
    proc, timed_out = GosomProvider._run_with_watchdog(
        [sys.executable, "-c", "print('hi')"], out, hard_timeout_s=60, stall_s=3, poll_s=0.5)
    assert timed_out is False and proc["returncode"] == 0
