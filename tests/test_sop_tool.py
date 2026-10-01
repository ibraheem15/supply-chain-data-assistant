import pytest

from resources import sop_tool
from resources.schema_context import build_schema_context


class FakeDocument:
    def __init__(self, text, metadata):
        self.page_content = text
        self.metadata = metadata


class FakeStore:
    def similarity_search_with_score(self, query, k):
        doc = FakeDocument(
            "If IOT_TEMP_C exceeds 4.0C a breach is declared.",
            {"source": "data/policy/x.md", "Header 1": "SOP", "Header 3": "1. Temperature"},
        )
        return [(doc, 0.81234)][:k]


def test_results_carry_citation_fields(monkeypatch):
    monkeypatch.setattr(sop_tool, "_get_vector_store", lambda: FakeStore())
    result = sop_tool.search_compliance_sop("temperature breach threshold")
    assert result["status"] == "ok"
    first = result["results"][0]
    assert first["source"] == "data/policy/x.md"
    assert first["headings"] == "SOP > 1. Temperature"
    assert first["score"] == 0.8123


@pytest.mark.parametrize("query, top_k", [("", 4), ("  ", 4), ("x", 0), ("x", 11), ("x", "3")])
def test_bad_input_is_error(query, top_k):
    assert sop_tool.search_compliance_sop(query, top_k)["status"] == "error"


def test_missing_index_is_unavailable_with_hint(monkeypatch):
    def missing():
        raise sop_tool.SopIndexUnavailable("index does not exist. Run `make sop-ingest`")

    monkeypatch.setattr(sop_tool, "_get_vector_store", missing)
    result = sop_tool.search_compliance_sop("anything")
    assert result["status"] == "unavailable" and "make sop-ingest" in result["error"]


def test_schema_context_has_view_and_legacy_glossary():
    context = build_schema_context()
    assert "ai.VW_SUPPLY_CHAIN_READINGS" in context
    assert "IOT_TEMP_C = iot_temperature" in context
    assert "TOP n" in context


@pytest.mark.integration
def test_live_search_finds_temperature_rule():
    result = sop_tool.search_compliance_sop("temperature breach threshold", top_k=2)
    assert result["status"] == "ok" and result["results"]
    assert "4.0" in result["results"][0]["text"]
    assert result["results"][0]["source"].endswith(".md")
