from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: Built by concatenation so this file never matches its own pattern.
KEY_PATTERN = re.compile("sk-" + r"ant-[A-Za-z0-9_\-]{20,}" + "|" + r"\bre" + r"_[A-Za-z0-9]{6,}_[A-Za-z0-9]{16,}")
SCANNED_SUFFIXES = {".py", ".md", ".json", ".toml", ".txt", ".html", ".js", ".css", ".example", ""}
SKIPPED_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "fixtures"}


def project_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIPPED_DIRS for part in path.parts):
            continue
        if path.name == ".env" or path.suffix not in SCANNED_SUFFIXES:
            continue
        yield path


def test_no_api_keys_in_project_files():
    leaks = [str(p.relative_to(ROOT)) for p in project_files()
             if KEY_PATTERN.search(p.read_text(encoding="utf-8", errors="ignore"))]
    assert leaks == [], f"API key found in: {leaks}"


def test_secrets_are_excluded_from_uploads_and_images():
    for name in (".railwayignore", ".dockerignore", ".gitignore"):
        lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
        assert ".env" in lines, name


def test_railway_config_matches_the_app():
    config = json.loads((ROOT / "railway.json").read_text(encoding="utf-8"))
    assert config["build"]["builder"] == "DOCKERFILE"
    assert config["deploy"]["healthcheckPath"] == "/health"
    assert config["deploy"]["numReplicas"] == 1  # sessions are held in process memory
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "APP_ENV=production" in dockerfile and "--workers 1" in dockerfile and "${PORT}" in dockerfile
