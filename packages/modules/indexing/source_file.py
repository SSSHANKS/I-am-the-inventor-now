"""Regex-based structure extraction for non-Python source.

Python keeps the AST indexer. Everything else ingest classifies as code is handled
here so planning and the boundary map are not starved of half the repository.

Regex is deliberately imperfect: it surfaces identifiers, imports, and entry points
well enough to mint evidence and register aliases. Exact language parsers can replace
individual profiles later without changing the index shape.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.modules.indexing.models import evidence
from packages.modules.skills.reading import Reader

# Names too generic to treat as callees - keeps call lists usable for planning.
_NOISY_CALLEES = frozenset(
    {
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "return",
        "new",
        "typeof",
        "instanceof",
        "delete",
        "await",
        "yield",
        "case",
        "else",
        "elif",
        "when",
        "match",
        "assert",
        "sizeof",
        "static_cast",
        "dynamic_cast",
        "reinterpret_cast",
        "const_cast",
    }
)


@dataclass(frozen=True)
class _LanguageProfile:
    name: str
    imports: tuple[re.Pattern[str], ...]
    classes: tuple[re.Pattern[str], ...]
    functions: tuple[re.Pattern[str], ...]
    entrypoints: tuple[re.Pattern[str], ...]
    calls: re.Pattern[str] | None = None
    # Optional: capture group that holds method names listed on a class match.
    class_methods: Callable[[str, int, list[str]], list[str]] | None = None


def _js_class_methods(_content: str, class_line: int, lines: list[str]) -> list[str]:
    """Collect method-looking names in the brace block that starts on/after class_line."""
    return _brace_block_methods(lines, class_line, _JS_METHOD)


def _java_class_methods(_content: str, class_line: int, lines: list[str]) -> list[str]:
    return _brace_block_methods(lines, class_line, _JAVA_METHOD)


def _cpp_class_methods(_content: str, class_line: int, lines: list[str]) -> list[str]:
    return _brace_block_methods(lines, class_line, _CPP_METHOD)


_JS_METHOD = re.compile(
    r"^\s*(?:async\s+)?(?:get\s+|set\s+)?([A-Za-z_][\w]*)\s*\([^;]*\)\s*\{"
)
_JAVA_METHOD = re.compile(
    r"^\s*(?:public|protected|private|static|final|synchronized|native|abstract|\s)+"
    r"[\w.<>,\[\]?]+\s+([A-Za-z_][\w]*)\s*\("
)
_CPP_METHOD = re.compile(
    r"^\s*(?:[\w:<>\*&]+\s+)+([A-Za-z_][\w]*)\s*\([^;]*\)\s*(?:const\s*)?(?:override\s*)?\{?"
)

_JS_TS = _LanguageProfile(
    name="javascript",
    imports=(
        re.compile(
            r"""(?:import\s+(?:type\s+)?(?:[\w*\s{},]+)\s+from\s+|import\s+|export\s+.+\s+from\s+|require\s*\(\s*)['"]([^'"]+)['"]"""
        ),
    ),
    classes=(
        re.compile(
            r"(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_][\w]*)"
        ),
        re.compile(r"(?:export\s+)?(?:default\s+)?interface\s+([A-Za-z_][\w]*)"),
        re.compile(r"(?:export\s+)?(?:default\s+)?(?:const\s+)?enum\s+([A-Za-z_][\w]*)"),
    ),
    functions=(
        re.compile(
            r"(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_][\w]*)\s*\("
        ),
        re.compile(
            r"(?:export\s+)?(?:const|let|var)\s+([A-Za-z_][\w]*)\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_]\w*\s*=>)"
        ),
    ),
    entrypoints=(
        re.compile(r"require\.main\s*===\s*module"),
        re.compile(r"""import\.meta\.url\s*===?\s*['"]file:"""),
    ),
    calls=re.compile(r"\b([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*)\s*\("),
    class_methods=_js_class_methods,
)

_JAVA_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
_JAVA = _LanguageProfile(
    name="java",
    imports=(
        re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)\s*;", re.MULTILINE),
        _JAVA_PACKAGE,
    ),
    classes=(
        re.compile(
            r"(?:public|protected|private)?\s*(?:static\s+)?(?:abstract\s+)?(?:final\s+)?"
            r"class\s+([A-Za-z_][\w]*)"
        ),
        re.compile(
            r"(?:public|protected|private)?\s*(?:static\s+)?(?:sealed\s+)?"
            r"interface\s+([A-Za-z_][\w]*)"
        ),
        re.compile(r"(?:public|protected|private)?\s*(?:static\s+)?enum\s+([A-Za-z_][\w]*)"),
    ),
    functions=(
        re.compile(
            r"(?:public|protected|private)\s+(?:static\s+)?(?:final\s+)?"
            r"[\w.<>,\[\]?]+\s+([A-Za-z_][\w]*)\s*\("
        ),
    ),
    entrypoints=(re.compile(r"public\s+static\s+void\s+main\s*\("),),
    calls=re.compile(r"\b([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*)\s*\("),
    class_methods=_java_class_methods,
)

_CPP = _LanguageProfile(
    name="cpp",
    imports=(re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.MULTILINE),),
    classes=(
        re.compile(r"\b(?:class|struct)\s+([A-Za-z_][\w]*)"),
        re.compile(r"\bnamespace\s+([A-Za-z_][\w]*)"),
    ),
    functions=(
        re.compile(
            r"^(?!\s*#)(?:template\s*<[^>]+>\s*)?(?:[\w:<>\*&]+(?:\s+|\s*[*&]\s*))+([A-Za-z_][\w]*)\s*\([^;{}]*\)\s*(?:const\s*)?(?:override\s*)?(?:noexcept\s*)?\{?",
            re.MULTILINE,
        ),
    ),
    entrypoints=(re.compile(r"\b(?:int|void)\s+main\s*\("),),
    calls=re.compile(r"\b([A-Za-z_][\w]*(?:::[A-Za-z_][\w]*)*)\s*\("),
    class_methods=_cpp_class_methods,
)

PROFILE_BY_SUFFIX: dict[str, _LanguageProfile] = {
    ".js": _JS_TS,
    ".mjs": _JS_TS,
    ".cjs": _JS_TS,
    ".jsx": _JS_TS,
    ".ts": _JS_TS,
    ".tsx": _JS_TS,
    ".java": _JAVA,
    ".c": _CPP,
    ".cc": _CPP,
    ".cxx": _CPP,
    ".cpp": _CPP,
    ".h": _CPP,
    ".hpp": _CPP,
}

#: Public alias used by the notebook indexer and tests.
LanguageProfile = _LanguageProfile



def index_source_file(relative_path: str, result: dict[str, Any], source_reader: Reader) -> None:
    """Index one non-Python source file into the shared code-index shape."""
    suffix = Path(relative_path).suffix.lower()
    profile = PROFILE_BY_SUFFIX.get(suffix)
    if profile is None:
        raise ValueError(f"No language profile for {suffix or '<none>'}")

    content = source_reader.read_file(relative_path)
    lines = content.splitlines()
    _index_with_profile(relative_path, result, content, lines, profile)


def _index_with_profile(
    relative_path: str,
    result: dict[str, Any],
    content: str,
    lines: list[str],
    profile: _LanguageProfile,
) -> None:
    result["files_indexed"].append(relative_path)

    imports = _extract_imports(relative_path, content, lines, profile)
    classes = _extract_classes(relative_path, content, lines, profile)
    functions = _extract_functions(relative_path, content, lines, profile)
    entrypoints = _extract_entrypoints(relative_path, content, lines, profile)
    calls = _extract_calls(relative_path, content, lines, profile)

    result["imports"].extend(imports)
    result["classes"].extend(classes)
    result["functions"].extend(functions)
    result["entrypoints"].extend(entrypoints)
    result["calls"].extend(calls)
    result["analysis_targets"].extend(
        _build_targets(relative_path, classes, functions, entrypoints, profile.name)
    )


def index_text_as_language(
    relative_path: str,
    result: dict[str, Any],
    content: str,
    lines: list[str],
    profile: _LanguageProfile,
    *,
    record_file: bool = True,
) -> None:
    """Index an in-memory source fragment (used by notebook cells).

    Evidence still points at `relative_path` / `lines` of the real on-disk file.
    """
    if record_file and relative_path not in result["files_indexed"]:
        result["files_indexed"].append(relative_path)

    imports = _extract_imports(relative_path, content, lines, profile)
    classes = _extract_classes(relative_path, content, lines, profile)
    functions = _extract_functions(relative_path, content, lines, profile)
    entrypoints = _extract_entrypoints(relative_path, content, lines, profile)
    calls = _extract_calls(relative_path, content, lines, profile)

    result["imports"].extend(imports)
    result["classes"].extend(classes)
    result["functions"].extend(functions)
    result["entrypoints"].extend(entrypoints)
    result["calls"].extend(calls)
    result["analysis_targets"].extend(
        _build_targets(relative_path, classes, functions, entrypoints, profile.name)
    )


def profile_for_suffix(suffix: str) -> _LanguageProfile | None:
    return PROFILE_BY_SUFFIX.get(suffix.lower())


def _extract_imports(
    relative_path: str,
    content: str,
    lines: list[str],
    profile: _LanguageProfile,
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for pattern in profile.imports:
        for match in pattern.finditer(content):
            module = match.group(1).strip()
            line_start, line_end = _span_lines(content, match.start(), match.end())
            if profile.name == "cpp":
                kind = "include"
            elif match.re is _JAVA_PACKAGE:
                kind = "package"
            else:
                kind = "import"
            found.append(
                {
                    "file": relative_path,
                    "kind": kind,
                    "module": module,
                    "name": module.rsplit(".", 1)[-1].rsplit("/", 1)[-1],
                    "alias": None,
                    "line_start": line_start,
                    "line_end": line_end,
                    "evidence": evidence(relative_path, lines, line_start, line_end),
                }
            )
    return sorted(found, key=lambda item: (item["line_start"], item["module"]))


def _extract_classes(
    relative_path: str,
    content: str,
    lines: list[str],
    profile: _LanguageProfile,
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for pattern in profile.classes:
        for match in pattern.finditer(content):
            name = match.group(1)
            line_start, line_end = _span_lines(content, match.start(), match.end())
            key = (name, line_start)
            if key in seen:
                continue
            seen.add(key)
            methods: list[str] = []
            if profile.class_methods is not None:
                methods = profile.class_methods(content, line_start, lines)
            found.append(
                {
                    "file": relative_path,
                    "name": name,
                    "qualified_name": name,
                    "bases": [],
                    "decorators": [],
                    "line_start": line_start,
                    "line_end": line_end,
                    "methods": methods,
                    "evidence": evidence(relative_path, lines, line_start),
                }
            )
    return sorted(found, key=lambda item: (item["line_start"], item["name"]))


def _extract_functions(
    relative_path: str,
    content: str,
    lines: list[str],
    profile: _LanguageProfile,
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    class_names = {item["name"] for item in _extract_classes(relative_path, content, lines, profile)}

    for pattern in profile.functions:
        for match in pattern.finditer(content):
            name = match.group(1)
            if name in class_names or name in _NOISY_CALLEES:
                continue
            # Skip Java/C++ constructors named like the enclosing type when matched as methods.
            line_start, line_end = _span_lines(content, match.start(), match.end())
            key = (name, line_start)
            if key in seen:
                continue
            seen.add(key)
            found.append(
                {
                    "file": relative_path,
                    "name": name,
                    "qualified_name": name,
                    "owner": "module",
                    "kind": "function",
                    "args": [],
                    "decorators": [],
                    "line_start": line_start,
                    "line_end": line_end,
                    "evidence": evidence(relative_path, lines, line_start),
                }
            )
    return sorted(found, key=lambda item: (item["line_start"], item["qualified_name"]))


def _extract_entrypoints(
    relative_path: str,
    content: str,
    lines: list[str],
    profile: _LanguageProfile,
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for pattern in profile.entrypoints:
        for match in pattern.finditer(content):
            line_start, line_end = _span_lines(content, match.start(), match.end())
            found.append(
                {
                    "file": relative_path,
                    "kind": f"{profile.name}_entry",
                    "line_start": line_start,
                    "line_end": line_end,
                    "evidence": evidence(relative_path, lines, line_start, line_end),
                }
            )
    return sorted(found, key=lambda item: item["line_start"])


def _extract_calls(
    relative_path: str,
    content: str,
    lines: list[str],
    profile: _LanguageProfile,
) -> list[dict[str, Any]]:
    if profile.calls is None:
        return []
    found: list[dict[str, Any]] = []
    for match in profile.calls.finditer(content):
        callee = match.group(1)
        head = callee.split(".", 1)[0].split("::", 1)[0]
        if head in _NOISY_CALLEES or callee in _NOISY_CALLEES:
            continue
        line_start, line_end = _span_lines(content, match.start(), match.end())
        found.append(
            {
                "file": relative_path,
                "caller": "module",
                "callee": callee,
                "line_start": line_start,
                "line_end": line_end,
                "evidence": evidence(relative_path, lines, line_start),
            }
        )
    return found


def _build_targets(
    relative_path: str,
    classes: list[dict[str, Any]],
    functions: list[dict[str, Any]],
    entrypoints: list[dict[str, Any]],
    language: str,
) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for entrypoint in entrypoints:
        targets.append(
            {
                "file": relative_path,
                "target": relative_path,
                "target_type": "file",
                "reason": f"file contains a {language} entry point",
                "evidence": entrypoint["evidence"],
            }
        )
    for class_item in classes:
        targets.append(
            {
                "file": relative_path,
                "target": class_item["qualified_name"],
                "target_type": "class",
                "reason": f"{language} type definition found in code",
                "evidence": class_item["evidence"],
            }
        )
    for function_item in functions:
        if function_item["owner"] == "module":
            targets.append(
                {
                    "file": relative_path,
                    "target": function_item["qualified_name"],
                    "target_type": "function",
                    "reason": f"{language} function found in code",
                    "evidence": function_item["evidence"],
                }
            )
    return targets


def _brace_block_methods(
    lines: list[str],
    class_line: int,
    method_pattern: re.Pattern[str],
) -> list[str]:
    """Scan the brace-balanced block starting near `class_line` for method names."""
    start_index = max(0, class_line - 1)
    depth = 0
    started = False
    methods: list[str] = []
    for line in lines[start_index:]:
        if not started:
            if "{" in line:
                started = True
                depth += line.count("{") - line.count("}")
            continue
        match = method_pattern.match(line)
        if match and depth == 1:
            name = match.group(1)
            if name not in _NOISY_CALLEES:
                methods.append(name)
        depth += line.count("{") - line.count("}")
        if started and depth <= 0:
            break
    return methods


def _span_lines(content: str, start: int, end: int) -> tuple[int, int]:
    """Convert a character span into a 1-based inclusive line range."""
    line_start = content.count("\n", 0, start) + 1
    line_end = content.count("\n", 0, max(start, end - 1)) + 1
    return line_start, line_end
