"""The clean-room boundary: nothing from the original may cross (CLAUDE.md section 2)."""

import json

from packages.modules.boundary import (
    AliasMap,
    annotate_border_review,
    find_residual_originals,
    neutral_evidence_reference,
    neutral_manifest,
    neutral_report,
)
from packages.modules.supervising.schemas import NeutralManifestSchema

DIRTY_REPORT = {
    "summary": {
        "label": "documented",
        "value": "Loads configuration during start-up.",
        "evidence": {
            "file": "src/app/config_loader.py",
            "line_start": 12,
            "line_end": 14,
            "excerpt": "cfg = json.load(handle)",
        },
    },
    "documentation_files_read": ["README.md", "docs/setup.md"],
    "features": [
        {
            "label": "documented",
            "value": "Rejects malformed records.",
            "evidence": {
                "file": "src/app/validate.py",
                "line_start": 3,
                "line_end": 3,
                "excerpt": "raise ValueError('bad record')",
            },
        }
    ],
}


# --- the alias map ----------------------------------------------------------


def test_the_same_location_always_gets_the_same_id():
    alias_map = AliasMap()
    first = alias_map.evidence_id("a/b.py", 1, 4)
    assert alias_map.evidence_id("a/b.py", 1, 4) == first
    assert alias_map.evidence_id("a/b.py", 9, 9) != first


def test_ids_are_opaque_and_sequential():
    alias_map = AliasMap()
    assert alias_map.evidence_id("x.py", 1, 1) == "EV-001"
    assert alias_map.evidence_id("y.py", 1, 1) == "EV-002"


def test_component_labels_are_neutral_and_stable():
    alias_map = AliasMap()
    assert alias_map.component_alias("GitRepoManifest") == "Component A"
    assert alias_map.component_alias("Reader") == "Component B"
    assert alias_map.component_alias("GitRepoManifest") == "Component A"


def test_component_labels_survive_past_the_alphabet():
    alias_map = AliasMap()
    labels = [alias_map.component_alias(f"Name{i}") for i in range(28)]
    assert labels[25] == "Component Z"
    assert labels[26] == "Component AA"


def test_the_map_round_trips_for_dirty_side_storage():
    alias_map = AliasMap()
    alias_map.evidence_id("a/b.py", 1, 2)
    alias_map.component_alias("Widget")
    alias_map.register_project("https://example.invalid/owner/original.git")

    restored = AliasMap.from_private_dict(alias_map.to_private_dict())
    assert restored.evidence_id("a/b.py", 1, 2) == "EV-001"
    assert restored.component_alias("Widget") == "Component A"
    assert restored.originals == alias_map.originals


def test_the_stored_map_warns_that_it_must_not_cross():
    payload = AliasMap().to_private_dict()
    assert "DIRTY SIDE ONLY" in payload["_warning"]


# --- neutralising artifacts -------------------------------------------------


def test_evidence_becomes_an_opaque_id():
    alias_map = AliasMap()
    reference = neutral_evidence_reference(
        {"file": "src/app.py", "line_start": 4, "line_end": 6, "excerpt": "code"}, alias_map
    )
    assert reference == "EV-001"


def test_evidence_without_a_location_yields_nothing_to_cite():
    assert neutral_evidence_reference({"file": None}, AliasMap()) is None
    assert neutral_evidence_reference(None, AliasMap()) is None


def test_a_neutralised_report_keeps_findings_and_drops_locations():
    alias_map = AliasMap()
    clean = neutral_report(DIRTY_REPORT, alias_map)

    assert clean["summary"]["value"] == "Loads configuration during start-up."
    assert clean["summary"]["evidence_id"] == "EV-001"
    assert "evidence" not in clean["summary"]
    assert "documentation_files_read" not in clean


def test_no_original_string_survives_neutralisation():
    alias_map = AliasMap()
    clean = json.dumps(neutral_report(DIRTY_REPORT, alias_map))
    for leaked in (
        "config_loader.py",
        "validate.py",
        "README.md",
        "json.load",
        "raise ValueError",
        "src/app",
    ):
        assert leaked not in clean, f"{leaked!r} crossed the boundary"


def test_the_neutral_manifest_describes_the_project_to_be_built(manifest):
    alias_map = AliasMap()
    payload = neutral_manifest(manifest, alias_map)

    assert payload["project_name"] == "PROJECT-X"
    assert payload["code_unit_count"] == len(manifest.code)
    assert "original-project" not in json.dumps(payload)
    assert "example.invalid" not in json.dumps(payload)
    assert NeutralManifestSchema().load(payload)


# --- leak detection (Dirty annotates; Border enforces) ----------------------


def test_a_known_original_is_flagged():
    alias_map = AliasMap()
    alias_map.evidence_id("src/app/validate.py", 3, 3)
    findings = find_residual_originals("see src/app/validate.py for details", alias_map)
    assert findings and "BORDER-REVIEW" in findings[0]


def test_generic_shapes_are_flagged_even_for_unknown_originals():
    alias_map = AliasMap()
    assert find_residual_originals("defined in helpers/tools.py", alias_map)
    assert find_residual_originals("at parser.py:120-140", alias_map)
    assert find_residual_originals("https://example.invalid/owner/x", alias_map)


def test_clean_prose_is_not_flagged():
    text = "The system validates each record and rejects malformed input. Evidence: EV-004."
    assert find_residual_originals(text, AliasMap()) == []


def test_findings_are_appended_rather_than_blocking():
    """Dirty annotates without blocking; Border is the enforcement gate (Q3)."""
    annotated = annotate_border_review("# Spec\n\nBody.", ["BORDER-REVIEW: something"])
    assert "# Spec" in annotated
    assert "BORDER-REVIEW" in annotated


def test_nothing_is_appended_when_there_is_nothing_to_report():
    assert annotate_border_review("# Spec\n", []) == "# Spec\n"


def test_one_failed_mini_task_does_not_discard_the_specification():
    """Regression: a single exhausted mini task raised and killed the whole spec stage,
    throwing away fragments that had already succeeded."""
    from packages.agents.dirt_team.spec_synthesizer_agent import (
        _handle_task_failure,
        _initial_payload,
        _make_finalizer,
        _serialize_spec_payload,
    )

    payload = _initial_payload()
    payload["fragments"].append(
        {
            "task_id": "SPEC-1",
            "output_field": "scope",
            "heading": "Scope",
            "markdown": "The system validates records.",
        }
    )
    _handle_task_failure(payload, {}, "system_overview", "SPEC-4", ValueError("bad json"))

    plan = [
        {"task_id": "SPEC-1", "output_field": "scope"},
        {"task_id": "SPEC-4", "output_field": "system_overview"},
        {"task_id": "SPEC-9", "output_field": "configuration"},
    ]
    markdown = _serialize_spec_payload(_make_finalizer(plan)(payload, set()))

    assert "The system validates records." in markdown, "good fragment was lost"
    assert "## System Overview — TODO" in markdown, "failed section not marked"
    assert "## Configuration — TODO" in markdown, "unplanned-output section not marked"


# --- identifier neutralisation (the leak the scanner used to miss) -----------

CODE_INDEX = {
    "classes": [{"name": "Calculadora", "methods": ["start", "_clear_input"]}],
    "functions": [
        {"name": "start", "qualified_name": "Calculadora.start"},
        {"name": "calculation", "qualified_name": "Calculador.calculation"},
    ],
    "imports": [{"module": "app.calculadora"}, {"module": "tkinter"}],
}


def test_code_identifiers_are_registered_in_the_alias_map():
    from packages.modules.boundary import register_code_identifiers

    alias_map = AliasMap()
    assert register_code_identifiers(CODE_INDEX, alias_map) > 0
    assert "start" in alias_map.identifiers
    assert "Calculadora" in alias_map.identifiers
    assert alias_map.alias_for("start") is not None


def test_the_scanner_detects_the_method_name_it_used_to_miss():
    """Regression: a real run shipped "started via its start method" and the scanner
    reported zero findings, because the map only ever learned file paths."""
    from packages.modules.boundary import register_code_identifiers

    alias_map = AliasMap()
    register_code_identifiers(CODE_INDEX, alias_map)

    leaked = "The component is instantiated with a root window and started via its start method."
    findings = find_residual_originals(leaked, alias_map)

    assert findings, "the scanner must not pass a spec containing an original method name"
    assert any("start" in f for f in findings)
    assert any("should be" in f for f in findings), "a finding should name the neutral alias"


def test_an_unregistered_map_still_cannot_see_identifiers():
    """Shows precisely why the old behaviour was blind: registration is the whole fix."""
    leaked = "started via its start method"
    assert find_residual_originals(leaked, AliasMap()) == []


def test_word_boundaries_stop_the_scanner_crying_wolf():
    from packages.modules.boundary import register_code_identifiers

    alias_map = AliasMap()
    register_code_identifiers(CODE_INDEX, alias_map)
    # "started"/"restart"/"startup" contain "start" but are ordinary English
    assert find_residual_originals("The service is restarted during startup.", alias_map) == []


def test_universal_identifiers_are_not_registered():
    """Registering `main` or `value` would rewrite ordinary English into nonsense."""
    from packages.modules.boundary import register_code_identifiers

    alias_map = AliasMap()
    register_code_identifiers(
        {"functions": [{"name": "main"}, {"name": "value"}, {"name": "go"}]}, alias_map
    )
    assert alias_map.identifiers == []


def test_identifiers_are_scrubbed_out_of_finding_prose():
    """Structural neutralisation kept prose verbatim, so class and method names flowed
    straight into the specification prompt."""
    from packages.modules.boundary import register_code_identifiers

    alias_map = AliasMap()
    register_code_identifiers(CODE_INDEX, alias_map)

    report = {
        "summary": {
            "label": "documented",
            "value": "The project exposes a Calculadora class in the app.calculadora module.",
            "evidence": {
                "file": "app/calculadora.py",
                "line_start": 1,
                "line_end": 2,
                "excerpt": "x",
            },
        }
    }
    clean = json.dumps(neutral_report(report, alias_map))
    assert "Calculadora" not in clean
    assert "app.calculadora" not in clean
    assert "Component" in clean or "Module" in clean


def test_longer_identifiers_are_replaced_before_the_names_they_contain():
    from packages.modules.boundary import register_code_identifiers, scrub_identifiers

    alias_map = AliasMap()
    register_code_identifiers(
        {"classes": [{"name": "Calculador"}, {"name": "Calculadora"}]}, alias_map
    )
    scrubbed = scrub_identifiers("Calculadora delegates to Calculador.", alias_map)
    assert "Calculador" not in scrubbed.replace(alias_map.alias_for("Calculador") or "", "")
    assert alias_map.alias_for("Calculadora") in scrubbed


# --- evidence quality for Border --------------------------------------------


def _scanned(text):
    from packages.modules.boundary import scan_residual_originals

    alias_map = AliasMap()
    alias_map.register_identifiers(
        [("start", "method"), ("calculation", "function"), ("copy", "module")]
    )
    return alias_map, scan_residual_originals(text, alias_map)


def test_a_name_shaped_use_is_classified_as_such():
    _, findings = _scanned("The component is started via its start method.")
    occurrences = [o for f in findings for o in f.occurrences]
    assert [o.classification for o in occurrences] == ["NAME-SHAPED"]


def test_a_descriptive_use_is_classified_as_such():
    _, findings = _scanned("Checks ensure successful application start procedures.")
    occurrences = [o for f in findings for o in f.occurrences]
    assert [o.classification for o in occurrences] == ["DESCRIPTIVE"]


def test_both_readings_are_still_reported_and_neither_is_suppressed():
    """The classification is a hint for Border, never a reason to drop a flag."""
    _, findings = _scanned(
        "It is started via its start method. Application start procedures then run."
    )
    assert len(findings) == 1, "one flag for the word, not one per reading"

    finding = findings[0]
    assert finding.count("NAME-SHAPED") == 1
    assert finding.count("DESCRIPTIVE") == 1
    assert len(finding.occurrences) == 2, "every occurrence is surfaced"

    # and the flag still reaches the document, with both readings visible
    annotated = annotate_border_review("# Spec", findings)
    assert "BORDER-REVIEW" in annotated
    assert "NAME-SHAPED" in annotated and "DESCRIPTIVE" in annotated
    assert "advisory" in annotated.lower()


def test_a_purely_descriptive_word_is_still_flagged():
    """No allow-list: 'a deep copy' is almost certainly innocent and still gets reported,
    because deciding that is Border's job and Border does not exist yet."""
    _, findings = _scanned("Outputs a deep copy of the theme configuration.")
    assert len(findings) == 1
    assert findings[0].original == "copy"
    assert findings[0].count("DESCRIPTIVE") == 1


def test_occurrences_are_counted_not_collapsed():
    text = " ".join(["The calculation utility performs a calculation step."] * 5)
    _, findings = _scanned(text)
    assert len(findings) == 1, "one line per word, not one per hit"
    assert len(findings[0].occurrences) == 10
    assert "10 occurrence(s)" in findings[0].summary


def test_an_ambiguous_use_is_uncertain_rather_than_assumed_innocent():
    """Guessing DESCRIPTIVE is how a real leak gets waved through."""
    _, findings = _scanned("start")
    assert [o.classification for f in findings for o in f.occurrences] == ["UNCERTAIN"]


def test_the_summary_carries_counts_and_the_alias():
    _, findings = _scanned("It is started via its start method.")
    summary = findings[0].summary
    assert "'start'" in summary
    assert "should be" in summary
    assert "1 occurrence(s)" in summary
    assert "1 name-shaped" in summary
    assert "[advisory]" in summary


def test_many_occurrences_report_a_sample_and_say_so():
    """Counts stay complete even when the printed examples are capped."""
    text = " ".join(["The calculation engine runs."] * 8)
    _, findings = _scanned(text)
    annotated = annotate_border_review("# Spec", findings)
    assert "8 occurrence(s)" in annotated
    assert "further" in annotated, "a capped sample must say how many it did not print"


# --- layer 2: copied-content detection ---------------------------------------
# The documentation leak ("git checkout -b feature/nome_da_modificação") defeated every
# rule above: it is not a registered identifier, not a path, not a URL. These rules find
# copied content by its shape, so they fire on an original nobody registered.


def _content(text, source_texts=()):
    from packages.modules.boundary import scan_content_leaks

    return scan_content_leaks(text, source_texts)


def test_a_git_command_is_flagged():
    findings = _content(
        "Contributors create a feature branch using git checkout -b feature/my_change."
    )
    assert [f.kind for f in findings] == ["command-shaped text"]
    assert "git checkout -b feature/my_change" in findings[0].original
    assert findings[0].count("VERBATIM") == 1


def test_the_original_documentation_leak_is_caught():
    """The exact line that crossed in the live run, which the scanner missed entirely."""
    leaked = (
        "- Feature Branch Creation: Contributors create a new feature branch using the "
        "command git checkout -b feature/nome_da_modificação (Evidence: EV-205)."
    )
    kinds = {f.kind for f in _content(leaked)}
    assert "command-shaped text" in kinds
    assert "source-language text" in kinds


def test_non_ascii_source_language_text_is_flagged():
    findings = _content("The section is titled ## Motivação in the source document.")
    assert [f.kind for f in findings] == ["source-language text"]
    assert "Motivação" in findings[0].original


def test_a_diacritic_free_foreign_phrase_is_flagged():
    """The case a character-set test cannot catch, and the reason these rules target
    shape rather than bytes."""
    findings = _content("Crie sua branch para realizar sua modificacao antes de comecar.")
    assert findings, "unaccented Portuguese must not pass as English"
    assert findings[0].kind == "source-language text"
    assert findings[0].count("VERBATIM") == 1


def test_ordinary_english_specification_prose_is_not_flagged():
    """These rules run on every specification, so a false positive is expensive."""
    for prose in (
        "The system shall make the request and go to the next screen.",
        "Component A is the single source of truth for evaluation state.",
        "Results are converted into exponential notation — see the note above.",
        "The service must set the display value and export the result.",
        "Given an instance of Component A, when evaluation runs, a result is returned.",
        "Released under the MIT license and published at example.com for reference.",
    ):
        assert _content(prose) == [], f"false positive on: {prose}"


def test_verbatim_prose_lifted_from_a_source_document_is_flagged():
    source = ["Run the application by executing the main entry point from the project root."]
    findings = _content(
        "Setup: run the application by executing the main entry point from the project "
        "root, then confirm the window appears.",
        source,
    )
    assert [f.kind for f in findings] == ["verbatim source-document prose"]
    assert "executing the main entry point" in findings[0].original


def test_paraphrased_prose_is_not_flagged_as_lifted():
    source = ["Run the application by executing the main entry point from the project root."]
    assert _content("Start the program from its top-level launcher.", source) == []


def test_an_ambiguous_command_word_is_uncertain_not_dropped():
    """'make -j4' might be a command or prose. Never silently pass."""
    findings = _content("The build step uses make -j4 to compile.")
    assert findings
    assert findings[0].count("UNCERTAIN") == 1, "unclear shapes stay a question for Border"


def test_content_findings_report_counts_and_context_like_identifier_flags():
    findings = _content("Run git clone https://example.invalid/x to begin.")
    summary = findings[0].summary
    assert "occurrence(s)" in summary and "verbatim" in summary and "[advisory]" in summary
    annotated = annotate_border_review("# Spec", findings)
    assert "BORDER-REVIEW" in annotated and "VERBATIM" in annotated
    assert "git clone" in annotated, "the flag carries the phrase it was found in"


def test_nothing_is_suppressed_when_several_rules_fire():
    """No allow-list, no deduplication across rules that saw different things."""
    findings = _content('Use git commit -m "Descrição da modificação" to save work.')
    assert len(findings) >= 2
    assert {"command-shaped text", "source-language text"} <= {f.kind for f in findings}


def test_content_rules_stay_out_of_the_plan_neutrality_gate():
    """Scrubbing cannot fix a command or a foreign phrase, so a plan tripping these
    would fail every round instead of being repaired. Advisory on the spec only."""
    from packages.modules.boundary import find_residual_originals

    plan = "Task: describe how git checkout -b feature/nome creates a branch."
    assert find_residual_originals(plan, AliasMap()) == []
    assert _content(plan), "the same text is still flagged on the specification path"


def test_a_tool_name_in_ordinary_prose_is_not_a_command():
    """From the verification re-run: 'standard python execution process arguments' was
    flagged VERBATIM. A tool name followed by words is prose until something in it looks
    like an actual argument."""
    assert (
        _content("Environment parameters include standard python execution process arguments.")
        == []
    )
    assert _content("The go to definition feature and the node in the tree are unrelated.") == []


def test_a_real_command_still_survives_the_prose_filter():
    for command in (
        "run git clone to obtain the sources",
        "install with pip install -r requirements.txt",
        "start it using python main.py from the root",
        "apply chmod +x scripts/setup.sh first",
    ):
        assert _content(command), f"missed a real command: {command}"


def test_an_unexpected_bare_tool_reference_stays_uncertain():
    """git is never an ordinary English word, so 'git something' remains a question even
    without argument evidence - dropped only for tools that double as prose."""
    findings = _content("The git workflow described here is conventional.")
    assert findings and findings[0].count("UNCERTAIN") == 1


# --- the catalogue is a crossing artifact, and is filtered like one -----------
# Third instance of the same blindness: _is_safe_label used the identifier scanner, so
# three verbatim git commands and a Portuguese heading became the labels the planner
# read. Unlike the specification scanner this filter is ENFORCING - the generic noun is
# always available, so rejecting costs one description and degrades nothing (Q26-Q28).


def _catalogue(*items, kind="commands"):
    from packages.modules.boundary import build_evidence_catalogue

    alias_map = AliasMap(project_name="PROJECT-X")
    index = {
        kind: [
            {**item, "evidence": {"file": "d.md", "line_start": n, "line_end": n}}
            for n, item in enumerate(items, start=1)
        ]
    }
    return alias_map, build_evidence_catalogue(alias_map, None, index)


def test_a_command_label_falls_back_to_the_generic_noun():
    _, art = _catalogue({"command": "git checkout -b feature/my_change"})
    assert art["entries"][0]["about"] == "a documented command"


def test_the_three_real_leaked_commands_are_all_rejected():
    """The exact labels that reached the planner in the live runs."""
    _, art = _catalogue(
        {"command": "git checkout -b feature/nome_da_modificação"},
        {"command": 'git commit -m "Descrição da modificação"'},
        {"command": "git push origin feature/nome_modificação"},
    )
    assert [e["about"] for e in art["entries"]] == ["a documented command"] * 3
    assert len(art["border_review"]) == 3


def test_a_foreign_language_heading_is_rejected():
    """'Motivação' is a section title, not a command - the other half of the leak."""
    _, art = _catalogue({"title": "Motivação"}, kind="sections")
    assert art["entries"][0]["about"] == "a documentation section"


def test_a_clean_label_survives_untouched():
    _, art = _catalogue({"title": "Configuration and theming"}, kind="sections")
    assert art["entries"][0]["about"] == "Configuration and theming"
    assert art["border_review"] == []


def test_a_known_original_label_is_still_rejected():
    """The original rule keeps working; content rules were added, not substituted. A path
    is the case scrubbing cannot repair - it is what the two silent rejections in the live
    runs actually were."""
    _, art = _catalogue({"command": "run main.py to start"})
    assert art["entries"][0]["about"] == "a documented command"
    assert "a known original" in art["border_review"][0]


def test_a_registered_identifier_is_repaired_rather_than_rejected():
    """Scrub first, judge after. Rejection is the fallback for what scrubbing cannot fix,
    so a label naming a known class keeps its shape with the alias substituted in."""
    from packages.modules.boundary import build_evidence_catalogue

    alias_map = AliasMap(project_name="PROJECT-X")
    alias_map.register_identifiers([("Calculador", "class")])
    art = build_evidence_catalogue(
        alias_map,
        None,
        {
            "sections": [
                {
                    "title": "How Calculador works",
                    "evidence": {"file": "d.md", "line_start": 1, "line_end": 1},
                }
            ]
        },
    )
    about = art["entries"][0]["about"]
    assert "Calculador" not in about
    assert about == f"How {alias_map.alias_for('Calculador')} works"
    assert art["border_review"] == []


def test_a_rejection_emits_exactly_one_note_naming_the_rule():
    _, art = _catalogue({"command": "git checkout -b feature/my_change"})
    assert len(art["border_review"]) == 1
    note = art["border_review"][0]
    assert "BORDER-REVIEW" in note and "command-shaped text" in note and "'command'" in note


def test_a_note_never_quotes_the_rejected_text():
    """The notes travel inside a crossing artifact. Quoting what was rejected would put
    back exactly what the filter removed."""
    _, art = _catalogue(
        {"command": "git checkout -b feature/nome_da_modificação"},
        {"title": "Motivação"},
    )
    blob = " ".join(art["border_review"])
    assert "git " not in blob
    assert "nome_da" not in blob
    assert all(ord(c) < 128 for c in blob), "no source-language text in the notes either"


def test_the_catalogue_artifact_validates_against_its_schema():
    from packages.modules.supervising.schemas import EvidenceCatalogueSchema

    _, art = _catalogue({"command": "git clone https://example.invalid/x"})
    assert EvidenceCatalogueSchema().validate(art) == {}


def test_the_entries_only_helper_keeps_its_list_contract():
    """`evidence_catalogue` is what the planner is handed; it stayed a plain list."""
    from packages.modules.boundary import build_evidence_catalogue, evidence_catalogue

    alias_map = AliasMap(project_name="PROJECT-X")
    index = {
        "sections": [
            {"title": "Theming", "evidence": {"file": "d.md", "line_start": 1, "line_end": 1}}
        ]
    }
    entries = evidence_catalogue(alias_map, None, index)
    assert isinstance(entries, list)
    assert (
        entries
        == build_evidence_catalogue(AliasMap(project_name="PROJECT-X"), None, index)["entries"]
    )


def test_the_filter_rejects_on_doubt_not_only_on_certainty():
    """Polarity inverts from the advisory scanner: there UNCERTAIN must not drop a flag,
    here UNCERTAIN rejects, because the fallback is free (Q27)."""
    from packages.modules.boundary import scan_content_leaks

    label = "The git workflow described here"
    findings = scan_content_leaks(label)
    assert findings and all(
        o.classification == "UNCERTAIN" for f in findings for o in f.occurrences
    ), "precondition: this label is only UNCERTAIN"

    _, art = _catalogue({"title": label}, kind="sections")
    assert art["entries"][0]["about"] == "a documentation section"


def test_the_lifted_prose_corpus_ignores_findings_with_no_source():
    """A `missing` finding has no file to quote, so the controller stores the agent's own
    English as its excerpt. Feeding that back made the scanner flag the specification for
    restating our own words - a false positive on every run reporting something missing."""
    from packages.modules.border import evidence_excerpts

    report = {
        "setup_and_run": [
            {
                "label": "missing",
                "value": "The documentation does not provide instructions for setting up the project.",
                "evidence": {
                    "file": None,
                    "line_start": None,
                    "line_end": None,
                    "excerpt": "The documentation does not provide instructions for setting up the project.",
                },
            },
            {
                "label": "documented",
                "value": "Describes how the entry point starts.",
                "evidence": {
                    "file": "README.md",
                    "line_start": 3,
                    "line_end": 3,
                    "excerpt": "Execute the launcher module to open the calculator window.",
                },
            },
        ]
    }
    corpus = evidence_excerpts(report)
    assert corpus == ("Execute the launcher module to open the calculator window.",)


def test_a_restated_missing_finding_is_no_longer_flagged_as_lifted():
    """End to end for the same defect: the exact sentence pair from the live run."""
    from packages.modules.border import evidence_excerpts

    sentence = "The documentation does not provide instructions for setting up and executing the project."
    report = {
        "setup_and_run": [
            {
                "label": "missing",
                "value": sentence,
                "evidence": {
                    "file": None,
                    "line_start": None,
                    "line_end": None,
                    "excerpt": sentence,
                },
            }
        ]
    }
    spec = f"- **Setup and Execution**: {sentence} (Evidence: EV-195)."
    assert _content(spec, evidence_excerpts(report)) == []


def test_real_lifted_prose_is_still_caught_after_the_fix():
    """The fix must not blunt the rule for excerpts that DO have a source."""
    from packages.modules.border import evidence_excerpts

    report = {
        "features": [
            {
                "label": "documented",
                "value": "Summarised.",
                "evidence": {
                    "file": "README.md",
                    "line_start": 9,
                    "line_end": 9,
                    "excerpt": "Run the application by executing the main entry point from the project root.",
                },
            }
        ]
    }
    spec = "Setup: run the application by executing the main entry point from the project root."
    findings = _content(spec, evidence_excerpts(report))
    assert [f.kind for f in findings] == ["verbatim source-document prose"]
