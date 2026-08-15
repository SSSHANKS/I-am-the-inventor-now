"""Schema for Border's stored verdict and LLM adjudication response.

The verdict is Dirty-side operational output (it names originals that leaked). It must
never be handed to Clean as a build input - Clean only receives artifacts Border passed.
It is still schema-validated before storage like every other artifact.
"""

from marshmallow import fields, validate

from packages.modules.supervising.schemas.common import LenientSchema, StrictSchema

_STATUSES = ["pass", "fail"]
_DECISIONS = ["fail", "dismiss"]


class BorderFindingSchema(StrictSchema):
    finding_id = fields.String(required=True, validate=validate.Regexp(r"^BF-\d+$"))
    artifact = fields.String(required=True, validate=validate.Length(min=1))
    original = fields.String(required=True, validate=validate.Length(min=1))
    alias = fields.String(required=True, allow_none=True)
    kind = fields.String(required=True, validate=validate.Length(min=1))
    classifications = fields.List(fields.String(), required=True)
    occurrence_count = fields.Integer(required=True, validate=validate.Range(min=1))
    decision = fields.String(required=True, validate=validate.OneOf(_DECISIONS))
    summary = fields.String(required=True, validate=validate.Length(min=1))
    examples = fields.List(fields.String(), required=True)
    rationale = fields.String(required=True, allow_none=True)


class BorderVerdictSchema(StrictSchema):
    status = fields.String(required=True, validate=validate.OneOf(_STATUSES))
    policy = fields.String(required=True, validate=validate.Length(min=1))
    artifacts_reviewed = fields.List(fields.String(), required=True)
    finding_count = fields.Integer(required=True, validate=validate.Range(min=0))
    findings = fields.List(fields.Nested(BorderFindingSchema), required=True)
    notes = fields.List(fields.String(), required=True)
    failed_artifacts = fields.List(fields.String(), required=True)


class _BorderDecision(LenientSchema):
    finding_id = fields.String(required=True, validate=validate.Regexp(r"^BF-\d+$"))
    decision = fields.String(required=True, validate=validate.OneOf(_DECISIONS))
    rationale = fields.String(required=True)


class BorderAdjudicationSchema(LenientSchema):
    """What the Border LLM returns for a batch of soft findings."""

    decisions = fields.List(fields.Nested(_BorderDecision), required=True)
