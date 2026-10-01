"""Integrazione con Gemini: generazione card e spiegazione delle risposte.

La chiave si risolve in tre livelli:

1. l'header ``X-Gemini-Key`` della richiesta (dal browser, mai salvata su disco)
2. la variabile d'ambiente ``GEMINI_API_KEY``
3. nessuna chiave: le funzioni restituiscono ``None`` e il chiamante
   ricade sul generatore locale
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

CHARS_PER_CHUNK = 6_000
MAX_CONTEXT_CHARS = 12_000


class GeminiError(Exception):
    """Errore dall'API Gemini o dai parametri ricevuti."""


def _client(api_key: str):
    if not api_key:
        raise GeminiError("Nessuna chiave API disponibile.")
    try:
        from google import genai
    except ImportError as exc:  # pragma: no cover
        raise GeminiError("google-genai non e' installato.") from exc
    return genai.Client(api_key=api_key)


def _extract_json(raw: str) -> Any:
    """Tira fuori il JSON anche se il modello ha aggiunto testo attorno.

    Gemini con response_mime_type dovrebbe restituire JSON puro, ma
    succede che finisca dentro un blocco markdown: meglio essere tolleranti
    che far fallire la generazione.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # ultimo tentativo: dal primo { all'ultimo }
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    raise GeminiError("Il modello non ha restituito JSON valido.")


def chunk_text(text: str, size: int = CHARS_PER_CHUNK) -> list[str]:
    """Divide il testo su confini di parola rispettando i blocchi."""
    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= size:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(paragraph) <= size:
            current = paragraph
        else:
            # paragrafo troppo grande: taglio su confini di parola
            words = paragraph.split()
            buffer: list[str] = []
            for word in words:
                if sum(len(w) + 1 for w in buffer) + len(word) > size and buffer:
                    chunks.append(" ".join(buffer))
                    buffer = []
                buffer.append(word)
            current = " ".join(buffer)
    if current.strip():
        chunks.append(current)
    return chunks


def generate_cards(
    text: str,
    api_key: str,
    model: str = DEFAULT_MODEL,
    max_cards: int = 30,
    source_hint: str = "",
) -> list[dict[str, Any]]:
    """Chiede a Gemini di creare card in italiano. Ritorna lista di dict."""
    if not text.strip():
        return []

    client = _client(api_key)
    chunks = chunk_text(text)
    collected: list[dict[str, Any]] = []

    for index, chunk in enumerate(chunks, start=1):
        prompt = f"""Sei un formatore di materiali didattici. Crea flash card di qualita' in italiano.

Materiale (parte {index} di {len(chunks)}{source_hint}):

\"\"\"
{chunk}
\"\"\"

Regole:
- Ogni card deve verificare un concetto presente nel testo, mai inventato.
- La domanda deve stare in piedi da sola: non usare "questo", "quello", "sopra".
- La risposta deve essere breve e precisa, massimo 40 parole.
- Evita card banali o tautologiche ("Che cos'e' un documento?").
- Varia i tipi: definizioni, cloze sui termini tecnici, elenchi, confronti.
- Se il testo e' in un'altra lingua, riscrivilo in italiano.
- Scegli la difficolta' da 1 (facile) a 5 (difficile) in base a quanto serve
  il testo per rispondere.

Rispondi SOLO con questo JSON, senza testo attorno:
{{"cards": [{{"card_type": "definition|cloze|section|summary", "question": "...", "answer": "...", "difficulty": 3}}]}}"""

        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "temperature": 0.4,
                },
            )
        except Exception as exc:
            raise GeminiError(f"Gemini ha risposto con un errore: {exc}") from exc

        try:
            payload = _extract_json(response.text or "")
        except GeminiError:
            continue

        items = payload.get("cards") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            continue

        for item in items:
            if not isinstance(item, dict):
                continue
            question = str(item.get("question", "")).strip()
            answer = str(item.get("answer", "")).strip()
            if not question or not answer:
                continue
            collected.append(
                {
                    "card_type": _normalise_type(str(item.get("card_type", "definition"))),
                    "question": question,
                    "answer": answer,
                    "difficulty": _clamp_int(item.get("difficulty", 3), 1, 5),
                }
            )
        if len(collected) >= max_cards * 2:
            break

    return collected[: max_cards * 2]


def explain_card(
    question: str,
    answer: str,
    user_answer: str,
    context: str,
    api_key: str,
    model: str = DEFAULT_MODEL,
) -> dict[str, str]:
    """Chiede a Gemini di spiegare la risposta a uno studente."""
    client = _client(api_key)

    graded = user_answer.strip() or "(nessuna risposta data)"
    prompt = f"""Uno studente ripassa una flash card su materiale di studio.

DOMANDA: {question}
RISPOSTA CORRETTA: {answer}
COSA HA DETTO LO STUDENTE: {graded}

MATERIALE DI RIFERIMENTO:
\"\"\"
{context[:MAX_CONTEXT_CHARS]}
\"\"\"

Rispondi in italiano, tono chiaro e diretto, senza preamboli.

Restituisci solo JSON con queste chiavi:
- "esito": "corretta", "parzialmente corretta" o "errata"
- "spiegazione": due o tre frasi che spiegano la risposta corretta e, se serve,
  dicono perche' lo studente ha sbagliato
- "concetto_chiave": la singola cosa da ricordare
- "suggerimento": come ripassare questo argomento, una frase"""

    try:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "temperature": 0.3,
            },
        )
        payload = _extract_json(response.text or "")
    except GeminiError:
        raise
    except Exception as exc:
        raise GeminiError(f"Gemini ha risposto con un errore: {exc}") from exc

    if not isinstance(payload, dict):
        raise GeminiError("Il modello non ha restituito un oggetto JSON.")

    return {
        "esito": str(payload.get("esito", "")).strip(),
        "spiegazione": str(payload.get("spiegazione", "")).strip(),
        "concetto_chiave": str(payload.get("concetto_chiave", "")).strip(),
        "suggerimento": str(payload.get("suggerimento", "")).strip(),
    }


def _normalise_type(value: str) -> str:
    lowered = value.strip().lower()
    aliases = {
        "definition": "definition",
        "definizione": "definition",
        "cloze": "cloze",
        "completion": "cloze",
        "fill": "cloze",
        "section": "section",
        "sezione": "section",
        "slide": "section",
        "summary": "summary",
        "riassunto": "summary",
        "list": "summary",
    }
    return aliases.get(lowered, "definition")


def _clamp_int(value: Any, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return low + (high - low) // 2