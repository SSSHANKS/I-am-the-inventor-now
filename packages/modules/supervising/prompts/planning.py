import json
from typing import Any


def build_semantic_correction_prompt(issues: list[dict[str, Any]]) -> str:
    blocking = [iss for iss in issues if iss.get("severity") == "error"]
    warnings = [iss for iss in issues if iss.get("severity") == "warning"]
    parts: list[str] = [
        "Your previous plan was structurally valid JSON, but it violated the planning content rules.",
        "Fix every blocking issue below and re-emit ONLY the corrected JSON object.",
        "Reuse the same indexed items and verified artifacts you already saw in the conversation; do not invent files, sections, line numbers, or evidence.",
        "Every input_ref MUST use one of the allowed source values, and every evidence MUST be a non-empty object with file, line_start, line_end, excerpt copied from the matching indexed item.",
        "",
        "Blocking issues:",
        json.dumps(blocking, indent=2, ensure_ascii=False) if blocking else "[]",
    ]
    if warnings:
        parts.extend(
            [
                "",
                "Non-blocking warnings (fix them if you can; they usually mean a documentation section or code target is not covered by any mini task):",
                json.dumps(warnings, indent=2, ensure_ascii=False),
            ]
        )
    parts.extend(
        [
            "",
            "Re-emit ONLY a single JSON object matching the planning schema. No Markdown, no code fences, no prose.",
        ]
    )
    return "\n".join(parts)


__all__ = ["build_semantic_correction_prompt"]
