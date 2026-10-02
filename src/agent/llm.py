"""Build the chat model that powers the Reasoner node: Gemini or Groq, chosen by LLM_PROVIDER."""

# `from __future__ import annotations` makes type hints lazy strings, so hints such as
# `str | None` work on any Python version and never run at import time. Safe to ignore.
from __future__ import annotations

# BaseChatModel is LangChain's common interface (~ a Java interface) that every chat model
# (Gemini, Groq, ...) implements, so the rest of the agent never cares which vendor is used.
from langchain_core.language_models import BaseChatModel

from resources.settings import LLM_ENV_FILE, load_settings

# Module-level constants (Python has no `final`; UPPER_CASE is the convention for "do not change").
# The allowed provider names. A tuple `( , )` is an immutable list.
PROVIDERS = ("gemini", "groq")
# Provider used when neither the CLI flag nor LLM_PROVIDER says otherwise.
DEFAULT_PROVIDER = "gemini"
TEMPERATURE = 0  # repeatable tool choice and SQL generation
MAX_RETRIES = 1  # free APIs: one retry for transient errors, no more
# Seconds to wait for the provider before giving up on one request.
TIMEOUT_SECONDS = 60
MAX_OUTPUT_TOKENS = 2048  # bounds the answer (and, for reasoning models, the reasoning) per request


# Custom exception type for bad/missing LLM settings. Subclassing Exception is like
# `extends RuntimeException`; the empty body is just the docstring (no members needed).
class LlmConfigError(Exception):
    """The LLM settings are missing or unusable."""


# Fetch a mandatory setting or fail with a clear message.
# Inputs: the settings dict (env-file values) and the key name. Output: the value.
# Raises LlmConfigError if absent, empty, or still the "replace-with..." placeholder from the
# example env file. Leading underscore = "private by convention" (no real access modifier).
def _require(settings: dict[str, str], key: str) -> str:
    # dict.get(key, default) ~ Map.getOrDefault: returns "" instead of raising when key is missing.
    value = settings.get(key, "")
    if not value or value.startswith("replace-with"):
        raise LlmConfigError(f"{key} is missing. Set it in {LLM_ENV_FILE}.")
    return value


# Decide which provider to use: CLI override > LLM_PROVIDER setting > DEFAULT_PROVIDER.
# Output: a normalised name ("gemini" or "groq"); raises LlmConfigError for anything else.
# `str | None` is a union type: the argument may be a string or None (~ Optional<String>).
def resolve_provider(settings: dict[str, str], override: str | None = None) -> str:
    # `a or b or c` returns the first truthy value (None and "" are falsy), a null-coalescing chain.
    provider = (override or settings.get("LLM_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    if provider not in PROVIDERS:
        raise LlmConfigError(f"LLM_PROVIDER must be one of {', '.join(PROVIDERS)}; got {provider!r}.")
    return provider


# Factory for the chat model the Reasoner node calls (the "brain" of the agent).
# Inputs: optional settings dict (tests pass one; otherwise read from envs/LLM.env) and optional
# provider override. Output: a LangChain chat model ready to use. Called once at CLI start-up.
def build_llm(settings: dict[str, str] | None = None, provider: str | None = None) -> BaseChatModel:
    """`provider` (e.g. from a CLI flag) wins over LLM_PROVIDER in the settings."""
    if settings is None:
        # prefixes=(...) keeps only env keys that start with one of these, ignoring unrelated ones.
        settings = load_settings(LLM_ENV_FILE, prefixes=("GEMINI_", "GROQ_", "LLM_"))
    provider = resolve_provider(settings, provider)

    if provider == "groq":
        # Import inside the branch (lazy import): the unused vendor's package is never loaded,
        # which keeps start-up fast. Python allows imports anywhere, not only at the top.
        from langchain_groq import ChatGroq

        # Keyword arguments (name=value) ~ a Java builder; order does not matter.
        return ChatGroq(
            model=_require(settings, "GROQ_MODEL"),
            api_key=_require(settings, "GROQ_API_KEY"),
            temperature=TEMPERATURE,
            max_retries=MAX_RETRIES,
            timeout=TIMEOUT_SECONDS,
            max_tokens=MAX_OUTPUT_TOKENS,
        )

    # Reached only when provider == "gemini" (the `if` above returned already, so no `else` needed).
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(
        model=_require(settings, "GEMINI_MODEL"),
        google_api_key=_require(settings, "GEMINI_API_KEY"),
        temperature=TEMPERATURE,
        max_retries=MAX_RETRIES,
        timeout=TIMEOUT_SECONDS,
        max_output_tokens=MAX_OUTPUT_TOKENS,
    )
