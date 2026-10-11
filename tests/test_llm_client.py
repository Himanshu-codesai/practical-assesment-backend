"""The LLM client, tested against a stand-in server (no network, no model)."""
import json

import httpx
import pytest

from app.errors import LLMError
from app.llm.client import LLMClient, Server, parse_json_reply
from app.llm.schemas import GroundedAnswer

VALID = '{"answerable": true, "answer": "Sigmoid.", "cited_chunk_indexes": [3]}'
PRIMARY = Server("primary", "http://primary.test", "", "model-a")
FALLBACK = Server("fallback", "http://fallback.test", "secret", "model-b")


def server(*replies):
    """A fake LLM server that returns `replies` in order and records what it was sent."""
    queue, seen = list(replies), []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        reply = queue.pop(0)
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json={
            "model": "test", "stop_reason": "end_turn", "usage": {},
            "content": [{"type": "text", "text": reply}],
        })

    return LLMClient(servers=[PRIMARY], transport=httpx.MockTransport(handle)), seen


def ask(client):
    return client.generate_structured(task="answer", system="s", prompt="p", schema=GroundedAnswer)


# ---- parsing the reply
def test_plain_json_is_parsed():
    assert parse_json_reply(VALID, GroundedAnswer).cited_chunk_indexes == [3]


def test_code_fence_and_surrounding_text_are_tolerated():
    reply = f"Here is the answer:\n```json\n{VALID}\n```\nHope that helps."
    assert parse_json_reply(reply, GroundedAnswer).answer == "Sigmoid."


@pytest.mark.parametrize("reply", [
    "I cannot answer that.",                             # no JSON at all
    '{"answerable": true}',                              # required fields missing
    '{"answerable": true, "answer": "x", "cited_chunk_indexes": ["a"]}',  # wrong type
    '{"answerable": true, "answer": "x", "cited_chunk_indexes": [1]',     # truncated
])
def test_invalid_replies_raise_value_error(reply):
    with pytest.raises(ValueError):
        parse_json_reply(reply, GroundedAnswer)


# ---- the request and the correction loop
def test_a_valid_reply_is_returned_and_the_schema_is_in_the_prompt():
    client, seen = server(VALID)
    assert ask(client).answer == "Sigmoid."
    assert len(seen) == 1 and seen[0]["system"] == "s"
    assert "cited_chunk_indexes" in seen[0]["messages"][0]["content"]  # the JSON schema


def test_an_invalid_reply_gets_one_correction_request():
    client, seen = server("not json", VALID)
    assert ask(client).answer == "Sigmoid."
    assert len(seen) == 2
    roles = [m["role"] for m in seen[1]["messages"]]
    assert roles == ["user", "assistant", "user"]           # the model sees its own bad reply
    assert "not valid" in seen[1]["messages"][2]["content"]


def test_two_invalid_replies_give_up_as_retryable():
    client, seen = server("not json", "still not json")
    with pytest.raises(LLMError) as raised:
        ask(client)
    assert raised.value.retryable and len(seen) == 2


# ---- errors: which ones are worth retrying
@pytest.mark.parametrize("status, retryable", [(500, True), (429, True), (401, False), (400, False)])
def test_server_errors_are_mapped(status, retryable):
    client, _ = server(httpx.Response(status, json={"error": {"message": "nope"}}))
    with pytest.raises(LLMError) as raised:
        ask(client)
    assert raised.value.retryable is retryable and "nope" in raised.value.message


def test_an_unreachable_server_is_retryable():
    def refuse(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMError) as raised:
        ask(LLMClient(servers=[PRIMARY], transport=httpx.MockTransport(refuse)))
    assert raised.value.retryable


def test_a_cut_off_reply_is_an_error():
    cut = httpx.Response(200, json={"stop_reason": "max_tokens", "content": [], "usage": {}})
    client, _ = server(cut)
    with pytest.raises(LLMError):
        ask(client)


# ---- the fallback server
def two_servers(primary_reply, fallback_reply):
    """Primary and fallback stand-ins; records which hosts were called and with what."""
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.host, json.loads(request.content)["model"],
                      request.headers.get("x-api-key")))
        reply = primary_reply if request.url.host == "primary.test" else fallback_reply
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json={"stop_reason": "end_turn", "usage": {},
                                         "content": [{"type": "text", "text": reply}]})

    client = LLMClient(servers=[PRIMARY, FALLBACK], transport=httpx.MockTransport(handle))
    return client, calls


def test_the_fallback_is_not_called_when_the_primary_answers():
    client, calls = two_servers(VALID, VALID)
    assert ask(client).answer == "Sigmoid."
    assert [host for host, _, _ in calls] == ["primary.test"]


def test_the_fallback_answers_when_the_primary_fails():
    client, calls = two_servers(httpx.Response(502, json={"error": {"message": "down"}}), VALID)
    assert ask(client).answer == "Sigmoid."
    assert calls == [("primary.test", "model-a", None), ("fallback.test", "model-b", "secret")]


def test_the_fallback_also_covers_a_rejected_request():
    client, calls = two_servers(httpx.Response(401, json={"error": {"message": "no"}}), VALID)
    assert ask(client).answer == "Sigmoid."
    assert len(calls) == 2


def test_the_last_error_is_raised_when_both_servers_fail():
    down = httpx.Response(503, json={"error": {"message": "down"}})
    client, calls = two_servers(down, down)
    with pytest.raises(LLMError) as raised:
        ask(client)
    assert "fallback" in raised.value.message and raised.value.retryable and len(calls) == 2


def test_thinking_parts_in_a_reply_are_ignored():
    with_thinking = httpx.Response(200, json={"stop_reason": "end_turn", "usage": {}, "content": [
        {"type": "thinking", "thinking": "..."}, {"type": "text", "text": VALID}]})
    client, _ = two_servers(with_thinking, VALID)
    assert ask(client).answer == "Sigmoid."
