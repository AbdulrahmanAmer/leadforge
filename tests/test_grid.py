from leadforge.grid import PlannedQuery, make_tiles, plan_counts


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
