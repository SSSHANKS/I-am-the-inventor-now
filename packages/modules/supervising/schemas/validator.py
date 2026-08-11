import json
import logging
from typing import Any

from marshmallow import Schema
from marshmallow import ValidationError as MarshmallowValidationError

log = logging.getLogger(__name__)


class SchemaValidationError(Exception):
    """Raised when an agent's output fails marshmallow validation after retries are exhausted."""

    def __init__(self, agent_name: str, errors: dict[str, Any], raw_output: str):
        self.agent_name = agent_name
        self.errors = errors
        self.raw_output = raw_output
        preview = json.dumps(errors, ensure_ascii=False)[:500]
        super().__init__(f"{agent_name} output failed schema validation: {preview}")


def get_validation_errors(content: str, schema: Schema) -> dict[str, Any] | None:
    """Return None if content is a valid JSON object matching the schema, else a dict of errors."""
    if not content or not content.strip():
        return {"_root_": ["Empty response. Return the required JSON object."]}

    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        return {
            "_root_": [
                f"Invalid JSON at line {exc.lineno}, col {exc.colno}: {exc.msg}. "
                "Return ONLY a single JSON object. No Markdown, no code fences, no prose."
            ]
        }

    if not isinstance(payload, dict):
        return {
            "_root_": [f"Expected a JSON object at the top level, got {type(payload).__name__}."]
        }

    try:
        schema.load(payload)
    except MarshmallowValidationError as exc:
        return exc.messages

    return None


def build_correction_prompt(errors: dict[str, Any]) -> str:
    return (
        "Your previous response did not match the required JSON schema. "
        "Validation errors (field path -> messages):\n"
        f"{json.dumps(errors, indent=2, ensure_ascii=False)}\n\n"
        "Re-emit ONLY the corrected JSON object matching the exact schema from the system instruction. "
        "Do not add Markdown, headings, code fences, or any text outside the JSON. "
        "Do not invent, rename, omit, or add fields. Use exactly the required top-level keys. "
        "Use the same evidence and findings you already gathered — only fix the structure."
    )
