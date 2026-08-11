from marshmallow import fields, validate

from packages.modules.supervising.schemas.common import (
    DOC_LABELS,
    MISSING_ONLY,
    LenientSchema,
    StrictSchema,
    labeled_value_schema,
    open_question_schema,
)

DocLabeledSchema = labeled_value_schema(DOC_LABELS)
DocOpenQuestionSchema = open_question_schema()


# Narrow schemas for the LLM-only "interpret text" step. The agent builds evidence
# itself from input_ref + tool result, so the model just has to produce {label, value}.
class DocNarrowLabeledSchema(LenientSchema):
    label = fields.String(required=True, validate=validate.OneOf(DOC_LABELS))
    value = fields.String(required=True)


class DocNarrowOpenQuestionSchema(LenientSchema):
    label = fields.String(required=True, validate=validate.OneOf(MISSING_ONLY))
    value = fields.String(required=True)


# Entry kind identifiers used by sequential mini-task execution to pick the right schema.
DOC_KIND_LABELED = "doc_labeled"
DOC_KIND_OPEN_QUESTION = "doc_open_question"

# Map of allowed mini-task output_field -> entry kind. Used by:
#   - PlanningAgent to constrain output_field
#   - PlanVerifier to validate output_field
#   - DocumentationAgent to dispatch mini-tasks to the right entry schema and aggregation slot.
# documentation_files_read is intentionally absent — it is computed from evidence.file of all entries.
DOC_OUTPUT_FIELDS: dict[str, str] = {
    "summary": DOC_KIND_LABELED,
    "project_purpose": DOC_KIND_LABELED,
    "setup_and_run": DOC_KIND_LABELED,
    "api_surface": DOC_KIND_LABELED,
    "features": DOC_KIND_LABELED,
    "warnings": DOC_KIND_LABELED,
    "open_questions": DOC_KIND_OPEN_QUESTION,
}

# Output fields stored as a single entry (not list).
DOC_SINGLE_FIELDS: frozenset[str] = frozenset({"summary"})


class DocAnalyzerSchema(StrictSchema):
    summary = fields.Nested(DocLabeledSchema, required=True)
    documentation_files_read = fields.List(fields.String(), required=True)
    project_purpose = fields.List(fields.Nested(DocLabeledSchema), required=True)
    setup_and_run = fields.List(fields.Nested(DocLabeledSchema), required=True)
    api_surface = fields.List(fields.Nested(DocLabeledSchema), required=True)
    features = fields.List(fields.Nested(DocLabeledSchema), required=True)
    warnings = fields.List(fields.Nested(DocLabeledSchema), required=True)
    open_questions = fields.List(fields.Nested(DocOpenQuestionSchema), required=True)
