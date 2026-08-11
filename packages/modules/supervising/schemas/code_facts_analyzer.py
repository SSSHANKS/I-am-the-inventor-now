from marshmallow import fields, validate

from packages.modules.supervising.schemas.common import (
    CODE_LABELS,
    EvidenceSchema,
    StrictSchema,
    open_question_schema,
)

CODE_FACTS_OUTPUT_FIELDS: tuple[str, ...] = (
    "symbols",
    "imports",
    "calls",
    "state_and_side_effects",
    "errors_and_exceptions",
    "open_questions",
)

_OpenQuestion = open_question_schema()


class _NarrowSymbolItem(StrictSchema):
    source_ref = fields.Integer(required=True, allow_none=True)
    label = fields.String(required=True, validate=validate.OneOf(CODE_LABELS))
    symbol = fields.String(required=True)
    kind = fields.String(
        required=True,
        validate=validate.OneOf(["module", "class", "function", "method", "constant", "config"]),
    )
    signature = fields.String(required=True, allow_none=True)
    inputs = fields.List(fields.String(), required=True)
    returns = fields.String(required=True, allow_none=True)
    description = fields.String(required=True)


class _NarrowImportItem(StrictSchema):
    source_ref = fields.Integer(required=True, allow_none=True)
    label = fields.String(required=True, validate=validate.OneOf(CODE_LABELS))
    import_ = fields.String(required=True, data_key="import")


class _NarrowCallItem(StrictSchema):
    source_ref = fields.Integer(required=True, allow_none=True)
    label = fields.String(required=True, validate=validate.OneOf(CODE_LABELS))
    caller = fields.String(required=True)
    callee = fields.String(required=True)


class _NarrowStateItem(StrictSchema):
    source_ref = fields.Integer(required=True, allow_none=True)
    label = fields.String(required=True, validate=validate.OneOf(CODE_LABELS))
    symbol = fields.String(required=True)
    state_read = fields.List(fields.String(), required=True)
    state_written = fields.List(fields.String(), required=True)
    side_effects = fields.List(fields.String(), required=True)
    value = fields.String(required=True, allow_none=True)


class _NarrowErrorItem(StrictSchema):
    source_ref = fields.Integer(required=True, allow_none=True)
    label = fields.String(required=True, validate=validate.OneOf(CODE_LABELS))
    symbol = fields.String(required=True)
    error = fields.String(required=True)
    handling = fields.String(required=True)
    value = fields.String(required=True, allow_none=True)


class _NarrowOpenQuestionItem(StrictSchema):
    source_ref = fields.Integer(required=True, allow_none=True)
    label = fields.String(required=True, validate=validate.OneOf(["missing"]))
    value = fields.String(required=True)


class CodeFactsNarrowSymbolsSchema(StrictSchema):
    items = fields.List(fields.Nested(_NarrowSymbolItem), required=True)


class CodeFactsNarrowImportsSchema(StrictSchema):
    items = fields.List(fields.Nested(_NarrowImportItem), required=True)


class CodeFactsNarrowCallsSchema(StrictSchema):
    items = fields.List(fields.Nested(_NarrowCallItem), required=True)


class CodeFactsNarrowStateSchema(StrictSchema):
    items = fields.List(fields.Nested(_NarrowStateItem), required=True)


class CodeFactsNarrowErrorsSchema(StrictSchema):
    items = fields.List(fields.Nested(_NarrowErrorItem), required=True)


class CodeFactsNarrowOpenQuestionsSchema(StrictSchema):
    items = fields.List(fields.Nested(_NarrowOpenQuestionItem), required=True)


class _SymbolItem(StrictSchema):
    label = fields.String(required=True, validate=validate.OneOf(CODE_LABELS))
    file = fields.String(required=True)
    symbol = fields.String(required=True)
    kind = fields.String(
        required=True,
        validate=validate.OneOf(["module", "class", "function", "method", "constant", "config"]),
    )
    signature = fields.String(required=True, allow_none=True)
    inputs = fields.List(fields.String(), required=True)
    returns = fields.String(required=True, allow_none=True)
    description = fields.String(required=True)
    evidence = fields.Nested(EvidenceSchema, required=True)


class _ImportItem(StrictSchema):
    label = fields.String(required=True, validate=validate.OneOf(CODE_LABELS))
    file = fields.String(required=True)
    import_ = fields.String(required=True, data_key="import")
    evidence = fields.Nested(EvidenceSchema, required=True)


class _CallItem(StrictSchema):
    label = fields.String(required=True, validate=validate.OneOf(CODE_LABELS))
    caller = fields.String(required=True)
    callee = fields.String(required=True)
    evidence = fields.Nested(EvidenceSchema, required=True)


class _StateItem(StrictSchema):
    label = fields.String(required=True, validate=validate.OneOf(CODE_LABELS))
    symbol = fields.String(required=True)
    state_read = fields.List(fields.String(), required=True)
    state_written = fields.List(fields.String(), required=True)
    side_effects = fields.List(fields.String(), required=True)
    value = fields.String(required=True, allow_none=True)
    evidence = fields.Nested(EvidenceSchema, required=True)


class _ErrorItem(StrictSchema):
    label = fields.String(required=True, validate=validate.OneOf(CODE_LABELS))
    symbol = fields.String(required=True)
    error = fields.String(required=True)
    handling = fields.String(required=True)
    value = fields.String(required=True, allow_none=True)
    evidence = fields.Nested(EvidenceSchema, required=True)


class CodeFactsAnalyzerSchema(StrictSchema):
    files_read = fields.List(fields.String(), required=True)
    symbols = fields.List(fields.Nested(_SymbolItem), required=True)
    imports = fields.List(fields.Nested(_ImportItem), required=True)
    calls = fields.List(fields.Nested(_CallItem), required=True)
    state_and_side_effects = fields.List(fields.Nested(_StateItem), required=True)
    errors_and_exceptions = fields.List(fields.Nested(_ErrorItem), required=True)
    open_questions = fields.List(fields.Nested(_OpenQuestion), required=True)
