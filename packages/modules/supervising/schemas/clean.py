from marshmallow import ValidationError, fields, validate, validates_schema

from packages.modules.supervising.schemas.common import StrictSchema


class CleanHandoffSchema(StrictSchema):
    schema_version = fields.Integer(required=True, validate=validate.OneOf([1, 2]))
    approval = fields.String(required=True, validate=validate.Equal("border-passed"))
    specification_file = fields.String(required=True, validate=validate.Equal("specification.md"))
    specification_sha256 = fields.String(
        required=True, validate=validate.Regexp(r"^[0-9a-f]{64}$")
    )
    compatibility_mode = fields.String(
        required=False,
        validate=validate.OneOf(["drop-in", "renamed"]),
    )

    @validates_schema
    def validate_version_fields(self, data, **kwargs):
        has_mode = "compatibility_mode" in data
        if data.get("schema_version") == 1 and has_mode:
            raise ValidationError("schema version 1 cannot declare compatibility_mode")
        if data.get("schema_version") == 2 and not has_mode:
            raise ValidationError("schema version 2 requires compatibility_mode")


class CleanDependencySchema(StrictSchema):
    name = fields.String(required=True, validate=validate.Length(min=1))
    purpose = fields.String(required=True, validate=validate.Length(min=1))
    required = fields.Boolean(required=True)


class CleanEntryPointSchema(StrictSchema):
    path = fields.String(required=True, validate=validate.Length(min=1))
    description = fields.String(required=True, validate=validate.Length(min=1))


class CleanRuntimeSchema(StrictSchema):
    language = fields.String(required=True, validate=validate.Length(min=1))
    minimum_version = fields.String(required=True, validate=validate.Length(min=1))


class CleanPackageSchema(StrictSchema):
    import_name = fields.String(required=True, validate=validate.Length(min=1))
    purpose = fields.String(required=True, validate=validate.Length(min=1))


class CleanSymbolContractSchema(StrictSchema):
    symbol_id = fields.String(required=True, validate=validate.Regexp(r"^SYM-\d{3,}$"))
    qualified_name = fields.String(required=True, validate=validate.Length(min=1))
    kind = fields.String(
        required=True,
        validate=validate.OneOf(
            ["function", "class", "constant", "exception", "decorator", "protocol"]
        ),
    )
    signature = fields.String(required=True, validate=validate.Length(min=1))
    visibility = fields.String(
        required=True,
        validate=validate.OneOf(["public", "private"]),
    )
    requirement_ids = fields.List(
        fields.String(validate=validate.Regexp(r"^(?:FR|BR|EH|AC|TC)-\d{3,}$")),
        required=True,
        validate=validate.Length(min=1),
    )


class CleanPlannedFileSchema(StrictSchema):
    path = fields.String(required=True, validate=validate.Length(min=1))
    purpose = fields.String(required=True, validate=validate.Length(min=1))
    requirement_ids = fields.List(
        fields.String(validate=validate.Regexp(r"^(?:FR|BR|EH|AC|TC)-\d{3,}$")),
        required=True,
    )
    provides = fields.List(
        fields.String(validate=validate.Regexp(r"^SYM-\d{3,}$")),
        required=True,
    )
    requires = fields.List(
        fields.String(validate=validate.Regexp(r"^SYM-\d{3,}$")),
        required=True,
    )
    depends_on = fields.List(fields.String(), required=True)
    generation_order = fields.Integer(required=True, validate=validate.Range(min=1))


class CleanPlanSchema(StrictSchema):
    schema_version = fields.Integer(required=True, validate=validate.Equal(2))
    summary = fields.String(required=True, validate=validate.Length(min=1))
    project_kind = fields.String(
        required=True,
        validate=validate.OneOf(["library", "application", "mixed"]),
    )
    runtime = fields.Nested(CleanRuntimeSchema, required=True)
    dependencies = fields.List(fields.Nested(CleanDependencySchema), required=True)
    packages = fields.List(fields.Nested(CleanPackageSchema), required=True)
    entry_points = fields.List(fields.Nested(CleanEntryPointSchema), required=True)
    symbol_contracts = fields.List(fields.Nested(CleanSymbolContractSchema), required=True)
    files = fields.List(
        fields.Nested(CleanPlannedFileSchema),
        required=True,
        validate=validate.Length(min=1),
    )
    validation_strategy = fields.List(fields.String(), required=True)
    open_questions = fields.List(fields.String(), required=True)


class CleanGeneratedFileSchema(StrictSchema):
    path = fields.String(required=True, validate=validate.Length(min=1))
    content = fields.String(required=True)
    requirement_ids = fields.List(fields.String(), required=True)


class CleanFileBatchSchema(StrictSchema):
    files = fields.List(
        fields.Nested(CleanGeneratedFileSchema),
        required=True,
        validate=validate.Length(min=1),
    )
    notes = fields.List(fields.String(), required=True)


class CleanReplacementSchema(StrictSchema):
    path = fields.String(required=True, validate=validate.Length(min=1))
    content = fields.String(required=True)


class CleanRepairSchema(StrictSchema):
    replacements = fields.List(fields.Nested(CleanReplacementSchema), required=True)
    rationale = fields.String(required=True, validate=validate.Length(min=1))


class CleanCheckSchema(StrictSchema):
    name = fields.String(required=True)
    status = fields.String(required=True, validate=validate.OneOf(["pass", "fail", "skipped"]))
    message = fields.String(required=True)
    paths = fields.List(fields.String(), required=True)


class CleanRequirementStatusSchema(StrictSchema):
    requirement_id = fields.String(required=True)
    status = fields.String(
        required=True, validate=validate.OneOf(["satisfied", "partial", "blocked"])
    )
    paths = fields.List(fields.String(), required=True)


class CleanBuildReportSchema(StrictSchema):
    schema_version = fields.Integer(required=True, validate=validate.Equal(3))
    status = fields.String(required=True, validate=validate.OneOf(["success", "partial", "failed"]))
    compatibility_mode = fields.String(
        required=True,
        validate=validate.OneOf(["drop-in", "renamed"]),
    )
    validation_level = fields.String(
        required=True,
        validate=validate.OneOf(["none", "structure", "syntax", "executable"]),
    )
    handoff_sha256 = fields.String(required=True, validate=validate.Regexp(r"^[0-9a-f]{64}$"))
    plan_sha256 = fields.String(required=True, validate=validate.Regexp(r"^[0-9a-f]{64}$"))
    generated_files = fields.Dict(
        keys=fields.String(),
        values=fields.String(validate=validate.Regexp(r"^[0-9a-f]{64}$")),
        required=True,
    )
    checks = fields.List(fields.Nested(CleanCheckSchema), required=True)
    repair_rounds = fields.Integer(required=True, validate=validate.Range(min=0))
    requirements = fields.List(fields.Nested(CleanRequirementStatusSchema), required=True)
    unresolved_gaps = fields.List(fields.String(), required=True)
