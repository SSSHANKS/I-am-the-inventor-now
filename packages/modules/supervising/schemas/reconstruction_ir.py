from marshmallow import fields, validate

from packages.modules.supervising.schemas.common import StrictSchema

_CONFIDENCE = ["high", "medium", "low"]
_EVIDENCE_ID = r"^EV-\d{3,}$"
_REQUIREMENT_ID = r"^(?:FR|BR|EH|AC|TC)-\d{3,}$"


class ReconstructionProjectSchema(StrictSchema):
    kind = fields.String(
        required=True,
        validate=validate.OneOf(["library", "cli", "application", "service", "tool", "mixed"]),
    )
    language = fields.String(required=True, validate=validate.Length(min=1))
    runtime_version = fields.String(required=True, allow_none=True)
    build_system = fields.String(required=True, allow_none=True)
    layout = fields.String(required=True, allow_none=True)


class ReconstructionEvidenceSchema(StrictSchema):
    evidence_id = fields.String(required=True, validate=validate.Regexp(_EVIDENCE_ID))
    source_kind = fields.String(
        required=True,
        validate=validate.OneOf(
            ["code", "test", "documentation", "configuration", "packaging", "example"]
        ),
    )
    summary = fields.String(required=True, validate=validate.Length(min=1))
    location = fields.String(required=True, allow_none=True)
    confidence = fields.String(required=True, validate=validate.OneOf(_CONFIDENCE))


class ReconstructionRequirementSchema(StrictSchema):
    requirement_id = fields.String(
        required=True, validate=validate.Regexp(_REQUIREMENT_ID)
    )
    category = fields.String(
        required=True, validate=validate.OneOf(["FR", "BR", "EH", "AC", "TC"])
    )
    statement = fields.String(required=True, validate=validate.Length(min=1))
    evidence_ids = fields.List(
        fields.String(validate=validate.Regexp(_EVIDENCE_ID)),
        required=True,
        validate=validate.Length(min=1),
    )
    confidence = fields.String(required=True, validate=validate.OneOf(_CONFIDENCE))


class ReconstructionModuleSchema(StrictSchema):
    module_id = fields.String(required=True, validate=validate.Regexp(r"^MOD-\d{3,}$"))
    neutral_name = fields.String(required=True, validate=validate.Length(min=1))
    kind = fields.String(
        required=True, validate=validate.OneOf(["package", "module", "namespace"])
    )
    public = fields.Boolean(required=True)
    evidence_ids = fields.List(
        fields.String(validate=validate.Regexp(_EVIDENCE_ID)), required=True
    )


class ReconstructionSymbolSchema(StrictSchema):
    symbol_id = fields.String(required=True, validate=validate.Regexp(r"^SYM-\d{3,}$"))
    module_id = fields.String(required=True, validate=validate.Regexp(r"^MOD-\d{3,}$"))
    neutral_name = fields.String(required=True, validate=validate.Length(min=1))
    kind = fields.String(
        required=True,
        validate=validate.OneOf(
            ["function", "class", "method", "exception", "constant", "attribute", "type"]
        ),
    )
    declaration = fields.String(required=True, validate=validate.Length(min=1))
    visibility = fields.String(
        required=True, validate=validate.OneOf(["public", "internal"])
    )
    requirement_ids = fields.List(
        fields.String(validate=validate.Regexp(_REQUIREMENT_ID)), required=True
    )
    evidence_ids = fields.List(
        fields.String(validate=validate.Regexp(_EVIDENCE_ID)), required=True
    )
    confidence = fields.String(required=True, validate=validate.OneOf(_CONFIDENCE))


class ReconstructionObservationSchema(StrictSchema):
    observation_id = fields.String(
        required=True, validate=validate.Regexp(r"^OBS-\d{3,}$")
    )
    kind = fields.String(
        required=True,
        validate=validate.OneOf(
            [
                "equals",
                "contains",
                "identity",
                "raises",
                "state-equals",
                "count",
                "ordering",
                "exit-code",
                "stdout",
                "stderr",
                "file-effect",
                "boundary-call",
            ]
        ),
    )
    target = fields.String(required=True, validate=validate.Length(min=1))
    value = fields.Raw(required=True, allow_none=True)


class ReconstructionInputSchema(StrictSchema):
    name = fields.String(required=True, validate=validate.Length(min=1))
    value = fields.Raw(required=True, allow_none=True)


class ReconstructionBehaviorSchema(StrictSchema):
    behavior_id = fields.String(required=True, validate=validate.Regexp(r"^BEH-\d{3,}$"))
    kind = fields.String(
        required=True,
        validate=validate.OneOf(
            ["return", "error", "state", "side-effect", "interaction", "lifecycle"]
        ),
    )
    description = fields.String(required=True, validate=validate.Length(min=1))
    requirement_ids = fields.List(
        fields.String(validate=validate.Regexp(_REQUIREMENT_ID)),
        required=True,
        validate=validate.Length(min=1),
    )
    subject_symbol_ids = fields.List(
        fields.String(validate=validate.Regexp(r"^SYM-\d{3,}$")), required=True
    )
    preconditions = fields.List(fields.String(validate=validate.Length(min=1)), required=True)
    inputs = fields.List(fields.Nested(ReconstructionInputSchema), required=True)
    observations = fields.List(
        fields.Nested(ReconstructionObservationSchema),
        required=True,
        validate=validate.Length(min=1),
    )
    evidence_ids = fields.List(
        fields.String(validate=validate.Regexp(_EVIDENCE_ID)),
        required=True,
        validate=validate.Length(min=1),
    )
    confidence = fields.String(required=True, validate=validate.OneOf(_CONFIDENCE))


class ReconstructionEntryPointSchema(StrictSchema):
    entry_point_id = fields.String(
        required=True, validate=validate.Regexp(r"^EP-\d{3,}$")
    )
    kind = fields.String(
        required=True,
        validate=validate.OneOf(["import", "cli", "service", "plugin", "script"]),
    )
    neutral_name = fields.String(required=True, validate=validate.Length(min=1))
    module_id = fields.String(required=True, validate=validate.Regexp(r"^MOD-\d{3,}$"))
    symbol_ids = fields.List(
        fields.String(validate=validate.Regexp(r"^SYM-\d{3,}$")),
        required=True,
        validate=validate.Length(min=1),
    )
    behavior_ids = fields.List(
        fields.String(validate=validate.Regexp(r"^BEH-\d{3,}$")), required=True
    )
    evidence_ids = fields.List(
        fields.String(validate=validate.Regexp(_EVIDENCE_ID)), required=True
    )


class ReconstructionDependencySchema(StrictSchema):
    dependency_id = fields.String(
        required=True, validate=validate.Regexp(r"^DEP-\d{3,}$")
    )
    source_id = fields.String(required=True, validate=validate.Length(min=1))
    target_id = fields.String(required=True, validate=validate.Length(min=1))
    kind = fields.String(
        required=True,
        validate=validate.OneOf(["import", "call", "inherit", "configure", "contain"]),
    )
    evidence_ids = fields.List(
        fields.String(validate=validate.Regexp(_EVIDENCE_ID)), required=True
    )


class ReconstructionConfigurationSchema(StrictSchema):
    configuration_id = fields.String(
        required=True, validate=validate.Regexp(r"^CFG-\d{3,}$")
    )
    neutral_key = fields.String(required=True, validate=validate.Length(min=1))
    value_type = fields.String(required=True, validate=validate.Length(min=1))
    required = fields.Boolean(required=True)
    default = fields.Raw(required=True, allow_none=True)
    precedence = fields.List(fields.String(validate=validate.Length(min=1)), required=True)
    requirement_ids = fields.List(
        fields.String(validate=validate.Regexp(_REQUIREMENT_ID)), required=True
    )
    behavior_ids = fields.List(
        fields.String(validate=validate.Regexp(r"^BEH-\d{3,}$")), required=True
    )
    evidence_ids = fields.List(
        fields.String(validate=validate.Regexp(_EVIDENCE_ID)), required=True
    )


class ReconstructionRuntimeDependencySchema(StrictSchema):
    neutral_name = fields.String(required=True, validate=validate.Length(min=1))
    constraint = fields.String(required=True, allow_none=True)
    required = fields.Boolean(required=True)
    evidence_ids = fields.List(
        fields.String(validate=validate.Regexp(_EVIDENCE_ID)), required=True
    )


class ReconstructionPackagingSchema(StrictSchema):
    distribution_kind = fields.String(
        required=True,
        validate=validate.OneOf(["library", "application", "service", "mixed"]),
    )
    import_roots = fields.List(fields.String(validate=validate.Length(min=1)), required=True)
    build_system = fields.String(required=True, allow_none=True)
    runtime_dependencies = fields.List(
        fields.Nested(ReconstructionRuntimeDependencySchema), required=True
    )
    entry_point_ids = fields.List(
        fields.String(validate=validate.Regexp(r"^EP-\d{3,}$")), required=True
    )
    evidence_ids = fields.List(
        fields.String(validate=validate.Regexp(_EVIDENCE_ID)), required=True
    )


class ReconstructionGapSchema(StrictSchema):
    gap_id = fields.String(required=True, validate=validate.Regexp(r"^GAP-\d{3,}$"))
    description = fields.String(required=True, validate=validate.Length(min=1))
    impact = fields.String(
        required=True,
        validate=validate.OneOf(["blocking", "degraded", "informational"]),
    )
    status = fields.String(
        required=True,
        validate=validate.OneOf(["unresolved", "unverifiable", "excluded"]),
    )
    scope_ids = fields.List(fields.String(validate=validate.Length(min=1)), required=True)
    evidence_ids = fields.List(
        fields.String(validate=validate.Regexp(_EVIDENCE_ID)), required=True
    )


class ReconstructionIRSchema(StrictSchema):
    schema_version = fields.Integer(required=True, validate=validate.Equal(1))
    project = fields.Nested(ReconstructionProjectSchema, required=True)
    evidence = fields.List(
        fields.Nested(ReconstructionEvidenceSchema),
        required=True,
        validate=validate.Length(min=1),
    )
    requirements = fields.List(
        fields.Nested(ReconstructionRequirementSchema),
        required=True,
        validate=validate.Length(min=1),
    )
    modules = fields.List(
        fields.Nested(ReconstructionModuleSchema),
        required=True,
        validate=validate.Length(min=1),
    )
    symbols = fields.List(fields.Nested(ReconstructionSymbolSchema), required=True)
    behaviors = fields.List(fields.Nested(ReconstructionBehaviorSchema), required=True)
    entry_points = fields.List(fields.Nested(ReconstructionEntryPointSchema), required=True)
    dependencies = fields.List(fields.Nested(ReconstructionDependencySchema), required=True)
    configurations = fields.List(
        fields.Nested(ReconstructionConfigurationSchema), required=True
    )
    packaging = fields.Nested(ReconstructionPackagingSchema, required=True)
    gaps = fields.List(fields.Nested(ReconstructionGapSchema), required=True)
