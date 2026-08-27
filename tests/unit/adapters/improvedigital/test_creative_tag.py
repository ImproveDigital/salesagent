"""Regression tests: Improve Digital tag synthesis for image-URL creatives.

The Third-Party-Tag bulk endpoint validates that ``tag`` is real HTML
(``<html>|<script>|<img>|<ins>|<div>|<iframe>|<a>``) — sending a bare image
URL is rejected with HTTP 400 ``creative.tag.valid`` (verified live on the
dev platform). ``_creative_payload`` must wrap
image-URL assets in an ``<img>`` tag (inside ``<a>`` when a clickthrough
URL exists) instead of passing the URL through as the tag.
"""

import pytest

from src.adapters.improvedigital.adapter import ImproveDigitalAdapter

pytestmark = pytest.mark.unit


def _payload(asset: dict) -> dict | None:
    # _creative_payload only touches ``self`` for _creative_type (staticmethod),
    # so invoking it unbound-style on a bare instance is safe.
    return ImproveDigitalAdapter._creative_payload(ImproveDigitalAdapter.__new__(ImproveDigitalAdapter), asset)


def _image_asset(**overrides) -> dict:
    asset = {
        "creative_id": "cr_1",
        "name": "Demo Banner 300x250",
        "asset_type": "image",
        "url": "https://cdn.example.com/banner.jpg",
        "click_url": "https://example.com/landing",
        "width": 300,
        "height": 250,
    }
    asset.update(overrides)
    return asset


class TestImageUrlTagSynthesis:
    def test_image_url_is_wrapped_in_anchor_and_img_tag(self):
        payload = _payload(_image_asset())
        assert payload is not None
        assert payload["type"] == "Third Party Tag"
        tag = payload["tag"]
        assert tag.startswith("<a ")
        assert 'href="https://example.com/landing"' in tag
        assert '<img src="https://cdn.example.com/banner.jpg"' in tag
        assert 'width="300"' in tag and 'height="250"' in tag

    def test_image_url_without_click_url_gets_bare_img_tag(self):
        payload = _payload(_image_asset(click_url=None, advertiser_domain="example.com"))
        assert payload is not None
        assert payload["tag"].startswith("<img ")

    def test_html_attribute_values_are_escaped(self):
        payload = _payload(_image_asset(url='https://cdn.example.com/b.jpg?a=1&b="2"'))
        assert payload is not None
        assert '"2"' not in payload["tag"]
        assert "&amp;" in payload["tag"]

    def test_snippet_asset_tag_passes_through_unchanged(self):
        snippet = '<script src="https://tags.example.com/ad.js"></script>'
        payload = _payload(_image_asset(asset_type="html", snippet=snippet))
        assert payload is not None
        assert payload["tag"] == snippet

    def test_explicit_tag_field_passes_through_unchanged(self):
        tag = '<div id="ad-slot"></div>'
        payload = _payload(_image_asset(asset_type="html", tag=tag))
        assert payload is not None
        assert payload["tag"] == tag

    def test_vast_url_asset_keeps_bare_url_tag(self):
        # Non-Third-Party-Tag types go to the multipart servlet, not the
        # tag-validated bulk endpoint — the URL must pass through untouched.
        payload = _payload(_image_asset(asset_type="video"))
        assert payload is not None
        assert payload["type"] == "VAST"
        assert payload["tag"] == "https://cdn.example.com/banner.jpg"

    def test_asset_without_url_or_snippet_is_rejected(self):
        payload = _payload(_image_asset(url=None))
        assert payload is None
