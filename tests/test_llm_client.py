from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx
from pydantic import BaseModel

from app.integrations.llm_client import FALLBACK_BETA, LLMClient

REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


class Output(BaseModel):
    answer: str


class _Stream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


class _Messages:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests: list[dict] = []

    def stream(self, **kwargs):
        self.requests.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _Stream(outcome)


def fake_client(*outcomes):
    messages = _Messages(outcomes)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def message(parsed=None, *, stop="end_turn", text=None):
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0, cache_read_input_tokens=90)
    content = [SimpleNamespace(type="text", text=text)] if text is not None else []
    return SimpleNamespace(stop_reason=stop, parsed_output=parsed, content=content, usage=usage, stop_details=None)


def test_request_shape_uses_adaptive_thinking_fallbacks_and_cached_context(settings):
    client, messages = fake_client(message(Output(answer="ok")))
    llm = LLMClient(settings, client=client)
    result, error = llm.structured(label="t", output_model=Output, instructions="Do the task.", context="<deck>x</deck>",
                                   images=[("Slide 1 (image):", b"\x89PNG")], effort="medium")
    assert error is None and result.answer == "ok"
    request = messages.requests[0]
    assert request["model"] == "claude-opus-5"
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"] == {"effort": "medium"}
    assert request["betas"] == [FALLBACK_BETA] and request["fallbacks"] == "default"
    assert request["output_format"] is Output
    assert request["system"][-1] == {"type": "text", "text": "<deck>x</deck>", "cache_control": {"type": "ephemeral"}}
    content = request["messages"][0]["content"]
    assert content[1]["type"] == "image" and content[-1] == {"type": "text", "text": "Do the task."}
    assert llm.calls == 1 and llm.input_tokens == 100


def test_refusal_and_truncation_are_failures(settings):
    client, _ = fake_client(message(stop="refusal"), message(stop="max_tokens"))
    llm = LLMClient(settings, client=client)
    assert llm.structured(label="t", output_model=Output, instructions="x") == (None, "the AI model declined this request")
    assert llm.structured(label="t", output_model=Output, instructions="x") == (None, "the AI response was cut off")


def test_text_json_is_validated_when_parsed_output_is_absent(settings):
    client, _ = fake_client(message(text='{"answer": "from text"}'), message(text="not json"))
    llm = LLMClient(settings, client=client)
    assert llm.structured(label="t", output_model=Output, instructions="x")[0].answer == "from text"
    assert llm.structured(label="t", output_model=Output, instructions="x")[1] == \
        "the AI response did not match the expected format"


def test_rejected_fallback_beta_is_retried_without_it(settings):
    rejected = anthropic.BadRequestError("fallbacks are not enabled for this organization",
                                         response=httpx.Response(400, request=REQUEST), body=None)
    client, messages = fake_client(rejected, message(Output(answer="ok")), message(Output(answer="again")))
    llm = LLMClient(settings, client=client)
    assert llm.structured(label="t", output_model=Output, instructions="x")[0].answer == "ok"
    assert "fallbacks" not in messages.requests[1]
    llm.structured(label="t", output_model=Output, instructions="x")
    assert "fallbacks" not in messages.requests[2]


def test_api_errors_are_returned_not_raised(settings):
    client, _ = fake_client(
        anthropic.APIConnectionError(request=REQUEST),
        anthropic.AuthenticationError("bad key", response=httpx.Response(401, request=REQUEST), body=None),
        anthropic.RateLimitError("slow down", response=httpx.Response(429, request=REQUEST), body=None),
        RuntimeError("unexpected"),
    )
    llm = LLMClient(settings, client=client)
    errors = [llm.structured(label="t", output_model=Output, instructions="x")[1] for _ in range(4)]
    assert errors == ["the AI model could not be reached", "the Anthropic API key was rejected",
                      "the AI model is rate-limited", "unexpected AI failure (RuntimeError)"]


def test_ping_uses_the_models_api_and_reports_rejected_keys(settings):
    retrieved = SimpleNamespace(display_name="Claude Opus 5")
    ok_client = SimpleNamespace(models=SimpleNamespace(retrieve=lambda model: retrieved))
    assert LLMClient(settings, client=ok_client).ping()[0] is True

    def reject(model):
        raise anthropic.AuthenticationError("invalid x-api-key", response=httpx.Response(401, request=REQUEST), body=None)

    ok, message = LLMClient(settings, client=SimpleNamespace(models=SimpleNamespace(retrieve=reject))).ping()
    assert not ok and "rejected" in message


def test_unconfigured_client_is_unavailable(settings):
    llm = LLMClient(settings)
    assert not llm.available
    result, error = llm.structured(label="t", output_model=Output, instructions="x")
    assert result is None and "not configured" in error
