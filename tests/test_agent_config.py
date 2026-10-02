import pytest
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_groq import ChatGroq

from agent.llm import MAX_OUTPUT_TOKENS, MAX_RETRIES, TEMPERATURE, LlmConfigError, build_llm, resolve_provider

# Fake settings dict (stands in for the .env file) with keys for both LLM providers.
# Tests pass it straight to build_llm, so no real environment variables are read.
BOTH = {"GEMINI_API_KEY": "k", "GEMINI_MODEL": "g-model", "GROQ_API_KEY": "k", "GROQ_MODEL": "q-model"}


# Proves: with no LLM_PROVIDER set, Gemini is chosen, and the agent is configured to be
# deterministic (temperature 0, one retry). Why: repeatable answers and bounded quota use.
def test_default_provider_is_gemini():
    llm = build_llm(BOTH)
    assert isinstance(llm, ChatGoogleGenerativeAI)
    assert TEMPERATURE == 0 and MAX_RETRIES == 1
    assert llm.temperature == 0 and llm.max_retries == 1


# Proves: LLM_PROVIDER=groq switches provider but keeps the same determinism settings.
# `{**BOTH, ...}` copies the dict and overrides/adds a key (like Map.putAll on a copy).
def test_setting_selects_groq_with_same_determinism():
    llm = build_llm({**BOTH, "LLM_PROVIDER": "groq"})
    assert isinstance(llm, ChatGroq)
    assert llm.temperature < 1e-6  # ChatGroq turns 0 into 1e-8: the Groq API rejects exactly 0
    assert llm.max_retries == 1


# Proves: the explicit function argument beats the config setting, and the provider name is
# normalised (case and whitespace ignored). Like a CLI flag overriding an env var.
def test_argument_overrides_setting():
    assert isinstance(build_llm({**BOTH, "LLM_PROVIDER": "gemini"}, provider="groq"), ChatGroq)
    assert resolve_provider({"LLM_PROVIDER": "GROQ "}) == "groq"


# Proves: an unsupported provider fails fast with a clear error naming the setting.
# `pytest.raises(..., match=...)` ~ assertThrows plus a regex check on the exception message.
def test_unknown_provider_is_rejected():
    with pytest.raises(LlmConfigError, match="LLM_PROVIDER"):
        build_llm({**BOTH, "LLM_PROVIDER": "openai"})


# Parametrized test ~ JUnit @ParameterizedTest: the test body below runs once per (settings, provider)
# tuple. Every case is a broken config (missing key, missing model, or a placeholder value copied
# from the example env file) and must raise LlmConfigError rather than fail later at call time.
@pytest.mark.parametrize(
    "settings, provider",
    [
        ({}, None),
        ({"GEMINI_API_KEY": "k"}, None),
        ({"GEMINI_MODEL": "m"}, None),
        ({"GEMINI_API_KEY": "replace-with-x", "GEMINI_MODEL": "m"}, None),
        ({"GEMINI_API_KEY": "k", "GEMINI_MODEL": "replace-with-your-gemini-model-name"}, None),
        ({"GEMINI_API_KEY": "k", "GEMINI_MODEL": "m"}, "groq"),  # groq selected, groq keys missing
        ({"GROQ_API_KEY": "k"}, "groq"),
    ],
)
# Proves: bad configuration is reported clearly at startup, not as an obscure API failure later.
def test_missing_or_placeholder_settings_fail_clearly(settings, provider):
    with pytest.raises(LlmConfigError):
        build_llm(settings, provider=provider)


# Proves: both providers get the same max output token cap (cost and runaway-answer guard).
# The attribute name differs per library (max_output_tokens vs max_tokens), hence two asserts.
def test_output_length_is_capped_for_both_providers():
    assert build_llm(BOTH).max_output_tokens == MAX_OUTPUT_TOKENS
    assert build_llm(BOTH, provider="groq").max_tokens == MAX_OUTPUT_TOKENS
