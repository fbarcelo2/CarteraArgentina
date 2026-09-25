"""An OpenAI-compatible completion backend, for narrating figures computed here.

The base URL, the model and the key are configuration, not constants, because the
honest default is *no* backend: the core computes everything without one, and this
adapter exists only to turn already-computed figures into prose. llama.cpp, Ollama
and vLLM all speak this shape, so one adapter covers local and remote without a
vendor SDK per provider.

The key is never logged, never echoed, and never placed in the request URL.
"""

from __future__ import annotations

import httpx

from cartera.domain.errors import CarteraError

#: Nothing below this is an error: the endpoint answered, and the answer is the text.
HTTP_ERROR_MIN = 400

#: Zero by default: narration should be reproducible, and a temperature is the
#: only knob in this call that would make it not.
DEFAULT_TEMPERATURE = 0.0


class AnalysisBackendError(CarteraError):
    """The narration backend failed, or answered with something unusable."""


class OpenAICompatibleBackend:
    """Talks to any ``/chat/completions`` endpoint that follows OpenAI's shape."""

    name = "openai-compatible"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: float = 120.0,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._client: httpx.AsyncClient | None = None

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def complete(self, system_prompt: str, user_prompt: str) -> str:
        """Ask for text. Never retried blindly: a failed narration is a result."""
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        client = await self._get_client()
        try:
            response = await client.post(self.endpoint, json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            # The URL is included, the key never is.
            raise AnalysisBackendError(f"analysis backend at {self.endpoint} is unreachable: {exc}") from exc

        if response.status_code >= HTTP_ERROR_MIN:
            detail = response.text[:200].replace("\n", " ")
            raise AnalysisBackendError(f"analysis backend returned HTTP {response.status_code}: {detail}")

        return self._extract(response)

    @staticmethod
    def _extract(response: httpx.Response) -> str:
        try:
            document = response.json()
        except ValueError as exc:
            raise AnalysisBackendError("analysis backend returned a body that is not JSON") from exc

        try:
            content = document["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AnalysisBackendError(
                f"analysis backend answered with an unexpected shape: {sorted(document)[:6]}",
            ) from exc

        if not isinstance(content, str) or not content.strip():
            raise AnalysisBackendError("analysis backend returned an empty narration")
        return content.strip()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout_seconds)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
