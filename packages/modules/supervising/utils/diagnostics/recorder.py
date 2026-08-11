import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# Characters Windows forbids in path components, plus a few that are technically allowed
# but routinely cause trouble in tooling (brackets, parentheses). We replace them with "_"
# so subdirectory names derived from agent names like "Documentation Agent [DOC-01 -> X]"
# don't blow up Path.mkdir on Windows.
_INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*\[\]]')


def _sanitize_path_component(value: str) -> str:
    cleaned = _INVALID_PATH_CHARS.sub("_", value)
    cleaned = cleaned.strip(" .")  # Windows also disallows trailing dots/spaces in a segment
    return cleaned or "unnamed"


class IterationRecorder:
    """Writes per-iteration diagnostic dumps for agent repair loops.

    One JSON file per iteration is written to:
        Artifacts/<repo_name>/_iterations/<scope>/<sub_scope>/<run_id>/iter_NN_<phase>_<status>.json

    Each file contains the model's content (raw, possibly invalid JSON) plus the
    schema errors or verifier issues that caused the retry. Use these dumps to
    look at *what the model actually emitted* on each retry — invaluable for
    diagnosing oscillation, regression, or stuck-on-the-same-issue patterns.
    """

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        log.info("Diagnostics dir -> %s", self.base_dir)

    @classmethod
    def for_repo(
        cls,
        repo_local_path: str | Path | None,
        scope: str,
        sub_scope: str | None = None,
    ) -> "IterationRecorder":
        repo_name = (
            _sanitize_path_component(Path(repo_local_path).name)
            if repo_local_path
            else "unknown_repo"
        )
        run_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        parts = [Path("Artifacts"), repo_name, "_iterations", _sanitize_path_component(scope)]
        if sub_scope:
            parts.append(_sanitize_path_component(sub_scope))
        parts.append(run_id)
        base_dir = Path(*parts)
        return cls(base_dir=base_dir)

    def record(
        self,
        retry: int,
        phase: str,
        status: str,
        content: str,
        *,
        errors: Any = None,
        issues: Any = None,
        extra: dict[str, Any] | None = None,
    ) -> Path:
        filename = f"iter_{retry:02d}_{phase}_{status}.json"
        payload: dict[str, Any] = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "retry": retry,
            "phase": phase,
            "status": status,
            "content": content,
        }
        if errors is not None:
            payload["errors"] = errors
        if issues is not None:
            payload["issues"] = issues
            payload["issue_count"] = len(issues) if isinstance(issues, list) else None
            if isinstance(issues, list):
                payload["blocking_count"] = sum(
                    1 for iss in issues if isinstance(iss, dict) and iss.get("severity") == "error"
                )
        if extra:
            payload["extra"] = extra

        file_path = self.base_dir / filename
        file_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return file_path


class NullRecorder:
    """No-op recorder. Use when diagnostics are disabled or no repo context exists."""

    def record(self, *args: Any, **kwargs: Any) -> None:
        return None
