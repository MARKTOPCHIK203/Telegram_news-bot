"""Deterministic, offline quality checks for the free RSS news bot.

These rules are heuristics, not independent fact checking.
"""
import re
from difflib import SequenceMatcher
from urllib.parse import urlparse

WORD_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
COORDINATES = re.compile(
    r"\b\d{1,2}\.\d{4,}\s*[,;]\s*\d{1,3}\.\d{4,}\b"
)
TACTICAL_PATTERNS = (
    "координат", "геолокац", "местоположени подразделени", "дислокаци",
    "маршрут колонн", "точка запуска", "точка пуска",
    "местонахождени расчет", "наводк на цель", "позиции пво",
    "места расположения войск", "где находятся подразделения",
    "coordinates", "geolocation", "troop positions", "air defense positions",
    "launch site", "target coordinates", "location of troops",
)
STOPWORDS = frozenset((
    "что", "это", "как", "для", "при", "после", "перед", "сво", "война",
    "новости", "сообщили", "сообщает", "заявили", "заявил", "заявила",
    "стало", "известно", "россии", "россия", "украина", "украине",
    "украины", "the", "and", "for", "from", "with", "about", "says",
    "said", "news", "russia", "ukraine", "war", "after",
))


def normalized_title(value):
    return " ".join(WORD_RE.findall((value or "").casefold().replace("ё", "е")))


def words(value):
    return {
        word for word in WORD_RE.findall(normalized_title(value))
        if len(word) >= 3 and word not in STOPWORDS
    }


def is_similar_story(first, second):
    """Conservative same-language deduplication, not semantic equivalence."""
    a = normalized_title(first)
    b = normalized_title(second)
    if not a or not b:
        return False
    if a == b:
        return True
    # Distinct counts/dates often indicate different events.
    digits_a = set(re.findall(r"\b\d+\b", a))
    digits_b = set(re.findall(r"\b\d+\b", b))
    if digits_a != digits_b:
        return False
    wa, wb = words(a), words(b)
    if min(len(wa), len(wb)) < 3:
        return SequenceMatcher(None, a, b).ratio() >= 0.94
    overlap = len(wa & wb)
    union = len(wa | wb)
    jaccard = overlap / union if union else 0
    sequence = SequenceMatcher(None, a, b).ratio()
    return (overlap >= 4 and jaccard >= 0.78) or (
        overlap >= 4 and jaccard >= 0.66 and sequence >= 0.88
    )


def is_safe_title(title):
    """Exclude obviously operationally sensitive or coordinate-bearing titles."""
    value = (title or "").casefold().replace("ё", "е")
    return not COORDINATES.search(value) and not any(
        pattern in value for pattern in TACTICAL_PATTERNS
    )


def source_allowed(source, publisher_names):
    """Whole publisher name match; avoid partial matches such as AP in 'Snap'."""
    normalized = normalized_title(source)
    if not normalized:
        return False
    for publisher in publisher_names:
        key = normalized_title(publisher)
        if key and (normalized == key or normalized.startswith(key + " ") or normalized.endswith(" " + key)):
            return True
    return False


def valid_news_url(value):
    try:
        parsed = urlparse(value or "")
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and not parsed.username
        and not parsed.password
    )



# Sensitive casualty allegations require manual editorial review regardless of
# which side is alleged to be responsible. Never silently rewrite the facts.
REVIEW_TERMS = (
    "мирных жител", "мирного жител", "гражданских лиц", "гражданское населени",
    "погибл", "убит", "жертв", "ранен", "пострадавш", "жилой дом",
    "жилых дом", "детей погиб", "дети погиб", "больниц", "роддом",
    "civilian killed", "civilians killed", "civilian deaths", "killed civilians",
    "casualties", "killed", "dead", "wounded", "residential building",
    "hospital strike",
)

def needs_editor_review(value):
    normalized = (value or "").casefold().replace("ё", "е")
    return any(term in normalized for term in REVIEW_TERMS)


def russian_text_ok(value):
    """Reject English-only or substantially untranslated prose."""
    value = value or ""
    cyr = len(re.findall(r"[а-яё]", value, re.I))
    latin_words = re.findall(r"\b[a-zA-Z]{3,}\b", value)
    return cyr >= 8 and len(latin_words) <= max(2, cyr // 45)

def classify(title):
    value = (title or "").casefold()
    if any(term in value for term in (
        "слух", "неподтвержд", "не подтвержд", "не проверен",
        "предположительно", "rumor", "unconfirmed", "reportedly",
    )):
        return "⚠️ Неподтверждённое сообщение"
    if any(term in value for term in (
        "заявил", "заявила", "утвержда", "по данным генштаба",
        "сообщило минобороны", "said", "claims", "according to",
    )):
        return "📣 Заявление / утверждение источника"
    return "📰 Сообщение СМИ (независимо не проверено)"


def topic(title):
    value = (title or "").casefold()
    if any(term in value for term in ("переговор", "мирн", "дипломат", "ceasefire", "peace talk", "negotiat")):
        return "Дипломатия"
    if any(term in value for term in ("энергет", "электр", "отключени", "power grid", "electricity", "energy")):
        return "Энергетика"
    if any(term in value for term in ("гуманитар", "бежен", "гражданск", "humanitarian", "refugee", "civilian")):
        return "Гуманитарная ситуация"
    if any(term in value for term in ("санкц", "помощ", "финанс", "sanction", "aid package")):
        return "Международные события"
    return "События"


def select_diverse(candidates, limit):
    """Prefer different publishers/topics, but fill remaining slots if needed."""
    remaining = list(candidates)
    selected = []
    while remaining and len(selected) < limit:
        sources = {x["source"].casefold() for x in selected}
        topics = {x["topic"] for x in selected}
        best_idx = max(
            range(len(remaining)),
            key=lambda i: (
                remaining[i]["source"].casefold() not in sources,
                remaining[i]["topic"] not in topics,
                remaining[i]["published"],
            ),
        )
        selected.append(remaining.pop(best_idx))
    return selected
