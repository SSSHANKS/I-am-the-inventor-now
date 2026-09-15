from marshmallow import fields, validate

from packages.modules.supervising.schemas.common import StrictSchema

_INVENTORY_KINDS = ["module", "class", "function", "entry_point", "configuration"]
_REPORT_NAMES = ["documentation", "code_facts", "behavior"]
_SOURCE_SCOPES = [
    "production",
    "test",
    "example",
    "documentation",
    "tooling",
    "vendored",
    "unknown",
]
_CONTRACT_ROLES = [
    "public_candidate",
    "protocol",
    "entry_point",
    "configuration",
    "support_module",
    "internal",
    "excluded",
]
_PRIORITIES = ["critical", "high", "medium", "low"]


class ReconstructionInventoryItemSchema(StrictSchema):
    inventory_id = fields.String(
        required=True, validate=validate.Regexp(r"^INV-[0-9A-F]{12}$")
    )
    kind = fields.String(required=True, validate=validate.OneOf(_INVENTORY_KINDS))
    source_path = fields.String(required=True, validate=validate.Length(min=1))
    line_start = fields.Integer(required=True, allow_none=True)
    line_end = fields.Integer(required=True, allow_none=True)
    source_name = fields.String(required=True, allow_none=True)
    qualified_name = fields.String(required=True, allow_none=True)
    signature = fields.String(required=True, allow_none=True)
    evidence_id = fields.String(
        required=True,
        allow_none=True,
        validate=validate.Regexp(r"^EV-\d{3,}$"),
    )
    covered_by = fields.List(
        fields.String(validate=validate.OneOf(_REPORT_NAMES)), required=True
    )
    source_scope = fields.String(
        required=True, validate=validate.OneOf(_SOURCE_SCOPES)
    )
    contract_role = fields.String(
        required=True, validate=validate.OneOf(_CONTRACT_ROLES)
    )
    priority = fields.String(required=True, validate=validate.OneOf(_PRIORITIES))
    reconstruction_target = fields.Boolean(required=True)
    classification_reasons = fields.List(
        fields.String(validate=validate.Length(min=1)),
        required=True,
        validate=validate.Length(min=1),
    )


class ReconstructionInventoryIssueSchema(StrictSchema):
    issue_id = fields.String(
        required=True, validate=validate.Regexp(r"^ISSUE-[0-9A-F]{12}$")
    )
    source_path = fields.String(required=True, validate=validate.Length(min=1))
    issue_type = fields.String(
        required=True, validate=validate.OneOf(["skipped", "index_error"])
    )
    detail = fields.String(required=True, validate=validate.Length(min=1))


class ReconstructionInventoryKindCoverageSchema(StrictSchema):
    total = fields.Integer(required=True, validate=validate.Range(min=0))
    covered = fields.Integer(required=True, validate=validate.Range(min=0))
    uncovered = fields.Integer(required=True, validate=validate.Range(min=0))


class ReconstructionInventoryCoverageSchema(StrictSchema):
    total_count = fields.Integer(required=True, validate=validate.Range(min=0))
    covered_count = fields.Integer(required=True, validate=validate.Range(min=0))
    uncovered_count = fields.Integer(required=True, validate=validate.Range(min=0))
    coverage_percent = fields.Float(
        required=True, validate=validate.Range(min=0, max=100)
    )
    by_kind = fields.Dict(
        keys=fields.String(validate=validate.OneOf(_INVENTORY_KINDS)),
        values=fields.Nested(ReconstructionInventoryKindCoverageSchema),
        required=True,
    )
    by_priority = fields.Dict(
        keys=fields.String(validate=validate.OneOf(_PRIORITIES)),
        values=fields.Nested(ReconstructionInventoryKindCoverageSchema),
        required=True,
    )
    reconstruction_target_count = fields.Integer(
        required=True, validate=validate.Range(min=0)
    )
    covered_reconstruction_target_count = fields.Integer(
        required=True, validate=validate.Range(min=0)
    )
    uncovered_reconstruction_target_count = fields.Integer(
        required=True, validate=validate.Range(min=0)
    )
    reconstruction_target_coverage_percent = fields.Float(
        required=True, validate=validate.Range(min=0, max=100)
    )
    uncovered_inventory_ids = fields.List(
        fields.String(validate=validate.Regexp(r"^INV-[0-9A-F]{12}$")),
        required=True,
    )
    uncovered_reconstruction_target_ids = fields.List(
        fields.String(validate=validate.Regexp(r"^INV-[0-9A-F]{12}$")),
        required=True,
    )
    issue_count = fields.Integer(required=True, validate=validate.Range(min=0))


class ReconstructionInventorySchema(StrictSchema):
    schema_version = fields.Integer(required=True, validate=validate.Equal(2))
    artifact_kind = fields.String(
        required=True, validate=validate.Equal("dirty-reconstruction-inventory")
    )
    items = fields.List(fields.Nested(ReconstructionInventoryItemSchema), required=True)
    issues = fields.List(fields.Nested(ReconstructionInventoryIssueSchema), required=True)
    coverage = fields.Nested(ReconstructionInventoryCoverageSchema, required=True)
