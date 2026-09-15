"""Run the isolated Clean reconstruction from a verified Border handoff."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import ConfigError, load_environment, load_settings, setup_logging
from packages.agents.base_agent import StubTextClient
from packages.agents.clean_team import CleanBuilderAgent, CleanPlannerAgent, CleanRepairAgent
from packages.modules.clean import (
    CleanBuildError,
    CleanRunner,
    LocalPythonValidationExecutor,
    WorkspaceError,
)
from packages.modules.handoff import HandoffError


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="clean_main.py",
        description="Reconstruct a project using only a verified Clean handoff.",
    )
    parser.add_argument("handoff", type=Path, help="directory containing handoff.json and specification.md")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="new or empty Clean workspace (default: clean_workspaces/<run>)",
    )
    parser.add_argument(
        "--clean-max-repairs",
        type=int,
        default=2,
        help="maximum deterministic validation repair rounds (default: 2)",
    )
    parser.add_argument("--stub", action="store_true", help="make no model/API calls")
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help=(
            "skip language syntax parsing; structural checks still run and the result "
            "cannot be successful"
        ),
    )
    parser.add_argument(
        "--execute-generated-code",
        action="store_true",
        help=(
            "opt in to local import and pytest validation; this uses restricted child "
            "processes but is not an OS security sandbox"
        ),
    )
    parser.add_argument(
        "--validation-timeout",
        type=float,
        default=30.0,
        help="per-process timeout for executable validation in seconds (default: 30)",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace):
    setup_logging()
    load_environment()
    settings = load_settings()
    output = args.output or Path(__file__).resolve().parent / "clean_workspaces" / args.handoff.parent.name

    def agent_kwargs(name: str) -> dict:
        agent = settings.agent(name)
        retry_path = agent.retry_profile_path(settings.model_profiles_dir)
        kwargs = {
            "model": agent.model,
            "profile_path": str(agent.profile_path(settings.model_profiles_dir)),
            "retry_profile_path": str(retry_path) if retry_path else None,
            "max_validation_retries": agent.max_validation_retries,
        }
        if args.stub:
            kwargs["chat_client"] = StubTextClient(_stubbed_clean_reply)
        return kwargs

    validation_executor = None
    if args.execute_generated_code:
        validation_executor = LocalPythonValidationExecutor(
            timeout_seconds=args.validation_timeout
        )

    runner = CleanRunner(
        CleanPlannerAgent(**agent_kwargs("clean_planner")),
        CleanBuilderAgent(**agent_kwargs("clean_builder")),
        CleanRepairAgent(**agent_kwargs("clean_repair")),
        max_repairs=args.clean_max_repairs,
        syntax_checks=not args.no_validate,
        validation_executor=validation_executor,
    )
    return runner.run(args.handoff, output)


def _stubbed_clean_reply(prompt: str) -> str:
    if "<clean_plan_request>" in prompt:
        validation_policy = json.loads(_tag(prompt, "executable_validation_policy"))
        executable_validation_required = validation_policy["enabled"]
        files = [
            {
                "path": "README.md",
                "purpose": "Usage documentation",
                "requirement_ids": ["FR-001"],
                "provides": [],
                "requires": [],
                "depends_on": [],
                "generation_order": 1,
            },
            {
                "path": "app.py",
                "purpose": "Small reconstructed entry point",
                "requirement_ids": ["FR-001"],
                "provides": ["SYM-001"],
                "requires": [],
                "depends_on": [],
                "generation_order": 2,
            },
        ]
        if executable_validation_required:
            files.append(
                {
                    "path": "tests/test_app.py",
                    "purpose": "Verify the reconstructed entry point",
                    "requirement_ids": ["FR-001"],
                    "provides": [],
                    "requires": ["SYM-001"],
                    "depends_on": ["app.py"],
                    "generation_order": 3,
                }
            )
        return json.dumps(
            {
                "schema_version": 2,
                "summary": "Dependency-free demonstration reconstruction.",
                "project_kind": "application",
                "runtime": {"language": "Python", "minimum_version": "3.12"},
                "dependencies": [],
                "packages": [],
                "entry_points": [{"path": "app.py", "description": "Example entry point"}],
                "symbol_contracts": [
                    {
                        "symbol_id": "SYM-001",
                        "qualified_name": "app.main",
                        "kind": "function",
                        "signature": "main() -> None",
                        "visibility": "public",
                        "requirement_ids": ["FR-001"],
                    }
                ],
                "files": files,
                "validation_strategy": (
                    ["Import the entry point and run pytest"]
                    if executable_validation_required
                    else ["Parse Python without executing it"]
                ),
                "open_questions": ["Stub mode does not reconstruct real behaviour."],
            }
        )
    if "<clean_file_task>" in prompt:
        task = json.loads(_tag(prompt, "requested_file"))
        if task["path"] == "README.md":
            content = "# Stub reconstruction\n\nNo model call was made.\n"
        elif task["path"] == "tests/test_app.py":
            content = (
                "from app import main\n\n\n"
                "def test_main_returns_none():\n"
                "    assert main() is None\n"
            )
        else:
            content = '"""Stub Clean reconstruction."""\n\n\ndef main() -> None:\n    print("stub")\n\n\nif __name__ == "__main__":\n    main()\n'
        return json.dumps(
            {
                "files": [
                    {
                        "path": task["path"],
                        "content": content,
                        "requirement_ids": task["requirement_ids"],
                    }
                ],
                "notes": [],
            }
        )
    if "<clean_repair_request>" in prompt:
        current = json.loads(_tag(prompt, "current_generated_files"))
        return json.dumps(
            {
                "replacements": [
                    {"path": path, "content": content} for path, content in current.items()
                ],
                "rationale": "Stub mode preserves the deterministic generated content.",
            }
        )
    return "{}"


def _tag(prompt: str, name: str) -> str:
    return prompt.split(f"<{name}>", 1)[1].split(f"</{name}>", 1)[0].strip()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = run(args)
    except (ConfigError, HandoffError, WorkspaceError, ValueError) as error:
        print(f"clean configuration/input problem: {error}", file=sys.stderr)
        return 2
    except CleanBuildError as error:
        print(f"clean build failed: {error}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    print(f"\nclean status: {result.status}")
    print(f"compatibility: {result.report['compatibility_mode']}")
    print(f"validation:   {result.report['validation_level']}")
    print(f"project:      {result.project_root}")
    print(f"report:       {result.report_path}")
    return 0 if result.succeeded else 4


if __name__ == "__main__":
    raise SystemExit(main())
