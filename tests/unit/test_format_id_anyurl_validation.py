"""Test format ID validation with AnyUrl objects in media buy creation.

This test specifically verifies that FormatId objects built from AnyUrl values
are handled correctly during format validation in create_media_buy. Since adcp 7,
``LegacyFormatId.agent_url`` is a wire-preserving ``str`` (AnyUrl would append a
trailing slash and change the normative tuple), so ``FormatId`` coerces AnyUrl
inputs to ``str`` and downstream code normalizes spelling before comparing.
"""

from src.core.schemas import FormatId


def test_format_validation_with_anyurl_objects():
    """Test that format validation handles FormatId built from AnyUrl correctly.

    This is a regression test for the bug where calling .rstrip() on
    AnyUrl objects caused AttributeError. On adcp 7 ``LegacyFormatId.agent_url``
    is a wire-preserving ``str``; ``FormatId._coerce_agent_url`` turns AnyUrl
    callers into that string without rewriting the spelling.
    """
    from pydantic import AnyUrl

    # Create FormatId objects; one caller passes an AnyUrl instance (matches real usage)
    product_format = FormatId(agent_url=AnyUrl("https://creative.adcontextprotocol.org/"), id="display_300x250")
    package_format = FormatId(
        agent_url="https://creative.adcontextprotocol.org",
        id="display_300x250",  # No trailing slash
    )

    # adcp 7: agent_url is stored as a plain str with the wire spelling preserved
    assert isinstance(product_format.agent_url, str), "Should be str (adcp 7 wire-preserving)"
    assert isinstance(package_format.agent_url, str), "Should be str (adcp 7 wire-preserving)"
    assert product_format.agent_url == "https://creative.adcontextprotocol.org/"
    assert package_format.agent_url == "https://creative.adcontextprotocol.org"

    # Simulate what the format validation code does:
    # Build set of product format keys
    product_format_keys = set()
    agent_url = product_format.agent_url
    format_id = product_format.id

    # This should NOT raise AttributeError: 'AnyUrl' object has no attribute 'rstrip'
    normalized_url = str(agent_url).rstrip("/") if agent_url else None
    product_format_keys.add((normalized_url, format_id))

    # Build set of requested format keys
    requested_format_keys = set()
    agent_url = package_format.agent_url
    format_id = package_format.id

    # This should also work
    normalized_url = str(agent_url).rstrip("/") if agent_url else None
    requested_format_keys.add((normalized_url, format_id))

    # Verify the normalization worked correctly
    assert product_format_keys == requested_format_keys, "Normalized URLs should match"
    assert ("https://creative.adcontextprotocol.org", "display_300x250") in product_format_keys


def test_format_display_with_anyurl():
    """Test that format_display helper handles AnyUrl-sourced agent_url correctly."""
    from pydantic import AnyUrl

    # Create FormatId from an AnyUrl instance
    format_id = FormatId(agent_url=AnyUrl("https://creative.adcontextprotocol.org/"), id="display_300x250")

    # adcp 7 stores it as a wire-preserving str
    assert isinstance(format_id.agent_url, str)

    # The format_display function should handle AnyUrl by converting to string
    # This is implicitly tested in the above test, but we can verify the pattern:

    # Simulate what format_display does
    url = format_id.agent_url
    clean_url = str(url).rstrip("/")  # Must convert to string first
    result = f"{clean_url}/{format_id.id}"

    assert result == "https://creative.adcontextprotocol.org/display_300x250"


def test_normalize_agent_url_with_anyurl():
    """Test URL normalization works with AnyUrl-sourced agent_url."""
    from pydantic import AnyUrl

    # Create FormatId from an AnyUrl instance with trailing slash
    format_id = FormatId(agent_url=AnyUrl("https://creative.adcontextprotocol.org/"), id="display_300x250")

    agent_url = format_id.agent_url
    assert isinstance(agent_url, str)  # adcp 7: wire-preserving str, not AnyUrl

    # Normalize by converting to string first, then rstrip
    normalized = str(agent_url).rstrip("/")

    assert normalized == "https://creative.adcontextprotocol.org"
    assert not normalized.endswith("/")
