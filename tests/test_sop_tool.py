import pytest

from resources import sop_tool
from resources.schema_context import build_schema_context


# Fake of a vector-store search hit: just the two attributes the tool reads (text and metadata).
class FakeDocument:
    def __init__(self, text, metadata):
        self.page_content = text
        self.metadata = metadata


# Fake Pinecone vector store returning one canned hit with a similarity score, so the SOP
# tool can be tested offline. Hand-written stub instead of a Mockito mock.
class FakeStore:
    def similarity_search_with_score(self, query, k):
        doc = FakeDocument(
            "If IOT_TEMP_C exceeds 4.0C a breach is declared.",
            {"source": "data/policy/x.md", "Header 1": "SOP", "Header 3": "1. Temperature"},
        )
        # List of (document, score) tuples; the slice [:k] mimics 'return at most k results'.
        return [(doc, 0.81234)][:k]


# Proves: each result exposes source file, joined heading path and a rounded score. Why: the
# agent must cite the SOP file and section for compliance answers.
# `monkeypatch.setattr` ~ Mockito/ReflectionTestUtils: swaps the real vector-store factory for
# a lambda returning the fake, and pytest undoes it after the test.
def test_results_carry_citation_fields(monkeypatch):
    monkeypatch.setattr(sop_tool, "_get_vector_store", lambda: FakeStore())
    result = sop_tool.search_compliance_sop("temperature breach threshold")
    assert result["status"] == "ok"
    first = result["results"][0]
    assert first["source"] == "data/policy/x.md"
    assert first["headings"] == "SOP > 1. Temperature"
    assert first["score"] == 0.8123


# Parametrized (JUnit @ParameterizedTest): empty/blank query, top_k out of 1..10, and top_k of
# the wrong type must all return status 'error' instead of raising or calling Pinecone.
@pytest.mark.parametrize("query, top_k", [("", 3), ("  ", 3), ("x", 0), ("x", 11), ("x", "3")])
# Input validation happens before any network call.
def test_bad_input_is_error(query, top_k):
    assert sop_tool.search_compliance_sop(query, top_k)["status"] == "error"


# Proves: if the Pinecone index is missing, the tool returns status 'unavailable' with a hint
# to run `make sop-ingest`, so the agent can tell the user what is wrong.
def test_missing_index_is_unavailable_with_hint(monkeypatch):
    # Local function that raises, used as the replacement factory to simulate a missing index.
    def missing():
        raise sop_tool.SopIndexUnavailable("index does not exist. Run `make sop-ingest`")

    monkeypatch.setattr(sop_tool, "_get_vector_store", missing)
    result = sop_tool.search_compliance_sop("anything")
    assert result["status"] == "unavailable" and "make sop-ingest" in result["error"]


# Proves: the schema text given to the LLM names the safe view, maps legacy SOP column names
# to real ones, and stays small (it is sent with every request, so size is a cost). The string
# after the comma in assert is the failure message.
def test_schema_context_has_view_and_legacy_glossary():
    context = build_schema_context()
    assert "ai.VW_SUPPLY_CHAIN_READINGS" in context
    assert "iot_temperature [SOP: IOT_TEMP_C]" in context
    assert "NOT columns" in context
    assert "TOP n" in context
    assert len(context) < 2600, "schema context is sent with every request: keep it short"


# Marker ~ JUnit @Tag("integration"): needs a live Pinecone index, skipped by default.
@pytest.mark.integration
# Proves: against the real index, a temperature query returns the 4.0C threshold rule from a
# Markdown source file. Checks the whole pipeline: ingestion, embedding, search.
def test_live_search_finds_temperature_rule():
    result = sop_tool.search_compliance_sop("temperature breach threshold", top_k=2)
    assert result["status"] == "ok" and result["results"]
    assert "4.0" in result["results"][0]["text"]
    assert result["results"][0]["source"].endswith(".md")
