"""Loads the single pipeline config and the local .env.

`agents_config.json` is the one source for the agent -> model mapping, the artifacts
location, and the workspace used for cloned analysis targets (CLAUDE.md section 4/6).
Credentials never live here - they come from the environment, which is what litellm
reads underneath AgentProvider.
"""

import difflib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "agents_config.json"
CONFIG_ENV_VAR = "IATIN_CONFIG"

#: Call options we know reach the model. litellm accepts more than this - including
#: provider-specific ones - so an unrecognised key is *not* proof of a mistake, which is
#: why an unknown key warns rather than rejects. A near-miss of a name on this list is a
#: different matter: that is almost certainly a typo, and litellm would silently ignore it,
#: leaving the agent running at settings nobody chose.
RECOGNISED_CALL_OPTIONS = frozenset(
    {
        "temperature",
        "top_p",
        "top_k",
        "max_tokens",
        "max_completion_tokens",
        "timeout",
        "num_retries",
        "response_format",
        "stop",
        "seed",
        "presence_penalty",
        "frequency_penalty",
        "stream",
        "user",
        "metadata",
        "reasoning_effort",
    }
)


class ConfigError(Exception):
    """Raised when the pipeline configuration is missing or unusable."""


@dataclass(frozen=True)
class AgentSettings:
    """How one agent reaches its model. Mirrors one entry under `agents`."""

    name: str
    model: str
    profile: str = "strict_json"
    retry_profile: str | None = None
    max_validation_retries: int = 6
    max_read_file_lines: int = 500

    def profile_path(self, profiles_dir: Path) -> Path:
        return self._resolve(profiles_dir, self.profile)

    def retry_profile_path(self, profiles_dir: Path) -> Path | None:
        """Profile used from the second attempt onwards.

        A repair loop at temperature 0 cannot escape a deterministic mistake: the same
        prompt yields byte-identical output every time. Retries therefore switch to a
        warmer profile so the model can produce a different, valid answer.
        """
        if not self.retry_profile:
            return None
        return self._resolve(profiles_dir, self.retry_profile)

    def _resolve(self, profiles_dir: Path, name: str) -> Path:
        path = profiles_dir / f"{name}.json"
        if not path.is_file():
            raise ConfigError(
                f"Agent {self.name!r} names model profile {name!r}, but {path} does not exist."
            )
        return path


@dataclass(frozen=True)
class Settings:
    """The whole pipeline configuration, already resolved to absolute paths."""

    artifacts_dir: Path
    workspace_dir: Path
    model_profiles_dir: Path
    agents: dict[str, AgentSettings] = field(default_factory=dict)

    def agent(self, name: str) -> AgentSettings:
        try:
            return self.agents[name]
        except KeyError:
            known = ", ".join(sorted(self.agents)) or "<none>"
            raise ConfigError(
                f"No configuration for agent {name!r}. Configured agents: {known}."
            ) from None


def load_settings(path: str | Path | None = None) -> Settings:
    """Read the config file, resolve its paths, and validate the agent mapping."""
    source = Path(path or os.environ.get(CONFIG_ENV_VAR) or DEFAULT_CONFIG_PATH)

    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"Cannot read config at {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in config at {source}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"Config at {source} must be a JSON object.")

    agents_raw = raw.get("agents")
    if not isinstance(agents_raw, dict) or not agents_raw:
        raise ConfigError(f"Config at {source} needs a non-empty 'agents' object.")

    agents: dict[str, AgentSettings] = {}
    for name, entry in agents_raw.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"Agent {name!r} in {source} must map to an object.")
        model = entry.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ConfigError(f"Agent {name!r} in {source} needs a non-empty 'model' string.")
        agents[name] = AgentSettings(
            name=name,
            model=model,
            profile=str(entry.get("profile", "strict_json")),
            retry_profile=(str(entry["retry_profile"]) if entry.get("retry_profile") else None),
            max_validation_retries=int(entry.get("max_validation_retries", 6)),
            max_read_file_lines=int(entry.get("max_read_file_lines", 500)),
        )

    settings = Settings(
        artifacts_dir=_resolve(raw.get("artifacts_dir", "artifacts")),
        workspace_dir=_resolve(raw.get("workspace_dir", "temp")),
        model_profiles_dir=_resolve(raw.get("model_profiles_dir", "config/model_profiles")),
        agents=agents,
    )

    # One pass over every profile an agent names, so a typo surfaces at startup rather
    # than as an agent quietly running at the wrong temperature.
    for agent in settings.agents.values():
        for path in (
            agent.profile_path(settings.model_profiles_dir),
            agent.retry_profile_path(settings.model_profiles_dir),
        ):
            if path is not None:
                check_call_options(path)

    return settings


def check_call_options(profile_path: Path) -> list[str]:
    """Warn about `call_options` keys that look wrong. Returns the warnings raised.

    A misspelled option is silently dropped by litellm, so the agent runs at settings
    nobody chose and nothing says so - `tempreture: 0.0` leaves the model at its default
    temperature with no signal anywhere.

    This warns rather than rejects, because litellm's option surface is open-ended and
    an unrecognised name may be perfectly valid. A name that is one edit away from a
    recognised one gets a pointed suggestion.
    """
    try:
        document = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []  # load_config raises on this later, with a better message

    options = document.get("call_options")
    if not isinstance(options, dict):
        return []

    warnings: list[str] = []
    for key in options:
        if key in RECOGNISED_CALL_OPTIONS:
            continue
        suggestions = difflib.get_close_matches(key, RECOGNISED_CALL_OPTIONS, n=1, cutoff=0.7)
        if suggestions:
            message = (
                f"{profile_path.name}: call option {key!r} is not recognised - "
                f"did you mean {suggestions[0]!r}? litellm will ignore it silently."
            )
        else:
            message = (
                f"{profile_path.name}: call option {key!r} is not one IATIN recognises. "
                "If litellm does not accept it either, it will be ignored silently."
            )
        log.warning(message)
        warnings.append(message)
    return warnings


def load_environment() -> None:
    """Load .env into the process so litellm can find provider credentials.

    AgentProvider deliberately never touches credentials, and litellm reads them
    straight from the environment, so somebody has to do this - it is the caller's
    job, which means ours.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dependency is declared in requirements
        return
    load_dotenv(REPO_ROOT / ".env")


def _resolve(value: Any) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else REPO_ROOT / path
