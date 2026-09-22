"""Price extraction: JSON-LD parsing against inline fixtures. No network here -
`extract_price` takes HTML text and returns a verdict, nothing more."""

from quartermaster.integrations.prices import extract_price


def html_with(*scripts: str) -> str:
    body = "\n".join(f'<script type="application/ld+json">{s}</script>' for s in scripts)
    return f"<html><head>{body}</head><body>ignored</body></html>"


class TestExtractPrice:
    def test_plain_product_with_single_offer(self):
        result = extract_price(
            html_with(
                '{"@context": "https://schema.org/", "@type": "Product", '
                '"offers": {"@type": "Offer", "price": "19.99", "priceCurrency": "USD"}}'
            )
        )
        assert result == {"ok": True, "price_cents": 1999, "currency": "USD"}

    def test_graph_wrapped_product(self):
        result = extract_price(
            html_with(
                '{"@context": "https://schema.org", "@graph": [ '
                '{"@type": "BreadcrumbList"}, '
                '{"@type": "Product", "offers": {"@type": "Offer", "price": 42.5, "priceCurrency": "EUR"}} '
                "]}"
            )
        )
        assert result == {"ok": True, "price_cents": 4250, "currency": "EUR"}

    def test_aggregate_offer_uses_low_price(self):
        result = extract_price(
            html_with(
                '{"@type": "Product", "offers": {"@type": "AggregateOffer", "lowPrice": "9.00", '
                '"highPrice": "15.00", "priceCurrency": "USD"}}'
            )
        )
        assert result == {"ok": True, "price_cents": 900, "currency": "USD"}

    def test_list_of_offers_takes_the_minimum(self):
        result = extract_price(
            html_with(
                '{"@type": "Product", "offers": ['
                '{"@type": "Offer", "price": "25.00", "priceCurrency": "USD"}, '
                '{"@type": "Offer", "price": "18.50", "priceCurrency": "USD"}'
                "]}"
            )
        )
        assert result == {"ok": True, "price_cents": 1850, "currency": "USD"}

    def test_price_with_thousands_separator(self):
        result = extract_price(
            html_with('{"@type": "Product", "offers": {"@type": "Offer", "price": "1,299.00"}}')
        )
        assert result["ok"] is True
        assert result["price_cents"] == 129900

    def test_defaults_currency_to_usd_when_absent(self):
        result = extract_price(html_with('{"@type": "Product", "offers": {"@type": "Offer", "price": "5"}}'))
        assert result["currency"] == "USD"

    def test_no_json_ld_at_all_is_a_failed_check_not_a_guess(self):
        result = extract_price("<html><body>Sold out, no price shown</body></html>")
        assert result["ok"] is False
        assert "price_cents" not in result

    def test_json_ld_present_but_not_a_product_is_a_failed_check(self):
        result = extract_price(html_with('{"@type": "Organization", "name": "Some Store"}'))
        assert result["ok"] is False

    def test_malformed_json_ld_does_not_crash(self):
        result = extract_price(html_with("{not valid json"))
        assert result["ok"] is False

    def test_product_without_offers_is_a_failed_check(self):
        result = extract_price(html_with('{"@type": "Product", "name": "Widget"}'))
        assert result["ok"] is False
