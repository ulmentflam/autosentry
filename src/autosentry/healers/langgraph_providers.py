"""Provider factory for the LangGraph healer.

One function (``get_chat_model``) hides the per-provider import + class
mapping. Adding or removing a provider is a single ``elif`` here —
nothing else in the codebase touches LangChain provider classes.

Provider matrix (autosentry 0.10.0):

| provider  | env var             | LangChain class                       |
|-----------|---------------------|---------------------------------------|
| anthropic | ANTHROPIC_API_KEY   | ``ChatAnthropic``                     |
| openai    | OPENAI_API_KEY      | ``ChatOpenAI``                        |
| google    | GOOGLE_API_KEY      | ``ChatGoogleGenerativeAI``            |

Each provider is a hard dep (see ``pyproject.toml``), so the import
itself can't fail — only "API key not set" raises here, with a message
that names the env var.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from autosentry.config import LangGraphProvider

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel


#: Environment variable each provider reads for its API key. Surfaced
#: by ``autosentry doctor`` so operators can tell which provider is
#: usable without firing a real request.
PROVIDER_API_KEY_ENV: dict[LangGraphProvider, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
}


class ProviderConfigError(RuntimeError):
    """Raised when a provider's required env var is missing.

    The message names the env var so operators can act on it without
    reading our docs.
    """


def get_chat_model(
    provider: LangGraphProvider,
    model: str,
    *,
    temperature: float = 0.0,
    request_timeout_seconds: int = 120,
) -> BaseChatModel:
    """Return a configured LangChain chat model for ``provider``.

    Raises :class:`ProviderConfigError` if the provider's API key env
    var isn't set. Doesn't make a network call — only constructs the
    client.
    """
    env_var = PROVIDER_API_KEY_ENV[provider]
    if not os.environ.get(env_var):
        msg = (
            f"LangGraph healer: provider={provider!r} requires the {env_var} environment "
            f"variable to be set. Either export it, switch providers in healing.langgraph, "
            f"or disable the healer (healing.langgraph.enabled: false)."
        )
        raise ProviderConfigError(msg)

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model_name=model,
            temperature=temperature,
            timeout=float(request_timeout_seconds),
            stop=None,
        )
    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model,
            temperature=temperature,
            timeout=float(request_timeout_seconds),
        )
    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=model,
            temperature=temperature,
            timeout=float(request_timeout_seconds),
        )
    # Pydantic Literal validation should make this unreachable; raise
    # rather than ``assert`` so a misconfigured runtime fails loud.
    msg = f"unknown provider: {provider!r}"
    raise ProviderConfigError(msg)


def available_providers() -> list[tuple[LangGraphProvider, str, bool]]:
    """Return ``(provider, env_var, key_present)`` for each supported provider.

    Used by ``autosentry doctor`` to report which providers are ready
    to go without firing a request.
    """
    return [(p, env, bool(os.environ.get(env))) for p, env in PROVIDER_API_KEY_ENV.items()]
