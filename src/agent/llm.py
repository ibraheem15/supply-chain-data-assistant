from resources.settings import load_settings, LLM_ENV_FILE

settings = load_settings(LLM_ENV_FILE)


def build_llm(provider: str | None = None):
    if provider is None:
        provider = settings["LLM_PROVIDER"]
        # settings.get("LLM_PROVIDER") returns None silently if the key is missing from the env file, and you only find out when the API call fails with an unclear error. settings["LLM_PROVIDER"] raises a clear KeyError right away.

    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=settings["GEMINI_MODEL"],
            api_key=settings["GEMINI_API_KEY"],
            temperature=0,  # 0 will give to the point answers
            max_retries=1,
        )
    if provider == "groq":
        from langchain_groq import ChatGroq

        return ChatGroq(
            model=settings["GROQ_MODEL"],
            api_key=settings["GROQ_API_KEY"],
            temperature=0,
            max_retries=1,
        )

    raise ValueError(f"Unsupported LLM provider: {provider}")


def describe_llm(provider: str | None = None) -> tuple[str, str]:
    """
    Returns (provider, model name), so the audit log can record which model answered.
    """
    provider = provider or settings["LLM_PROVIDER"]
    return provider, settings[f"{provider.upper()}_MODEL"]


if __name__ == "__main__":
    print(build_llm().invoke("Say hi in 3 words").text)
