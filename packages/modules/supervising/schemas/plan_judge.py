"""Schema for a plan judgement.

Deliberately does not include a neutrality score. Neutrality is a pass/fail gate decided
by the deterministic scanner, and giving the judge a neutrality field would invite it to
be averaged into the total - which is exactly how a leaking plan wins on points
(CLAUDE.md section 2).
"""

from marshmallow import fields, validate

from packages.modules.supervising.schemas.common import LenientSchema


class _Scores(LenientSchema):
    crux_coverage = fields.Integer(required=True, validate=validate.Range(min=0, max=5))
    proportional_decomposition = fields.Integer(
        required=True, validate=validate.Range(min=0, max=5)
    )
    completeness = fields.Integer(required=True, validate=validate.Range(min=0, max=5))


class PlanJudgementSchema(LenientSchema):
    strongest_objection = fields.String(required=True)
    scores = fields.Nested(_Scores, required=True)
    actions = fields.List(fields.String(), required=True)
