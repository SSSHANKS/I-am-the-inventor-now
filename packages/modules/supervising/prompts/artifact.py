import json
from typing import Any


def build_artifact_correction_prompt(issues: list[dict[str, Any]]) -> str:
    blocking = [iss for iss in issues if iss.get("severity") == "error"]
    warnings = [iss for iss in issues if iss.get("severity") == "warning"]
    parts: list[str] = [
        "Your previous artifact was structurally valid JSON, but its evidence violated the verification rules.",
        "Fix every blocking issue below and re-emit ONLY the corrected JSON object.",
        "Every documented or inferred finding MUST have evidence.file as a non-empty string from the allowed files, "
        "line_start and line_end as integers >= 1, and excerpt copied verbatim from the referenced lines.",
        "For missing findings without a related file, use null (JSON null) for file/line_start/line_end and put a note in excerpt.",
        "Do NOT invent file paths, line numbers, or excerpts. Reuse the indexed items or tool results already present in the conversation.",
        "",
        "Blocking issues:",
        json.dumps(blocking, indent=2, ensure_ascii=False) if blocking else "[]",
    ]
    if warnings:
        parts.extend(
            [
                "",
                "Non-blocking warnings (fix them if you can):",
                json.dumps(warnings, indent=2, ensure_ascii=False),
            ]
        )
    parts.extend(
        [
            "",
            "Re-emit ONLY a single JSON object matching the agent's schema. No Markdown, no code fences, no prose.",
        ]
    )
    return "\n".join(parts)


__all__ = ["build_artifact_correction_prompt"]
