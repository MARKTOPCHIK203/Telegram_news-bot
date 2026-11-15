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

TZ = ZoneInfo("Europe/Moscow")
AGE_SECONDS = 26 * 60 * 60
MAX_ITEMS_PER_GROUP = 3
MAX_TOTAL_ITEMS = 9

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
PUBLIC_BRIEFING = (
    # Only broad public commentary, not combat operations or exact movements.
    "минобороны", "военном ведомств", "военных ведомств", "генштаб",
    "официальн сводк", "военной обстановк", "о ходе конфликта",
    "обстановк на фронте", "боевых действиях", "ходе войны",
    "комментарий военн", "заявлени военн",
)
# Only region-wide context; never settlements, front lines, troop routes, or units.
REGIONS = (
    ("Северные приграничные области", ("курск", "сумск")),
    ("Харьковская область", ("харьковск",)),
    ("Донбасс", ("донбас", "донецк", "луганск")),
    ("Запорожская область", ("запорожск",)),
    ("Херсонская область", ("херсонск",)),
)
REGIONAL_GENERAL = (
    "общая обстановк", "ситуац", "восстановлен", "гуманитар",
    "энергет", "инфраструктур", "помощ", "мирн",
)
RUSSIAN_SIDE = (
    "кремл", "минобороны рф", "минобороны россии", "мид россии",
    "песков", "путин", "лавров", "российск власти",
    "российск делегац", "россия заяв", "в россии заяв",
    "вс рф", "генштаб россии", "генштаб рф", "российск военн",
)
UKRAINIAN_SIDE = (
    "зеленск", "мид украины", "киев заяв", "украинск власти",
    "украинск делегац", "украина заяв", "в украине заяв",
    "генштаб всу", "всу заяв", "украинск военн", "минобороны украины",
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
    # Only broad regional reporting or public-policy/official statements.
    region = next((name for name, terms in REGIONS
                   if any(term in lower for term in terms)), "")
    broad_regional = bool(region and any(term in lower for term in REGIONAL_GENERAL))
    if not (broad_regional or any(term in lower for term in
                                 DIPLOMACY + INTERNATIONAL + HUMANITARIAN + PUBLIC_BRIEFING)):
        return None
    if not any(term in lower for term in ("украин", "росси", "рф", "киев", "зеленск",
                                          "путин", "кремл", "сво", "конфликт", "войн",
                                          "всу", "генштаб", "минобороны", "военн")):
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
    if any(term in lower for term in PUBLIC_BRIEFING):
        category = "Публичные заявления о военной обстановке"
    elif any(term in lower for term in DIPLOMACY):
        category = "Дипломатия"
    elif broad_regional:
        category = "Региональный обзор"
    elif any(term in lower for term in HUMANITARIAN):
        category = "Гуманитарная повестка"
    else:
        category = "Международные события"
    side = ("Российская сторона" if any(x in lower for x in RUSSIAN_SIDE)
            else "Украинская сторона" if any(x in lower for x in UKRAINIAN_SIDE)
            else "")
    return dict(title=title, source=source, link=link, published=ts,
                category=category, side=side, region=region)


def select_digest_stories(entries, allowed_sources, now=None):
    now = time.time() if now is None else now
    candidates = [x for entry in entries if (x := eligible(entry, allowed_sources, now))]
    candidates.sort(key=lambda x: x["published"], reverse=True)
    chosen = []
    counts = {}

    def add(item):
        if len(chosen) >= MAX_TOTAL_ITEMS:
            return False
        if counts.get(item["category"], 0) >= MAX_ITEMS_PER_GROUP:
            return False
        if any(quality.is_similar_story(item["title"], prior["title"]) for prior in chosen):
            return False
        chosen.append(item)
        counts[item["category"]] = counts.get(item["category"], 0) + 1
        return True

    # Preserve both reported perspectives where eligible coverage exists.
    # Never fabricate the missing side or loosen the channel's source bans.
    for side in ("Российская сторона", "Украинская сторона"):
        for candidate in candidates:
            if candidate["side"] == side and add(candidate):
                break
    for item in candidates:
        add(item)
    return chosen


def render_digest(items, now=None):
    """Build an HTML Telegram message, grounded entirely in RSS metadata."""
    if not items:
        return None
    now = time.time() if now is None else now
    stamp = datetime.fromtimestamp(now, TZ).strftime("%d.%m.%Y")
    parts = [
        "🗞 <b>Ежедневная сводка: Россия и Украина</b>",
        f"📅 {stamp} · около 12:00 (Москва, МСК)",
        "",
        "Публичные сообщения за последние сутки, включая заявления обеих сторон. "
        "Каждое сообщение передаёт позицию указанного источника, "
        "а не самостоятельно установленный факт.",
    ]
    for group, emoji in (
        ("Публичные заявления о военной обстановке", "📣"),
        ("Дипломатия", "🕊"),
        ("Международные события", "🌍"),
        ("Гуманитарная повестка", "🤝"),
        ("Региональный обзор", "🗺"),
    ):
        matching = [x for x in items if x["category"] == group]
        if not matching:
            continue
        parts.extend(("", f"{emoji} <b>{group}</b>"))
        for item in matching:
            title = html.escape(item["title"][:225])
            source = html.escape(item["source"][:55])
            url = html.escape(item["link"], quote=True)
            parts.append(f'• <a href="{url}">{title}</a> — по сообщению {source}; независимая проверка не проводилась')
    # Macro-regions only; this is a map of topics mentioned, not troop locations.
    regions = list(dict.fromkeys(x.get("region") for x in items if x.get("region")))
    parts.extend(("", "🗺 <b>Региональный контекст</b>"))
    if regions:
        parts.append("В отобранных публикациях упоминаются: " +
                     ", ".join(html.escape(r) for r in regions[:5]) +
                     ". Это не сведения об изменениях линии фронта.")
    else:
        parts.append("В доступной подборке недостаточно сведений для регионального обзора.")

    parts.extend(("", "🧭 <b>Заявления сторон в публикациях</b>"))
    for side in ("Российская сторона", "Украинская сторона"):
        match = next((x for x in items if x["side"] == side), None)
        if match:
            title = html.escape(match["title"][:150])
            url = html.escape(match["link"], quote=True)
            source = html.escape(match["source"][:55])
            parts.append(f'• {side}: <a href="{url}">{title}</a> — по сообщению {source}')
        else:
            parts.append(f"• {side}: в разрешённых источниках недостаточно сведений.")
    # Objective, non-generative conclusion grounded solely in selected items.
    categories = (
        ("Публичные заявления о военной обстановке", "общие заявления ведомств"),
        ("Дипломатия", "дипломатия"),
        ("Международные события", "международные события"),
        ("Гуманитарная повестка", "гуманитарные вопросы"),
        ("Региональный обзор", "общие региональные события"),
    )
    category_counts = [
        f"{label} — {sum(x['category'] == category for x in items)}"
        for category, label in categories
        if any(x["category"] == category for x in items)
    ]
    sides_present = {x["side"] for x in items if x["side"]}
    comparison = (
        "Есть сообщения о публичных позициях обеих сторон; это не означает "
        "независимого подтверждения их заявлений."
        if len(sides_present) == 2 else
        "Подборка не отражает заявления обеих сторон в равной мере."
    )
    parts.extend((
        "",
        "📌 <b>Что произошло за сутки — вывод по публикациям</b>",
        "Из доступных заголовков: " + "; ".join(category_counts) + ".",
        comparison,
        "Достоверно установить изменения линии фронта, результаты боевых "
        "действий или «победителя дня» по этим сообщениям нельзя. "
        "Оперативные позиции, маршруты и неподтверждённые сведения о потерях опущены.",
        "",
        "ℹ️ Ссылки на СМИ не гарантируют достоверность сообщений; "
        "подборка не является независимой проверкой фактов.",
        "",
        "#СводкаДня #Новости #Россия #Украина",
    ))
    result = "\n".join(parts)
    if len(result) > 3800:
        # The list is already capped; choose fewer stories rather than cutting HTML.
        return render_digest(items[:-1], now)
    return result
