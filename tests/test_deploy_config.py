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
    assert "APP_ENV=production" in (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_start_commands_never_rely_on_shell_expansion():
    """Railway runs start commands without a shell; a literal $PORT crash-loops the container."""
    start = json.loads((ROOT / "railway.json").read_text(encoding="utf-8"))["deploy"]["startCommand"]
    procfile = (ROOT / "Procfile").read_text(encoding="utf-8").strip()
    dockerfile_cmd = next(line for line in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
                          if line.startswith("CMD"))
    assert start == "python -m app.main"
    assert procfile == "web: python -m app.main"
    assert dockerfile_cmd == 'CMD ["python", "-m", "app.main"]'
    for command in (start, procfile, dockerfile_cmd):
        assert "$" not in command


def test_server_reads_port_from_the_environment():
    import pytest

    from app.main import server_options

    platform = server_options({"PORT": "8080"})
    assert (platform["host"], platform["port"], platform["workers"], platform["factory"]) == ("0.0.0.0", 8080, 1, True)
    assert server_options({})["host"] == "127.0.0.1" and server_options({})["port"] == 8000
    assert server_options({"PORT": "9000", "HOST": "127.0.0.1"})["host"] == "127.0.0.1"
    with pytest.raises(SystemExit, match="integer"):
        server_options({"PORT": "$PORT"})
    with pytest.raises(SystemExit, match="between"):
        server_options({"PORT": "70000"})
