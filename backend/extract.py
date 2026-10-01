"""Estrazione del testo da PDF, DOCX e PPTX con informazioni di provenienza.

Ogni unita' di testo mantiene dove viene dal documento (pagina, slide,
heading) cosi' le card possono mostrare la fonte.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".pptx"}

# magic bytes minimi per distinguere un file dal suo semplice nome
MAGIC_BYTES: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF",),
    ".docx": (b"PK\x03\x04",),
    # anche .docx e .pptx sono archivi zip, quindi la firma coincide
    ".pptx": (b"PK\x03\x04",),
}

CHARS_PER_UNIT = 6_000


class ExtractionError(Exception):
    """Errore leggibile dall'utente quando un file non puo' essere letto."""


def validate_extension(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise ExtractionError(f"Formato non supportato: '{ext}'. Supportati: {allowed}.")
    return ext


def validate_magic_bytes(path: Path, ext: str) -> None:
    """Controlla che il contenuto corrisponda all'estensione."""
    signatures = MAGIC_BYTES.get(ext, ())
    with path.open("rb") as fh:
        header = fh.read(8)
    if signatures and not any(header.startswith(sig) for sig in signatures):
        raise ExtractionError(
            f"Il contenuto del file non corrisponde a un file '{ext}' valido."
        )


def _clean(text: str) -> str:
    """Normalizza spazi, spaziature fisse e trattini copia-incolla."""
    for bad in ("\u00ad", "\u200b", "\u200e", "\u200f", "\ufeff"):
        text = text.replace(bad, "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\t", " ")
    # unione dei trattini spezzati a fine riga
    text = text.replace("-\n", "")
    return "\n".join(" ".join(line.split()) for line in text.split("\n"))


def _reflow(text: str) -> str:
    """Ricollassa le righe spezzate dentro un paragrafo.

    pypdf restituisce il testo a righe: la stessa frase viene spezzata su piu'
    righe e i trattini di fine riga restano dentro le parole ("genet- ic"). Se
    non ricollasso, il generatore locale tratta ogni riga come una frase
    separata e le card nascono mozzate. Una riga che inizia minuscola o senza
    punteggiatura finale continua quasi sempre la precedente.
    """
    # le righe vuote sono confini di paragrafo reali: le conservo come
    # separatore invece di scartarle, altrimenti due paragrafi distinti
    # finiscono uniti nello stesso blocco
    lines = [line.strip() for line in text.split("\n")]
    if len(lines) < 2:
        return "\n".join(line for line in lines if line)

    paragraphs: list[str] = []
    current = ""

    for line in lines:
        if not line:
            if current:
                paragraphs.append(current)
                current = ""
            paragraphs.append("")
            continue

        if not current:
            current = line
            continue

        # la riga corrente chiude una frase e la successiva riparte da un
        # soggetto nuovo: e' un paragrafo diverso
        previous_closed = current.rstrip()[-1:] in ".!?:;"
        starts_new = previous_closed and (
            line[0].isupper() or bool(re.match(r"^[\u2022\u2013\u2014\d]+[.)\s]", line))
        )
        if starts_new:
            paragraphs.append(current)
            current = line
        else:
            current = f"{current} {line}"

    if current:
        paragraphs.append(current)

    return "\n".join(paragraphs)


def _extract_pdf(path: Path) -> tuple[list[dict[str, Any]], int]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise ExtractionError("pypdf non e' installato: pip install -r requirements.txt") from exc

    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:
                raise ExtractionError(
                    "Il PDF e' protetto da password. Rimuovila prima di caricarlo."
                ) from exc

        units: list[dict[str, Any]] = []
        for index, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            text = _reflow(_clean(text))
            if len(text.split()) >= 8:
                units.append(
                    {"text": text, "page": index, "kind": "pdf_page", "title": f"Pagina {index}"}
                )
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(f"Impossibile leggere il PDF: {exc}") from exc

    if not units:
        raise ExtractionError(
            "Nessun testo estraibile dal PDF. Se e' un PDF scansionato "
            "serve un OCR, non e' supportato per ora."
        )
    return units, len(units)


def _docx_heading_level(style_name: str) -> int | None:
    """Estrae il livello da 'Heading 2', 'Titolo 3', 'heading_1' o simili."""
    lowered = style_name.lower()
    for prefix in ("heading", "titolo"):
        if lowered.startswith(prefix):
            digits = "".join(ch for ch in lowered if ch.isdigit())
            if digits:
                return int(digits)
    return None


def _extract_docx(path: Path) -> tuple[list[dict[str, Any]], int]:
    try:
        import docx
    except ImportError as exc:  # pragma: no cover
        raise ExtractionError("python-docx non e' installato") from exc

    try:
        document = docx.Document(str(path))
    except Exception as exc:
        raise ExtractionError(f"Impossibile leggere il .docx: {exc}") from exc

    units: list[dict[str, Any]] = []
    current_heading: str | None = None
    buffer: list[str] = []
    section_index = 0

    def flush() -> None:
        nonlocal buffer, section_index
        body = _clean("\n".join(buffer)).strip()
        buffer = []
        if len(body.split()) < 8:
            return
        section_index += 1
        units.append(
            {
                "text": body,
                "page": section_index,
                "kind": "docx_section",
                "title": current_heading or f"Sezione {section_index}",
            }
        )

    for para in document.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        level = _docx_heading_level(para.style.name if para.style else "")
        if level is not None:
            flush()
            current_heading = text
        else:
            buffer.append(text)

    flush()

    # le tabelle contengono spesso le definizioni piu' utili del documento
    for table in document.tables:
        rows = []
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                rows.append(" | ".join(cells))
        if len(rows) >= 2:
            section_index += 1
            units.append(
                {
                    "text": _clean("\n".join(rows)),
                    "page": section_index,
                    "kind": "docx_table",
                    "title": current_heading or f"Tabella {section_index}",
                }
            )

    if not units:
        raise ExtractionError("Il .docx non contiene testo leggibile.")
    return units, section_index


def _extract_pptx(path: Path) -> tuple[list[dict[str, Any]], int]:
    try:
        from pptx import Presentation
    except ImportError as exc:  # pragma: no cover
        raise ExtractionError("python-pptx non e' installato") from exc

    try:
        presentation = Presentation(str(path))
    except Exception as exc:
        raise ExtractionError(f"Impossibile leggere il .pptx: {exc}") from exc

    units: list[dict[str, Any]] = []
    for index, slide in enumerate(presentation.slides, start=1):
        parts: list[str] = []
        title = ""
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            chunk = shape.text_frame.text.strip()
            if not chunk:
                continue
            if not title and shape.is_placeholder and shape.placeholder_format.idx == 0:
                title = chunk
                continue
            parts.append(chunk)

        # il titolo non entra nel corpo: la card di sezione lo usa come domanda
        notes = ""
        if slide.has_notes_slide:
            notes = (slide.notes_slide.notes_text_frame.text or "").strip()
        if notes:
            parts.append(notes)

        text = _clean("\n".join(parts)).strip()
        if len(text.split()) >= 5:
            units.append(
                {
                    "text": text,
                    "page": index,
                    "kind": "pptx_slide",
                    "title": title.split("\n")[0][:120] or f"Slide {index}",
                }
            )

    if not units:
        raise ExtractionError("Il .pptx non contiene testo leggibile.")
    return units, len(units)


EXTRACTORS: dict[str, Callable[[Path], tuple[list[dict[str, Any]], int]]] = {
    ".pdf": _extract_pdf,
    ".docx": _extract_docx,
    ".pptx": _extract_pptx,
}


def extract(path: Path, ext: str) -> tuple[list[dict[str, Any]], int]:
    """Estrae le unita' di testo. Ritorna (unita', numero unita' originali)."""
    extractor = EXTRACTORS.get(ext)
    if extractor is None:
        raise ExtractionError(f"Nessun estrattore per '{ext}'.")
    return extractor(path)


def join_units(units: list[dict[str, Any]]) -> str:
    """Concatena le unita' inserendo intestazioni di sezione.

    Le intestazioni aiutano sia il generatore locale sia il prompt AI a
    capire la struttura del documento.
    """
    blocks: list[str] = []
    for unit in units:
        title = unit.get("title") or ""
        marker = {
            "pdf_page": "Pagina",
            "docx_section": "Sezione",
            "docx_table": "Tabella",
            "pptx_slide": "Slide",
        }.get(unit.get("kind", ""), "Sezione")
        header = f"\n[{marker} {unit.get('page')}"
        if title and not title.startswith(f"{marker} "):
            header += f": {title}"
        header += "]\n"
        blocks.append(header + unit["text"])
    return "\n\n".join(blocks).strip()