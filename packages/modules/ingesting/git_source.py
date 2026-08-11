"""Ingest a git repository into a private workspace and describe the snapshot."""

import logging
import subprocess
import uuid
from pathlib import Path

from packages.modules.ingesting.base import BaseIngestor
from packages.modules.ingesting.manifest import (
    IngestingError,
    SourceManifest,
    SourceType,
    classify_files,
    remove_tree,
)

log = logging.getLogger(__name__)

GIT_TIMEOUT_SECONDS = 300


class GitRepoIngestor(BaseIngestor):
    """Shallow-clones a repository and reports what is in it.

    Only the newest commit is fetched - the pipeline analyses one snapshot, never a
    history.
    """

    def ingest(self, source: str, branch: str | None = None) -> SourceManifest:
        target = self._clone_target(source)
        log.info("Cloning %s into %s", source, target)

        command = ["git", "clone", "--depth", "1"]
        if branch:
            command += ["--branch", branch]
        command += [source, str(target)]

        try:
            self._run(command)
            commit_hash = self._run(["git", "rev-parse", "HEAD"], cwd=target)
            resolved_branch = self._run(["git", "branch", "--show-current"], cwd=target) or None
            tracked = self._run(["git", "ls-files"], cwd=target).splitlines()
        except subprocess.CalledProcessError as exc:
            remove_tree(target)
            raise IngestingError(
                f"Could not clone or inspect {source}: {(exc.stderr or '').strip()}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            remove_tree(target)
            raise IngestingError(
                f"Timed out after {GIT_TIMEOUT_SECONDS}s cloning {source}"
            ) from exc

        classified = classify_files(tracked)
        manifest = SourceManifest(
            source_type=SourceType.URL_GIT_REPO.value,
            repo_url=source,
            branch=resolved_branch,
            commit_hash=commit_hash,
            repo_local_path=str(target),
            **classified,
        )
        log.info(
            "Ingested %s at %s - %d doc / %d code / %d ignored",
            source,
            (commit_hash or "?")[:8],
            len(manifest.documentation),
            len(manifest.code),
            len(manifest.ignored),
        )
        return manifest

    def _clone_target(self, source: str) -> Path:
        """A fresh directory per run.

        The legacy version always cloned to `temp/<repo-name>`, so a second run hit a
        non-empty directory and git refused. A unique suffix means re-runs never
        collide and two ingests can be in flight at once.
        """
        name = source.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git") or "repository"
        workspace = Path(self.config.workspace_dir)
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace / f"{name}-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _run(command: list[str], cwd: Path | None = None) -> str:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        return result.stdout.strip()
