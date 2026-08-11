from marshmallow import EXCLUDE, Schema, fields, validate

from packages.modules.supervising.schemas.common import StrictSchema


# Mini-task and input-ref schemas tolerate unknown fields. The model often emits stray
# keys ("agent", "kind", "ref_id", "target", etc.) that downstream code does not need;
# letting them silently drop is far cheaper than blowing up the whole repair loop.
class _LenientSchema(Schema):
    class Meta:
        unknown = EXCLUDE


class _InputRef(_LenientSchema):
    """What a mini task points at.

    An opaque evidence id and nothing else. The controller resolves it to a file and
    line range dirty-side, so the plan itself stays a neutral crossing artifact
    (CLAUDE.md section 2). `file`, `line_start`, `line_end` and `evidence` are gone
    deliberately - a plan carrying them is carrying the original.
    """

    source = fields.String(required=True)
    evidence_id = fields.String(required=True)


class _MiniTask(_LenientSchema):
    task_id = fields.String(required=True)
    task_type = fields.String(required=True)
    output_field = fields.String(required=True)
    input_refs = fields.List(fields.Nested(_InputRef), required=True)
    requirements = fields.List(fields.String(), required=True)
    min_items = fields.Integer(required=True)


class _NotApplicable(_LenientSchema):
    """A section the planner is excusing, and why.

    The justification is required because an unjustified omission is indistinguishable
    from laziness - which is what the first live run produced, twice, with the same
    sentence pasted into both.
    """

    output_field = fields.String(required=True)
    justification = fields.String(required=True)


class PlanningSchema(StrictSchema):
    stage = fields.String(
        required=True,
        validate=validate.OneOf(["documentation", "code_facts", "behavior", "specification"]),
    )
    summary = fields.String(required=True)
    mini_tasks = fields.List(fields.Nested(_MiniTask), required=True)
    not_applicable = fields.List(fields.Nested(_NotApplicable), required=False, load_default=list)
