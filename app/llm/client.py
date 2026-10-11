"""The only file that talks to the language model.

It sends one plain HTTP request per call to an LLM server that accepts the "messages"
format (`POST /v1/messages`). No vendor SDK is involved. There can be two servers: the
primary (LLM_BASE_URL) and a fallback (LLM_FALLBACK_BASE_URL) that is asked when the
primary fails.

Every call returns a validated Pydantic object. The JSON schema of that object is written
into the prompt, the model replies with JSON as text, and the reply is validated here. If
it does not validate, the model is shown the error and asked once to correct it.
"""
import json
import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import TypeVar

import httpx
from pydantic import BaseModel

from app.config import settings
from app.errors import LLMError

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

JSON_INSTRUCTIONS = """

Reply with a single JSON object and nothing else: no explanation before or after it and \
no Markdown code fence. The object must validate against this JSON Schema:

{schema}"""


def parse_json_reply(text: str, schema: type[T]) -> T:
    """Validate a plain-text reply against `schema`.

    Tolerates a code fence or a sentence around the object by taking the outermost
    {...}. Raises ValueError (pydantic's ValidationError is one) if it does not fit.
    """
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("The reply contains no JSON object.")
    return schema.model_validate_json(text[start:end + 1])


@dataclass
class Server:
    """One place a request can be sent."""

    name: str
    base_url: str
    api_key: str
    model: str


def configured_servers() -> list[Server]:
    """The primary server, then the fallback if one is set in the environment."""
    servers = [Server("primary", settings.llm_base_url, settings.llm_api_key, settings.llm_model)]
    if settings.llm_fallback_base_url:
        servers.append(Server(
            "fallback", settings.llm_fallback_base_url, settings.llm_fallback_api_key,
            settings.llm_fallback_model or settings.llm_model))
    return servers


class LLMClient:
    def __init__(self, servers: list[Server] | None = None,
                 transport: httpx.BaseTransport | None = None) -> None:
        # Both arguments exist for tests: fixed servers, and replies without a network.
        self.servers = servers or configured_servers()
        self.http = httpx.Client(timeout=settings.llm_timeout_seconds, transport=transport)

    def generate_structured(self, *, task: str, system: str, prompt: str, schema: type[T]) -> T:
        """Run one request and return a validated instance of `schema`, or raise LLMError."""
        instructions = JSON_INSTRUCTIONS.format(schema=json.dumps(schema.model_json_schema()))
        messages = [{"role": "user", "content": prompt + instructions}]
        for attempt in (1, 2):
            reply = self._send(task, system, messages)
            try:
                return parse_json_reply(reply, schema)
            except ValueError as exc:
                log.warning("llm reply failed validation", extra={
                    "task": task, "attempt": attempt, "reason": str(exc)[:500]})
                # Show the model its own reply and the validation error, and ask again.
                messages = messages + [
                    {"role": "assistant", "content": reply},
                    {"role": "user", "content": f"That reply was not valid: {exc}\n"
                                                "Return the corrected JSON object only."},
                ]
        raise LLMError(f"The '{task}' reply did not match the expected schema.", retryable=True)

    def _send(self, task: str, system: str, messages: list[dict]) -> str:
        """Ask the primary server; if it fails for any reason, ask the fallback.

        Raises the last error if every server failed.
        """
        for position, server in enumerate(self.servers, 1):
            try:
                return self._post(server, task, system, messages)
            except LLMError as exc:
                if position == len(self.servers):
                    raise
                log.warning("llm server failed, trying the next one", extra={
                    "task": task, "server": server.name, "reason": exc.message[:300]})

    def _post(self, server: Server, task: str, system: str, messages: list[dict]) -> str:
        """One HTTP request to one server. Returns the reply text, or raises LLMError.

        `retryable` tells the background worker whether trying again later can help: a
        network problem, a rate limit or a server error can pass; a rejected request cannot.
        """
        started = time.perf_counter()
        # anthropic-version is required by hosted Claude APIs and ignored by the local gateway.
        headers = {"anthropic-version": "2023-06-01"}
        if server.api_key:
            headers["x-api-key"] = server.api_key
        try:
            response = self.http.post(
                f"{server.base_url.rstrip('/')}/v1/messages", headers=headers,
                json={"model": server.model, "max_tokens": settings.llm_max_tokens,
                      "system": system, "messages": messages})
        except httpx.HTTPError as exc:
            raise LLMError(f"Could not reach the {server.name} LLM server: {type(exc).__name__}",
                           retryable=True) from exc

        if response.status_code >= 400:
            try:
                detail = response.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                detail = response.text[:200]
            raise LLMError(
                f"The {server.name} LLM server returned {response.status_code}: {detail}",
                retryable=response.status_code == 429 or response.status_code >= 500)

        body = response.json()
        usage = body.get("usage", {})
        log.info("llm call", extra={
            "task": task,
            "server": server.name,
            "model": body.get("model"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "seconds": round(time.perf_counter() - started, 2),
            "stop_reason": body.get("stop_reason"),
        })
        if body.get("stop_reason") == "max_tokens":
            raise LLMError(f"The '{task}' reply was cut off; raise LLM_MAX_TOKENS.")
        # A reply can hold other parts (for example the model's thinking); only text is used.
        return "".join(part.get("text", "") for part in body.get("content", [])
                       if part.get("type") == "text")


@lru_cache
def get_llm() -> LLMClient:
    """One shared client per process."""
    return LLMClient()
