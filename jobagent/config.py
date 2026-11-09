"""Load YAML config + environment."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("JOBAGENT_CONFIG_DIR")
                  or (ROOT / "config" if (ROOT / "config").exists() else ROOT / "config.example"))
DATA_DIR = Path(os.environ.get("JOBAGENT_DATA_DIR", ROOT / "data"))
OUTPUT_DIR = DATA_DIR / "applications"
DB_PATH = DATA_DIR / "jobs.db"
BROWSER_PROFILE = Path(
    os.environ.get("JOBAGENT_BROWSER_PROFILE", Path.home() / ".jobagent" / "browser-profile")
)

load_dotenv(ROOT / ".env")

FAST_MODEL = os.environ.get("JOBAGENT_FAST_MODEL", "claude-haiku-5-5")    # scoring
SMART_MODEL = os.environ.get("JOBAGENT_SMART_MODEL", "claude-sonnet-5-5")  # form mapping, writing


def _load(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache
def profile() -> dict:
    return _load("profile.yaml")


@lru_cache
def answers() -> dict:
    return _load("answers.yaml")


@lru_cache
def criteria() -> dict:
    return _load("criteria.yaml")


@lru_cache
def companies() -> list[dict]:
    return [c for c in _load("companies.yaml").get("companies", []) if c.get("enabled", True)]


@lru_cache
def resume_text() -> str:
    path = CONFIG_DIR / "resume.txt"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def variants() -> list[str]:
    return list((profile().get("resume_variants") or {}).keys()) or ["main"]


def resume_path(variant: str | None) -> Path | None:
    p = profile()
    variants = p.get("resume_variants", {})
    rel = variants.get(variant or "") or variants.get(p.get("default_variant", ""))
    if not rel:
        return None
    path = (ROOT / rel).resolve()
    return path if path.exists() else None


def todo_fields(obj=None, prefix="") -> list[str]:
    """List config keys still set to TODO, so the CLI can warn."""
    if obj is None:
        out = []
        for name, data in (("profile", profile()), ("answers", answers())):
            out += todo_fields(data, name)
        return out
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += todo_fields(v, f"{prefix}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += todo_fields(v, f"{prefix}[{i}]")
    elif isinstance(obj, str) and obj.strip().startswith("TODO"):
        out.append(prefix)
    return out
