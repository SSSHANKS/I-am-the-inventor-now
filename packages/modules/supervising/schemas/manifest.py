"""Schemas for the ingestion manifest.

CLAUDE.md section 6 requires every artifact to pass a schema before it is stored. The
manifest had none, so a malformed snapshot description could reach disk and every
later stage would build on it.

There are two schemas because there are two manifests:

- `ManifestSchema` validates the DIRTY-SIDE manifest, which names the real repository.
- `NeutralManifestSchema` validates the version that CROSSES to the clean team. It has
  no URL and no original paths - it describes the project to be built, not the original
  (CLAUDE.md section 2).
"""

from marshmallow import fields, validate

from packages.modules.supervising.schemas.common import StrictSchema

SOURCE_TYPES = ["url_git_repo"]


class ManifestSchema(StrictSchema):
    """Dirty-side snapshot description. Never crosses the boundary."""

    source_type = fields.String(required=True, validate=validate.OneOf(SOURCE_TYPES))
    repo_url = fields.String(required=True)
    branch = fields.String(required=True, allow_none=True)
    commit_hash = fields.String(required=True, allow_none=True)
    repo_local_path = fields.String(required=True, allow_none=True)
    documentation = fields.List(fields.String(), required=True)
    code = fields.List(fields.String(), required=True)
    ignored = fields.List(fields.String(), required=True)


class NeutralManifestSchema(StrictSchema):
    """What the clean team receives instead.

    `project_name` is invented. Counts survive because "there were 12 code units" is a
    fact about scale, not an identifier. No URL, no commit, no original paths.
    """

    project_name = fields.String(required=True)
    summary = fields.String(required=True)
    documentation_unit_count = fields.Integer(required=True, validate=validate.Range(min=0))
    code_unit_count = fields.Integer(required=True, validate=validate.Range(min=0))
    border_review = fields.List(fields.String(), required=True)
