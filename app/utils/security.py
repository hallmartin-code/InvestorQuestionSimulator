"""Security primitives: filename sanitization, secret comparison and private temp workspaces."""

from __future__ import annotations

import contextlib
import hmac
import re
import secrets
import shutil
import tempfile
import time
import unicodedata
from collections.abc import Iterator
from pathlib import Path

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_filename(name: str | None, *, max_length: int = 100) -> str:
    """Strip directories, control and non-ASCII characters from an uploaded filename.

    The result is used only for display and messages; files on disk get fixed names.
    """
    base = (name or "").replace("\\", "/").split("/")[-1]
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode("ascii")
    base = _UNSAFE_FILENAME.sub("_", base).strip("._")
    if not base:
        base = "upload"
    if len(base) > max_length:
        stem, dot, ext = base.rpartition(".")
        if dot and len(ext) <= 8:
            base = stem[: max_length - len(ext) - 1] + "." + ext
        else:
            base = base[:max_length]
    return base


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def tokens_equal(a: str | None, b: str | None) -> bool:
    """Constant-time comparison; ``None`` never matches."""
    if a is None or b is None:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def normalize_mime(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


@contextlib.contextmanager
def private_workspace(root: Path, *, prefix: str = "deck-") -> Iterator[Path]:
    """A randomly named, owner-only directory under ``root`` that is always deleted."""
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=root))
    try:
        try:
            path.chmod(0o700)
        except OSError:  # pragma: no cover
            pass
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def purge_stale_files(root: Path, max_age_seconds: float) -> int:
    """Delete entries in ``root`` older than ``max_age_seconds`` (orphans from crashed requests)."""
    if not root.is_dir():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for entry in root.iterdir():
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue
    return removed
