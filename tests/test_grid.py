from leadforge.grid import PlannedQuery, build_plan, make_tiles, plan_counts


def test_make_tiles_respects_cap():
    bbox = [-95.8, 29.5, -95.0, 30.1]  # ~Houston-ish box
    tiles = make_tiles(bbox, cell_km=1.0, max_tiles=20)
    assert 1 <= len(tiles) <= 20
    # every tile inside the bbox
    for t in tiles:
        assert bbox[0] - 1e-6 <= t.bbox[0] < t.bbox[2] <= bbox[2] + 1e-6
        assert bbox[1] - 1e-6 <= t.bbox[1] < t.bbox[3] <= bbox[3] + 1e-6


def test_make_tiles_single_when_small():
    tiles = make_tiles([-95.31, 29.75, -95.30, 29.76], cell_km=3.0, max_tiles=60)
    assert len(tiles) == 1


def test_plan_counts():
    qs = [PlannedQuery(text="a in X", category="a", area="X"),
          PlannedQuery(text="b in X", category="b", area="X")]
    c = plan_counts(qs)
    assert c["queries"] == 2 and c["tiles"] == 0 and c["est_max_results"] == 240


# --- v0.2.0: plan ordering, tiled planning, geocode resilience ---------------------------------
def _icp(categories, areas, country="GB", **caps):
    from leadforge.models import ICP
    body = {"campaign": "t", "offer": {"what": "x"},
            "target": {"categories": categories,
                       "geography": {"areas": areas, "country": country}}}
    if caps:
        body["caps"] = caps
    return ICP.model_validate(body)


def test_text_plan_rotates_categories_and_areas(cfg):
    """caps.max_leads stops discovery mid-plan; if the plan is category-major, the last category
    never runs at all. Every category must appear before any pair repeats."""
    icp = _icp(["cat a", "cat b", "cat c"], ["Leeds", "York"])
    qs = build_plan(icp, cfg)
    assert len(qs) == 6
    first_round = [q.category for q in qs[:3]]
    assert sorted(first_round) == ["cat a", "cat b", "cat c"]  # all categories in round 1
    assert {q.area for q in qs[:3]} == {"Leeds"}               # ...within one area
    assert {q.area for q in qs[3:]} == {"York"}


def _stub_geocode(monkeypatch, bbox=None):
    calls = {"n": 0}

    def fake(area, cfg, country):
        calls["n"] += 1
        return {"lat": 52.48, "lng": -1.9, "bbox": bbox or [-2.0, 52.4, -1.8, 52.55],
                "display": f"{area} (stub)", "type": "city"}

    monkeypatch.setattr("leadforge.grid.geocode", fake)
    return calls


def test_grid_plan_tiles_and_rotates_by_tile_then_category(cfg, monkeypatch):
    _stub_geocode(monkeypatch)
    cfg.discovery.grid_mode = "auto"
    icp = _icp(["cat a", "cat b"], ["Birmingham"], max_tiles=6)
    qs = build_plan(icp, cfg)
    tiles = {q.tile.bbox for q in qs}
    assert len(tiles) > 1 and len(qs) == len(tiles) * 2      # every category on every tile
    assert all(q.tile is not None for q in qs)
    # tile-major rotation: both categories are served on tile 1 before tile 2 starts
    assert sorted(q.category for q in qs[:2]) == ["cat a", "cat b"]
    assert qs[0].tile.bbox == qs[1].tile.bbox != qs[2].tile.bbox
    assert all("Birmingham" in q.text for q in qs)           # text still constrains the scraper


def test_grid_off_never_geocodes(cfg, monkeypatch):
    calls = _stub_geocode(monkeypatch)
    icp = _icp(["cat a"], ["Birmingham"])          # cfg.discovery.grid_mode defaults to "off"
    qs = build_plan(icp, cfg)
    assert calls["n"] == 0 and all(q.tile is None for q in qs)


def test_bbox_campaign_without_grid_explains_itself(cfg):
    from leadforge.models import ICP
    from leadforge.util import InputError
    icp = ICP.model_validate({"campaign": "t", "offer": {"what": "x"},
                              "target": {"categories": ["cat a"],
                                         "geography": {"country": "GB", "bbox": [-2.0, 52.4, -1.8, 52.55]}}})
    try:
        build_plan(icp, cfg)
        raise AssertionError("expected InputError")
    except InputError as e:
        assert "grid_mode" in str(e)  # names the switch that would make the bbox usable


def test_plan_counts_reports_distinct_tiles_and_runtime(cfg, monkeypatch):
    _stub_geocode(monkeypatch)
    cfg.discovery.grid_mode = "auto"
    icp = _icp(["cat a", "cat b"], ["Birmingham"], max_tiles=6)
    c = plan_counts(build_plan(icp, cfg), cfg)
    assert c["tiles"] < c["queries"]          # distinct tiles, not tiled-query count
    assert c["est_runtime_min"] > 0


def test_geocode_retries_transient_errors_then_succeeds(cfg, monkeypatch, tmp_path):
    import httpx

    from leadforge.grid import geocode
    monkeypatch.setattr("leadforge.grid.time.sleep", lambda s: None)
    calls = {"n": 0}

    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"boundingbox": ["52.4", "52.55", "-2.0", "-1.8"], "lat": "52.48", "lon": "-1.9",
                     "display_name": "Birmingham", "importance": 0.9}]

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectTimeout("boom")
        return _R()

    monkeypatch.setattr("leadforge.grid.httpx.get", flaky)
    out = geocode("Birmingham", cfg, "GB")
    assert calls["n"] == 3 and out["bbox"] == [-2.0, 52.4, -1.8, 52.55]


def test_geocode_gives_up_with_a_useful_message(cfg, monkeypatch):
    import httpx

    from leadforge.grid import geocode
    from leadforge.util import InputError
    monkeypatch.setattr("leadforge.grid.time.sleep", lambda s: None)
    monkeypatch.setattr("leadforge.grid.httpx.get",
                        lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectTimeout("boom")))
    try:
        geocode("Birmingham", cfg, "GB")
        raise AssertionError("expected InputError")
    except InputError as e:
        assert "Birmingham" in str(e)
