"""Runtime configuration, read from environment variables (and an optional .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SOLUTION_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMAS_DIR = SOLUTION_DIR.parent / "assignment-a-data" / "schemas"

# Optional: path of the env file to load instead of ``solution/.env`` (used by tests to isolate runs).
ENV_FILE_VARIABLE = "LEDGER_ENV_FILE"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def resolve_env_file(path: Path | None = None) -> Path:
    """Env file to load: explicit ``path``, else ``$LEDGER_ENV_FILE``, else ``solution/.env``."""
    if path is not None:
        return path
    override = os.environ.get(ENV_FILE_VARIABLE, "").strip()
    return Path(override) if override else SOLUTION_DIR / ".env"


def load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from the env file without overriding real env vars.

    A missing file is not an error (nothing is loaded).
    """
    env_file = resolve_env_file(path)
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    database_url: str
    encryption_keys: tuple[str, ...]
    hmac_key: str
    schemas_dir: Path

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        database_url = os.environ.get("DATABASE_URL", "").strip()
        if not database_url:
            raise ConfigError("DATABASE_URL is not set (see .env.example)")
        raw_keys = os.environ.get("PII_ENCRYPTION_KEY", "").strip()
        keys = tuple(k.strip() for k in raw_keys.split(",") if k.strip())
        if not keys:
            raise ConfigError("PII_ENCRYPTION_KEY is not set (see .env.example)")
        hmac_key = os.environ.get("PII_HMAC_KEY", "").strip()
        if len(hmac_key) < 32:
            raise ConfigError("PII_HMAC_KEY must be set and at least 32 characters long")
        schemas_dir = Path(os.environ.get("SCHEMAS_DIR") or DEFAULT_SCHEMAS_DIR)
        return cls(database_url, keys, hmac_key, schemas_dir)