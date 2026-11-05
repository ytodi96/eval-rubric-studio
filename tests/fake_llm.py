"""A stand-in for the model: returns canned JSON per schema name, so tests need no network or money."""

from evalstudio.llm import LLMResult


class FakeLLM:
    def __init__(self, replies: dict):
        self.replies = replies  # schema_name -> dict, or a function(user_message) -> dict
        self.calls = []

    def __call__(self, provider, model, system, messages, *, schema=None, schema_name="answer", **kw):
        user = messages[-1]["content"]
        self.calls.append({"schema_name": schema_name, "system": system, "user": user})
        reply = self.replies[schema_name]
        data = reply(user) if callable(reply) else reply
        return LLMResult(text="", data=data, provider=provider, model=model,
                         input_tokens=1000, cached_tokens=0, output_tokens=500)
