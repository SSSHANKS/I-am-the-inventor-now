from marshmallow import fields, validate

from packages.modules.supervising.schemas.common import (
    CODE_LABELS,
    EvidenceWithSourceSchema,
    StrictSchema,
)

EXPECTED_SOURCES = ["code", "documentation", "recommendation", "unknown"]
REQUIREMENT_SOURCES = ["documentation", "code", "recommendation", "unknown"]
TESTABILITY = ["unit", "integration", "gui", "manual", "unknown"]
TEST_TYPES = ["unit", "integration", "gui", "manual"]
BEHAVIOR_OUTPUT_FIELDS: tuple[str, ...] = (
    "behaviors",
    "edge_cases",
    "error_handling",
    "test_candidates",
    "specification_requirements",
    "open_questions",
)


class _NarrowBehaviorItem(StrictSchema):
    source_ref = fields.Integer(required=True, allow_none=True)
    label = fields.String(required=True, validate=validate.OneOf(CODE_LABELS))
    name = fields.String(required=True)
    description = fields.String(required=True)
    inputs = fields.List(fields.String(), required=True)
    outputs = fields.List(fields.String(), required=True)
    preconditions = fields.List(fields.String(), required=True)
    postconditions = fields.List(fields.String(), required=True)
    testability = fields.String(required=True, validate=validate.OneOf(TESTABILITY))


class _NarrowEdgeCaseItem(StrictSchema):
    source_ref = fields.Integer(required=True, allow_none=True)
    label = fields.String(required=True, validate=validate.OneOf(CODE_LABELS))
    behavior = fields.String(required=True)
    case = fields.String(required=True)
    current_result = fields.String(required=True, allow_none=True)
    expected_result = fields.String(required=True, allow_none=True)
    expected_source = fields.String(required=True, validate=validate.OneOf(EXPECTED_SOURCES))


class _NarrowErrorHandlingItem(StrictSchema):
    source_ref = fields.Integer(required=True, allow_none=True)
    label = fields.String(required=True, validate=validate.OneOf(CODE_LABELS))
    behavior = fields.String(required=True)
    error = fields.String(required=True)
    handling = fields.String(required=True)


class _NarrowTestCandidateItem(StrictSchema):
    source_ref = fields.Integer(required=True, allow_none=True)
    label = fields.String(required=True, validate=validate.OneOf(CODE_LABELS))
    name = fields.String(required=True)
    test_type = fields.String(required=True, validate=validate.OneOf(TEST_TYPES))
    given = fields.List(fields.String(), required=True)
    when = fields.List(fields.String(), required=True)
    then = fields.List(fields.String(), required=True)
    assertions = fields.List(fields.String(), required=True)


class _NarrowRequirementItem(StrictSchema):
    source_ref = fields.Integer(required=True, allow_none=True)
    label = fields.String(required=True, validate=validate.OneOf(CODE_LABELS))
    requirement = fields.String(required=True)
    source = fields.String(required=True, validate=validate.OneOf(REQUIREMENT_SOURCES))


class _NarrowOpenQuestionItem(StrictSchema):
    source_ref = fields.Integer(required=True, allow_none=True)
    label = fields.String(required=True, validate=validate.OneOf(["missing"]))
    value = fields.String(required=True)


class BehaviorNarrowBehaviorsSchema(StrictSchema):
    items = fields.List(fields.Nested(_NarrowBehaviorItem), required=True)


class BehaviorNarrowEdgeCasesSchema(StrictSchema):
    items = fields.List(fields.Nested(_NarrowEdgeCaseItem), required=True)


class BehaviorNarrowErrorHandlingSchema(StrictSchema):
    items = fields.List(fields.Nested(_NarrowErrorHandlingItem), required=True)


class BehaviorNarrowTestCandidatesSchema(StrictSchema):
    items = fields.List(fields.Nested(_NarrowTestCandidateItem), required=True)


class BehaviorNarrowRequirementsSchema(StrictSchema):
    items = fields.List(fields.Nested(_NarrowRequirementItem), required=True)


class BehaviorNarrowOpenQuestionsSchema(StrictSchema):
    items = fields.List(fields.Nested(_NarrowOpenQuestionItem), required=True)


class _BehaviorItem(StrictSchema):
    label = fields.String(required=True, validate=validate.OneOf(CODE_LABELS))
    name = fields.String(required=True)
    description = fields.String(required=True)
    inputs = fields.List(fields.String(), required=True)
    outputs = fields.List(fields.String(), required=True)
    preconditions = fields.List(fields.String(), required=True)
    postconditions = fields.List(fields.String(), required=True)
    testability = fields.String(required=True, validate=validate.OneOf(TESTABILITY))
    evidence = fields.Nested(EvidenceWithSourceSchema, required=True)


class _EdgeCaseItem(StrictSchema):
    label = fields.String(required=True, validate=validate.OneOf(CODE_LABELS))
    behavior = fields.String(required=True)
    case = fields.String(required=True)
    current_result = fields.String(required=True, allow_none=True)
    expected_result = fields.String(required=True, allow_none=True)
    expected_source = fields.String(required=True, validate=validate.OneOf(EXPECTED_SOURCES))
    evidence = fields.Nested(EvidenceWithSourceSchema, required=True)


class _ErrorHandlingItem(StrictSchema):
    label = fields.String(required=True, validate=validate.OneOf(CODE_LABELS))
    behavior = fields.String(required=True)
    error = fields.String(required=True)
    handling = fields.String(required=True)
    evidence = fields.Nested(EvidenceWithSourceSchema, required=True)


class _TestCandidateItem(StrictSchema):
    label = fields.String(required=True, validate=validate.OneOf(CODE_LABELS))
    name = fields.String(required=True)
    test_type = fields.String(required=True, validate=validate.OneOf(TEST_TYPES))
    given = fields.List(fields.String(), required=True)
    when = fields.List(fields.String(), required=True)
    then = fields.List(fields.String(), required=True)
    assertions = fields.List(fields.String(), required=True)
    evidence = fields.Nested(EvidenceWithSourceSchema, required=True)


class _RequirementItem(StrictSchema):
    label = fields.String(required=True, validate=validate.OneOf(CODE_LABELS))
    requirement = fields.String(required=True)
    source = fields.String(required=True, validate=validate.OneOf(REQUIREMENT_SOURCES))
    evidence = fields.Nested(EvidenceWithSourceSchema, required=True)


class _OpenQuestionWithSource(StrictSchema):
    label = fields.String(required=True, validate=validate.OneOf(["missing"]))
    value = fields.String(required=True)
    evidence = fields.Nested(EvidenceWithSourceSchema, required=True)


class BehaviorAnalyzerSchema(StrictSchema):
    files_read = fields.List(fields.String(), required=True)
    behaviors = fields.List(fields.Nested(_BehaviorItem), required=True)
    edge_cases = fields.List(fields.Nested(_EdgeCaseItem), required=True)
    error_handling = fields.List(fields.Nested(_ErrorHandlingItem), required=True)
    test_candidates = fields.List(fields.Nested(_TestCandidateItem), required=True)
    specification_requirements = fields.List(fields.Nested(_RequirementItem), required=True)
    open_questions = fields.List(fields.Nested(_OpenQuestionWithSource), required=True)
