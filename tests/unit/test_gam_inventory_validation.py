"""Unit tests for GAM product inventory-configuration validation.

Covers the consistency gates enforced before a GAM product is saved: inventory
must be targeted, targeted IDs must exist in synced inventory, placements must
be ACTIVE, and creative sizes must be serveable by the targeted ad units.
See ``services.gam_inventory_validation.validate_gam_inventory_config``.
"""

from types import SimpleNamespace

from src.services.gam_inventory_validation import validate_gam_inventory_config


def _ad_unit(inventory_id, sizes=None, status="ACTIVE"):
    return SimpleNamespace(
        inventory_id=inventory_id,
        status=status,
        inventory_metadata={"sizes": sizes or []},
    )


def _placement(inventory_id, status="ACTIVE"):
    return SimpleNamespace(inventory_id=inventory_id, status=status, inventory_metadata={})


def _size(width, height, is_native=False):
    return {"width": width, "height": height, "is_native": is_native}


def _config(ad_units=None, placements=None, placeholders=None):
    cfg: dict = {}
    if ad_units is not None:
        cfg["targeted_ad_unit_ids"] = ad_units
    if placements is not None:
        cfg["targeted_placement_ids"] = placements
    if placeholders is not None:
        cfg["creative_placeholders"] = placeholders
    return cfg


def test_valid_config_passes():
    cfg = _config(ad_units=["111"], placeholders=[_size(300, 250)])
    rows = [_ad_unit("111", sizes=[{"width": 300, "height": 250}])]

    assert validate_gam_inventory_config(cfg, rows, []) == []


def test_no_targeting_is_rejected():
    errors = validate_gam_inventory_config(_config(placeholders=[_size(300, 250)]), [], [])

    assert len(errors) == 1
    assert "incomplete" in errors[0].lower()


def test_missing_ad_unit_is_rejected():
    cfg = _config(ad_units=["111", "222"], placeholders=[_size(300, 250)])
    rows = [_ad_unit("111", sizes=[{"width": 300, "height": 250}])]

    errors = validate_gam_inventory_config(cfg, rows, [])

    assert any("not found" in e.lower() and "222" in e for e in errors)


def test_missing_placement_is_rejected():
    cfg = _config(placements=["999"])
    errors = validate_gam_inventory_config(cfg, [], [])

    assert any("not found" in e.lower() and "999" in e for e in errors)


def test_inactive_placement_is_rejected():
    cfg = _config(placements=["555"])
    rows = [_placement("555", status="ARCHIVED")]

    errors = validate_gam_inventory_config(cfg, [], rows)

    assert any("not active" in e.lower() and "555" in e for e in errors)


def test_active_placement_passes():
    cfg = _config(placements=["555"])
    rows = [_placement("555", status="ACTIVE")]

    assert validate_gam_inventory_config(cfg, [], rows) == []


def test_creative_size_mismatch_is_rejected():
    # Product wants 160x600; ad unit only supports 300x250 / 728x90.
    cfg = _config(ad_units=["111"], placeholders=[_size(160, 600)])
    rows = [_ad_unit("111", sizes=[{"width": 300, "height": 250}, {"width": 728, "height": 90}])]

    errors = validate_gam_inventory_config(cfg, rows, [])

    assert any("size mismatch" in e.lower() for e in errors)


def test_partial_size_overlap_passes():
    # One of the product's sizes is supported -> not a hard mismatch.
    cfg = _config(ad_units=["111"], placeholders=[_size(300, 250), _size(160, 600)])
    rows = [_ad_unit("111", sizes=[{"width": 300, "height": 250}])]

    assert validate_gam_inventory_config(cfg, rows, []) == []


def test_run_of_network_ad_unit_skips_size_check():
    # An ad unit with no declared sizes serves any size -> no mismatch.
    cfg = _config(ad_units=["111", "222"], placeholders=[_size(160, 600)])
    rows = [
        _ad_unit("111", sizes=[{"width": 300, "height": 250}]),
        _ad_unit("222", sizes=[]),  # run-of-network
    ]

    assert validate_gam_inventory_config(cfg, rows, []) == []


def test_native_placeholder_skipped_in_size_check():
    cfg = _config(ad_units=["111"], placeholders=[_size(1, 1, is_native=True)])
    rows = [_ad_unit("111", sizes=[{"width": 300, "height": 250}])]

    assert validate_gam_inventory_config(cfg, rows, []) == []


def test_placement_only_product_skips_size_check():
    cfg = _config(placements=["555"], placeholders=[_size(160, 600)])
    rows = [_placement("555", status="ACTIVE")]

    assert validate_gam_inventory_config(cfg, [], rows) == []


def test_missing_ad_unit_when_never_synced_prompts_sync():
    cfg = _config(ad_units=["111"], placeholders=[_size(300, 250)])

    errors = validate_gam_inventory_config(cfg, [], [], synced_ad_unit_count=0)

    assert len(errors) == 1
    assert "not been synced" in errors[0].lower()
    # Should not dump the ID as if it were a genuine unknown-ID error.
    assert "111" not in errors[0]


def test_missing_ad_unit_when_inventory_exists_reports_unknown_id():
    cfg = _config(ad_units=["999"], placeholders=[_size(300, 250)])
    other_units = [_ad_unit("111", sizes=[{"width": 300, "height": 250}])]  # something is synced

    errors = validate_gam_inventory_config(cfg, [], [], synced_ad_unit_count=len(other_units))

    assert any("not found" in e.lower() and "999" in e for e in errors)


def test_missing_placement_when_never_synced_prompts_sync():
    cfg = _config(placements=["555"])

    errors = validate_gam_inventory_config(cfg, [], [], synced_placement_count=0)

    assert any("not been synced" in e.lower() for e in errors)


def test_multiple_errors_reported_together():
    cfg = _config(ad_units=["404"], placements=["500"], placeholders=[_size(300, 250)])
    placement_rows = [_placement("500", status="INACTIVE")]

    errors = validate_gam_inventory_config(cfg, [], placement_rows)

    # Missing ad unit + inactive placement both surface.
    assert any("404" in e for e in errors)
    assert any("500" in e and "not active" in e.lower() for e in errors)
