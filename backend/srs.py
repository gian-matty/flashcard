"""Scheduling spaced repetition, variante semplificata di SM-2."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# giorno in minuti per gli intervalli fra 0 e 1, cosi' la prima
# ripassazione di una card sbagliata torna dopo pochi minuti
DAY_MINUTES = 1440

GRADE_GIUSTA = "giusta"
GRADE_SBAGLIATA = "sbagliata"
GRADE_ANCORA = "ancora"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def initial_schedule() -> dict[str, float | int]:
    """Stato di una card appena creata: da ripassare subito."""
    return {
        "ease_factor": 2.5,
        "interval_days": 0,
        "repetitions": 0,
        "lapses": 0,
        "due_at": _now().isoformat(timespec="seconds"),
    }


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def review(
    card: dict,
    grade: str,
    difficulty_hint: int | None = None,
) -> dict[str, float | int]:
    """Calcola il prossimo stato di una card dopo un giudizio.

    In SM-2 la qualita' va da 0 a 5. La mappatura:
    sbagliata -> 1, ancora -> 3, giusta -> 5.
    """
    ease = float(card.get("ease_factor", 2.5) or 2.5)
    interval = float(card.get("interval_days", 0) or 0)
    repetitions = int(card.get("repetitions", 0) or 0)
    lapses = int(card.get("lapses", 0) or 0)

    if difficulty_hint:
        # la difficolta' stimata dall'AI alza o abbassa leggermente la qualita'
        quality = 5 if difficulty_hint <= 2 else 4 if difficulty_hint == 3 else 3
        if grade == GRADE_SBAGLIATA:
            quality = 1
        elif grade == GRADE_ANCORA:
            quality = 3
    elif grade == GRADE_SBAGLIATA:
        quality = 1
    elif grade == GRADE_ANCORA:
        quality = 3
    else:
        quality = 5

    if quality < 3:
        # azzerata: la ripartenza ripete da subito
        repetitions = 0
        lapses += 1
        interval = 10 / DAY_MINUTES
        ease = _clamp(ease - 0.2, 1.3, 3.0)
    else:
        if repetitions == 0:
            interval = 1 if quality >= 4 else 0.5
        elif repetitions == 1:
            interval = 3
        else:
            interval = round(interval * ease, 2)
        repetitions += 1

    ease = _clamp(ease + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)), 1.3, 3.0)

    due_at = _now() + timedelta(days=interval)

    return {
        "ease_factor": round(ease, 3),
        "interval_days": interval,
        "repetitions": repetitions,
        "lapses": lapses,
        "due_at": due_at.isoformat(timespec="seconds"),
    }