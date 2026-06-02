"""Test that GetProductsResponse __str__ provides human-readable content for protocols."""

from src.core.schemas import GetProductsResponse
from tests.helpers.adcp_factories import create_test_product

ANONYMOUS_AUCTION_PRICING = {
    "pricing_option_id": "cpm_usd_auction",
    "pricing_model": "cpm",
    "currency": "USD",
    "is_fixed": False,
    "price_guidance": {"floor": 1.0, "p50": 5.0},
}


def test_get_products_response_str_single_product():
    """Test that __str__ returns appropriate message for single product."""
    product = create_test_product(product_id="test", name="Test Product")
    response = GetProductsResponse(products=[product])

    content = str(response)
    assert content == "Found 1 product that matches your requirements."
    assert "{" not in content
    assert "product_id" not in content


def test_get_products_response_str_multiple_products():
    """Test that __str__ generates appropriate message for multiple products."""
    products = [create_test_product(product_id=f"test{i}", name=f"Test {i}") for i in range(3)]

    response = GetProductsResponse(products=products)
    content = str(response)

    assert content == "Found 3 products that match your requirements."
    assert "{" not in content


def test_get_products_response_str_empty():
    """Test that __str__ handles empty product list."""
    response = GetProductsResponse(products=[])
    assert str(response) == "No products matched your requirements."


def test_get_products_response_str_anonymous_user():
    """Test that __str__ detects anonymous users (no pricing) and adds auth message."""
    products = [
        create_test_product(
            product_id=f"test{i}",
            name=f"Test {i}",
            pricing_options=[ANONYMOUS_AUCTION_PRICING],
        )
        for i in range(2)
    ]

    response = GetProductsResponse(products=products)

    assert str(response) == (
        "Found 2 products that match your requirements. "
        "Please connect through an authorized buying agent for pricing data."
    )


def test_get_products_response_model_dump_still_has_full_data():
    """Verify that model_dump() still returns full structured data."""
    product = create_test_product(product_id="test", name="Test Product")
    response = GetProductsResponse(products=[product])

    assert str(response) == "Found 1 product that matches your requirements."

    data = response.model_dump()
    assert "products" in data
    assert len(data["products"]) == 1
    assert data["products"][0]["product_id"] == "test"
    assert "message" not in data
