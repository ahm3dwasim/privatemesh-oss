"""Settings from the environment (or a local .env). No secret ever has a default."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


#: The env file `Settings.load()` reads when the caller names none. It is read from the environment
#: rather than fixed to `.env` for one reason: a process that must NOT inherit the operator's
#: credentials needs a way to say so. The test suite is that process — run from the owner's own
#: checkout it used to load the real `.env` through `os.environ.setdefault`, which put real provider
#: keys into every test in the run, for the rest of the run. That is how the demo tests came to fail
#: in the owner's tree and pass everywhere else: demo mode correctly refuses to start beside a real
#: credential, and the suite had quietly supplied one. Point this at a path that does not exist and
#: nothing is loaded.
ENV_FILE_VAR = "PM_ENV_FILE"


def _load_dotenv(path: Path | None = None) -> None:
    path = Path(os.environ.get(ENV_FILE_VAR, ".env")) if path is None else path
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    model_fast: str
    model_reason: str
    model_deep: str
    tavily_key: str
    # Comma-separated browser origins allowed to call the API (the web app's origin). Empty means
    # the local development pair; it is not a wildcard, because this API spends the owner's money
    # and reads the owner's memories.
    web_origins: str = ""

    @classmethod
    def load(cls) -> "Settings":
        _load_dotenv()
        e = os.environ.get
        return cls(
            api_key=e("NEBIUS_API_KEY", ""),
            base_url=e("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/"),
            model_fast=e("PM_MODEL_FAST", ""),
            model_reason=e("PM_MODEL_REASON", ""),
            model_deep=e("PM_MODEL_DEEP", ""),
            tavily_key=e("TAVILY_API_KEY", ""),
            web_origins=e("PM_WEB_ORIGINS", ""),
        )
