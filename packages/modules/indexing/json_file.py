import json
from typing import Any

from packages.modules.indexing.models import evidence
from packages.modules.skills.reading import Reader


def index_json_file(relative_path: str, result: dict[str, Any], source_reader: Reader) -> None:
    content = source_reader.read_file(relative_path)
    lines = content.splitlines()
    payload = json.loads(content)

    result["files_indexed"].append(relative_path)
    result["configs"].append(
        {
            "file": relative_path,
            "kind": "json",
            "line_count": len(lines),
            "top_level_type": type(payload).__name__,
            "top_level_keys": list(payload.keys()) if isinstance(payload, dict) else [],
            "evidence": evidence(relative_path, lines, 1),
        }
    )
    result["analysis_targets"].append(
        {
            "file": relative_path,
            "target": relative_path,
            "target_type": "config",
            "reason": "configuration file listed as code",
            "evidence": evidence(relative_path, lines, 1),
        }
    )
