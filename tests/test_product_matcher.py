import json

from src.utils.product_matcher import ProductMatcher
from src.utils.synonym_expander import SynonymExpander


def _write_index(tmp_path, docs):
    index_path = tmp_path / "website_index.json"
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(docs, f)
    return index_path


def test_product_matcher_handles_singular_plural_variation(tmp_path):
    docs = {
        "website:product:business/general/all-risks-cover": {
            "type": "product",
            "title": "All Risks Cover",
            "category": "business",
            "subcategory": "general",
            "url": "https://example.com/all-risks-cover",
        }
    }
    matcher = ProductMatcher(index_path=_write_index(tmp_path, docs))

    results = matcher.match_products("tell me about all risk cover", top_k=3)

    assert results, "Expected product match for singular/plural variation"
    assert results[0][2]["name"].lower() == "all risks cover"


def test_synonym_expander_adds_all_risks_variants():
    expander = SynonymExpander()
    expanded = expander.expand_query("tell me about all risks cover")

    assert "all risk cover" in expanded.lower()
    assert "gadget insurance" in expanded.lower()
