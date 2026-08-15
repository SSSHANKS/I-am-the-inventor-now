"""Turn dirty-side artifacts into the neutral versions that may cross.

Two jobs:

1. Build the neutral artifacts themselves - a manifest describing the project *to be
   built* rather than the one that was read, and evidence references reduced to
   opaque IDs.
2. Notice when something original survived anyway, and mark it `BORDER-REVIEW`.

Job 2 is deliberately **not** an enforcement gate. Nothing here rejects, blocks, or
raises on a suspected leak - it annotates. Enforcement belongs to Border, which is a
later step (CLAUDE.md section 1, decided in Q3).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from packages.modules.boundary.alias_map import AliasMap

BORDER_REVIEW = "BORDER-REVIEW"

# Shapes that betray an original even when no name is recognisable: a path with a
# source extension, or a bare file:line reference.
_PATH_LIKE = re.compile(
    r"\b[\w./\\-]+\.(?:py|js|mjs|cjs|jsx|ts|tsx|java|c|cc|cxx|cpp|h|hpp|ipynb|md|rst|txt|json|toml|yaml|yml)\b",
    re.IGNORECASE,
)
_FILE_LINE = re.compile(r"\b[\w./\\-]+:\d+(?:-\d+)?\b")
_URL = re.compile(r"https?://\S+")

# --- content-shape rules ------------------------------------------------------
# The rules above look for originals we know about. These look for the *shape* of
# copied content, so they fire on an original nobody registered - a command line, a
# sentence lifted from a README, a phrase in the source project's language. Targeting
# shape rather than bytes is deliberate: a Portuguese branch name stripped of its
# accents defeats any character-set test.

#: Executables whose name is not also an ordinary English word. One of these followed
#: by an argument is strong evidence of a copied command line.
_COMMAND_TOOLS = (
    r"git|pip3?|python3?|npm|npx|yarn|pnpm|cargo|docker(?:-compose)?|kubectl|poetry|"
    r"pipenv|conda|apt(?:-get)?|brew|chmod|chown|mkdir|virtualenv|venv|pytest|tox|ruff|"
    r"mypy|gradle|mvn|dotnet|deno|systemctl|curl|wget|unzip|scp|ssh|rmdir|touch"
)
#: Shell commands that are also everyday English verbs - "make the request", "go to the
#: settings page", "source of truth". A bare one of these is prose; one carrying a flag
#: is probably a command, but only probably, so it is reported as UNCERTAIN.
_COMMAND_AMBIGUOUS = r"make|go|source|node|service|cd|rm|cp|mv|tar|sh|bash|zsh|set|export"
_COMMAND_ARG = r"(?:[-+]{1,2}[^\W\d][\w-]*|[\w./~@][\w./~@:+=-]*)"
_COMMAND_STRONG = re.compile(rf"\b(?:{_COMMAND_TOOLS})\b(?:\s+{_COMMAND_ARG})+")
_COMMAND_WEAK = re.compile(rf"\b(?:{_COMMAND_AMBIGUOUS})\b(?:\s+-{{1,2}}[^\W\d][\w-]*)+")

#: A tool name followed by words is not yet a command - "standard python execution
#: process arguments" is prose about Python, and flagging it buries the real signal.
#: Something in the match has to look like an actual argument: a flag, a token carrying
#: path or assignment punctuation, or a recognised subcommand.
_COMMAND_SUBCOMMANDS = frozenset(
    [
        "clone",
        "checkout",
        "commit",
        "push",
        "pull",
        "fetch",
        "merge",
        "rebase",
        "init",
        "add",
        "status",
        "branch",
        "tag",
        "install",
        "uninstall",
        "update",
        "upgrade",
        "run",
        "build",
        "start",
        "stop",
        "restart",
        "test",
        "exec",
        "logs",
        "up",
        "down",
        "serve",
        "migrate",
        "freeze",
        "list",
        "search",
        "publish",
        "new",
        "create",
        "remove",
        "login",
    ]
)
_COMMAND_EVIDENCE = re.compile(r"(?:^|\s)[-+]{1,2}[^\W\d]|[\w][./=][\w]|~/")
#: Tools whose names double as ordinary prose subjects. Without argument evidence these
#: are dropped; the rest stay as UNCERTAIN, because a bare "git something" is odd enough
#: to remain a question for Border.
_PROSE_TOOLS = frozenset({"python", "python3", "go", "node", "make", "source", "venv", "touch"})

#: Punctuation a model emits routinely. Non-ASCII on its own would flag every em-dash
#: and curly quote in a generated document, which buries the real signal.
_TYPOGRAPHIC = "‘’“”‚„–—… •·→×÷≤≥±°€£™®©"
_NON_ASCII_TOKEN = re.compile(r"\S*[^\x00-\x7F]\S*")

#: High-frequency function words of the languages project documentation is most often
#: written in (pt, es, fr, de, it, pl), in their unaccented forms. Function words are
#: used because they survive translation of the content around them and do not depend
#: on the subject matter. Deliberately excludes forms that are also English words.
_FOREIGN_STOPWORDS = frozenset(
    [
        "sua",
        "seu",
        "seus",
        "suas",
        "nao",
        "voce",
        "voces",
        "crie",
        "faca",
        "arquivo",
        "arquivos",
        "projeto",
        "usuario",
        "entao",
        "tambem",
        "apenas",
        "atraves",
        "partir",
        "sendo",
        "pelas",
        "pelos",
        "aqui",
        "agora",
        "para",
        "com",
        "que",
        "uma",
        "este",
        "esta",
        "esse",
        "essa",
        "pero",
        "desde",
        "hacia",
        "cuales",
        "aunque",
        "usted",
        "ustedes",
        "archivo",
        "proyecto",
        "usuario",
        "entonces",
        "tambien",
        "solamente",
        "avec",
        "pour",
        "dans",
        "votre",
        "vous",
        "cette",
        "elles",
        "ceux",
        "fichier",
        "projet",
        "utilisateur",
        "ensuite",
        "egalement",
        "seulement",
        "depuis",
        "und",
        "oder",
        "nicht",
        "eine",
        "einen",
        "einem",
        "eine",
        "mit",
        "fuer",
        "sich",
        "auch",
        "dann",
        "datei",
        "projekt",
        "benutzer",
        "nur",
        "ausserdem",
        "zwischen",
        "della",
        "delle",
        "degli",
        "sono",
        "anche",
        "quindi",
        "soltanto",
        "utente",
        "progetto",
        "nie",
        "jest",
        "oraz",
        "tego",
        "dla",
        "jak",
        "sie",
        "przez",
        "wszystkie",
        "plik",
        "projekt",
        "uzytkownik",
        "wtedy",
        "takze",
        "tylko",
        "ktore",
    ]
)

#: Markers real enough to corroborate a foreign phrase, but too collision-prone to
#: raise one alone: "com" is in every .com domain, "mit" lower-cases out of "MIT
#: license", "para" prefixes English words. They count toward a run, never start one.
_FOREIGN_WEAK = frozenset({"com", "mit", "para", "set", "son", "nur", "dans"})

#: How many consecutive words shared with a source document count as lifted prose.
#: Six is long enough that ordinary technical phrasing rarely collides, short enough
#: to catch a lifted clause. (Decided with the user during the layer-2 gate.)
_LIFT_WINDOW = 6
#: Foreign stopwords needed inside one window before the run is called foreign rather
#: than a coincidence. One hit alone is reported, but only as UNCERTAIN.
_FOREIGN_RUN = 5


def neutral_manifest(
    manifest: Any,
    alias_map: AliasMap,
    summary: str | None = None,
) -> dict[str, Any]:
    """Describe the project to be built, with nothing identifying the original.

    Counts survive on purpose: "there are 12 code units to reconstruct" tells the
    clean team about scale, which is behaviour-adjacent, and reveals no identifier.
    """
    project_name = alias_map.register_project(manifest.repo_url)
    findings = _review_notes(summary or "")
    return {
        "project_name": project_name,
        "summary": summary
        or (
            f"{project_name} is to be implemented from behavioural specification "
            f"alone. No source, naming, or layout from any prior implementation is "
            f"available or implied."
        ),
        "documentation_unit_count": len(manifest.documentation),
        "code_unit_count": len(manifest.code),
        "border_review": findings,
    }


def neutral_evidence_reference(
    evidence: dict[str, Any] | None,
    alias_map: AliasMap,
) -> str | None:
    """Reduce one dirty-side evidence object to an opaque ID.

    Returns None when there is nothing to point at, so a caller can write
    "Evidence: not available" rather than invent a reference.
    """
    if not isinstance(evidence, dict):
        return None
    file = evidence.get("file")
    if not isinstance(file, str) or not file.strip():
        return None
    return alias_map.evidence_id(
        file,
        evidence.get("line_start"),
        evidence.get("line_end"),
    )


#: Keys whose values are original locations or original text, and which therefore
#: never survive into a prompt that produces a crossing artifact.
_LOCATION_KEYS = frozenset(
    {"file", "line_start", "line_end", "excerpt", "repo_url", "commit_hash", "branch"}
)
_DROP_KEYS = frozenset({"documentation_files_read", "files_indexed", "files_skipped"})


def register_code_identifiers(code_index: dict[str, Any], alias_map: AliasMap) -> int:
    """Teach `alias_map` every identifier the code index found. Returns how many are new.

    Call this once, dirty-side, as soon as the index exists. Everything downstream
    depends on it: `neutral_report` can only scrub identifiers it knows, and
    `find_residual_originals` can only flag identifiers it knows. Skipping this leaves
    both blind, and a clean scanner result then means nothing.
    """
    pairs: list[tuple[str, str]] = []

    for item in code_index.get("classes", []):
        name = item.get("name")
        pairs.append((name, "class"))
        for method in item.get("methods", []) or []:
            pairs.append((method, "method"))

    for item in code_index.get("functions", []):
        pairs.append((item.get("name"), "function"))
        qualified = item.get("qualified_name")
        if qualified and qualified != item.get("name"):
            pairs.append((qualified, "function"))

    for item in code_index.get("imports", []):
        module = item.get("module")
        if module and not module.startswith("."):
            pairs.append((module, "module"))

    return alias_map.register_identifiers((n, k) for n, k in pairs if isinstance(n, str))


#: Index collections worth citing in a plan, and the neutral noun describing each. The
#: label is what a planner sees instead of a path, so it has to say enough to choose by.
_CODE_CITABLE = {
    "analysis_targets": "an analysis target",
    "entrypoints": "a program entry point",
    "classes": "a component definition",
    "functions": "an operation definition",
    "configs": "a configuration document",
}
_DOC_CITABLE = {
    "sections": "a documentation section",
    "commands": "a documented command",
    "code_blocks": "a documented usage example",
    "headings": "a documentation heading",
}


def mint_evidence_ids(alias_map: AliasMap, *indexes: dict[str, Any]) -> int:
    """Give every indexed item an opaque id, before anything plans against it.

    Timing is the whole point. Evidence ids used to be minted during spec-stage
    neutralisation, which is *after* every planning stage - so a planner had nothing to
    cite and had to name files instead. Minting when the boundary opens means the plan
    can be neutral and still point at something real.

    Returns the number of ids in the map afterwards.
    """
    for index in indexes:
        for collection in (*_CODE_CITABLE, *_DOC_CITABLE, "imports", "calls", "references"):
            for item in index.get(collection) or []:
                evidence = item.get("evidence") if isinstance(item, dict) else None
                if isinstance(evidence, dict) and isinstance(evidence.get("file"), str):
                    alias_map.evidence_id(
                        evidence["file"], evidence.get("line_start"), evidence.get("line_end")
                    )
    return len(alias_map._by_alias)


def build_evidence_catalogue(
    alias_map: AliasMap,
    code_index: dict[str, Any] | None = None,
    doc_index: dict[str, Any] | None = None,
    limit_per_collection: int = 40,
) -> dict[str, Any]:
    """The catalogue as a storable crossing artifact: entries plus its review notes.

    The catalogue is what makes a plan neutral, and for a long time nothing could audit
    it: the filter that admits a label was silent about rejections, and the catalogue was
    never written to disk. A label carrying three verbatim commands therefore reached the
    planner with no record anywhere. Persisting it, notes included, closes that.
    """
    entries: list[dict[str, str]] = []
    notes: list[str] = []
    seen: set[str] = set()

    def add(index: dict[str, Any], collections: dict[str, str]) -> None:
        for collection, noun in collections.items():
            for item in (index.get(collection) or [])[:limit_per_collection]:
                evidence = item.get("evidence") if isinstance(item, dict) else None
                if not isinstance(evidence, dict) or not isinstance(evidence.get("file"), str):
                    continue
                reference = neutral_evidence_reference(evidence, alias_map)
                if not reference or reference in seen:
                    continue
                seen.add(reference)
                entries.append(
                    {
                        "evidence_id": reference,
                        "kind": noun,
                        "about": _neutral_about(item, noun, alias_map, reference, notes),
                    }
                )

    if code_index:
        add(code_index, _CODE_CITABLE)
    if doc_index:
        add(doc_index, _DOC_CITABLE)
    return {"entries": entries, "border_review": notes}


def evidence_catalogue(
    alias_map: AliasMap,
    code_index: dict[str, Any] | None = None,
    doc_index: dict[str, Any] | None = None,
    limit_per_collection: int = 40,
) -> list[dict[str, str]]:
    """The neutral menu a planner chooses from: opaque id plus what it is.

    An id alone is unusable - a planner cannot decide that `EV-014` is the crux without
    knowing what sits there. This gives it a description scrubbed of original names, so
    the planner can exercise judgement without ever seeing an identifier.

    Entries only. Use `build_evidence_catalogue` when the review notes matter too.
    """
    return build_evidence_catalogue(alias_map, code_index, doc_index, limit_per_collection)[
        "entries"
    ]


def _neutral_about(
    item: dict[str, Any],
    noun: str,
    alias_map: AliasMap,
    evidence_id: str = "",
    notes: list[str] | None = None,
) -> str:
    """A short, scrubbed description of what an indexed item covers.

    Documentation titles are author prose and can name anything, so everything here goes
    through the scrubber before it is offered to a planner. A candidate that still looks
    copied is dropped in favour of the generic noun, and the substitution is recorded.
    """
    for key in ("title", "reason", "command", "language"):
        raw = item.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        candidate = scrub_identifiers(raw.strip()[:120], alias_map)
        rejected_for = _label_rejection(candidate, alias_map)
        if rejected_for is None:
            return candidate
        if notes is not None:
            notes.append(
                f"{BORDER_REVIEW}: the indexed {key!r} field of {evidence_id or 'an entry'} "
                f"matched {rejected_for}, so the catalogue shows the generic kind instead. "
                f"The rejected text is withheld - this artifact crosses."
            )
    return noun


def _label_rejection(text: str, alias_map: AliasMap) -> str | None:
    """Why a candidate description may not be put in front of a planner, or None.

    Two families, same as the specification scanner: originals the map knows, and the
    shape of copied content it does not. The second family is what a git command needed -
    it is no identifier, path or URL, so the first family admitted it.

    Unlike the specification scanner this is *enforcing*, and the polarity inverts with
    it. There, acting on an UNCERTAIN reading could suppress a real leak, so doubt must
    never drop a flag. Here rejecting is the safe move - the generic noun is always
    available and costs one description - so doubt rejects (decided: Q27).
    """
    if find_residual_originals(text, alias_map):
        return "a known original"
    findings = scan_content_leaks(text)
    if findings:
        return " and ".join(sorted({finding.kind for finding in findings}))
    return None


def _is_safe_label(text: str, alias_map: AliasMap) -> bool:
    """Whether a candidate description is clean enough to put in front of a planner."""
    return _label_rejection(text, alias_map) is None


def scrub_identifiers(text: str, alias_map: AliasMap) -> str:
    """Replace every known original identifier with its neutral alias.

    This is the part that actually holds the line. Instructing a model not to write
    original names is unreliable while the prompt is full of original names to copy -
    a real run put 24 of them in front of the specification agent, and two came out the
    other side. Removing them from the input removes the opportunity.

    Longest identifier first, so a name that contains another is replaced whole.
    """
    for original in alias_map.identifiers:
        alias = alias_map.alias_for(original)
        if not alias:
            continue
        text = re.sub(rf"\b{re.escape(original)}\b", alias, text)
    return text


def neutral_report(payload: Any, alias_map: AliasMap) -> Any:
    """Rewrite a dirty-side report into one safe to put in a crossing prompt.

    Every `evidence` object collapses to its opaque ID, and every original location or
    excerpt is dropped. What survives is the finding itself - the label and the prose -
    which is the behaviour we actually want to carry across.

    This matters more than the prompt wording: an instruction not to copy paths is
    useless if the prompt is full of paths to copy.
    """
    if isinstance(payload, dict):
        result: dict[str, Any] = {}
        for key, value in payload.items():
            if key in _DROP_KEYS:
                continue
            if key == "evidence":
                reference = neutral_evidence_reference(value, alias_map)
                if reference:
                    result["evidence_id"] = reference
                continue
            if key in _LOCATION_KEYS:
                continue
            result[key] = neutral_report(value, alias_map)
        return result
    if isinstance(payload, list):
        return [neutral_report(item, alias_map) for item in payload]
    if isinstance(payload, str):
        # Findings are prose, and prose is where identifiers hide. Structural
        # neutralisation alone let class and method names straight through.
        return scrub_identifiers(payload, alias_map)
    return payload


NAME_SHAPED = "NAME-SHAPED"
DESCRIPTIVE = "DESCRIPTIVE"
UNCERTAIN = "UNCERTAIN"
#: Content that matches a copied-form rule outright: a real command line, a run of
#: source-language text, a clause shared word-for-word with a source document.
VERBATIM = "VERBATIM"

#: Kinds of finding, and the readings that make sense for each. An identifier is judged
#: by *how it is used*; copied content is judged by *how sure the shape rule is*.
COMMAND = "command-shaped text"
FOREIGN = "source-language text"
LIFTED = "verbatim source-document prose"
_CONTENT_KINDS = frozenset({COMMAND, FOREIGN, LIFTED})
_IDENTIFIER_READINGS = (NAME_SHAPED, DESCRIPTIVE, UNCERTAIN)
_CONTENT_READINGS = (VERBATIM, UNCERTAIN)
#: Examples print the alarming readings first, whatever order the counts are summarised in.
_EXAMPLE_ORDER = (VERBATIM, NAME_SHAPED, UNCERTAIN, DESCRIPTIVE)

#: Nouns that turn a preceding word into a name reference: "the start method".
_NAME_NOUNS = (
    r"method|function|class|module|attribute|property|routine|constructor|callable|"
    r"variable|parameter|field|symbol|identifier"
)
#: Verbs that introduce a name: "calls calculation", "named start".
_NAME_VERBS = r"calls?|invokes?|invoking|named|called|defined|declares?|implements?|references?"

_CONTEXT_WINDOW = 48
#: How many example phrases to print per category. Counts are always complete; this
#: only bounds how much prose the report prints.
_MAX_EXAMPLES = 3


@dataclass(frozen=True)
class Occurrence:
    """One appearance of a flagged word, with an advisory reading of how it is used."""

    phrase: str
    classification: str


@dataclass(frozen=True)
class ResidualFinding:
    """One flagged original and everything known about how it appears.

    Carries every occurrence, so Border can see that a word appearing 29 times as a
    plain noun is a different proposition from one appearing once as a method name.
    """

    original: str
    alias: str | None
    kind: str
    occurrences: tuple[Occurrence, ...]

    def count(self, classification: str) -> int:
        return sum(1 for o in self.occurrences if o.classification == classification)

    @property
    def readings(self) -> tuple[str, ...]:
        """Which readings apply to this kind of finding, in reporting order."""
        return _CONTENT_READINGS if self.kind in _CONTENT_KINDS else _IDENTIFIER_READINGS

    @property
    def summary(self) -> str:
        total = len(self.occurrences)
        detail = ", ".join(f"{self.count(r)} {r.lower()}" for r in self.readings)
        if self.kind == "identifier":
            target = f"original identifier present -> {self.original!r}"
            if self.alias:
                target += f" (should be {self.alias!r})"
        elif self.kind in _CONTENT_KINDS:
            target = f"{self.kind} present -> {self.original!r}"
        else:
            target = f"{self.kind} survived neutralisation -> {self.original!r}"
        return f"{BORDER_REVIEW}: {target} - {total} occurrence(s): {detail} [advisory]"


def classify_occurrence(before: str, after: str) -> str:
    """How a word appears to be used. Advisory, and deliberately cautious.

    Only strong syntactic evidence earns a verdict. `DESCRIPTIVE` needs positive
    evidence of ordinary prose, never merely the absence of a name marker - guessing
    "descriptive" is how a real leak gets waved through, so anything unclear is
    UNCERTAIN and stays a question for Border.
    """
    name_markers = (
        # a call, or a dotted path: calculation(), copy.deepcopy, handle.start
        after.startswith("("),
        before.endswith("."),
        bool(re.match(r"^\.\w", after)),
        # rendered as code
        before.endswith("`") or after.startswith("`"),
        # "... start method", "... calculation function". The noun that follows makes
        # this a name reference whatever precedes it - "the", "its", "a second".
        bool(re.match(rf"(?i)^\s+({_NAME_NOUNS})\b", after)),
        # "calls calculation", "named start", "def start"
        bool(re.search(rf"(?i)\b({_NAME_VERBS})\s+$", before)),
        bool(re.search(r"(?i)\b(def|class)\s+$", before)),
    )
    if any(name_markers):
        return NAME_SHAPED

    # Ordinary prose: an everyday word follows, and it is not one that would turn this
    # into a name reference.
    following = re.match(r"^\s+([a-z]{2,})", after)
    if following and not re.match(rf"(?i)^({_NAME_NOUNS})$", following.group(1)):
        return DESCRIPTIVE
    if re.search(r"(?i)\b(a|an|the|deep|shallow|standard|successful|each|every)\s+$", before) and (
        re.match(r"^\s*[.,;:)]", after) or not after.strip()
    ):
        return DESCRIPTIVE

    return UNCERTAIN


def scan_residual_originals(text: str, alias_map: AliasMap) -> list[ResidualFinding]:
    """Find everything original-looking in `text`, with per-occurrence context.

    Reports; never decides. A classification is a hint attached to a flag, not a reason
    to drop one - adjudicating these is Border's job (CLAUDE.md section 1), and Border
    does not exist yet.
    """
    findings: list[ResidualFinding] = []
    identifiers = set(alias_map.identifiers)

    for original in alias_map.originals:
        if not original:
            continue
        pattern = rf"\b{re.escape(original)}\b" if original in identifiers else re.escape(original)
        occurrences = _occurrences(text, pattern)
        if occurrences:
            findings.append(
                ResidualFinding(
                    original=original,
                    alias=alias_map.alias_for(original),
                    kind="identifier" if original in identifiers else "original reference",
                    occurrences=occurrences,
                )
            )

    for shape, label in (
        (_URL, "URL"),
        (_FILE_LINE, "file:line reference"),
        (_PATH_LIKE, "source-like path"),
    ):
        for match in dict.fromkeys(shape.findall(text)):
            if any(match == f.original for f in findings):
                continue
            findings.append(
                ResidualFinding(
                    original=match,
                    alias=None,
                    kind=label,
                    occurrences=_occurrences(text, re.escape(match)),
                )
            )

    return findings


def scan_content_leaks(
    text: str,
    source_texts: Sequence[str] = (),
) -> list[ResidualFinding]:
    """Find copied *content* - commands, source-language text, lifted doc prose.

    Deliberately a separate entry point from `scan_residual_originals`, which looks for
    originals the map already knows. These rules need no map at all: they fire on the
    shape of the thing, which is the only way to catch an original nobody registered.

    Kept separate so it cannot leak into the planner's neutrality gate by accident. The
    gate rejects a plan when scrubbing cannot fix it, and scrubbing can never fix a
    command or a foreign phrase - a plan tripping these would fail every round instead
    of being repaired. Advisory on the specification only (decided with the user).

    Reports; never decides, never suppresses. Border adjudicates (CLAUDE.md section 1).
    """
    findings: list[ResidualFinding] = []
    claimed: dict[str, list[str]] = {}

    def add(kind: str, snippet: str, classification: str) -> None:
        snippet = snippet.strip()
        # Collapse fragments of a flag this rule already raised, never across rules: a
        # command containing source-language text is two separate things Border needs
        # to know, and dropping either would be suppression.
        seen = claimed.setdefault(kind, [])
        if not snippet or any(snippet in previous for previous in seen):
            return
        seen.append(snippet)
        findings.append(
            ResidualFinding(
                original=snippet,
                alias=None,
                kind=kind,
                occurrences=_occurrences(text, re.escape(snippet), classification),
            )
        )

    for match in _COMMAND_STRONG.finditer(text):
        phrase = match.group(0)
        words = phrase.lower().split()
        if _COMMAND_EVIDENCE.search(phrase) or _COMMAND_SUBCOMMANDS.intersection(words[1:]):
            add(COMMAND, phrase, VERBATIM)
        elif words[0] not in _PROSE_TOOLS:
            add(COMMAND, phrase, UNCERTAIN)
    for match in _COMMAND_WEAK.finditer(text):
        add(COMMAND, match.group(0), UNCERTAIN)

    for match in _NON_ASCII_TOKEN.finditer(text):
        token = match.group(0)
        if any(ord(c) > 127 and c not in _TYPOGRAPHIC for c in token):
            add(FOREIGN, token, VERBATIM)

    for snippet, classification in _foreign_phrases(text):
        add(FOREIGN, snippet, classification)

    for snippet in _lifted_prose(text, source_texts):
        add(LIFTED, snippet, VERBATIM)

    return findings


def _words(text: str) -> list[tuple[str, int, int]]:
    """Word tokens with their offsets, lowercased and stripped of surrounding markup."""
    return [(m.group(0).lower(), m.start(), m.end()) for m in re.finditer(r"[^\W_]+", text)]


def _foreign_phrases(text: str) -> list[tuple[str, str]]:
    """Runs of text carrying source-language function words.

    Accent-free by design: `Crie sua branch` is caught by "crie"+"sua", not by any
    character-set test, which is the case that motivated this rule. Two hits close
    together is confident; a lone hit is reported as UNCERTAIN rather than dropped,
    because deciding it is Border's job.
    """
    markers = _FOREIGN_STOPWORDS | _FOREIGN_WEAK
    tokens = _words(text)
    hits = [i for i, (word, _, _) in enumerate(tokens) if word in markers]
    if not hits:
        return []

    found: list[tuple[str, str]] = []
    used: set[int] = set()
    for position, index in enumerate(hits):
        near = [j for j in hits[position + 1 :] if j - index <= _FOREIGN_RUN]
        if near:
            start, end = index, near[-1]
            if any(i in used for i in range(start, end + 1)):
                continue
            used.update(range(start, end + 1))
            # Widen by a word either side so the phrase reads as a phrase.
            left = tokens[max(0, start - 1)][1]
            right = tokens[min(len(tokens) - 1, end + 1)][2]
            found.append((text[left:right], VERBATIM))

    for index in hits:
        if index in used or tokens[index][0] in _FOREIGN_WEAK:
            continue
        left = tokens[max(0, index - 1)][1]
        right = tokens[min(len(tokens) - 1, index + 1)][2]
        found.append((text[left:right], UNCERTAIN))
    return found


def _lifted_prose(text: str, source_texts: Sequence[str]) -> list[str]:
    """Word runs shared verbatim with a source document.

    Catches prose an agent copied instead of describing, including translated sentences
    that kept a fragment intact. The window is long enough that ordinary technical
    phrasing rarely collides.
    """
    if not source_texts:
        return []

    source_ngrams: set[tuple[str, ...]] = set()
    for source in source_texts:
        source_words = [w for w, _, _ in _words(source)]
        for i in range(len(source_words) - _LIFT_WINDOW + 1):
            source_ngrams.add(tuple(source_words[i : i + _LIFT_WINDOW]))
    if not source_ngrams:
        return []

    tokens = _words(text)
    matched: set[int] = set()
    for i in range(len(tokens) - _LIFT_WINDOW + 1):
        if tuple(w for w, _, _ in tokens[i : i + _LIFT_WINDOW]) in source_ngrams:
            matched.update(range(i, i + _LIFT_WINDOW))

    runs: list[str] = []
    start: int | None = None
    for i in range(len(tokens) + 1):
        if i in matched and start is None:
            start = i
        elif i not in matched and start is not None:
            runs.append(text[tokens[start][1] : tokens[i - 1][2]])
            start = None
    return runs


def _occurrences(
    text: str,
    pattern: str,
    classification: str | None = None,
) -> tuple[Occurrence, ...]:
    found: list[Occurrence] = []
    for match in re.finditer(pattern, text):
        before = text[max(0, match.start() - _CONTEXT_WINDOW) : match.start()]
        after = text[match.end() : match.end() + _CONTEXT_WINDOW]
        phrase = " ".join(f"{before}{match.group(0)}{after}".split())
        found.append(
            Occurrence(
                phrase=phrase,
                classification=classification or classify_occurrence(before, after),
            )
        )
    return tuple(found)


def find_residual_originals(text: str, alias_map: AliasMap) -> list[str]:
    """Report anything in `text` that still looks like it came from the original.

    Checks the strings this map knows are original, plus generic path / file:line /
    URL shapes for originals it never saw. Reporting only - never raises.
    """
    return [finding.summary for finding in scan_residual_originals(text, alias_map)]


def annotate_border_review(
    markdown: str,
    findings: list[str] | list[ResidualFinding],
) -> str:
    """Append a visible BORDER-REVIEW block so a leak cannot pass unnoticed.

    Given structured findings, each flag also carries the phrases it was found in, so
    Border can judge from evidence rather than from a bare word. The artifact is still
    produced either way, and every flag is printed - a DESCRIPTIVE reading is a hint,
    never a dismissal.
    """
    if not findings:
        return markdown

    lines = [
        markdown.rstrip(),
        "",
        f"## {BORDER_REVIEW}",
        "",
        "Automated scanning flagged the following as possibly originating from the "
        "analysed source. This is advisory: no enforcement gate runs in Step 1, and the "
        "NAME-SHAPED / DESCRIPTIVE / UNCERTAIN readings are heuristic hints for review, "
        "not decisions. Nothing here has been suppressed.",
        "",
    ]

    for finding in findings:
        if isinstance(finding, str):
            lines.append(f"- {finding}")
            continue
        lines.append(f"- {finding.summary}")
        lines.extend(_example_lines(finding))

    return "\n".join(lines) + "\n"


def _example_lines(finding: ResidualFinding) -> list[str]:
    """Sample phrases per category, saying plainly when the sample is partial."""
    lines: list[str] = []
    for classification in _EXAMPLE_ORDER:
        matching = [o for o in finding.occurrences if o.classification == classification]
        if not matching:
            continue
        shown = matching[:_MAX_EXAMPLES]
        for occurrence in shown:
            lines.append(f"    - [{classification}] ...{occurrence.phrase}...")
        if len(matching) > len(shown):
            lines.append(
                f"    - [{classification}] ... and {len(matching) - len(shown)} further "
                f"occurrence(s), counted above but not printed"
            )
    return lines


def _review_notes(text: str) -> list[str]:
    return [] if not text else find_residual_originals(text, AliasMap())
