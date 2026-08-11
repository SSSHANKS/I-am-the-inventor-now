"""The private original -> neutral map.

This is the mechanism behind CLAUDE.md section 2's hard rule: *nothing from the
original reaches the clean team*. The dirty team keeps a map from real things to
invented ones, and every artifact that crosses the boundary carries only the invented
side.

    original/path/to/module.py:39-41  ->  EV-001
    https://host/owner/original.git   ->  PROJECT-X
    GitRepoManifest                   ->  Component A

The map itself is the most sensitive artifact in the pipeline: anyone holding it can
undo the neutralisation. It is written under `_private/` and must never be handed
across, quoted in a spec, or pasted into a prompt that produces a crossing artifact.

Aliases are assigned in first-seen order, so the same run always produces the same
IDs and two runs over the same snapshot stay comparable.
"""

import string
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

DEFAULT_PROJECT_NAME = "PROJECT-X"

_KIND_NOUNS = {
    "component": "Component",
    "class": "Component",
    "function": "Operation",
    "method": "Operation",
    "module": "Module",
}

#: Identifiers too short or too universal to be worth hiding. Registering these would
#: rewrite ordinary English ("run", "get") and bury real findings in noise. They are
#: language furniture, not expression belonging to the original.
UNIVERSAL_IDENTIFIERS = frozenset(
    {
        "main",
        "init",
        "self",
        "args",
        "kwargs",
        "test",
        "data",
        "value",
        "name",
        "path",
        "file",
        "type",
        "list",
        "dict",
        "json",
        "text",
        "item",
        "items",
        "index",
        "result",
        "error",
        "config",
        "setup",
        "print",
        "open",
        "read",
        "write",
        "close",
        "load",
        "save",
        "next",
        "size",
        "line",
        "time",
        "date",
    }
)

MIN_IDENTIFIER_LENGTH = 4


def is_registrable_identifier(name: object) -> bool:
    """Whether an identifier is distinctive enough to hide and to hunt for."""
    return (
        isinstance(name, str)
        and len(name.strip()) >= MIN_IDENTIFIER_LENGTH
        and not name.startswith("__")
        and name.strip().lower() not in UNIVERSAL_IDENTIFIERS
    )


def _component_label(position: int, noun: str = "Component") -> str:
    """A, B, ... Z, AA, AB, ... - spreadsheet-column style, unbounded."""
    letters = string.ascii_uppercase
    label = ""
    position += 1
    while position > 0:
        position, remainder = divmod(position - 1, len(letters))
        label = letters[remainder] + label
    return f"{noun} {label}"


@dataclass
class AliasMap:
    """Assigns and remembers neutral names for original things."""

    project_name: str = DEFAULT_PROJECT_NAME
    _evidence: dict[tuple[str, int | None, int | None], str] = field(default_factory=dict)
    _components: dict[str, str] = field(default_factory=dict)
    _repo_urls: set[str] = field(default_factory=set)
    #: alias -> location, kept alongside the forward map so a plan citing dozens of
    #: ids does not rescan the whole table per lookup.
    _by_alias: dict[str, tuple[str, int | None, int | None]] = field(default_factory=dict)

    # --- assignment ----------------------------------------------------------

    def evidence_id(
        self,
        file: str,
        line_start: int | None = None,
        line_end: int | None = None,
    ) -> str:
        """Return the opaque ID standing in for one original location."""
        key = (file, line_start, line_end)
        if key not in self._evidence:
            alias = f"EV-{len(self._evidence) + 1:03d}"
            self._evidence[key] = alias
            self._by_alias[alias] = key
        return self._evidence[key]

    def component_alias(self, original_name: str, kind: str = "component") -> str:
        """Return the neutral label standing in for one original identifier.

        `kind` only changes the noun ("Component A", "Operation B", "Module C") so a
        scrubbed sentence still reads sensibly - "its Operation D method" rather than
        "its Component D method".
        """
        if original_name not in self._components:
            noun = _KIND_NOUNS.get(kind, "Component")
            self._components[original_name] = _component_label(len(self._components), noun=noun)
        return self._components[original_name]

    def register_identifiers(self, names: Iterable[tuple[str, str]]) -> int:
        """Teach the map a batch of (identifier, kind) pairs. Returns how many are new.

        Registration is what makes an identifier *visible* - both to the scrubber that
        removes it from prose and to the scanner that checks it did not survive. An
        unregistered identifier is invisible to both.
        """
        before = len(self._components)
        for name, kind in names:
            if is_registrable_identifier(name):
                self.component_alias(name, kind=kind)
        return len(self._components) - before

    @property
    def identifiers(self) -> list[str]:
        """Every original identifier this map knows, longest first.

        Longest-first matters for substitution: replacing `Calculador` before
        `Calculadora` would leave a stray "a" behind.
        """
        return sorted(self._components, key=len, reverse=True)

    def alias_for(self, original_name: str) -> str | None:
        return self._components.get(original_name)

    def location_for(self, evidence_id: str) -> tuple[str, int | None, int | None] | None:
        """Resolve an opaque evidence id back to the original location.

        This is the dirty-side half of the boundary: a plan carries only `EV-014`, and the
        controller uses this to recover the file and line range so it can pre-read the
        source an executing agent needs. It must never be reachable from anything that
        crosses - it reverses the whole neutralisation.
        """
        return self._by_alias.get(evidence_id)

    def register_project(self, repo_url: str) -> str:
        """Record the original repository and return the invented project name."""
        self._repo_urls.add(repo_url)
        return self.project_name

    # --- inspection ----------------------------------------------------------

    @property
    def originals(self) -> list[str]:
        """Every original string this map can currently reveal.

        Used by the leak scanner to check whether a neutral artifact accidentally
        still mentions something it should not.
        """
        found = {file for file, _, _ in self._evidence}
        found |= set(self._components)
        found |= self._repo_urls
        return sorted(found)

    def __len__(self) -> int:
        return len(self._evidence) + len(self._components)

    # --- persistence (DIRTY SIDE ONLY) --------------------------------------

    def to_private_dict(self) -> dict[str, Any]:
        """Serialise for `_private/` storage. Never include this in a crossing artifact."""
        return {
            "_warning": (
                "DIRTY SIDE ONLY. This map reverses the clean-room neutralisation. "
                "It must never be given to the clean team (CLAUDE.md section 2)."
            ),
            "project_name": self.project_name,
            "repo_urls": sorted(self._repo_urls),
            "evidence": [
                {
                    "id": alias,
                    "file": file,
                    "line_start": line_start,
                    "line_end": line_end,
                }
                for (file, line_start, line_end), alias in self._evidence.items()
            ],
            "components": [
                {"alias": alias, "original": original}
                for original, alias in self._components.items()
            ],
        }

    @classmethod
    def from_private_dict(cls, payload: dict[str, Any]) -> "AliasMap":
        alias_map = cls(project_name=payload.get("project_name", DEFAULT_PROJECT_NAME))
        for item in payload.get("evidence", []):
            key = (item["file"], item.get("line_start"), item.get("line_end"))
            alias_map._evidence[key] = item["id"]
            alias_map._by_alias[item["id"]] = key
        for item in payload.get("components", []):
            alias_map._components[item["original"]] = item["alias"]
        alias_map._repo_urls = set(payload.get("repo_urls", []))
        return alias_map
