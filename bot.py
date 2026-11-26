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
from urllib.parse import urlencode, urljoin, urlparse

import feedparser
from bs4 import BeautifulSoup
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from PIL import Image, ImageOps

import news_quality as quality
import daily_digest
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
    # Дополнительно допускаемые русскоязычные редакции.
    # СМИ из стоп-листа news_quality.py не включаются в автоподборку.
    "Осторожно, новости", "BBC News Русская служба",
    "Русская служба Би-би-си",
    # Международные агентства и редакции
    "Reuters", "Associated Press", "AP News", "Agence France-Presse",
    "AFP", "BBC", "BBC News", "France 24", "Euronews",
    "Al Jazeera", "The Guardian", "The Independent",
    "Financial Times",
    "Bloomberg", "CNN", "Sky News", "Politico", "POLITICO",
    "The Washington Post", "The New York Times", "NBC News",
    "ABC News", "CBS News", "Axios", "The Wall Street Journal",
    "Le Monde", "Der Spiegel", "Tagesschau", "Anadolu Agency",
    "NHK World", "Deutsche Presse-Agentur", "dpa",
    "The Economist", "The Telegraph", "The Times", "PBS",
    "NPR", "United Press International", "UPI",
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


def text_only(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", value or ""))).strip()


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
    return attach_rss_details(items)


def attach_rss_details(items):
    """Recover descriptions from the same publisher's direct feed."""
    def identity(entry):
        source = text_only(entry.get("source", {}).get("title", ""))
        title = text_only(entry.get("title", ""))
        if title.casefold().endswith(" - " + source.casefold()):
            title = title[:-(len(source) + 3)]
        return source.casefold(), quality.normalized_title(title)

    details = {}
    for entry in items:
        if entry.get("direct_feed"):
            summary = extract_summary(entry, entry.get("title", ""))
            if summary and not quality.needs_editor_review(summary):
                details[identity(entry)] = summary

    for entry in items:
        if not entry.get("direct_feed") and (summary := details.get(identity(entry))):
            entry["publisher_rss_summary"] = summary
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
    if entry.get("publisher_rss_summary"):
        return entry["publisher_rss_summary"]
    raw = quality.without_links(text_only(entry.get("summary") or entry.get("description") or ""))
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
    return key_details(raw)


def key_details(text):
    sentences = re.split(r"(?<=[.!?])\s+", text)
    digest = " ".join(sentences[:5])
    if len(digest) > 1200:
        digest = digest[:1199].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return digest


ARTICLE_HOSTS = frozenset({
    "bbc.com", "www.bbc.com", "bbc.co.uk", "www.bbc.co.uk",
    "rbc.ru", "www.rbc.ru", "tass.ru", "www.tass.ru",
    "kommersant.ru", "www.kommersant.ru", "interfax.ru", "www.interfax.ru",
})
ARTICLE_BOILERPLATE = re.compile(
    r"^(?:читайте также|подпишитесь|реклама|автор фото|источник фото|"
    r"это видео|ваш браузер|read more|subscribe|advertisement|image source|"
    r"sign up|related topics|this video)", re.I
)


def article_details(url, title):
    """Extract a short lead from a supported publisher's public article."""
    if not url:
        return ""
    try:
        for attempt in range(3):
            parsed = urlparse(url)
            if (parsed.scheme != "https" or parsed.netloc.lower() not in ARTICLE_HOSTS
                    or quality.blocked_media_url(url)):
                return ""
            with SESSION.get(url, timeout=7, stream=True, allow_redirects=False) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    target = response.headers.get("Location")
                    if not target:
                        return ""
                    url = urljoin(url, target)
                    continue
                if response.status_code != 200 or "text/html" not in response.headers.get("Content-Type", "").lower():
                    return ""
                body = bytearray()
                for chunk in response.iter_content(65536):
                    body.extend(chunk)
                    if len(body) > 1_500_000:
                        return ""
                break
        else:
            return ""
        page = BeautifulSoup(bytes(body), "html.parser")
        container = page.select_one(
            '[itemprop="articleBody"], [data-component="text-block"], '
            '.article__text, .news__text, .doc__text'
        )
        container = page.find("article") or container
        if container:
            for element in container.select("aside, nav, footer, figure, script, style, [hidden]"):
                element.decompose()
        paragraphs = []
        for paragraph in container.find_all("p") if container else []:
            value = quality.without_links(paragraph.get_text(" ", strip=True))
            if len(value) < 65 or ARTICLE_BOILERPLATE.match(value):
                continue
            if quality.is_similar_story(value, title) or value in paragraphs:
                continue
            paragraphs.append(value)
            if len(paragraphs) == 3:
                break
        lead = " ".join(paragraphs)
        if not has_news_details(lead, title):
            for script in page.select('script[type="application/ld+json"]'):
                try:
                    metadata = json.loads(script.get_text())
                except ValueError:
                    continue
                records = metadata if isinstance(metadata, list) else [metadata]
                for record in records:
                    if not isinstance(record, dict):
                        continue
                    graph = record.get("@graph")
                    for article in [record] + (graph if isinstance(graph, list) else []):
                        if not isinstance(article, dict) or not isinstance(article.get("articleBody"), str):
                            continue
                        candidate = key_details(quality.without_links(text_only(article["articleBody"])))
                        if has_news_details(candidate, title):
                            lead = candidate
                            break
                    if has_news_details(lead, title):
                        break
                if has_news_details(lead, title):
                    break
        if not has_news_details(lead, title):
            description = page.select_one('meta[property="og:description"], meta[name="description"]')
            lead = quality.without_links(description.get("content", "")) if description else ""
        if not has_news_details(lead, title) or not quality.is_safe_title(lead) or quality.needs_editor_review(lead):
            return ""
        return key_details(lead)
    except (requests.RequestException, ValueError):
        return ""


def translate_summary(summary):
    if not summary or re.search(r"[а-яё]", summary, re.IGNORECASE):
        return summary
    try:
        translated = GoogleTranslator(source="auto", target="ru").translate(summary)
        if translated and len(translated) <= 4500:
            return translated.strip()
    except Exception as exc:
        print(f"Не удалось перевести описание: {type(exc).__name__}")
    return summary


def choose_news(entries, state):
    now = time.time()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    history = [post for post in state["posts"] if isinstance(post, dict)]
    already = {post.get("id") for post in history}
    previous_links = {post.get("link") for post in history}
    today_posts = [post for post in history if post.get("date") == today]
    limit = min(MAX_POSTS_PER_RUN, max(0, MAX_POSTS_PER_DAY - len(today_posts)))
    if limit <= 0:
        print(f"Достигнут дневной лимит публикаций: {len(today_posts)}/{MAX_POSTS_PER_DAY} (UTC).")
        return []

    recent_titles = [
        title for post in history if now - float(post.get("ts", 0)) < 72 * 3600
        for title in (post.get("title"), post.get("original_title")) if title
    ]
    daily_sources = {}
    for post in today_posts:
        key = (post.get("source") or "").casefold()
        daily_sources[key] = daily_sources.get(key, 0) + 1

    candidates = []
    local_ids = set()
    local_links = set()
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
        if article_id in already or article_id in local_ids or link in previous_links or link in local_links:
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
        local_links.add(link)
        local_titles.append(title)
        candidates.append({
            "id": article_id, "title": title, "original_title": title, "source": source,
            "link": link, "published": published_ts,
            "status": quality.classify(title), "topic": quality.topic(title),
            "direct_feed": bool(entry.get("direct_feed")),
            "summary": summary,
            "rss_media": [
                media.get("url") or media.get("href", "")
                for field in ("media_content", "media_thumbnail", "enclosures")
                for media in entry.get(field, [])
                if media.get("medium") == "image" or
                media.get("type", "").startswith("image/") or field == "media_thumbnail"
            ],
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
        if not quality.is_safe_title(translated_title) or not quality.is_safe_title(translated_summary):
            continue
        if any(quality.is_similar_story(translated_title, title)
               for title in recent_titles + [post["title"] for post in checked]):
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
    if any(word in lower for word in ("переговор", "дипломат", "саммит")):
        return "diplomatic meeting United Nations conference room"
    if any(word in lower for word in ("гуманитар", "бежен", "помощь")):
        return "humanitarian aid supplies warehouse"
    if any(word in lower for word in ("киев", "київ", "kyiv")):
        return "Kyiv city skyline panorama"
    return None


PORTRAIT_PEOPLE = (
    (r"мерц(?:а|у|ем|е)?|мерч(?=\s+(?:заявил|сообщил|выразил|готов))|merz",
     "Фридрих Мерц", "Friedrich Merz"),
    (r"зеленск(?:ий|ого|ому|им|ом)|zelensky[yi]?", "Владимир Зеленский", "Volodymyr Zelenskyy"),
    (r"путин(?:а|у|ым|е)?|putin", "Владимир Путин", "Vladimir Putin"),
    (r"трамп(?:а|у|ом|е)?|trump", "Дональд Трамп", "Donald Trump"),
    (r"макрон(?:а|у|ом|е)?|macron", "Эмманюэль Макрон", "Emmanuel Macron"),
    (r"стармер(?:а|у|ом|е)?|starmer", "Кир Стармер", "Keir Starmer"),
    (r"лавров(?:а|у|ым|е)?|lavrov", "Сергей Лавров", "Sergey Lavrov"),
    (r"песков(?:а|у|ым|е)?|peskov", "Дмитрий Песков", "Dmitry Peskov"),
    (r"си\s+цзиньпин(?:а|у|ом|е)?|xi\s+jinping", "Си Цзиньпин", "Xi Jinping"),
    (r"эрдоган(?:а|у|ом|е)?|erdogan|erdoğan", "Реджеп Тайип Эрдоган", "Erdoğan"),
    (r"орбан(?:а|у|ом|е)?|orbán", "Виктор Орбан", "Viktor Orbán"),
    (r"фон\s+дер\s+ляйен|von\s+der\s+leyen", "Урсула фон дер Ляйен", "Ursula von der Leyen"),
    (r"рютте|rutte", "Марк Рютте", "Mark Rutte"),
    (r"нетаньяху|netanyahu", "Биньямин Нетаньяху", "Benjamin Netanyahu"),
)
PORTRAIT_PATTERNS = tuple(
    (re.compile(rf"\b(?:{aliases})\b", re.IGNORECASE), display_name, commons_name)
    for aliases, display_name, commons_name in PORTRAIT_PEOPLE
)


def headline_person(title):
    """Choose the first recognized person mentioned in the headline."""
    matches = [
        (match.start(), display_name, commons_name)
        for pattern, display_name, commons_name in PORTRAIT_PATTERNS
        if (match := pattern.search(title))
    ]
    return min(matches)[1:] if matches else None


def commons_photo(title, person=None):
    """Find a reusable archive portrait or a thematic illustration."""
    category = f'intitle:"{person[1]}"' if person else image_category(title)
    if not category:
        return None
    params = {
        "action": "query", "generator": "search",
        "gsrsearch": category, "gsrnamespace": "6", "gsrlimit": "24" if person else "12",
        "prop": "imageinfo", "iiprop": "url|extmetadata", "iiurlwidth": "1280",
        "format": "json", "formatversion": "2",
    }
    try:
        response = SESSION.get("https://commons.wikimedia.org/w/api.php", params=params, timeout=15)
        response.raise_for_status()
        pages = response.json().get("query", {}).get("pages", [])
        if person:
            pages.sort(key=lambda page: not any(
                hint in page.get("title", "").casefold()
                for hint in ("portrait", "headshot", "official")
            ))
        for page in pages:
            filename = re.sub(r"[_-]+", " ", page.get("title", "")).casefold()
            if person and person[1].casefold() not in filename:
                continue
            info = (page.get("imageinfo") or [{}])[0]
            meta = info.get("extmetadata", {})
            license_name = text_only(meta.get("LicenseShortName", {}).get("value", ""))
            license_lower = license_name.casefold()
            # Use only CC0/public-domain imagery to avoid automatic
            # cropping/redistribution of photos with additional obligations.
            attribution_required = text_only(meta.get("AttributionRequired", {}).get("value", ""))
            if attribution_required.casefold() == "true" or not re.fullmatch(
                r"cc0(?: 1\.0)?|public domain(?: mark 1\.0)?|pdm", license_lower
            ):
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
            min_width = 350 if person else 640
            if img.width < min_width or img.height < 350 or img.width * img.height > 24_000_000:
                continue
            if person:
                img = ImageOps.contain(img.convert("RGB"), (1200, 900))
            else:
                img = ImageOps.fit(img.convert("RGB"), (1200, 675))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=85, optimize=True)
            buf.seek(0)
            return buf
    except (requests.RequestException, ValueError, OSError) as exc:
        print(f"Commons недоступен: {exc}")
    return None


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


def make_caption(item, limit=1024):
    title = quality.without_links(item["title"])[:220]
    source = quality.without_links(item["source"])[:80]
    summary = quality.without_links(item.get("summary") or "")
    event_time = datetime.fromtimestamp(item["published"], timezone.utc).strftime("%d.%m.%Y, %H:%M UTC")
    label = item.get("status", "")
    status_line = ""
    if "Неподтвержд" in label:
        status_line = "⚠️ СЛУХ. Сообщение не подтверждено независимо.\n\n"
    elif "Заявление" in label:
        status_line = "📣 Это заявление источника, а не установленный факт.\n\n"
    header = f"⚡ <b>{html.escape(title)}</b>\n\n{status_line}"
    footer = (
        f"📰 <b>Источник:</b> {html.escape(source)}\n"
        f"🕒 <b>Опубликовано:</b> {event_time}\n{news_hashtags(item)}"
    )
    if item.get("original_title", item["title"]) != item["title"] or (
        item.get("original_summary") and item.get("original_summary") != item.get("summary")
    ):
        footer += "\n🌐 Автоматический перевод."
    if item.get("photo_note"):
        photo_note = quality.without_links(item["photo_note"])
        footer += "\n📷 " + html.escape(photo_note[:110])
    if summary and summary.casefold() != title.casefold():
        visible = html.unescape(re.sub(r"</?b>", "", header + footer))
        used = len(visible.encode("utf-16-le")) // 2
        budget = max(0, limit - used - 2)
        if len(summary.encode("utf-16-le")) // 2 > budget:
            short = summary.encode("utf-16-le")[:max(0, budget - 1) * 2].decode("utf-16-le", errors="ignore")
            summary = short.rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
        header += html.escape(summary) + "\n\n"
    return header + footer


RSS_IMAGE_HOSTS = frozenset({
    "ichef.bbci.co.uk", "images.bbci.co.uk", "static.files.bbci.co.uk",
})


def rss_photo(item):
    """Use article media explicitly provided by a supported RSS publisher."""
    for url in dict.fromkeys(item.get("rss_media", [])):
        try:
            parsed = urlparse(url)
            if (parsed.scheme != "https" or parsed.netloc.lower() not in RSS_IMAGE_HOSTS):
                continue
            with SESSION.get(url, timeout=8, stream=True, allow_redirects=False) as response:
                if response.status_code != 200 or not response.headers.get("Content-Type", "").startswith("image/"):
                    continue
                data = io.BytesIO()
                for chunk in response.iter_content(65536):
                    data.write(chunk)
                    if data.tell() > 8_000_000:
                        break
                if data.tell() > 8_000_000:
                    continue
                data.seek(0)
                with Image.open(data) as image:
                    if image.width < 640 or image.height < 350 or image.width * image.height > 24_000_000:
                        continue
                    photo = ImageOps.fit(image.convert("RGB"), (1200, 675))
                    result = io.BytesIO()
                    photo.save(result, "JPEG", quality=88, optimize=True)
                    result.seek(0)
                    return result
        except (requests.RequestException, OSError, ValueError, Image.DecompressionBombError):
            continue
    return None


def has_news_details(summary, title):
    summary = quality.without_links(summary or "")
    title = quality.without_links(title)
    extra_words = quality.words(summary) - quality.words(title)
    return (len(summary) >= 65 and len(extra_words) >= 6
            and not ARTICLE_BOILERPLATE.match(summary)
            and not quality.is_similar_story(summary, title))


def prepare_post(item):
    if quality.blocked_publisher(item["source"]) or quality.blocked_media_url(item["link"]):
        return None
    publisher_url = item["link"]
    if article_preview.google_article_id(publisher_url):
        publisher_url = article_preview.resolve_publisher_url(publisher_url, SESSION)
        if publisher_url and quality.blocked_media_url(publisher_url):
            return None
    item = dict(item, title=quality.without_links(item["title"]),
                source=quality.without_links(item["source"]))
    if len(item.get("summary") or "") < 230 or not has_news_details(item.get("summary"), item["title"]):
        lead = article_details(publisher_url, item.get("original_title", item["title"]))
        translated = translate_summary(lead)
        if (has_news_details(translated, item["title"]) and quality.russian_text_ok(translated)
                and quality.is_safe_title(translated) and not quality.needs_editor_review(translated)
                and len(translated) > len(item.get("summary") or "")):
            item["original_summary"] = lead
            item["summary"] = translated
    summary = quality.without_links(item.get("summary") or "")
    if (not item["title"] or not has_news_details(summary, item["title"])
            or not quality.russian_text_ok(summary) or not quality.is_safe_title(summary)
            or quality.needs_editor_review(summary)):
        return None
    item["summary"] = key_details(summary)
    return item


def send_post(item):
    item = prepare_post(item)
    if not item:
        print("Пропуск: нет содержательного текста новости.")
        return False
    image_file = rss_photo(item)
    if image_file is not None:
        item["photo_note"] = "Фото из RSS источника"
    else:
        person = headline_person(item["title"])
        illustration = commons_photo(item["title"], person) if person else None
        if illustration:
            label = f"Архивное фото: {person[0]}"
        else:
            illustration = commons_photo(item["title"])
            label = "Иллюстрация"
        if illustration:
            image_file = illustration
            item["photo_note"] = label
    message = make_caption(item, limit=4096 if image_file is None else 1024)
    if image_file is None:
        method = "sendMessage"
        request = {"json": {
            "chat_id": CHAT_ID, "text": message, "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }}
    else:
        method = "sendPhoto"
        request = {
            "data": {"chat_id": CHAT_ID, "caption": message, "parse_mode": "HTML"},
            "files": {"photo": ("news.jpg", image_file, "image/jpeg")},
        }
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    for attempt in range(3):
        if image_file is not None:
            image_file.seek(0)
        try:
            response = SESSION.post(api_url, timeout=35, **request)
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
        except requests.RequestException:
            raise RuntimeError("Неизвестен результат отправки новости; повтор отключён.") from None
    raise RuntimeError("Telegram: превышен лимит запросов, сообщение не отправлено.")



def publish_daily_digest(entries, state):
    """Once-per-day source-attributed report after 12:00 Moscow time."""
    if not daily_digest.due_for_digest(state):
        return False
    items = [
        prepared for item in daily_digest.select_digest_stories(entries, PUBLISHERS)
        if (prepared := prepare_post(item))
    ]
    message = daily_digest.render_digest(items)
    if not message:
        print("Ежедневная сводка: подходящих безопасных заголовков нет; повторим позже.")
        return False
    response = SESSION.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        },
        timeout=35,
    )
    try:
        result = response.json()
    except ValueError:
        result = {}
    if not response.ok or not result.get("ok"):
        description = str(result.get("description", "Unknown Telegram API response"))
        raise RuntimeError(f"Не удалось отправить ежедневную сводку: {description.replace(BOT_TOKEN, '[TOKEN HIDDEN]')}")
    state["daily_digest_date"] = daily_digest.digest_date()
    save_state(state)
    print(f"Ежедневная сводка за {state['daily_digest_date']} опубликована: {len(items)} сообщений.")
    return True


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
    entries = get_entries()
    selected = choose_news(entries, state)
    print(f"Подходящих новых сообщений: {len(selected)}; DRY_RUN={DRY_RUN}")
    if DRY_RUN:
        if daily_digest.due_for_digest(state):
            preview_items = daily_digest.select_digest_stories(entries, PUBLISHERS)
            print(f"[ПРОВЕРКА] Ежедневная сводка: {len(preview_items)} заголовков")
        for item in selected:
            prepared = prepare_post(item)
            if prepared:
                print(f"[ПРОВЕРКА] {make_caption(prepared, limit=4096)}")
            else:
                print("[ПРОВЕРКА] Пропуск: нет содержательного текста новости.")
        return
    try:
        publish_daily_digest(entries, state)
    except (requests.RequestException, RuntimeError) as exc:
        # Keep regular news publishing and the automatic GitHub relay working.
        print(f"Ежедневная сводка не отправлена, попробуем снова: {type(exc).__name__}: {exc}")
    posted_count = 0
    for item in selected:
        if not send_post(item):
            continue
        posted_count += 1
        state["posts"].append({
            "id": item["id"], "ts": time.time(),
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "title": item["title"], "source": item["source"],
            "original_title": item["original_title"], "link": item["link"],
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
