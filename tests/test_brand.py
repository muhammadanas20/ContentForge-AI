"""Tests for Brand configuration module."""

from __future__ import annotations

from contentforge.brand import BrandConfig


def test_brand_config_defaults():
    brand = BrandConfig()
    assert brand.name == "StudentTools.pk"
    assert brand.handle == "@studenttools.pk"
    assert brand.palette.primary == "#FF5722"
    assert brand.typography.headline_case == "title"

    d = brand.to_dict()
    assert d["name"] == "StudentTools.pk"

    loaded = BrandConfig.from_dict(d)
    assert loaded.name == brand.name
    assert loaded.palette.primary == "#FF5722"


def test_brand_config_load_from_file(tmp_path):
    p = tmp_path / "brand.yaml"
    p.write_text(
        "name: CustomBrand\n"
        "handle: '@custom'\n"
        "default_cta: 'Subscribe for more.'\n"
        "palette:\n"
        "  primary: '#00FF00'\n"
    )
    b = BrandConfig.load_from_file(p)
    assert b.name == "CustomBrand"
    assert b.handle == "@custom"
    assert b.palette.primary == "#00FF00"
