"""Upload validation for decks: size, extension, MIME type, magic bytes and archive sanity.

The extension is never trusted on its own. A file is accepted only when its magic bytes agree
with an allowed extension and the declared MIME type is consistent with both. PPTX archives
are inspected for structure and decompressed size before python-pptx ever opens them.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.errors import UnsupportedFormatError, UploadRejectedError
from app.utils.security import normalize_mime, sanitize_filename

DeckFormat = Literal["pdf", "pptx", "ppt"]

PDF_MAGIC = b"%PDF"
ZIP_MAGIC = b"PK\x03\x04"
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # legacy Office binary / encrypted OOXML

ALLOWED_EXTENSIONS: dict[str, DeckFormat] = {".pdf": "pdf", ".pptx": "pptx", ".ppt": "ppt"}

ALLOWED_MIME_TYPES: dict[DeckFormat, frozenset[str]] = {
    "pdf": frozenset({"application/pdf", "application/x-pdf"}),
    "pptx": frozenset({
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/zip", "application/x-zip-compressed",
    }),
    "ppt": frozenset({
        "application/vnd.ms-powerpoint", "application/mspowerpoint", "application/powerpoint",
        "application/x-mspowerpoint",
    }),
}
#: Browsers send these when they do not know the type; the magic bytes decide instead.
GENERIC_MIME_TYPES = frozenset({"", "application/octet-stream", "binary/octet-stream"})

#: Decompressed-size ceiling for PPTX archives (zip-bomb guard).
MAX_PPTX_UNCOMPRESSED = 512 * 1024 * 1024
MAX_PPTX_MEMBERS = 10_000


@dataclass(frozen=True)
class ValidatedUpload:
    safe_name: str
    format: DeckFormat
    size_bytes: int


def sniff_format(head: bytes) -> DeckFormat | None:
    """Identify a deck from its first bytes, ignoring the extension."""
    if head.startswith(PDF_MAGIC) or b"%PDF-" in head[:1024]:
        return "pdf"
    if head.startswith(ZIP_MAGIC):
        return "pptx"
    if head.startswith(OLE_MAGIC):
        return "ppt"
    return None


def check_mime(content_type: str | None, fmt: DeckFormat, safe_name: str) -> None:
    mime = normalize_mime(content_type)
    if mime in GENERIC_MIME_TYPES or mime in ALLOWED_MIME_TYPES[fmt]:
        return
    raise UnsupportedFormatError(
        f"{safe_name} was sent as '{mime}', which does not match its {fmt.upper()} contents. "
        "Upload the original PDF or PowerPoint file."
    )


def check_pptx_archive(data: bytes) -> None:
    """Reject ZIPs that are not PowerPoint packages or that decompress implausibly large."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            names = {m.filename for m in members}
    except zipfile.BadZipFile as exc:
        raise UploadRejectedError(
            "The file has a PowerPoint extension but is not a valid PPTX archive. "
            "It may be corrupt or truncated; re-export it from PowerPoint or upload a PDF."
        ) from exc
    if len(members) > MAX_PPTX_MEMBERS:
        raise UploadRejectedError("The PPTX archive contains an implausible number of parts and was rejected.")
    if sum(m.file_size for m in members) > MAX_PPTX_UNCOMPRESSED:
        raise UploadRejectedError("The PPTX archive decompresses to more than 512 MB and was rejected as unsafe.")
    if "ppt/presentation.xml" not in names:
        raise UnsupportedFormatError(
            "The archive is not a PowerPoint presentation (no ppt/presentation.xml). "
            "Word, Excel and other archives are not supported."
        )


def validate_deck_upload(filename: str | None, data: bytes, *, content_type: str | None,
                         max_bytes: int) -> ValidatedUpload:
    """Validate an uploaded deck; raise an actionable error or return its safe identity."""
    safe_name = sanitize_filename(filename)
    if not data:
        raise UploadRejectedError(f"{safe_name} is empty (0 bytes).")
    if len(data) > max_bytes:
        raise UploadRejectedError(
            f"{safe_name} is {len(data) / 1_048_576:.1f} MB, above the {max_bytes / 1_048_576:.0f} MB "
            "upload limit. Compress images or export a lighter PDF."
        )
    extension = Path(safe_name).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise UnsupportedFormatError(
            f"{safe_name}: unsupported file type '{extension or 'none'}'. "
            "Upload a PDF (.pdf) or PowerPoint (.pptx, .ppt) deck."
        )
    sniffed = sniff_format(data[:1024])
    if sniffed is None:
        raise UnsupportedFormatError(
            f"{safe_name} is not a readable PDF or PowerPoint file (unrecognized contents). "
            "The file may be corrupt; re-export it and try again."
        )
    expected = ALLOWED_EXTENSIONS[extension]
    if sniffed != expected:
        if expected == "pptx" and sniffed == "ppt":
            raise UnsupportedFormatError(
                f"{safe_name} looks password-protected or is a legacy .ppt saved with a .pptx extension. "
                "Remove the password and save as .pptx, or export to PDF."
            )
        raise UnsupportedFormatError(
            f"{safe_name} has a {extension} extension but its contents are {sniffed.upper()}. "
            "Rename or re-export the file so the extension matches its format."
        )
    check_mime(content_type, sniffed, safe_name)
    if sniffed == "pptx":
        check_pptx_archive(data)
    return ValidatedUpload(safe_name=safe_name, format=sniffed, size_bytes=len(data))
