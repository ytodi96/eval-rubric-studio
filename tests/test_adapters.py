"""The four target adapters."""

import json
from types import SimpleNamespace as NS

from evalstudio import adapters
from evalstudio.adapters import fill_template, get_path, make_adapter, messages_for
from evalstudio.llm import LLMError, LLMResult
from evalstudio.models import Target, TestCase

CASE = TestCase(id="c1", input="Where is my order?", category="x", expected_behaviours=["y"],
                context={"order": "#BC-1", "status": "in transit"})
CONVO = TestCase(id="c2", category="x", expected_behaviours=["y"],
                 input=[{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello!"},
                        {"role": "user", "content": "Refund please"}])


def test_messages_put_context_before_the_last_user_message():
    msgs = messages_for(CASE)
    assert msgs == [{"role": "user", "content": "<context>\norder: #BC-1\nstatus: in transit\n</context>\n\nWhere is my order?"}]
    assert [m["role"] for m in messages_for(CONVO)] == ["user", "assistant", "user"]


def test_prompt_adapter(monkeypatch):
    seen = {}

    def fake_complete(provider, model, system, messages, **kw):
        seen.update(provider=provider, model=model, system=system, messages=messages, **kw)
        return LLMResult(text="Hi there!", data=None, provider=provider, model=model, input_tokens=100, output_tokens=20)

    monkeypatch.setattr(adapters, "complete", fake_complete)
    t = Target.model_validate({"config": {"adapter": "prompt", "model": "gpt-6-luna", "system_prompt": "Be kind.",
                                          "max_tokens": 900}})
    r = make_adapter(t).respond(CASE)
    assert r.output == "Hi there!" and r.output_tokens == 20 and r.model == "gpt-6-luna" and r.cost_usd > 0
    assert seen["system"] == "Be kind." and seen["max_tokens"] == 900 and seen["provider"] == "openai"


def test_prompt_adapter_records_errors(monkeypatch):
    def broken(*a, **kw):
        raise LLMError("The OpenAI account has no credit left.")

    monkeypatch.setattr(adapters, "complete", broken)
    t = Target.model_validate({"config": {"adapter": "prompt", "model": "m", "system_prompt": "s"}})
    r = make_adapter(t).respond(CASE)
    assert r.output is None and "no credit" in r.error


def test_fill_template():
    body = {"message": "{{input}}", "history": "{{messages}}", "order": "Order {{context.order}}", "meta": {"id": "{{id}}"}}
    out = fill_template(body, CASE)
    assert out["message"] == "Where is my order?" and isinstance(out["history"], list)
    assert out["order"] == "Order #BC-1" and out["meta"] == {"id": "c1"}


def test_get_path():
    data = {"choices": [{"message": {"content": "ok"}}]}
    assert get_path(data, "choices.0.message.content") == "ok" and get_path(data, None) is data


def test_http_adapter(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "secret")
    sent = {}

    class Client:
        def post(self, url, headers, timeout, **kw):
            sent.update(url=url, headers=headers, **kw)
            return NS(status_code=200, json=lambda: {"reply": {"text": "Sure!"}}, text="")

    t = Target.model_validate({"config": {"adapter": "http", "url": "http://localhost:9/chat",
                                          "headers": {"Authorization": "Bearer ${MY_TOKEN}"},
                                          "body": {"q": "{{input}}"}, "response_path": "reply.text"}})
    a = make_adapter(t)
    a.client = Client()
    r = a.respond(CASE)
    assert r.output == "Sure!" and sent["json"] == {"q": "Where is my order?"}
    assert sent["headers"]["Authorization"] == "Bearer secret"


def test_http_adapter_records_http_errors():
    class Client:
        def post(self, *a, **kw):
            return NS(status_code=500, text="boom", json=lambda: {})

    t = Target.model_validate({"config": {"adapter": "http", "url": "http://x", "body": {}, "response_path": "a"}})
    a = make_adapter(t)
    a.client = Client()
    assert a.respond(CASE).error == "HTTP 500: boom"


def test_python_adapter():
    t = Target.model_validate({"config": {"adapter": "python", "function": "tests.fake_target:reply",
                                          "response_path": "draft"}})
    r = make_adapter(t).respond(CASE)
    assert r.output == "Echo: Where is my order? (#BC-1)" and r.cost_usd == 0.001


def test_offline_adapter(tmp_path):
    f = tmp_path / "responses.csv"
    f.write_text("test_case_id,output\nc1,Saved answer\n", encoding="utf-8")
    t = Target.model_validate({"config": {"adapter": "offline", "file": str(f)}})
    a = make_adapter(t)
    assert a.respond(CASE).output == "Saved answer"
    assert "No response for 'c2'" in a.respond(CONVO).error
    j = tmp_path / "r.json"
    j.write_text(json.dumps([{"id": "c2", "text": "From JSON"}]), encoding="utf-8")
    t = Target.model_validate({"config": {"adapter": "offline", "file": str(j), "id_field": "id", "output_field": "text"}})
    assert make_adapter(t).respond(CONVO).output == "From JSON"
