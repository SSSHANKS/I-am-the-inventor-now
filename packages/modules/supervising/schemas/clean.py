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


_REQUIREMENT_ID = r"^(?:FR|BR|EH|AC)-\d{3,}$"
_SCENARIO_ID = r"^TC-\d{3,}$"


class CleanProjectProfileSchema(StrictSchema):
    kind = fields.String(
        required=True,
        validate=validate.OneOf(
            ["library", "cli", "application", "service", "tool", "mixed"]
        ),
    )
    language = fields.String(required=True, validate=validate.Length(min=1))
    runtime_version = fields.String(required=True, validate=validate.Length(min=1))
    build_system = fields.String(required=True, validate=validate.Length(min=1))
    layout = fields.String(required=True, validate=validate.Length(min=1))
    compatibility_mode = fields.String(
        required=True,
        validate=validate.OneOf(["drop-in", "renamed"]),
    )


class CleanCapabilitySchema(StrictSchema):
    capability_id = fields.String(
        required=True,
        validate=validate.Regexp(r"^CAP-\d{3,}$"),
    )
    purpose = fields.String(required=True, validate=validate.Length(min=1))
    requirement_ids = fields.List(
        fields.String(validate=validate.Regexp(_REQUIREMENT_ID)),
        required=True,
        validate=validate.Length(min=1),
    )
    scenario_ids = fields.List(
        fields.String(validate=validate.Regexp(_SCENARIO_ID)),
        required=True,
    )
    component_ids = fields.List(
        fields.String(validate=validate.Regexp(r"^CMP-\d{3,}$")),
        required=True,
        validate=validate.Length(min=1),
    )


class CleanComponentSchema(StrictSchema):
    component_id = fields.String(
        required=True,
        validate=validate.Regexp(r"^CMP-\d{3,}$"),
    )
    purpose = fields.String(required=True, validate=validate.Length(min=1))
    kind = fields.String(
        required=True,
        validate=validate.OneOf(
            ["domain", "interface", "adapter", "entrypoint", "configuration"]
        ),
    )
    requirement_ids = fields.List(
        fields.String(validate=validate.Regexp(_REQUIREMENT_ID)),
        required=True,
        validate=validate.Length(min=1),
    )
    depends_on = fields.List(
        fields.String(validate=validate.Regexp(r"^CMP-\d{3,}$")),
        required=True,
    )


class CleanArchitectureContractSchema(StrictSchema):
    contract_id = fields.String(
        required=True,
        validate=validate.Regexp(r"^SYM-\d{3,}$"),
    )
    component_id = fields.String(
        required=True,
        validate=validate.Regexp(r"^CMP-\d{3,}$"),
    )
    qualified_name = fields.String(required=True, validate=validate.Length(min=1))
    kind = fields.String(
        required=True,
        validate=validate.OneOf(
            ["function", "class", "constant", "exception", "decorator", "protocol"]
        ),
    )
    declaration = fields.String(required=True, validate=validate.Length(min=1))
    visibility = fields.String(
        required=True,
        validate=validate.OneOf(["public", "private"]),
    )
    requirement_ids = fields.List(
        fields.String(validate=validate.Regexp(_REQUIREMENT_ID)),
        required=True,
        validate=validate.Length(min=1),
    )


class CleanArchitectureEntryPointSchema(StrictSchema):
    entry_point_id = fields.String(
        required=True,
        validate=validate.Regexp(r"^EP-\d{3,}$"),
    )
    component_id = fields.String(
        required=True,
        validate=validate.Regexp(r"^CMP-\d{3,}$"),
    )
    description = fields.String(required=True, validate=validate.Length(min=1))
    contract_ids = fields.List(
        fields.String(validate=validate.Regexp(r"^SYM-\d{3,}$")),
        required=True,
        validate=validate.Length(min=1),
    )


class CleanDependencyDecisionSchema(StrictSchema):
    name = fields.String(required=True, validate=validate.Length(min=1))
    purpose = fields.String(required=True, validate=validate.Length(min=1))
    required = fields.Boolean(required=True)
    source = fields.String(
        required=True,
        validate=validate.OneOf(
            ["specification", "adapter-baseline", "clean-proposal"]
        ),
    )
    requirement_ids = fields.List(
        fields.String(validate=validate.Regexp(_REQUIREMENT_ID)),
        required=True,
    )


class CleanProposalSchema(StrictSchema):
    proposal_id = fields.String(
        required=True,
        validate=validate.Regexp(r"^PROP-\d{3,}$"),
    )
    field = fields.String(required=True, validate=validate.Length(min=1))
    chosen_value = fields.String(required=True, validate=validate.Length(min=1))
    reason = fields.String(required=True, validate=validate.Length(min=1))
    source = fields.String(
        required=True,
        validate=validate.OneOf(["adapter-baseline", "clean-proposal"]),
    )
    affected_component_ids = fields.List(
        fields.String(validate=validate.Regexp(r"^CMP-\d{3,}$")),
        required=True,
    )
    affects_public_compatibility = fields.Boolean(required=True)


class CleanArchitectureSchema(StrictSchema):
    schema_version = fields.Integer(required=True, validate=validate.Equal(1))
    project_profile = fields.Nested(CleanProjectProfileSchema, required=True)
    capabilities = fields.List(
        fields.Nested(CleanCapabilitySchema),
        required=True,
        validate=validate.Length(min=1),
    )
    components = fields.List(
        fields.Nested(CleanComponentSchema),
        required=True,
        validate=validate.Length(min=1),
    )
    contracts = fields.List(
        fields.Nested(CleanArchitectureContractSchema),
        required=True,
    )
    entry_points = fields.List(
        fields.Nested(CleanArchitectureEntryPointSchema),
        required=True,
    )
    dependency_decisions = fields.List(
        fields.Nested(CleanDependencyDecisionSchema),
        required=True,
    )
    proposals = fields.List(fields.Nested(CleanProposalSchema), required=True)
    unresolved_gaps = fields.List(fields.String(), required=True)


class CleanManifestFileSchema(StrictSchema):
    path = fields.String(required=True, validate=validate.Length(min=1))
    category = fields.String(
        required=True,
        validate=validate.OneOf(
            ["source", "metadata", "configuration", "documentation", "asset"]
        ),
    )
    component_id = fields.String(
        required=True,
        validate=validate.Regexp(r"^CMP-\d{3,}$"),
    )
    purpose = fields.String(required=True, validate=validate.Length(min=1))
    requirement_ids = fields.List(
        fields.String(validate=validate.Regexp(_REQUIREMENT_ID)),
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
    generation_owner = fields.String(
        required=True,
        validate=validate.OneOf(["model", "adapter"]),
    )
    local_validators = fields.List(
        fields.String(validate=validate.Length(min=1)),
        required=True,
        validate=validate.Length(min=1),
    )
    integration_checks = fields.List(
        fields.String(validate=validate.Length(min=1)),
        required=True,
    )


class CleanManifestEntryPointSchema(StrictSchema):
    entry_point_id = fields.String(
        required=True,
        validate=validate.Regexp(r"^EP-\d{3,}$"),
    )
    path = fields.String(required=True, validate=validate.Length(min=1))


class CleanReadinessObligationSchema(StrictSchema):
    obligation_id = fields.String(
        required=True,
        validate=validate.Regexp(r"^READY-\d{3,}$"),
    )
    kind = fields.String(
        required=True,
        validate=validate.OneOf(
            [
                "manifest",
                "syntax",
                "contracts",
                "imports",
                "packaging",
                "build",
                "entry-point",
                "smoke",
            ]
        ),
    )
    description = fields.String(required=True, validate=validate.Length(min=1))
    component_ids = fields.List(
        fields.String(validate=validate.Regexp(r"^CMP-\d{3,}$")),
        required=True,
    )
    paths = fields.List(fields.String(), required=True)
    required = fields.Boolean(required=True)


class CleanManifestSchema(StrictSchema):
    schema_version = fields.Integer(required=True, validate=validate.Equal(1))
    architecture_sha256 = fields.String(
        required=True,
        validate=validate.Regexp(r"^[0-9a-f]{64}$"),
    )
    files = fields.List(
        fields.Nested(CleanManifestFileSchema),
        required=True,
        validate=validate.Length(min=1),
    )
    entry_points = fields.List(
        fields.Nested(CleanManifestEntryPointSchema),
        required=True,
    )
    readiness_obligations = fields.List(
        fields.Nested(CleanReadinessObligationSchema),
        required=True,
        validate=validate.Length(min=1),
    )


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


class CleanBehaviorProbeSchema(StrictSchema):
    probe_id = fields.String(
        required=True,
        validate=validate.Regexp(r"^PROBE-\d{3,}$"),
    )
    capability_id = fields.String(
        required=True,
        validate=validate.Regexp(r"^CAP-\d{3,}$"),
    )
    requirement_ids = fields.List(
        fields.String(validate=validate.Regexp(_REQUIREMENT_ID)),
        required=True,
        validate=validate.Length(min=1),
    )
    scenario_ids = fields.List(
        fields.String(validate=validate.Regexp(_SCENARIO_ID)),
        required=True,
    )
    code = fields.String(required=True, validate=validate.Length(min=1))


class CleanBehaviorProbeSuiteSchema(StrictSchema):
    schema_version = fields.Integer(required=True, validate=validate.Equal(1))
    probes = fields.List(
        fields.Nested(CleanBehaviorProbeSchema),
        required=True,
        validate=validate.Length(min=1),
    )


class CleanDiagnosticSchema(StrictSchema):
    diagnostic_id = fields.String(
        required=True,
        validate=validate.Regexp(r"^D-[0-9A-F]{12}$"),
    )
    stage = fields.String(required=True, validate=validate.Length(min=1))
    check_id = fields.String(required=True, validate=validate.Length(min=1))
    severity = fields.String(
        required=True,
        validate=validate.OneOf(["error", "warning"]),
    )
    message = fields.String(required=True, validate=validate.Length(min=1))
    paths = fields.List(fields.String(), required=True)
    contract_ids = fields.List(fields.String(), required=True)
    requirement_ids = fields.List(fields.String(), required=True)
    expected = fields.String(required=True, allow_none=True)
    actual = fields.String(required=True, allow_none=True)
    repair_hint = fields.String(required=True, allow_none=True)


class CleanCheckSchema(StrictSchema):
    name = fields.String(required=True)
    status = fields.String(required=True, validate=validate.OneOf(["pass", "fail", "skipped"]))
    message = fields.String(required=True)
    paths = fields.List(fields.String(), required=True)
    diagnostics = fields.List(
        fields.Nested(CleanDiagnosticSchema),
        required=False,
    )


class CleanRequirementStatusSchema(StrictSchema):
    requirement_id = fields.String(required=True)
    status = fields.String(
        required=True, validate=validate.OneOf(["satisfied", "partial", "blocked"])
    )
    paths = fields.List(fields.String(), required=True)


class CleanBuildReportSchema(StrictSchema):
    schema_version = fields.Integer(required=True, validate=validate.Equal(4))
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
    repair_stop_reason = fields.String(required=True, allow_none=True)
    planning_mode = fields.String(
        required=True,
        validate=validate.OneOf(["legacy", "project-first"]),
    )
    architecture_sha256 = fields.String(
        required=True,
        allow_none=True,
        validate=validate.Regexp(r"^[0-9a-f]{64}$"),
    )
    manifest_sha256 = fields.String(
        required=True,
        allow_none=True,
        validate=validate.Regexp(r"^[0-9a-f]{64}$"),
    )
    behavior_probe_sha256 = fields.String(
        required=True,
        allow_none=True,
        validate=validate.Regexp(r"^[0-9a-f]{64}$"),
    )
    runtime_adapter = fields.String(
        required=True,
        allow_none=True,
        validate=validate.Length(min=1),
    )
    requirements = fields.List(fields.Nested(CleanRequirementStatusSchema), required=True)
    unresolved_gaps = fields.List(fields.String(), required=True)
