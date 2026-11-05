"""The LLM layer, tested with stand-in clients (no network, no cost)."""

import json
from types import SimpleNamespace as NS

import pytest

from evalstudio.config import load_settings
from evalstudio.llm import LLMError, complete, cost_usd

SCHEMA = {"type": "object", "properties": {"score": {"type": "integer"}}, "required": ["score"],
          "additionalProperties": False}


class FakeOpenAI:
    def __init__(self, text='{"score": 3}', status="completed", refusal=False):
        self.sent = None
        content = [NS(type="refusal" if refusal else "output_text")]
        self.response = NS(status=status, incomplete_details=NS(reason="max_output_tokens"),
                           output=[NS(type="message", content=content)], output_text=text,
                           usage=NS(input_tokens=1000, input_tokens_details=NS(cached_tokens=400), output_tokens=200))
        self.responses = NS(create=self._create)

    def _create(self, **kw):
        self.sent = kw
        return self.response


class FakeAnthropic:
    def __init__(self, text='{"score": 2}', stop_reason="end_turn"):
        self.sent = None
        self.message = NS(stop_reason=stop_reason, content=[NS(type="text", text=text)],
                          usage=NS(input_tokens=900, cache_creation_input_tokens=0,
                                   cache_read_input_tokens=100, output_tokens=50))
        self.messages = NS(stream=self._stream)

    def _stream(self, **kw):
        self.sent = kw
        fake = self

        class Ctx:
            def __enter__(self):
                return NS(get_final_message=lambda: fake.message)

            def __exit__(self, *a):
                return False

        return Ctx()


def test_openai_structured_output():
    client = FakeOpenAI()
    r = complete("openai", "gpt-6.1-sol", "sys", [{"role": "user", "content": "hi"}],
                 schema=SCHEMA, schema_name="grade", effort="medium", client=client)
    assert r.data == {"score": 3}
    fmt = client.sent["text"]["format"]
    assert fmt == {"type": "json_schema", "name": "grade", "schema": SCHEMA, "strict": True}
    assert client.sent["instructions"] == "sys" and client.sent["reasoning"] == {"effort": "medium"}
    assert client.sent["store"] is False
    assert (r.input_tokens, r.cached_tokens, r.output_tokens) == (600, 400, 200)
    assert r.cost_usd == pytest.approx((600 * 2 + 400 * 0.1 + 200 * 10) / 1e6)


def test_openai_plain_text_has_no_format():
    client = FakeOpenAI(text="Hello")
    r = complete("openai", "gpt-6-luna", "sys", [{"role": "user", "content": "hi"}], client=client)
    assert r.text == "Hello" and r.data is None and "text" not in client.sent and "reasoning" not in client.sent


def test_openai_cut_off_and_refusal_are_errors():
    with pytest.raises(LLMError, match="Raise max_tokens"):
        complete("openai", "m", "s", [{"role": "user", "content": "x"}], client=FakeOpenAI(status="incomplete"))
    with pytest.raises(LLMError, match="declined"):
        complete("openai", "m", "s", [{"role": "user", "content": "x"}], client=FakeOpenAI(refusal=True))


def test_anthropic_structured_output():
    client = FakeAnthropic()
    r = complete("anthropic", "claude-opus-5-5", "sys", [{"role": "user", "content": "hi"}],
                 schema=SCHEMA, effort="high", client=client)
    assert r.data == {"score": 2}
    assert client.sent["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}}
    assert (r.input_tokens, r.cached_tokens, r.output_tokens) == (900, 100, 50)


def test_anthropic_refusal_is_an_error():
    with pytest.raises(LLMError, match="declined"):
        complete("anthropic", "m", "s", [{"role": "user", "content": "x"}], client=FakeAnthropic(stop_reason="refusal"))


def test_invalid_json_is_a_clear_error():
    with pytest.raises(LLMError, match="isn't valid JSON"):
        complete("openai", "m", "s", [{"role": "user", "content": "x"}], schema=SCHEMA, client=FakeOpenAI(text="nope"))


def test_unknown_model_costs_zero_instead_of_crashing():
    assert cost_usd("some-future-model", 1000, 0, 1000) == 0


def test_settings_default_to_openai(monkeypatch):
    for v in ("EVALSTUDIO_PROVIDER", "EVALSTUDIO_JUDGE_PROVIDER", "EVALSTUDIO_JUDGE_MODEL"):
        monkeypatch.delenv(v, raising=False)
    s = load_settings()
    assert s.provider == "openai" and s.judge.model == "gpt-6.1-sol" and s.pass_mark == 0.8


def test_judge_can_use_another_provider(monkeypatch):
    monkeypatch.setenv("EVALSTUDIO_PROVIDER", "openai")
    monkeypatch.setenv("EVALSTUDIO_JUDGE_PROVIDER", "anthropic")
    monkeypatch.delenv("EVALSTUDIO_JUDGE_MODEL", raising=False)
    s = load_settings()
    assert (s.judge.provider, s.judge.model) == ("anthropic", "claude-opus-5-5")
    assert s.generator.provider == "openai"


def test_schema_round_trip_is_plain_json():
    assert json.loads(json.dumps(SCHEMA)) == SCHEMA
