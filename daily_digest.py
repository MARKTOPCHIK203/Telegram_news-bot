"""Daily public-news digest for Telegram, using only attributed RSS headlines.

No battlefield-position tracking, troop-loss calculations, unverified victor
claims or LLM-generated facts. Inherit the channel's editorial allow/deny lists.
"""
import calendar
import html
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import news_quality as quality

TZ = ZoneInfo("Asia/Krasnoyarsk")
AGE_SECONDS = 26 * 60 * 60
MAX_ITEMS_PER_GROUP = 3
MAX_TOTAL_ITEMS = 8

# The digest deliberately covers high-level political and humanitarian news.
# Location-specific military movements are not suitable for autopublication.
DIPLOMACY = (
    "переговор", "дипломат", "мирн", "перемири", "встреч", "диалог",
    "договор", "соглашен", "обсуд", "контакт",
)
INTERNATIONAL = (
    "санкц", "международ", "европ", "сша", "белый дом", "нато",
    "эконом", "помощ", "финанс", "поставк", "посредник",
)
HUMANITARIAN = (
    "гуманитар", "обмен плен", "пленных", "бежен", "эвакуац",
    "электроснабж", "энергосист", "восстановлен",
)
RUSSIAN_SIDE = (
    "кремл", "минобороны рф", "минобороны россии", "мид россии",
    "песков", "путин", "лавров", "российск власти",
    "российск делегац", "россия заяв", "в россии заяв",
)
UKRAINIAN_SIDE = (
    "зеленск", "мид украины", "киев заяв", "украинск власти",
    "украинск делегац", "украина заяв", "в украине заяв",
)
# Exclude operational reporting, alleged destruction figures and targeting
# even where publishers happen to be on the general allowlist.
OPERATIONAL_TERMS = (
    "продвиж", "наступлен", "отступлен", "контрнаступ",
    "уничтож", "ликвидир", "взят под контрол", "захват",
    "прорыв", "окружен", "потери вс", "потерь вс",
    "направлени фронт", "линии фронт", "позици военн",
    "военной техни", "военную техник", "танк", "артиллери",
    "дислокац", "воинск част", "батальон", "бригады",
    "сбили", "ударил", "обстрел", "атака дрон", "ракетн удар",
)


def digest_date(now=None):
    return datetime.fromtimestamp(time.time() if now is None else now, TZ).date().isoformat()


def due_for_digest(state, now=None):
    local = datetime.fromtimestamp(time.time() if now is None else now, TZ)
    return local.hour >= 12 and state.get("daily_digest_date") != local.date().isoformat()


def text_only(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", value or ""))).strip()


def eligible(entry, allowed_sources, now):
    source = text_only((entry.get("source") or {}).get("title", ""))
    title = text_only(entry.get("title", ""))
    if title.endswith(" - " + source):
        title = title[:-(len(source) + 3)].strip()
    link = entry.get("link", "")
    if (not quality.source_allowed(source, allowed_sources) or
            not quality.valid_news_url(link) or quality.blocked_media_url(link) or
            not quality.is_safe_title(title) or quality.needs_editor_review(title) or
            not quality.russian_text_ok(title)):
        return None
    lower = title.casefold().replace("ё", "е")
    if any(term in lower for term in OPERATIONAL_TERMS):
        return None
    # Only well-defined high-level public-policy topics; no tactical detail.
    if not any(term in lower for term in DIPLOMACY + INTERNATIONAL + HUMANITARIAN):
        return None
    if not any(term in lower for term in ("украин", "росси", "рф", "киев", "зеленск",
                                          "путин", "кремл", "сво", "конфликт", "войн")):
        return None
    published = entry.get("published_parsed") or entry.get("updated_parsed")
    if not published:
        return None
    try:
        ts = calendar.timegm(published)
    except (TypeError, ValueError, OverflowError):
        return None
    if not 0 <= now - ts <= AGE_SECONDS:
        return None
    if any(term in lower for term in DIPLOMACY):
        category = "Дипломатия"
    elif any(term in lower for term in HUMANITARIAN):
        category = "Гуманитарная повестка"
    else:
        category = "Международные события"
    side = ("Российская сторона" if any(x in lower for x in RUSSIAN_SIDE)
            else "Украинская сторона" if any(x in lower for x in UKRAINIAN_SIDE)
            else "")
    return dict(title=title, source=source, link=link, published=ts,
                category=category, side=side)


def select_digest_stories(entries, allowed_sources, now=None):
    now = time.time() if now is None else now
    candidates = [x for entry in entries if (x := eligible(entry, allowed_sources, now))]
    candidates.sort(key=lambda x: x["published"], reverse=True)
    chosen = []
    counts = {}
    for item in candidates:
        if len(chosen) >= MAX_TOTAL_ITEMS:
            break
        if counts.get(item["category"], 0) >= MAX_ITEMS_PER_GROUP:
            continue
        if any(quality.is_similar_story(item["title"], prior["title"]) for prior in chosen):
            continue
        chosen.append(item)
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    return chosen


def render_digest(items, now=None):
    """Build an HTML Telegram message, grounded entirely in RSS metadata."""
    if not items:
        return None
    now = time.time() if now is None else now
    stamp = datetime.fromtimestamp(now, TZ).strftime("%d.%m.%Y")
    parts = [
        "🗞 <b>Ежедневная сводка: Россия и Украина</b>",
        f"📅 {stamp} · около 12:00 (Красноярск)",
        "",
        "Главные публичные сообщения за последние сутки. "
        "Это подборка заголовков источников, а не независимая проверка фактов.",
    ]
    for group, emoji in (
        ("Дипломатия", "🕊"),
        ("Международные события", "🌍"),
        ("Гуманитарная повестка", "🤝"),
    ):
        matching = [x for x in items if x["category"] == group]
        if not matching:
            continue
        parts.extend(("", f"{emoji} <b>{group}</b>"))
        for item in matching:
            title = html.escape(item["title"][:225])
            source = html.escape(item["source"][:55])
            url = html.escape(item["link"], quote=True)
            parts.append(f'• <a href="{url}">{title}</a> — {source}')
    mentioned_sides = {x["side"] for x in items if x["side"]}
    parts.extend(("", "🧭 <b>Позиции сторон</b>"))
    if "Российская сторона" in mentioned_sides:
        parts.append("• Есть сообщения с заявлениями российской стороны (ссылки выше).")
    else:
        parts.append("• Отдельных сообщений с заявлениями российской стороны в этой подборке нет.")
    if "Украинская сторона" in mentioned_sides:
        parts.append("• Есть сообщения с заявлениями украинской стороны (ссылки выше).")
    else:
        parts.append("• Отдельных сообщений с заявлениями украинской стороны в этой подборке нет.")
    parts.extend((
        "",
        "📌 <b>Итог</b>",
        "На основании одной подборки СМИ нельзя надёжно определить «победителя дня». "
        "Заявления сторон и оценки могут расходиться. "
        "Точные позиции войск и неподтверждённые данные о потерях не приводятся.",
        "",
        "#СводкаДня #Новости #Россия #Украина",
    ))
    result = "\n".join(parts)
    if len(result) > 3800:
        # The list is already capped; choose fewer stories rather than cutting HTML.
        return render_digest(items[:-1], now)
    return result
