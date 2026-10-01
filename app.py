"""Flashcard AI: carichi un documento e studia con spaced repetition.

Avvio locale:  python app.py
Produzione:     gunicorn app:app  (vedi Procfile)
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from backend import cards as card_generator
from backend import db, extract, gemini
from backend.srs import GRADE_ANCORA, GRADE_GIUSTA, GRADE_SBAGLIATA, review as srs_review

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "25"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_CARDS_PER_DOCUMENT = 60
DEFAULT_MAX_CARDS = 40

ALLOWED_GRADES = {GRADE_GIUSTA, GRADE_SBAGLIATA, GRADE_ANCORA}

# come vengono mostrate le unita' del documento all'utente
SOURCE_LABELS = {
    "pdf_page": "Pagina",
    "docx_section": "Sezione",
    "docx_table": "Tabella",
    "pptx_slide": "Slide",
}


from flask import Flask, jsonify, render_template, request  # noqa: E402
from werkzeug.exceptions import RequestEntityTooLarge  # noqa: E402
from werkzeug.utils import secure_filename  # noqa: E402


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "sviluppo-locale")
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
    app.config["JSON_AS_ASCII"] = False

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()

    register_routes(app)
    return app


def resolve_api_key() -> str:
    """Header della richiesta prima, poi la variabile d'ambiente."""
    return (request.headers.get("X-Gemini-Key") or os.environ.get("GEMINI_API_KEY") or "").strip()


def model_name() -> str:
    requested = (request.args.get("model") or "").strip()
    return requested or os.environ.get("GEMINI_MODEL", gemini.DEFAULT_MODEL)


def client_has_key() -> bool:
    return bool(resolve_api_key())


def safe_int(value: str | None, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value))) if value is not None else default
    except (TypeError, ValueError):
        return default


def _card_payload(card: dict[str, Any]) -> dict[str, Any]:
    """Rimuove dal JSON i campi interni che il frontend non usa."""
    return {
        "id": card["id"],
        "deck_id": card["deck_id"],
        "card_type": card["card_type"],
        "question": card["question"],
        "answer": card["answer"],
        "difficulty": card["difficulty"],
        "source_text": card.get("source_text"),
        "source_page": card.get("source_page"),
        "source_kind": card.get("source_kind"),
        "ease_factor": card["ease_factor"],
        "interval_days": card["interval_days"],
        "repetitions": card["repetitions"],
        "lapses": card["lapses"],
        "due_at": card["due_at"],
        "last_reviewed": card.get("last_reviewed"),
    }


def register_routes(app: Flask) -> None:
    # --- pagina ---

    @app.get("/")
    def index() -> str:
        return render_template("index.html", has_server_key=bool(os.environ.get("GEMINI_API_KEY")))

    @app.get("/api/health")
    def health() -> Any:
        return jsonify(
            status="ok",
            ai_available=client_has_key(),
            model=os.environ.get("GEMINI_MODEL", gemini.DEFAULT_MODEL),
            max_upload_mb=MAX_UPLOAD_MB,
            storage="filesystem",
        )

    # --- documenti ---

    @app.post("/api/documents")
    def upload_document() -> Any:
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            return jsonify(error="Seleziona un file da caricare."), 400

        original_name = uploaded.filename
        try:
            ext = extract.validate_extension(original_name)
        except extract.ExtractionError as exc:
            return jsonify(error=str(exc)), 400

        stored_path = UPLOAD_DIR / f"{uuid.uuid4().hex}{ext}"
        uploaded.save(stored_path)

        try:
            size_bytes = stored_path.stat().st_size
            if size_bytes == 0:
                raise extract.ExtractionError("Il file e' vuoto.")
            if size_bytes > MAX_UPLOAD_BYTES:
                raise extract.ExtractionError(
                    f"Il file supera il limite di {MAX_UPLOAD_MB} MB."
                )

            extract.validate_magic_bytes(stored_path, ext)
            units, unit_count = extract.extract(stored_path, ext)
            full_text = extract.join_units(units)
            if len(full_text.split()) < 30:
                raise extract.ExtractionError(
                    "Il documento contiene troppo poco testo per creare delle card."
                )
        except extract.ExtractionError as exc:
            stored_path.unlink(missing_ok=True)
            return jsonify(error=str(exc)), 400
        except Exception as exc:
            stored_path.unlink(missing_ok=True)
            return jsonify(error=f"Errore imprevisto durante la lettura: {exc}"), 500

        document = db.insert_document(
            filename=secure_filename(original_name) or original_name,
            stored_path=str(stored_path),
            file_type=ext.lstrip("."),
            size_bytes=size_bytes,
            unit_count=unit_count,
            word_count=len(full_text.split()),
            text=full_text,
        )

        # il mazzo nasce solo quando l'utente sceglie le card, cosi' in
        # sidebar non compaiono deck vuoti da documenti appena caricati
        return jsonify(
            document={
                "id": document["id"],
                "filename": document["filename"],
                "file_type": document["file_type"],
                "size_bytes": document["size_bytes"],
                "unit_count": document["unit_count"],
                "word_count": document["word_count"],
            },
        ), 201

    @app.get("/api/documents")
    def list_documents() -> Any:
        return jsonify(documents=db.list_documents())

    @app.delete("/api/documents/<doc_id>")
    def delete_document(doc_id: str) -> Any:
        removed = db.delete_document(doc_id)
        if removed is None:
            return jsonify(error="Documento non trovato."), 404
        return jsonify(ok=True)

    # --- generazione card ---

    @app.post("/api/documents/<doc_id>/cards")
    def create_cards(doc_id: str) -> Any:
        document = db.get_document(doc_id)
        if document is None:
            return jsonify(error="Documento non trovato."), 404

        payload = request.get_json(silent=True) or {}
        max_cards = safe_int(
            str(payload.get("max_cards", DEFAULT_MAX_CARDS)), DEFAULT_MAX_CARDS, 5, MAX_CARDS_PER_DOCUMENT
        )
        requested_types = payload.get("types")
        types = (
            [t for t in requested_types if t in card_generator.CARD_TYPES]
            if isinstance(requested_types, list)
            else None
        )
        # le card AI portano anche source_kind, dal momento che il generatore
        # locale lavora su unita' gia' divise mentre il testo e' un blocco solo
        source_kind = _infer_kind(document["file_type"])

        api_key = resolve_api_key()
        generator_used = "locale"
        generated: list[dict[str, Any]] = []

        if api_key:
            try:
                generated = gemini.generate_cards(
                    document["text"],
                    api_key=api_key,
                    model=model_name(),
                    max_cards=max_cards,
                    source_hint=f"Formato originale: {document['filename']}.",
                )
                generator_used = "gemini" if generated else "locale"
            except gemini.GeminiError:
                # se Gemini fallisce la generazione locale prende il posto
                generated = []

        if generator_used == "locale":
            units = _units_for(document)
            if not units:
                return jsonify(error="Impossibile rileggere il documento salvato."), 500
            generated = card_generator.generate_cards(units, max_cards=max_cards, types=types)

        for card in generated:
            card.setdefault("source_kind", source_kind)

        # le card tornano solo come anteprima: l'utente sceglie e poi
        # il frontend le salva con POST /api/documents/<id>/decks
        return jsonify(
            document_id=document["id"],
            cards=generated,
            generator=generator_used,
            model=model_name() if generator_used == "gemini" else None,
        )

    @app.post("/api/documents/<doc_id>/decks")
    def save_selected_cards(doc_id: str) -> Any:
        document = db.get_document(doc_id)
        if document is None:
            return jsonify(error="Documento non trovato."), 404

        payload = request.get_json(silent=True) or {}
        selected = payload.get("cards")
        if not isinstance(selected, list) or not selected:
            return jsonify(error="Nessuna card selezionata."), 400

        clean = [
            {
                "card_type": card.get("card_type", "definition"),
                "question": str(card.get("question", "")).strip(),
                "answer": str(card.get("answer", "")).strip(),
                "difficulty": safe_int(str(card.get("difficulty", 3)), 3, 1, 5),
                "source_text": card.get("source_text"),
                "source_page": card.get("source_page"),
                "source_kind": card.get("source_kind"),
            }
            for card in selected
            if isinstance(card, dict)
            and str(card.get("question", "")).strip()
            and str(card.get("answer", "")).strip()
        ][:MAX_CARDS_PER_DOCUMENT]

        if not clean:
            return jsonify(error="Nessuna card valida da salvare."), 400

        deck = db.insert_deck(doc_id, str(payload.get("name") or document["filename"])[:120])
        count = db.insert_cards(deck["id"], clean)
        return jsonify(deck=deck, card_count=count), 201

    @app.delete("/api/decks/<deck_id>")
    def remove_deck(deck_id: str) -> Any:
        if not db.delete_deck(deck_id):
            return jsonify(error="Deck non trovato."), 404
        return jsonify(ok=True)

    # --- studio ---

    @app.get("/api/decks")
    def list_decks() -> Any:
        decks = db.list_decks()
        for deck in decks:
            deck["due_now"] = db.due_count(deck["id"])
        return jsonify(decks=decks)

    @app.get("/api/decks/<deck_id>")
    def get_deck(deck_id: str) -> Any:
        deck = db.get_deck(deck_id)
        if deck is None:
            return jsonify(error="Deck non trovato."), 404
        return jsonify(deck=deck, stats=db.deck_stats(deck_id))

    @app.get("/api/decks/<deck_id>/cards")
    def list_cards(deck_id: str) -> Any:
        if db.get_deck(deck_id) is None:
            return jsonify(error="Deck non trovato."), 404
        card_type = request.args.get("type")
        cards = db.list_cards(deck_id, card_type)
        return jsonify(cards=[_card_payload(c) for c in cards])

    @app.get("/api/decks/<deck_id>/next")
    def next_card(deck_id: str) -> Any:
        if db.get_deck(deck_id) is None:
            return jsonify(error="Deck non trovato."), 404
        card_type = request.args.get("type")
        queue = db.due_cards(deck_id, limit=50)
        if card_type and card_type != "all":
            queue = [c for c in queue if c["card_type"] == card_type]
        if not queue:
            return jsonify(card=None, done=True)
        return jsonify(card=_card_payload(queue[0]), done=False)

    @app.post("/api/cards/<card_id>/review")
    def review_card(card_id: str) -> Any:
        card = db.get_card(card_id)
        if card is None:
            return jsonify(error="Card non trovata."), 404

        payload = request.get_json(silent=True) or {}
        grade = str(payload.get("grade", "")).strip().lower()
        if grade not in ALLOWED_GRADES:
            return jsonify(error=f"Giudizio non valido. Usa: {', '.join(sorted(ALLOWED_GRADES))}."), 400

        schedule = srs_review(card, grade, card.get("difficulty"))
        updated = db.update_card_schedule(
            card_id,
            ease_factor=float(schedule["ease_factor"]),
            interval_days=float(schedule["interval_days"]),
            repetitions=int(schedule["repetitions"]),
            lapses=int(schedule["lapses"]),
            due_at=str(schedule["due_at"]),
        )
        db.add_review(
            card_id, grade, float(schedule["interval_days"]), float(schedule["ease_factor"])
        )

        return jsonify(
            card=_card_payload(updated) if updated else None,
            stats=db.deck_stats(card["deck_id"]),
            due_now=db.due_count(card["deck_id"]),
        )

    @app.delete("/api/cards/<card_id>")
    def remove_card(card_id: str) -> Any:
        if not db.delete_card(card_id):
            return jsonify(error="Card non trovata."), 404
        return jsonify(ok=True)

    # --- spiegazione ---

    @app.post("/api/cards/<card_id>/explain")
    def explain_card(card_id: str) -> Any:
        card = db.get_card(card_id)
        if card is None:
            return jsonify(error="Card non trovata."), 404

        deck = db.get_deck(card["deck_id"])
        document = db.get_document(deck["document_id"]) if deck else None

        payload = request.get_json(silent=True) or {}
        user_answer = str(payload.get("user_answer", "")).strip()

        # contesto: la frase sorgente, poi il testo del documento come riserva
        context_parts = []
        if card.get("source_text"):
            context_parts.append(
                f"[{SOURCE_LABELS.get(card.get('source_kind', ''), 'Fonte')} "
                f"{card.get('source_page', '?')}]\n{card['source_text']}"
            )
        if document:
            context_parts.append(document["text"])
        context = "\n\n".join(context_parts)

        glossary_entries = card_generator.glossary(context)

        api_key = resolve_api_key()
        if not api_key:
            # modalita' offline: mostriamo la fonte, che e' comunque utile
            return jsonify(
                mode="fonte",
                message=(
                    "Nessuna chiave API attiva: mostro la frase del documento da cui "
                    "nasce questa card."
                ),
                source_text=card.get("source_text"),
                source_page=card.get("source_page"),
                source_kind=card.get("source_kind"),
                source_label=SOURCE_LABELS.get(card.get("source_kind", ""), "Fonte"),
                glossary=card_generator.terms_in_card(card, glossary_entries),
            )

        try:
            explanation = gemini.explain_card(
                question=card["question"],
                answer=card["answer"],
                user_answer=user_answer,
                context=context,
                api_key=api_key,
                model=model_name(),
            )
        except gemini.GeminiError as exc:
            return jsonify(
                mode="fonte",
                message=f"Spiegazione AI non disponibile: {exc}",
                source_text=card.get("source_text"),
                source_page=card.get("source_page"),
                source_kind=card.get("source_kind"),
                source_label=SOURCE_LABELS.get(card.get("source_kind", ""), "Fonte"),
                glossary=card_generator.terms_in_card(card, glossary_entries),
                error=True,
            )

        return jsonify(
            mode="ai",
            model=model_name(),
            **explanation,
            source_page=card.get("source_page"),
            source_kind=card.get("source_kind"),
            source_label=SOURCE_LABELS.get(card.get("source_kind", ""), "Fonte"),
            source_text=card.get("source_text"),
            glossary=glossary_entries[:4],
        )

    # --- errori ---

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_: RequestEntityTooLarge) -> Any:
        return jsonify(error=f"Il file supera il limite di {MAX_UPLOAD_MB} MB."), 413

    @app.errorhandler(404)
    def not_found(_: Any) -> Any:
        return jsonify(error="Risorsa non trovata."), 404

    @app.errorhandler(500)
    def server_error(exc: Exception) -> Any:
        app.logger.exception("Errore interno: %s", exc)
        return jsonify(error="Errore interno del server."), 500


def _units_for(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Rilegge le unita' dal file salvato, con fallback sul testo in memoria."""
    try:
        units, _ = extract.extract(Path(document["stored_path"]), f".{document['file_type']}")
        if units:
            return units
    except (extract.ExtractionError, OSError):
        pass
    # il file non e' piu' sul disco: ricostruisco da una unita' singola
    return [
        {
            "text": document["text"],
            "page": 1,
            "kind": _infer_kind(document["file_type"]),
            "title": document["filename"],
        }
    ]


def _infer_kind(file_type: str) -> str:
    return {
        "pdf": "pdf_page",
        "docx": "docx_section",
        "pptx": "pptx_slide",
    }.get(file_type, "pdf_page")


app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    print(f"\n  Flashcard AI su http://127.0.0.1:{port}")
    print(f"  Chiave API: {'dal .env' if os.environ.get('GEMINI_API_KEY') else 'non configurata, solo generatore locale'}\n")
    app.run(host="127.0.0.1", port=port, debug=debug)