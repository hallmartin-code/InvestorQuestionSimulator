"""The format-neutral deck model every ingestion path produces."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SourceFormat = Literal["pdf", "pptx"]

#: Below this many characters a page is treated as having no usable text (image-only).
LOW_TEXT_CHARS = 25
#: Share of low-text pages above which the whole deck is treated as scanned.
SCANNED_SHARE = 0.6
#: Upper bound on deck text sent to the model (roughly 40k tokens).
MAX_CONTEXT_CHARS = 150_000


class DeckPage(BaseModel):
    number: int = Field(ge=1)
    title: str | None = None
    text: str = ""
    notes: str | None = None
    tables: list[str] = Field(default_factory=list)

    @property
    def char_count(self) -> int:
        return len(self.text) + sum(len(t) for t in self.tables)

    @property
    def low_text(self) -> bool:
        return self.char_count < LOW_TEXT_CHARS

    def lines(self) -> list[str]:
        blocks = [self.text, *self.tables, self.notes or ""]
        return [ln.strip() for block in blocks for ln in block.split("\n") if ln.strip()]


class DeckDocument(BaseModel):
    filename: str
    source_format: SourceFormat
    pages: list[DeckPage] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    converted_from_ppt: bool = False

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def unit(self) -> str:
        return "Slide" if self.source_format == "pptx" else "Page"

    @property
    def low_text_pages(self) -> list[int]:
        return [p.number for p in self.pages if p.low_text]

    @property
    def is_scanned(self) -> bool:
        if not self.pages:
            return False
        return len(self.low_text_pages) / len(self.pages) >= SCANNED_SHARE

    @property
    def total_chars(self) -> int:
        return sum(p.char_count + len(p.notes or "") for p in self.pages)

    def page(self, number: int) -> DeckPage | None:
        return next((p for p in self.pages if p.number == number), None)

    def page_text(self, number: int) -> str:
        page = self.page(number)
        if page is None:
            return ""
        return "\n".join([page.text, *page.tables, page.notes or ""]).strip()

    def full_text(self) -> str:
        return "\n".join(self.page_text(p.number) for p in self.pages)

    def llm_context(self, *, max_chars: int = MAX_CONTEXT_CHARS) -> tuple[str, list[str]]:
        """Deck text with page markers, plus a warning if it had to be cut to fit."""
        parts: list[str] = []
        used = 0
        warnings: list[str] = []
        for page in self.pages:
            header = f"=== Slide {page.number}" + (f": {page.title}" if page.title else "") + " ==="
            body = self.page_text(page.number) or "[no extractable text on this slide]"
            block = f"{header}\n{body}"
            if used + len(block) > max_chars:
                warnings.append(
                    f"The deck's text exceeds {max_chars:,} characters; only slides 1-{page.number - 1} "
                    "were sent to the AI model. Later slides were used by rule-based extraction only."
                )
                break
            parts.append(block)
            used += len(block)
        return "\n\n".join(parts), warnings
