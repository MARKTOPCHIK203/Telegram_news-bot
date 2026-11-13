#!/usr/bin/env python3
"""Бесплатный новостной Telegram-бот: RSS -> подпись источника -> фото/карточка -> канал.

Важно: автоматическая подборка НЕ является независимой фактологической проверкой.
"""
import calendar
import hashlib
import html
import io
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

import feedparser
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from PIL import Image, ImageDraw, ImageFont, ImageOps

import news_quality as quality
import article_preview
from deep_translator import GoogleTranslator

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
DRY_RUN = os.getenv("DRY_RUN", "0").lower() in ("1", "true", "yes")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
MAX_POSTS_PER_RUN = 5
MAX_POSTS_PER_DAY = 180  # Moderate daily ceiling; previously 100 stopped publishing by evening.
MIN_AGE_MINUTES = 0
MAX_AGE_HOURS = 30
USER_AGENT = "TelegramNewsBot/1.0 (public news digest; contact via repository)"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})
# Retry transient RSS/Commons GET failures, but never automatically retry
# Telegram POST requests: repeating a successful POST can duplicate a post.
retry = Retry(
    total=2, backoff_factor=0.7, status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(("GET",))
)
adapter = HTTPAdapter(max_retries=retry)
SESSION.mount("https://news.google.com/", adapter)
SESSION.mount("https://commons.wikimedia.org/", adapter)
SESSION.mount("https://upload.wikimedia.org/", adapter)

# Google News RSS — бесплатная подборка заголовков. Не выдавать её за фактчекинг.
QUERIES = [
    ("Украина Россия последние новости when:1d", "ru", "RU", "RU:ru"),
    ("Украина Россия фронт события when:1d", "ru", "RU", "RU:ru"),
    ("Украина Россия удары последствия when:1d", "ru", "RU", "RU:ru"),
    ("Украина Россия беспилотники ПВО when:1d", "ru", "RU", "RU:ru"),
    ("Украина Россия переговоры дипломатия when:1d", "ru", "RU", "RU:ru"),
    ("Украина Россия санкции помощь when:1d", "ru", "RU", "RU:ru"),
    ("Украина Россия гуманитарная ситуация when:1d", "ru", "RU", "RU:ru"),
    ("Ukraine Russia war latest when:1d", "en", "US", "US:en"),
    ("Ukraine Russia strikes civilians infrastructure when:1d", "en", "US", "US:en"),
    ("Ukraine Russia peace talks sanctions when:1d", "en", "US", "US:en"),
    # Additional discovery queries for Russian and independent publishers
    ("Украина Россия site:kommersant.ru OR site:vedomosti.ru OR site:rbc.ru when:1d", "ru", "RU", "RU:ru"),
    ("Украина Россия site:tass.ru OR site:interfax.ru OR site:ria.ru when:1d", "ru", "RU", "RU:ru"),
    ("Украина Россия Красноярск Сибирь when:1d", "ru", "RU", "RU:ru"),
    ("Ukraine Russia site:reuters.com OR site:apnews.com OR site:bbc.com when:1d", "en", "US", "US:en"),
]

# Отбор узнаваемых редакций, включая источники с разных сторон конфликта.
# Их публикация не означает независимого подтверждения сообщения.
PUBLISHERS = [
    # Российские информагентства и федеральные СМИ
    "ТАСС", "РИА Новости", "Интерфакс", "РБК", "РБК Новости",
    "Коммерсантъ", "Ведомости", "Известия", "Российская газета",
    "Лента.ру", "Газета.Ru", "Аргументы и факты", "АиФ",
    "Комсомольская правда", "Московский комсомолец", "МК",
    "Независимая газета", "Парламентская газета", "REGNUM",
    "ИА Регнум", "News.ru", "NEWS.ru", "RT на русском",
    "РЕН ТВ", "НТВ", "Первый канал", "Вести", "Вести.ru",
    "Россия 24", "ТВ Центр", "360.ru", "360", "URA.RU",
    "БИЗНЕС Online", "Реальное время", "Прайм", "ПРАЙМ",
    "Forbes Russia", "Forbes", "Frank Media", "Банки.ру",
    "Росбалт", "Абзац", "Царьград", "Взгляд", "Деловой Петербург",
    # Региональные российские издания
    "Фонтанка", "74.ru", "59.ru", "66.ru", "63.ru", "93.ru",
    "E1.RU", "НГС", "NGS.RU", "НГС24", "NGS24.RU",
    "Городские порталы", "Чита.Ру", "ИрСити", "ИрСити.ру",
    "Сиб.фм", "Тайга.инфо", "Проспект Мира", "Newslab",
    "Дела.ру", "КП-Красноярск", "Красноярск Онлайн",
    "Кубань 24", "Фонтанка.ру", "Петербургский дневник",
    # Независимые и эмигрантские русскоязычные редакции
    "Медуза", "Meduza", "Медиазона", "Mediazona",
    "Новая газета", "Новая газета Европа", "Новая газета. Европа",
    "The Moscow Times", "Дождь", "Телеканал Дождь",
    "Настоящее Время", "Радио Свобода", "Радио Свобода / Radio Svoboda",
    "Важные истории", "Истории", "Агентство", "Агентство. Новости",
    "The Insider", "Инсайдер", "Верстка", "Вёрстка",
    "Холод", "7x7", "7х7", "Сибирь.Реалии", "Кавказ.Реалии",
    "Idel.Реалии", "Пепел", "ASTRA", "Астра", "Сота", "SOTA",
    "Осторожно, новости", "ОВД-Инфо", "BBC News Русская служба",
    "Русская служба Би-би-си", "DW на русском", "Deutsche Welle",
    # Международные агентства и редакции
    "Reuters", "Associated Press", "AP News", "Agence France-Presse",
    "AFP", "BBC", "BBC News", "DW", "France 24", "Euronews",
    "Al Jazeera", "The Guardian", "The Independent",
    "Financial Times",
    "Bloomberg", "CNN", "Sky News", "Politico", "POLITICO",
    "The Washington Post", "The New York Times", "NBC News",
    "ABC News", "CBS News", "Axios", "The Wall Street Journal",
    "Le Monde", "Der Spiegel", "Tagesschau", "Anadolu Agency",
    "NHK World", "Deutsche Presse-Agentur", "dpa",
    "The Economist", "The Telegraph", "The Times", "PBS",
    "NPR", "RFE/RL", "Radio Free Europe/Radio Liberty",
    "Bellingcat", "United Press International", "UPI",
]
RELEVANCE = (
    "украин", "росси", "сво", "фронт", "донец", "харьков", "киев",
    "київ", "запорож", "беспилот", "дрон", "ракет", "обстрел",
    "взрыв", "атак", "войн", "переговор", "пво", "всу", "энергет",
    "ukrain", "russia", "russian", "kyiv", "moscow", "drone", "missile",
    "ceasefire", "zelensky", "putin", "sanction", "peace talks", "war",
)
# Не публикуем оперативные наводки, точные координаты и расположение подразделений.
TACTICAL = (
    "координат", "геолокац", "местоположени подразделени",
    "дислокаци", "маршрут колонн", "точка запуска", "точка пуска",
    "местонахождени расчет", "наводк на цель", "позиции пво",
    "coordinates", "geolocation", "troop positions",
    "air defense positions", "launch site", "target coordinates",
)
COORDINATES = re.compile(r"\b\d{1,2}\.\d{4,}\s*[,;]\s*\d{1,3}\.\d{4,}\b")
FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def text_only(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", value or ""))).strip()


def font(size, bold=False):
    try:
        return ImageFont.truetype(FONT_BOLD if bold else FONT_REG, size)
    except OSError:
        return ImageFont.load_default()


def get_state():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data.get("posts"), list) else {"posts": []}
    except (FileNotFoundError, ValueError, TypeError):
        return {"posts": []}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def get_entries():
    items = []
    for query, language, country, edition in QUERIES:
        url = "https://news.google.com/rss/search?" + urlencode({
            "q": query, "hl": language, "gl": country, "ceid": edition
        })
        try:
            response = SESSION.get(url, timeout=18)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            items.extend(feed.entries)
            print(f"RSS: {query}: {len(feed.entries)} материалов")
        except (requests.RequestException, ValueError) as exc:
            print(f"RSS недоступен: {query}: {exc}")
    # Прямые RSS только от разрешённых редакцией источников.
    direct_feeds = [
        ("BBC News Русская служба", "https://feeds.bbci.co.uk/russian/rss.xml"),
    ]
    for publisher, feed_url in direct_feeds:
        try:
            response = SESSION.get(feed_url, timeout=12)
            response.raise_for_status()
            direct = feedparser.parse(response.content)
            for entry in direct.entries:
                entry["source"] = {"title": publisher}
                entry["direct_feed"] = True
            items.extend(direct.entries)
            print(f"{publisher} (прямой RSS): {len(direct.entries)} материалов")
        except (requests.RequestException, ValueError) as exc:
            print(f"Прямой RSS {publisher} недоступен: {type(exc).__name__}")
    return items


def normalize(value):
    return re.sub(r"[^а-яёa-z0-9]+", " ", value.casefold()).strip()


def translate_headline(title):
    """Free best-effort Russian translation; original text if service fails."""
    if re.search(r"[а-яё]", title, re.IGNORECASE):
        return title
    try:
        translated = GoogleTranslator(source="auto", target="ru").translate(title)
        if translated and len(translated) < 350:
            return translated.strip()
    except Exception as exc:
        print(f"Перевод недоступен ({type(exc).__name__}), оставляю оригинал")
    return title


def extract_summary(entry, title):
    """Only substantive RSS descriptions; never manufacture context."""
    raw = text_only(entry.get("summary") or entry.get("description") or "")
    raw = re.sub(r"\s*Читать далее.*$", "", raw, flags=re.I).strip()
    if not raw or raw.casefold() == title.casefold():
        return ""
    # Google News embeds a list of links, not a trustworthy article synopsis.
    if "news.google.com" in (entry.get("link") or ""):
        return ""
    if not quality.is_safe_title(raw):
        return ""
    if len(raw) < 65:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", raw)
    short = " ".join(sentences[:5]).strip()
    if len(short) > 650:
        short = short[:650].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return short


def translate_summary(summary):
    if not summary or re.search(r"[а-яё]", summary, re.IGNORECASE):
        return summary
    try:
        translated = GoogleTranslator(source="auto", target="ru").translate(summary)
        if translated and len(translated) <= 1100:
            return translated.strip()
    except Exception as exc:
        print(f"Не удалось перевести описание: {type(exc).__name__}")
    return summary


def choose_news(entries, state):
    now = time.time()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    history = [post for post in state["posts"] if isinstance(post, dict)]
    already = {post.get("id") for post in history}
    today_posts = [post for post in history if post.get("date") == today]
    limit = min(MAX_POSTS_PER_RUN, max(0, MAX_POSTS_PER_DAY - len(today_posts)))
    if limit <= 0:
        print(f"Достигнут дневной лимит публикаций: {len(today_posts)}/{MAX_POSTS_PER_DAY} (UTC).")
        return []

    # Keep enough history to prevent duplicate posts from different publishers
    # within 24 hours, but allow genuinely recurring stories on later days.
    recent_titles = [
        post.get("title", "") for post in history
        if post.get("title") and now - float(post.get("ts", 0)) < 24 * 3600
    ]
    daily_sources = {}
    for post in today_posts:
        key = (post.get("source") or "").casefold()
        daily_sources[key] = daily_sources.get(key, 0) + 1

    candidates = []
    local_ids = set()
    local_titles = []
    rejected = {"source": 0, "topic": 0, "safety": 0, "age": 0, "duplicate": 0}
    for entry in entries:
        source = text_only(entry.get("source", {}).get("title", ""))
        title = text_only(entry.get("title", ""))
        if title.endswith(" - " + source):
            title = title[:-(len(source) + 3)].strip()
        link = entry.get("link", "")
        if (not quality.source_allowed(source, PUBLISHERS)
                or not title or not quality.valid_news_url(link)
                or quality.blocked_media_url(link)):
            rejected["source"] += 1
            continue
        lower = normalize(title)
        if not any(word in lower for word in RELEVANCE):
            rejected["topic"] += 1
            continue
        if not quality.is_safe_title(title) or quality.needs_editor_review(title):
            rejected["safety"] += 1
            continue
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        if not published:
            rejected["age"] += 1
            continue
        try:
            published_ts = calendar.timegm(published)
        except (ValueError, TypeError, OverflowError):
            rejected["age"] += 1
            continue
        age = now - published_ts
        if not (MIN_AGE_MINUTES * 60 <= age <= MAX_AGE_HOURS * 3600):
            rejected["age"] += 1
            continue
        article_id = hashlib.sha256(
            (source.casefold() + "|" + lower).encode()
        ).hexdigest()[:24]
        if article_id in already or article_id in local_ids:
            rejected["duplicate"] += 1
            continue
        if daily_sources.get(source.casefold(), 0) >= 20:
            rejected["duplicate"] += 1
            continue
        if any(quality.is_similar_story(title, previous) for previous in recent_titles):
            rejected["duplicate"] += 1
            continue
        if any(quality.is_similar_story(title, previous) for previous in local_titles):
            rejected["duplicate"] += 1
            continue
        summary = extract_summary(entry, title)
        if quality.needs_editor_review(summary):
            rejected["safety"] += 1
            continue
        local_ids.add(article_id)
        local_titles.append(title)
        candidates.append({
            "id": article_id, "title": title, "original_title": title, "source": source,
            "link": link, "published": published_ts,
            "status": quality.classify(title), "topic": quality.topic(title),
            "direct_feed": bool(entry.get("direct_feed")),
            "summary": summary,
        })
    # Favor informative direct-feed stories; don't flood the channel with
    # headline-only Google News results when proper descriptions exist.
    # Do not starve fresh headline-only stories: choose by recency with a
    # modest preference for RSS descriptions, never an absolute requirement.
    candidates.sort(key=lambda x: x["published"], reverse=True)
    recent_window = now - 4 * 3600
    fresh = [item for item in candidates if item["published"] >= recent_window]
    older = [item for item in candidates if item["published"] < recent_window]
    def rank(item):
        detail_bonus = 30 * 60 if len(item.get("summary", "")) >= 65 else 0
        return item["published"] + detail_bonus
    fresh.sort(key=rank, reverse=True)
    older.sort(key=rank, reverse=True)
    selected = quality.select_diverse(fresh, limit)
    if len(selected) < limit:
        remaining = [item for item in older if item["id"] not in {x["id"] for x in selected}]
        selected += quality.select_diverse(remaining, limit - len(selected))
    checked = []
    for item in selected:
        original_title = item["title"]
        original_summary = item.get("summary", "")
        translated_title = translate_headline(original_title)
        translated_summary = translate_summary(original_summary)
        if not quality.russian_text_ok(translated_title) or (
            translated_summary and not quality.russian_text_ok(translated_summary)
        ):
            print(f"Пропуск: перевод не прошёл проверку русского языка: {item['source']}")
            continue
        if quality.needs_editor_review(translated_title) or quality.needs_editor_review(translated_summary):
            print(f"Пропуск: чувствительная тема для ручной проверки: {item['source']}")
            continue
        item["title"] = translated_title
        item["original_summary"] = original_summary
        item["summary"] = translated_summary
        checked.append(item)
    selected = checked
    print(f"Кандидатов: {len(candidates)}; свежих за 4 часа: {len(fresh)}; "
          f"выбрано: {len(selected)}; отфильтровано: {rejected}")
    return selected


def image_category(title):
    lower = title.casefold()
    if any(word in lower for word in ("электр", "энерг", "свет", "тэц", "подстанц")):
        return "Ukraine power lines electricity"
    if any(word in lower for word in ("поезд", "железнодор", "вокзал")):
        return "Ukraine railway station"
    # Только нейтральные общие виды, не фото чужого удара/боя.
    return "Ukraine city panorama"


def commons_photo(title):
    """Только лицензированные иллюстративные изображения; не фото описанного события."""
    params = {
        "action": "query", "generator": "search",
        "gsrsearch": image_category(title), "gsrnamespace": "6", "gsrlimit": "12",
        "prop": "imageinfo", "iiprop": "url|extmetadata", "iiurlwidth": "1280",
        "format": "json", "formatversion": "2",
    }
    try:
        response = SESSION.get("https://commons.wikimedia.org/w/api.php", params=params, timeout=15)
        response.raise_for_status()
        pages = response.json().get("query", {}).get("pages", [])
        for page in pages:
            info = (page.get("imageinfo") or [{}])[0]
            meta = info.get("extmetadata", {})
            license_name = text_only(meta.get("LicenseShortName", {}).get("value", ""))
            license_lower = license_name.casefold()
            # Use only CC0/public-domain imagery to avoid automatic
            # cropping/redistribution of photos with additional obligations.
            if not any(x in license_lower for x in ("cc0", "public domain")):
                continue
            image_url = info.get("thumburl") or info.get("url", "")
            host = urlparse(image_url).hostname or ""
            if not host.endswith(".wikimedia.org"):
                continue
            if not image_url.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png")):
                continue
            photo = SESSION.get(image_url, timeout=18)
            photo.raise_for_status()
            if len(photo.content) > 8_000_000:
                continue
            img = Image.open(io.BytesIO(photo.content))
            if img.width < 640 or img.height < 350:
                continue
            img = ImageOps.fit(img.convert("RGB"), (1200, 675))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=85, optimize=True)
            buf.seek(0)
            artist = text_only(meta.get("Artist", {}).get("value", ""))[:65] or "автор указан на Commons"
            page_url = info.get("descriptionurl") or (
                "https://commons.wikimedia.org/wiki/" + quote(page.get("title", "").replace(" ", "_"))
            )
            return buf, f"{artist}, {license_name}", page_url
    except (requests.RequestException, ValueError, OSError) as exc:
        print(f"Commons недоступен, создаю карточку: {exc}")
    return None


def wrap(draw, value, face, max_width, max_lines):
    words = value.split()
    lines, current = [], ""
    for word in words:
        trial = (current + " " + word).strip()
        if draw.textbbox((0, 0), trial, font=face)[2] <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines and len(words) > sum(len(x.split()) for x in lines):
        lines[-1] = lines[-1].rstrip(" .,") + "…"
    return lines


def make_card(item):
    """Собственная графика, чтобы пост всегда имел изображение без нарушения прав."""
    img = Image.new("RGB", (1200, 675), "#132238")
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, 18, 675), fill="#dc6868")
    draw.rounded_rectangle((64, 57, 470, 119), radius=12, fill="#243950")
    draw.text((88, 72), "ГОЛОС СВО · НОВОСТИ", font=font(25, True), fill="#f0f4f8")
    draw.text((72, 137), item.get("topic", "События").upper(), font=font(24, True), fill="#a8d1e8")
    face = font(55, True)
    lines = wrap(draw, item["title"], face, 1070, 5)
    for i, line in enumerate(lines):
        draw.text((72, 180 + i * 77), line, font=face, fill="white")
    draw.line((72, 572, 1120, 572), fill="#60748a", width=2)
    draw.text((72, 593), item["source"][:45], font=font(26), fill="#c8d6e5")
    draw.text((835, 593), "Иллюстрация", font=font(25), fill="#c8d6e5")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=89)
    buf.seek(0)
    return buf


def news_hashtags(item):
    """Compact topic-specific Telegram hashtags."""
    topic = item.get("topic", "")
    tags = ["#ГолосСВО", "#Новости"]
    topic_tags = {
        "Дипломатия": "#Переговоры",
        "Энергетика": "#Энергетика",
        "Гуманитарная ситуация": "#ГуманитарнаяПомощь",
        "Международные события": "#МеждународныеНовости",
        "События": "#Украина",
    }
    tag = topic_tags.get(topic, "#События")
    if tag not in tags:
        tags.append(tag)
    return " ".join(tags)


def make_caption(item, credit=None):
    """Telegram editorial style; short original summary, not full article."""
    title = html.escape(item["title"][:220])
    source = html.escape(item["source"][:80])
    url = html.escape(item["link"], quote=True)
    summary = (item.get("summary") or "").strip()
    if summary and summary.casefold() == item["title"].casefold():
        summary = ""
    summary = html.escape(summary[:650])
    details = f"{summary}\n\n" if summary else ""
    event_time = datetime.fromtimestamp(item["published"], timezone.utc).strftime("%d.%m.%Y, %H:%M UTC")
    label = item.get("status", "")
    status_line = ""
    if "Неподтвержд" in label:
        status_line = "⚠️ Сообщение не подтверждено независимо.\n\n"
    elif "Заявление" in label:
        status_line = "📣 Это заявление источника, а не установленный факт.\n\n"
    result = (
        f"⚡ <b>{title}</b>\n\n"
        f"{details}{status_line}"
        f'📰 <b>Источник:</b> <a href="{url}">{source}</a>\n'
        f'🕒 <b>Опубликовано:</b> {event_time}\n'
        f"{news_hashtags(item)}"
    )
    if item.get("original_title") != item["title"] or (
        item.get("original_summary") and item.get("original_summary") != item.get("summary")
    ):
        result += "\n🌐 Автоматический перевод."
    if credit:
        author, photo_url = credit
        result += (
            "\n📷 Архивное фото, не снимок события: "
            f'<a href="{html.escape(photo_url, quote=True)}">{html.escape(author[:70])}</a>'
        )
    if len(text_only(result)) > 1000:
        # Keep essential details and attribution rather than fail the run.
        excess = len(text_only(result)) - 990
        if summary and excess > 0:
            short = html.escape((item.get("summary") or "")[:max(90, 650-excess-30)])
            result = result.replace(details, short + "…\n\n", 1)
        if len(text_only(result)) > 1024:
            raise ValueError("Слишком длинная подпись для Telegram")
    return result


def send_post(item):
    # Final safeguard: never publish blocked sources even if selection changes.
    if quality.blocked_publisher(item["source"]) or quality.blocked_media_url(item["link"]):
        print(f"Пропуск запрещённого источника: {item['source']}")
        return False
    # Use Telegram's publisher-native preview when possible. Google News RSS
    # provides an aggregator link; resolve it to the original publisher page
    # to show the story's own og:image without re-uploading copyrighted photos.
    preview_link = None
    if item.get("direct_feed") and quality.valid_news_url(item["link"]):
        preview_link = item["link"]
    elif article_preview.google_article_id(item["link"]):
        preview_link = article_preview.resolve_publisher_url(item["link"], SESSION)
        if preview_link:
            item["link"] = preview_link  # Attribute the actual publisher, not Google.
            print(f"Оригинальная статья для предпросмотра: {preview_link[:150]}")
        else:
            print("Ссылка Google News не раскрылась; используется карточка.")

    if preview_link and quality.blocked_media_url(preview_link):
        print(f"Пропуск: ссылка ведёт к исключённому СМИ: {item['source']}")
        return False

    if preview_link:
        caption = make_caption(item)
        api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        try:
            response = SESSION.post(api_url, json={
                "chat_id": CHAT_ID, "text": caption, "parse_mode": "HTML",
                "link_preview_options": {
                    "url": preview_link,
                    "is_disabled": False,
                    "prefer_large_media": True,
                    "show_above_text": True,
                },
            }, timeout=35)
            payload = response.json()
            if response.ok and payload.get("ok"):
                print(f"Опубликовано с предпросмотром оригинала: {item['source']}")
                return True
            print("Telegram отклонил предпросмотр; использую фирменную карточку.")
        except (requests.RequestException, ValueError):
            # A network timeout could occur after a successful Telegram send.
            # Do not blindly retry: this might duplicate a published item.
            raise RuntimeError("Неизвестен результат отправки предпросмотра; повтор отключён.") from None
    # Never attach a generic stock image as if it documented this event.
    # Fall back to a clearly branded title card when no article preview exists.
    image_file = make_card(item)
    caption = make_caption(item)
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    for attempt in range(3):
        image_file.seek(0)
        try:
            response = SESSION.post(
                api_url,
                data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                files={"photo": ("news.jpg", image_file, "image/jpeg")},
                timeout=35,
            )
            if response.status_code == 429:
                delay = min(30, int(response.json().get("parameters", {}).get("retry_after", 5)))
                time.sleep(delay)
                continue
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            if not response.ok or not payload.get("ok"):
                description = str(payload.get("description", "Ответ Telegram без описания ошибки"))
                description = description.replace(BOT_TOKEN, "[TOKEN HIDDEN]")
                raise RuntimeError(
                    f"Telegram API вернул HTTP {response.status_code}: {description}. "
                    "Проверь TELEGRAM_CHAT_ID, права бота и формат подписи."
                )
            return True
        except requests.RequestException as exc:
            if attempt == 2:
                raise RuntimeError(f"Telegram: ошибка сети при отправке: {type(exc).__name__}") from None
            time.sleep(3)
    raise RuntimeError("Telegram: превышен лимит запросов, сообщение не отправлено.")


def main():
    if not DRY_RUN and (not BOT_TOKEN or not CHAT_ID):
        raise SystemExit("Настрой TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в GitHub Secrets.")
    state = get_state()
    now = time.time()
    print(f"Начало проверки: {datetime.now(timezone.utc).isoformat()}; "
          f"режим: {'тест' if DRY_RUN else 'публикация'}", flush=True)
    recent_posts = []
    for post in state["posts"]:
        if not isinstance(post, dict):
            continue
        try:
            if now - float(post.get("ts", 0)) < 30 * 86400:
                recent_posts.append(post)
        except (TypeError, ValueError):
            continue
    state["posts"] = recent_posts
    selected = choose_news(get_entries(), state)
    print(f"Подходящих новых сообщений: {len(selected)}; DRY_RUN={DRY_RUN}")
    if DRY_RUN:
        for item in selected:
            print(f"[ПРОВЕРКА] {item['source']}: {item['title']}\n{item['link']}")
        return
    posted_count = 0
    for item in selected:
        if not send_post(item):
            continue
        posted_count += 1
        state["posts"].append({
            "id": item["id"], "ts": time.time(),
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "title": item["title"], "source": item["source"],
            "topic": item["topic"],
        })
        # Сохраняем после каждого успешного поста.
        save_state(state)
        print(f"Отправлено: {item['source']}: {item['title']}")
        time.sleep(0.5)
    save_state(state)
    print(f"Проверка завершена: {datetime.now(timezone.utc).isoformat()}; "
          f"отправлено: {posted_count}", flush=True)


if __name__ == "__main__":
    main()
