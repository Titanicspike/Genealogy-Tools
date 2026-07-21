"""Credential loading for the scrapers.

Values live in a .env file at the project root, which is gitignored. See
.env.example for the keys each scraper expects.

Call require() from inside the scraper's entry point rather than at module
level: tasks.py imports every scraper at startup, so raising on import would
stop the whole app from booting for anyone who only uses file uploads.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

# override=False so a real environment variable wins over the file, which lets
# you override a single credential from the shell without editing .env.
load_dotenv(ENV_PATH, override=False)


def require(name: str) -> str:
    """Return the named credential, or explain what is missing."""
    value = os.environ.get(name, "").strip()

    if not value:
        raise RuntimeError(
            f"Missing credential {name}. Copy .env.example to .env and fill in "
            f"{name} (see the Setup section of README.md). Looked for "
            f"{ENV_PATH}."
        )

    return value
