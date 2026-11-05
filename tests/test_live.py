"""Real API calls. Skipped unless you run `pytest --live` (costs well under $0.01)."""

import pytest

from evalstudio.config import load_settings
from evalstudio.llm import complete


@pytest.mark.live
def test_cheapest_model_answers_with_structured_output():
    load_settings()
    schema = {"type": "object", "properties": {"answer": {"type": "integer"}}, "required": ["answer"],
              "additionalProperties": False}
    r = complete("openai", "gpt-6-luna", "Answer with JSON.", [{"role": "user", "content": "What is 2 + 2?"}],
                 schema=schema, max_tokens=500)
    assert r.data == {"answer": 4} and r.cost_usd < 0.01
