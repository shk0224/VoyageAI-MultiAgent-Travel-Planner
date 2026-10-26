"""One place that decides HOW we ask the LLM for structured output.

`with_structured_output` can work in different ways ("methods"):
- function_calling: the model "calls a tool" whose arguments are our form (default)
- json_schema: the provider forces the reply to match our JSON schema
- json_mode: the model is told to reply with JSON only

Different Groq models support these differently. If extraction fails with a
tool-calling error, set GROQ_STRUCTURED_METHOD=json_schema in .env.
Run `python check_setup.py` to see which method works with your key and model.
"""
from __future__ import annotations

import os

VALID_METHODS = ("function_calling", "json_schema", "json_mode")


def structured_method() -> str:
    method = os.getenv("GROQ_STRUCTURED_METHOD", "function_calling").strip()
    return method if method in VALID_METHODS else "function_calling"


def structured(llm, schema):
    """llm.with_structured_output(schema) using the configured method."""
    return llm.with_structured_output(schema, method=structured_method())
