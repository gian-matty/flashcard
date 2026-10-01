"""Generatore locale di flash card, senza alcuna chiamata di rete.

Quattro strategie complementari:

- ``cloze``       i termini piu' caratteristici del documento diventano il buco
- ``definition``  le frasi che definiscono un termine (X e Y, X: Y, ...)
- ``section``     cosa contiene una sezione, slide o heading
- ``summary``     i punti principali, con punteggio estrattivo

La selezione dei termini usa TF-IDF calcolato in casa con ``Counter`` e
``math.log``: niente scikit-learn, niente modelli addestrati.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable
from typing import Any

# lunghezza minima e massima, in parole, per una card usabile
MIN_WORDS = 4
MAX_WORDS = 42
MIN_ANSWER_WORDS = 1
MAX_ANSWER_WORDS = 60

CARD_TYPES = ("cloze", "definition", "section", "summary")

# parole vuote italiane e inglesi, unite alle virgolette perche' contengono
# spaziature e accenti. Servono a non scegliere come buco "della" o "the".
STOPWORDS = {
    # italiane e inglesi, unite perche' contengono spaziature e accenti:
    # servono a non scegliere come buco una parola come "della" o "the"
    "a", "ad", "al", "alla", "alle", "allo",
    "ai", "all", "aglio", "anche", "ancora", "avere",
    "aveva", "avevano", "avuto", "anzi", "appena", "come",
    "con", "contro", "cosa", "cui", "che", "chi",
    "citta", "cioe", "cio", "da", "dal", "dalla",
    "dalle", "dallo", "dei", "del", "della", "delle",
    "dello", "dentro", "deve", "devono", "dopo", "dove",
    "due", "dunque", "e", "ed", "ecco", "egli",
    "ella", "essere", "era", "erano", "fare", "fatto",
    "fra", "fine", "gli", "grande", "ha", "hai",
    "hanno", "ho", "i", "il", "in", "infatti",
    "inoltre", "invece", "io", "l", "la", "le",
    "lei", "li", "lo", "loro", "lui", "lungo",
    "ma", "mentre", "mi", "mia", "mio", "molto",
    "ne", "negli", "nei", "nel", "nella", "nelle",
    "nello", "nemmeno", "non", "nostro", "o", "ogni",
    "oppure", "ora", "per", "perche", "pero", "percio",
    "piu", "poco", "poi", "posso", "prima", "qua",
    "quale", "quando", "quanto", "quasi", "quel", "questa",
    "queste", "questi", "questo", "qui", "sara", "se",
    "sempre", "senza", "si", "sia", "siamo", "sono",
    "sopra", "sotto", "stato", "stesso", "su", "sul",
    "sulla", "sulle", "suo", "tra", "tu", "tua",
    "tuo", "tutti", "tutto", "un", "una", "uno",
    "va", "vedi", "verso", "vi", "voi", "the",
    "an", "and", "or", "but", "if", "of",
    "at", "by", "for", "with", "about", "to",
    "from", "on", "is", "are", "was", "were",
    "be", "been", "being", "it", "its", "this",
    "that", "these", "those", "as", "we", "you",
    "they", "he", "she", "his", "her", "their",
    "our", "can", "will", "not", "no", "do",
    "does", "did", "has", "have", "had", "which",
    "than", "then", "there", "here", "so", "such",
    "into", "over",
}

# parole che non ha senso rendere cloze: di per se' gia' un concetto
# generico o un riferimento, quindi la risposta sarebbe troppo banale
WEAK_TERMS = {
    "figura", "tabella", "esempio", "esempi", "nota", "note", "parte",
    "punto", "punti", "sezione", "capitolo", "documento", "testo",
    "questo", "questa", "modo", "cose", "categoria", "categorie",
    "tipo", "tipi", "numero", "numeri", "dato", "dati", "informazione",
    "informazioni", "contenuto", "contenuti", "risposta", "risposte",
    "domanda", "domande", "immagine", "immagini", "grafico",
}

TOKEN_RE = re.compile(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’-]{2,}")
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÀ-Þ0-9\"'(])")

# verbi che introducono una definizione. "e" da solo e' escluso perche' e' una
# congiunzione: "Le fonti orali e i pamphlets possono essere distorti" non e'
# una definizione di "fonti orali".
DEFINITION_VERBS = (
    r"è|sono|consiste in|consistono in|significa|indica|definisce|"
    r"rappresenta|si definisce come|può essere definito come|comprende|include"
)

# un termine piu' lungo di questo e' quasi sempre una frase, non un concetto
MAX_TERM_WORDS = 6

# ogni pattern ha esattamente due gruppi: (1) termine, (2) definizione.
# cosi' generate_definitions puo' usare gli stessi indici per tutti.
DEFINITION_PATTERNS = [
    # "X: definizione", tipico degli elenchi glossari
    re.compile(r"^([^:\n]{3,60}):\s+(.{15,})$"),
    # "X (definizione in parentesi)"
    re.compile(r"^([A-ZÀ-Þ][\w\s'-]{2,40}?)\s*\((.{15,})\)$"),
    # "Il ciclo di Krebs è la via metabolica che..."
    re.compile(rf"^(.{{3,60}}?)\s+(?:{DEFINITION_VERBS})\s+(.{{15,}})$", re.IGNORECASE),
]

# articoli iniziali da togliere per ottenere il termine nudo
LEADING_ARTICLE = re.compile(
    r"^(?:Il|Lo|Gli|Le|Un|Una|I|L'|Un')\s+", re.IGNORECASE
)

# virgolette e bold che diventano cloze nelle liste
TERM_LINE_RE = re.compile(r"^(?:[-*\u2022\u2013\u2014\d.)\s]+)?(.+?):\s*(.{10,})$")

# lo stesso pattern ma che tollera il wrapped: "Mito DNA: e' un genoma
# circolare\nsituato nei mitocondri" deve restare un'unica definizione
DEFINITION_TAIL = re.compile(r"^(.{3,60}?):\s*(\S.{9,})$", re.DOTALL)


def tokenize(text: str) -> list[str]:
    """Divide in parole minuscole, scartando numeri e parole vuote."""
    return [
        m.group(0).lower()
        for m in TOKEN_RE.finditer(text)
        if m.group(0).lower() not in STOPWORDS
    ]


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in SENTENCE_RE.split(text) if p.strip()]
    result = []
    for part in parts:
        # le frasi spezzate su a capo leggono male: le ricongiungo
        part = part.replace("\n", " ")
        part = re.sub(r"\s{2,}", " ", part).strip()
        if part:
            result.append(part)
    return result


def word_count(text: str) -> int:
    return len(text.split())


class TfIdf:
    """TF-IDF calcolato sul documento corrente.

    Le parole che compaiono in tutte le pagine hanno IDF vicino a zero e
    vengono scartate, restano solo i termini davvero distintivi.
    """

    def __init__(self, units: Iterable[dict[str, Any]]) -> None:
        units = list(units)
        self.doc_count = max(1, len(units))
        self.doc_freq: Counter[str] = Counter()
        self.term_freq: dict[int, Counter[str]] = {}

        for index, unit in enumerate(units):
            counts = Counter(tokenize(unit["text"]))
            self.term_freq[index] = counts
            self.doc_freq.update(counts.keys())

    def idf(self, term: str) -> float:
        df = self.doc_freq.get(term, 0)
        if df == 0:
            return math.log(self.doc_count + 1)
        return math.log((self.doc_count + 1) / (df + 0.5))

    def top_terms(self, index: int, limit: int = 8) -> list[tuple[str, float]]:
        """Termini della unita' `index`, gia' esclusi stopword e parole banali."""
        counts = self.term_freq.get(index, Counter())
        if not counts:
            return []
        scores: list[tuple[str, float]] = []
        for term, count in counts.items():
            if len(term) < 4 or term in WEAK_TERMS:
                continue
            # normalizza per la lunghezza del termine e per la sua frequenza,
            # cosi' una parola ripetuta 30 volte non domina tutto
            score = self.idf(term) * (1 + math.log(count)) * (1 + math.log(len(term)) / 4)
            scores.append((term, score))
        scores.sort(key=lambda item: item[1], reverse=True)
        return scores[:limit]


def _find_term_span(sentence: str, term: str) -> tuple[int, int] | None:
    """Trova l'occorrenza del termine ignorando maiuscole e accenti lievi."""
    pattern = re.compile(
        r"\b" + re.escape(term).replace(r"\ ", r"[\s\-_]+") + r"\b",
        re.IGNORECASE,
    )
    match = pattern.search(sentence)
    return match.span() if match else None


def _make_card(
    card_type: str,
    question: str,
    answer: str,
    unit: dict[str, Any],
    difficulty: int = 3,
) -> dict[str, Any]:
    return {
        "card_type": card_type,
        "question": question.strip(),
        "answer": answer.strip(),
        "difficulty": difficulty,
        "source_text": unit["text"].strip(),
        "source_page": unit.get("page"),
        "source_kind": unit.get("kind"),
    }


def _acceptable(question: str, answer: str) -> bool:
    q_words, a_words = word_count(question), word_count(answer)
    if q_words < MIN_WORDS or a_words < MIN_ANSWER_WORDS:
        return False
    if a_words > MAX_ANSWER_WORDS:
        return False
    # una card cloze puo' anche essere lunga, perche' la risposta e' un buco
    return "___" in question or q_words <= MAX_WORDS


def generate_cloze(unit: dict[str, Any], index: int, tfidf: TfIdf, limit: int = 2) -> list[dict]:
    """Sostituisce i termini salienti con un buco, uno per card."""
    cards: list[dict[str, Any]] = []
    # sulle tabelle il cloze non funziona: le celle sono spezzate sui
    # separatori e la frase risultante e' illeggibile
    if unit.get("kind") == "docx_table":
        return []

    for term, _score in tfidf.top_terms(index, limit=limit):
        for sentence in split_sentences(unit["text"]):
            span = _find_term_span(sentence, term)
            if span is None:
                continue
            start, end = span
            answer = sentence[start:end]
            question = f"{sentence[:start]}____{sentence[end:]}"
            if not _acceptable(question, answer):
                continue
            # piu' il termine e' lungo e raro, piu' la card e' difficile
            difficulty = 4 if len(answer) > 18 else 2 if len(answer) <= 8 else 3
            cards.append(_make_card("cloze", question, answer, unit, difficulty))
            break
    return cards


# un termine seguito dai due punti, a meta' paragrafo: e' il formato dei
# glossari. Senza questo la seconda voce finisce dentro la prima e nessuna
# delle due diventa una card
INLINE_DEFINITION_RE = re.compile(
    r"(?:^|(?<=[.!?;])\s|(?<=:)\s|(?<=\n))\s*"
    r"([A-ZÀ-Þ][\w'’\-](?:[\w\s'’-]{1,48})):\s"
)


def expand_definition_markers(text: str) -> str:
    """Mette su righe proprie i termini seguiti dai due punti.

    Serve quando il glossario e' scritto di seguito ("X: ... Y: ...") o
    quando l'estrattore del PDF ha messo piu' voci sullo stesso paragrafo.
    """
    if ":" not in text:
        return text

    out: list[str] = []
    position = 0
    for match in INLINE_DEFINITION_RE.finditer(text):
        # la riga deve essere un blocco autonomo: niente mezza frase
        if match.start() < position:
            continue
        out.append(text[position : match.start()])
        out.append("\n" + match.group(1) + ": ")
        position = match.end()

    out.append(text[position:])
    return "".join(out)


def logical_blocks(text: str) -> list[str]:
    """Raggruppa le righe in blocchi logici.

    Una riga che finisce con i due punti, o che non chiude una frase, apre un
    blocco che continua nelle righe seguenti. Serve perche' una definizione
    lunga venga spezzata a meta' dall'estrattore del PDF, e una definizione
    troncata a meta' e' una card inutile.
    """
    blocks: list[str] = []
    current: list[str] = []

    for raw in text.split("\n"):
        line = raw.strip(" \t\u2022*->")
        if not line:
            continue

        # righe di tabella e liste sono gia' blocchi autonomi
        standalone = (
            "|" in line
            or bool(re.match(r"^[\d.)\s]+", line))
            or bool(re.match(r"^[-*\u2022\u2013\u2014]", line))
        )
        if standalone and current:
            blocks.append(" ".join(current))
            current = []
        current.append(line)

        # una riga "Termine: definizione" e' gia' un blocco completo: non
        # voglio che "Mitocondrio:" venga accorpata alla voce precedente
        closes = line.rstrip().endswith((".", "!", "?", ":", ";")) or standalone
        if closes or TERM_LINE_RE.match(line):
            blocks.append(" ".join(current))
            current = []

    if current:
        blocks.append(" ".join(current))
    return [b.strip() for b in blocks if b.strip()]


def clean_term(raw: str) -> str:
    """Pulisce il termine: niente numeri, articoli iniziali, virgolette."""
    term = re.sub(r"^[\d.)\s]+", "", raw.strip())
    term = LEADING_ARTICLE.sub("", term)
    return term.strip(" \t.,:;\"'()")


def generate_definitions(unit: dict[str, Any]) -> list[dict[str, Any]]:
    """Card domanda/risposta ricavate dalle frasi che definiscono un termine."""
    cards: list[dict[str, Any]] = []

    for line in logical_blocks(expand_definition_markers(unit["text"])):
        if len(line) < 12:
            continue

        # le intestazioni che aggiunge join_units ("[Pagina 3: ...]") non
        # sono contenuto del documento: non generano card
        if line.startswith("["):
            continue

        term = ""
        definition = ""

        # 1. righe "Termine: definizione"
        match = TERM_LINE_RE.match(line)
        if match:
            term, definition = clean_term(match.group(1)), match.group(2).strip()
        else:
            # 2. i pattern con sempre due gruppi (termine, definizione)
            for pattern in DEFINITION_PATTERNS:
                found = pattern.match(line)
                if not found:
                    continue
                term, definition = clean_term(found.group(1)), found.group(2).strip()
                break

        if not term or not definition:
            continue
        if not (2 <= len(term) <= 60):
            continue
        # un termine troppo lungo e' una frase travestita: la card diventa
        # illeggibile e il vantaggio della definizione sparisce
        if not (1 <= word_count(term) <= MAX_TERM_WORDS):
            continue
        if not (3 <= word_count(definition) <= MAX_ANSWER_WORDS):
            continue
        # il termine deve essere un sintagma, non una frase con un verbo dentro
        if term.lower() in STOPWORDS or re.search(rf"\s+(?:{DEFINITION_VERBS})\s+", term, re.I):
            continue

        difficulty = 2 if len(term) > 4 and " " not in term else 3
        cards.append(
            _make_card("definition", f"Che cos'e' {term}?", definition, unit, difficulty)
        )

    return cards


def generate_section_cards(unit: dict[str, Any], index: int, tfidf: TfIdf) -> list[dict]:
    """Chiede cosa contiene una sezione, slide o pagina."""
    kind = unit.get("kind", "")
    title = (unit.get("title") or "").strip()
    text = unit["text"].strip()
    if len(text.split()) < 25:
        return []

    if kind == "pptx_slide":
        question = f"Cosa afferma la slide \"{title}\"?"
    elif kind == "docx_section":
        question = f"Cosa dice la sezione \"{title}\"?"
    elif kind == "pdf_page":
        # un PDF non ha titili per pagina: chiedere "cosa contiene la pagina 1"
        # per ogni pagina produce domande quasi identiche, quindi per le pagine
        # senza titolo di sezione uso l'unica domanda che ha senso, sul documento
        question = f"Quali sono i temi principali di {title.lower()}?"
    else:
        question = f"Cosa contiene \"{title}\"?" if title else "Quali sono i temi principali?"

    cards = [
        _make_card(
            "section",
            question,
            text[:600] + ("..." if len(text) > 600 else ""),
            unit,
            3,
        )
    ]

    top = [term for term, _ in tfidf.top_terms(index, limit=5)]
    if top:
        cards.append(
            {
                "card_type": "section",
                "question": f"Quali sono i concetti chiave di \"{title}\"?",
                "answer": ", ".join(top),
                "difficulty": 2,
                "source_text": text,
                "source_page": unit.get("page"),
                "source_kind": kind,
            }
        )
    return cards


def generate_summaries(units: list[dict[str, Any]], tfidf: TfIdf, limit: int = 4) -> list[dict]:
    """Card riassunto: i punti principali di ogni sezione, in ordine di rilevanza."""
    cards: list[dict[str, Any]] = []
    for index, unit in enumerate(units):
        text = unit["text"]
        sentences = split_sentences(text)
        if len(sentences) < 4:
            continue

        title = (unit.get("title") or "").strip()
        scored: list[tuple[float, int, str]] = []
        for position, sentence in enumerate(sentences):
            if word_count(sentence) < 6 or word_count(sentence) > 45:
                continue
            terms = tokenize(sentence)
            if not terms:
                continue
            score = sum(tfidf.idf(term) for term in terms) / math.sqrt(len(terms))
            # le prime frasi di una sezione introducono il tema
            score *= 1 + max(0, (6 - position)) * 0.08
            # sfavore leggera le frasi che sono solo citazioni o numeri
            if sentence.count("|") > 1:
                score *= 0.6
            scored.append((score, position, sentence))

        if len(scored) < 2:
            continue

        scored.sort(key=lambda item: item[0], reverse=True)
        best = sorted(scored[: min(3, len(scored))], key=lambda item: item[1])
        bullet_points = "\n".join(f"- {s}" for _, _, s in best)

        label = title or f"parte {index + 1}"
        question = f"Quali sono i punti principali di \"{label}\"?"

        hint = top_terms_hint(tfidf, index)
        cards.append(
            {
                "card_type": "summary",
                "question": question,
                "answer": f"{hint}{bullet_points}",
                "difficulty": 4,
                "source_text": text[:900],
                "source_page": unit.get("page"),
                "source_kind": unit.get("kind"),
            }
        )
        if len(cards) >= limit:
            break
    return cards


def top_terms_hint(tfidf: TfIdf, index: int, limit: int = 4) -> str:
    top = [term for term, _ in tfidf.top_terms(index, limit=limit)]
    return f"Concetti chiave: {', '.join(top)}.\n\n" if top else ""


def generate_cards(
    units: list[dict[str, Any]],
    max_cards: int = 40,
    types: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Genera le card ordinate per rilevanza e senza duplicati."""
    wanted = set(types) if types else set(CARD_TYPES)
    tfidf = TfIdf(units)
    candidates: list[dict[str, Any]] = []

    if "definition" in wanted:
        for unit in units:
            candidates.extend(generate_definitions(unit))

    if "cloze" in wanted:
        for index, unit in enumerate(units):
            candidates.extend(generate_cloze(unit, index, tfidf))

    if "section" in wanted:
        for index, unit in enumerate(units):
            candidates.extend(generate_section_cards(unit, index, tfidf))

    if "summary" in wanted:
        candidates.extend(generate_summaries(units, tfidf))

    candidates = _dedupe(candidates)
    candidates = _rank(candidates, tfidf)
    return candidates[:max_cards]


def _dedupe(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Toglie domande identiche e cloze con lo stesso buco e stessa risposta."""
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for card in cards:
        fingerprint = re.sub(r"\s+", " ", card["question"].lower()).strip()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(card)
    return unique


def _rank(cards: list[dict[str, Any]], tfidf: TfIdf) -> list[dict[str, Any]]:
    """Ordina prima le card piu' corte e specifiche, che rendono meglio."""
    def score(card: dict[str, Any]) -> float:
        length = word_count(card["question"])
        specificity = min(len(card["answer"]), 60) / 60
        # i riassunti sono utili ma pesanti: vanno in fondo
        type_penalty = 0.6 if card["card_type"] == "summary" else 1.0
        return (specificity * 2 - length / 20) * type_penalty

    return sorted(cards, key=score, reverse=True)


def glossary(text: str, limit: int = 12) -> list[dict[str, str]]:
    """Termini e definizioni trovati nel testo, per il glossario della card."""
    entries: dict[str, str] = {}
    for line in logical_blocks(expand_definition_markers(text)):
        if line.startswith("["):
            continue
        m = TERM_LINE_RE.match(line)
        if not m:
            continue
        term = re.sub(r"^[\d.)\s]+", "", m.group(1).strip()).strip(" .,:;\"'")
        definition = m.group(2).strip()
        if 2 <= len(term) <= 60 and word_count(definition) >= 3:
            entries.setdefault(term.lower(), f"{term}: {definition}")
        if len(entries) >= limit * 3:
            break
    return [{"term": k, "definition": v} for k, v in list(entries.items())[:limit]]


def terms_in_card(card: dict[str, Any], glossary_entries: list[dict[str, str]]) -> list[dict[str, str]]:
    """Interseca i termini della domanda con il glossario del documento."""
    haystack = card["question"].lower()
    found: list[dict[str, str]] = []
    for entry in glossary_entries:
        term = entry["term"]
        if re.search(r"\b" + re.escape(term) + r"\b", haystack):
            found.append(entry)
    return found[:4]