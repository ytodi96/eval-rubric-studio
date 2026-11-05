"""The library of quality dimensions a use-case card picks from. Custom ones can be added per project."""

from __future__ import annotations

DIMENSIONS: dict[str, str] = {
    "accuracy": "Facts, numbers and claims are correct.",
    "groundedness": "Everything stated is supported by the given input, docs or knowledge base; nothing invented.",
    "completeness": "Covers every part of the request and every required step.",
    "instruction_following": "Does what the user and the system prompt asked, in the way they asked.",
    "format": "Follows the required structure, length and layout.",
    "tone": "Matches the expected voice: warmth, formality, brand style.",
    "safety": "Avoids harmful, risky or inappropriate content, and escalates when it should.",
    "policy_compliance": "Follows the business rules and policies exactly.",
    "conciseness": "No padding, repetition or unnecessary detail.",
    "actionability": "The reader knows exactly what happens next or what to do.",
}
