"""App-wide settings (~/.config/audiomagic/settings.json)."""

import json
import os

from .project import atomic_write_json


def config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "audiomagic")


class Settings:
    DEFAULTS = {"last_project": None, "output": None, "export": {}}

    def __init__(self, path=None):
        self.path = path or os.path.join(config_dir(), "settings.json")
        self.data = dict(self.DEFAULTS)
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass

    def get(self, key):
        return self.data.get(key, self.DEFAULTS.get(key))

    def set(self, key, value):
        self.data[key] = value
        self.save()

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        atomic_write_json(self.path, self.data)
