"""Accesso al database SQLite: schema, connessione e funzioni CRUD."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "flashcards.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id           TEXT PRIMARY KEY,
    filename     TEXT NOT NULL,
    stored_path  TEXT NOT NULL,
    file_type    TEXT NOT NULL,
    size_bytes   INTEGER NOT NULL,
    unit_count   INTEGER DEFAULT 0,
    word_count   INTEGER DEFAULT 0,
    text         TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decks (
    id           TEXT PRIMARY KEY,
    document_id  TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    card_count   INTEGER DEFAULT 0,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cards (
    id            TEXT PRIMARY KEY,
    deck_id       TEXT NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    card_type     TEXT NOT NULL,
    question      TEXT NOT NULL,
    answer        TEXT NOT NULL,
    difficulty    INTEGER DEFAULT 3,
    source_text   TEXT,
    source_page   INTEGER,
    source_kind   TEXT,
    -- colonna FTS per cercare le risposte nel documento
    fts           TEXT,
    -- campi spaced repetition (SM-2)
    ease_factor   REAL DEFAULT 2.5,
    interval_days REAL DEFAULT 0,
    repetitions   INTEGER DEFAULT 0,
    lapses        INTEGER DEFAULT 0,
    due_at        TEXT NOT NULL,
    last_reviewed TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id         TEXT PRIMARY KEY,
    card_id    TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    grade      TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    interval_days REAL,
    ease_factor  REAL
);

CREATE INDEX IF NOT EXISTS idx_cards_deck ON cards(deck_id);
CREATE INDEX IF NOT EXISTS idx_cards_due ON cards(deck_id, due_at);
CREATE INDEX IF NOT EXISTS idx_reviews_card ON reviews(card_id);
CREATE INDEX IF NOT EXISTS idx_decks_document ON decks(document_id);
"""


def now_iso() -> str:
    """Timestamp UTC in formato ISO 8601, compatibile con le stringhe SQLite."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


def connect() -> sqlite3.Connection:
    """Apre una connessione con foreign keys attive e righe come dizionari."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Contesto che fa commit automatico e rollback in caso di errore."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with transaction() as conn:
        conn.executescript(SCHEMA)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


# --- documenti ---

def insert_document(
    filename: str,
    stored_path: str,
    file_type: str,
    size_bytes: int,
    unit_count: int,
    word_count: int,
    text: str,
) -> dict[str, Any]:
    doc_id = new_id()
    with transaction() as conn:
        conn.execute(
            """INSERT INTO documents
               (id, filename, stored_path, file_type, size_bytes,
                unit_count, word_count, text, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                doc_id,
                filename,
                stored_path,
                file_type,
                size_bytes,
                unit_count,
                word_count,
                text,
                now_iso(),
            ),
        )
    return get_document(doc_id)  # type: ignore[return-value]


def get_document(doc_id: str) -> dict[str, Any] | None:
    with transaction() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    return row_to_dict(row)


def list_documents() -> list[dict[str, Any]]:
    with transaction() as conn:
        rows = conn.execute(
            """SELECT id, filename, file_type, size_bytes, unit_count,
                      word_count, created_at
               FROM documents ORDER BY created_at DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def delete_document(doc_id: str) -> str | None:
    """Elimina documento, deck e card collegati. Ritorna il path del file."""
    with transaction() as conn:
        row = conn.execute(
            "SELECT stored_path FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    stored = row["stored_path"]
    # il file puo' gia' non esserci, o essere gia' stato rimosso a mano
    with contextlib.suppress(OSError):
        os.remove(stored)
    return stored


# --- deck ---

def insert_deck(document_id: str, name: str) -> dict[str, Any]:
    deck_id = new_id()
    with transaction() as conn:
        conn.execute(
            "INSERT INTO decks (id, document_id, name, card_count, created_at) VALUES (?, ?, ?, 0, ?)",
            (deck_id, document_id, name, now_iso()),
        )
    return get_deck(deck_id)  # type: ignore[return-value]


def get_deck(deck_id: str) -> dict[str, Any] | None:
    with transaction() as conn:
        row = conn.execute("SELECT * FROM decks WHERE id = ?", (deck_id,)).fetchone()
    return row_to_dict(row)


def list_decks() -> list[dict[str, Any]]:
    with transaction() as conn:
        rows = conn.execute(
            """SELECT d.*, doc.filename AS source_filename
               FROM decks d
               LEFT JOIN documents doc ON doc.id = d.document_id
               ORDER BY d.created_at DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def delete_deck(deck_id: str) -> bool:
    with transaction() as conn:
        cur = conn.execute("DELETE FROM decks WHERE id = ?", (deck_id,))
    return cur.rowcount > 0


# --- card ---

def insert_cards(deck_id: str, cards: Iterable[dict[str, Any]]) -> int:
    """Salva in blocco le card di un deck e aggiorna il contatore."""
    from backend.srs import initial_schedule

    rows = list(cards)
    if not rows:
        return 0

    with transaction() as conn:
        for card in rows:
            conn.execute(
                """INSERT INTO cards
                   (id, deck_id, card_type, question, answer, difficulty,
                    source_text, source_page, source_kind, fts,
                    ease_factor, interval_days, repetitions, lapses,
                    due_at, last_reviewed, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id(),
                    deck_id,
                    card.get("card_type", "cloze"),
                    card["question"],
                    card["answer"],
                    int(card.get("difficulty", 3)),
                    card.get("source_text"),
                    card.get("source_page"),
                    card.get("source_kind"),
                    card.get("answer", ""),
                    initial_schedule()["ease_factor"],
                    initial_schedule()["interval_days"],
                    0,
                    0,
                    now_iso(),
                    None,
                    now_iso(),
                ),
            )
        conn.execute("UPDATE decks SET card_count = ? WHERE id = ?", (len(rows), deck_id))
    return len(rows)


def get_card(card_id: str) -> dict[str, Any] | None:
    with transaction() as conn:
        row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    return row_to_dict(row)


def list_cards(deck_id: str, card_type: str | None = None) -> list[dict[str, Any]]:
    query = "SELECT * FROM cards WHERE deck_id = ?"
    params: list[Any] = [deck_id]
    if card_type and card_type != "all":
        query += " AND card_type = ?"
        params.append(card_type)
    query += " ORDER BY created_at ASC"

    with transaction() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def delete_card(card_id: str) -> bool:
    with transaction() as conn:
        row = conn.execute("SELECT deck_id FROM cards WHERE id = ?", (card_id,)).fetchone()
        if row is None:
            return False
        deck_id = row["deck_id"]
        conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))
        conn.execute(
            "UPDATE decks SET card_count = (SELECT COUNT(*) FROM cards WHERE deck_id = ?) WHERE id = ?",
            (deck_id, deck_id),
        )
    return True


def update_card_schedule(
    card_id: str,
    ease_factor: float,
    interval_days: float,
    repetitions: int,
    lapses: int,
    due_at: str,
) -> dict[str, Any] | None:
    with transaction() as conn:
        conn.execute(
            """UPDATE cards
               SET ease_factor = ?, interval_days = ?, repetitions = ?,
                   lapses = ?, due_at = ?, last_reviewed = ?
               WHERE id = ?""",
            (ease_factor, interval_days, repetitions, lapses, due_at, now_iso(), card_id),
        )
    return get_card(card_id)


def add_review(
    card_id: str,
    grade: str,
    interval_days: float,
    ease_factor: float,
) -> None:
    with transaction() as conn:
        conn.execute(
            """INSERT INTO reviews (id, card_id, grade, reviewed_at, interval_days, ease_factor)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (new_id(), card_id, grade, now_iso(), interval_days, ease_factor),
        )


def due_cards(deck_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Card scadute o mai ripassate, ordinate dalle piu' dimenticate."""
    with transaction() as conn:
        rows = conn.execute(
            """SELECT * FROM cards
               WHERE deck_id = ? AND due_at <= ?
               ORDER BY repetitions ASC, due_at ASC
               LIMIT ?""",
            (deck_id, now_iso(), limit),
        ).fetchall()
    return [dict(r) for r in rows]


def due_count(deck_id: str) -> int:
    with transaction() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM cards WHERE deck_id = ? AND due_at <= ?",
            (deck_id, now_iso()),
        ).fetchone()
    return int(row["n"]) if row else 0


def deck_stats(deck_id: str) -> dict[str, Any]:
    """Statistiche aggregate del deck per la sidebar."""
    with transaction() as conn:
        summary = conn.execute(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(repetitions), 0) AS repetitions,
                      COALESCE(SUM(lapses), 0) AS lapses,
                      COALESCE(AVG(CASE WHEN last_reviewed IS NOT NULL
                                       THEN difficulty END), 0) AS avg_difficulty
               FROM cards WHERE deck_id = ?""",
            (deck_id,),
        ).fetchone()
        graded = conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN grade IN ('giusta','ancora') THEN 1 ELSE 0 END), 0) AS good,
                      COUNT(*) AS total
               FROM reviews WHERE card_id IN (SELECT id FROM cards WHERE deck_id = ?)""",
            (deck_id,),
        ).fetchone()
        weak = conn.execute(
            """SELECT id, question, answer, lapses, due_at
               FROM cards
               WHERE deck_id = ? AND (lapses >= 2 OR repetitions = 0)
               ORDER BY lapses DESC, due_at ASC
               LIMIT 5""",
            (deck_id,),
        ).fetchall()

    total = int(summary["total"]) if summary else 0
    good = int(graded["good"]) if graded else 0
    graded_total = int(graded["total"]) if graded else 0

    return {
        "total_cards": total,
        "due_now": due_count(deck_id),
        "reviews": graded_total,
        "correct": good,
        "accuracy": round(good / graded_total * 100) if graded_total else 0,
        "avg_difficulty": round(float(summary["avg_difficulty"]), 2) if summary else 0,
        "weak_cards": [dict(r) for r in weak] if weak else [],
    }


def get_document_text(document_id: str) -> str:
    """Testo completo del documento, usato come contesto per le spiegazioni."""
    with transaction() as conn:
        row = conn.execute(
            "SELECT text FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
    return str(row["text"]) if row else ""