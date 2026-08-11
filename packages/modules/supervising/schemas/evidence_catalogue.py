"""Schema for the evidence catalogue.

The catalogue is the neutral menu the planner chooses from, so it CROSSES the boundary
(CLAUDE.md section 2) and is validated before storage like every other artifact
(section 6). It was previously never stored at all, which is why a label carrying three
verbatim commands reached the planner with no record anywhere.

`border_review` carries the substitutions the neutralisation filter made. Those notes say
*that* a label was replaced and which rule rejected it, never what the label said - the
notes travel inside a crossing artifact, so quoting the rejected text would reintroduce
exactly what the filter removed.
"""

from marshmallow import fields, validate

from packages.modules.supervising.schemas.common import StrictSchema


class EvidenceCatalogueEntrySchema(StrictSchema):
    """One opaque id and what sits behind it, in neutral words."""

    evidence_id = fields.String(required=True, validate=validate.Regexp(r"^EV-\d+$"))
    kind = fields.String(required=True, validate=validate.Length(min=1))
    about = fields.String(required=True, validate=validate.Length(min=1))


class EvidenceCatalogueSchema(StrictSchema):
    """What the planner is offered, plus the record of what was withheld from it."""

    entries = fields.List(fields.Nested(EvidenceCatalogueEntrySchema), required=True)
    border_review = fields.List(fields.String(), required=True)
