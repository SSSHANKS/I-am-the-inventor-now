from marshmallow import fields

from packages.modules.supervising.schemas.common import StrictSchema


class _SpecMarkdownItem(StrictSchema):
    source_ref = fields.Raw(required=False, allow_none=True)
    heading = fields.String(required=True)
    markdown = fields.String(required=True)


class SpecNarrowMarkdownSchema(StrictSchema):
    items = fields.List(fields.Nested(_SpecMarkdownItem), required=True)
