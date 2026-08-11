from marshmallow import EXCLUDE, RAISE, Schema, fields, validate

DOC_LABELS = ["documented", "inferred", "missing"]
CODE_LABELS = ["observed", "inferred", "missing"]
MISSING_ONLY = ["missing"]
EVIDENCE_SOURCES = ["code", "documentation", "code_facts", "inference"]


class StrictSchema(Schema):
    class Meta:
        unknown = RAISE


class LenientSchema(Schema):
    """For entry-level schemas where models often emit stray meta fields (kind, output_field).
    Top-level report schemas should stay StrictSchema."""

    class Meta:
        unknown = EXCLUDE


class EvidenceSchema(LenientSchema):
    file = fields.String(required=True, allow_none=True)
    line_start = fields.Integer(required=True, allow_none=True)
    line_end = fields.Integer(required=True, allow_none=True)
    excerpt = fields.String(required=True)


class EvidenceWithSourceSchema(EvidenceSchema):
    source = fields.String(required=True, validate=validate.OneOf(EVIDENCE_SOURCES))


def labeled_value_schema(allowed_labels: list[str]) -> type[Schema]:
    """A {label, value, evidence} entry — used heavily by doc/code analyzer outputs."""

    class _LabeledValue(LenientSchema):
        label = fields.String(required=True, validate=validate.OneOf(allowed_labels))
        value = fields.String(required=True)
        evidence = fields.Nested(EvidenceSchema, required=True)

    return _LabeledValue


def open_question_schema() -> type[Schema]:
    return labeled_value_schema(MISSING_ONLY)
