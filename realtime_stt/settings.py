import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class AppSettings:
    """Small human-readable settings file for runtime UI preferences."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return {str(key): str(value) for key, value in data.items()}
        except (OSError, ValueError, TypeError):
            logger.exception("Could not read settings: %s", self.path)
            return {}

    def set(self, key: str, value: str) -> None:
        data = self.load()
        data[key] = value
        self.path.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
