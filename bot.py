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
from deep_translator import GoogleTranslator

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
DRY_RUN = os.getenv("DRY_RUN", "0").lower() in ("1", "true", "yes")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
MAX_POSTS_PER_RUN = 5
MAX_POSTS_PER_DAY = 100
MIN_AGE_MINUTES = 0
MAX_AGE_HOURS = 12
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
]

# Отбор узнаваемых редакций, включая источники с разных сторон конфликта.
# Их публикация не означает независимого подтверждения сообщения.
PUBLISHERS = [
    # Международные
    "Reuters", "Associated Press", "AP News", "Agence France-Presse", "AFP",
    "BBC", "DW", "Deutsche Welle", "France 24", "Euronews", "Al Jazeera",
    "The Guardian", "The Independent", "The Kyiv Independent", "Kyiv Post",
    "Financial Times", "Bloomberg", "CNN", "Sky News", "Politico",
    "The Washington Post", "The New York Times", "NBC News", "ABC News",
    "CBS News", "Axios", "The Wall Street Journal", "Le Monde",
    "Der Spiegel", "Tagesschau", "Anadolu Agency", "NHK World",
    # Российские и русскоязычные
    "РБК", "ТАСС", "Интерфакс", "Коммерсантъ", "Ведомости",
    "РИА Новости", "Известия", "Лента.ру", "Газета.Ru", "Фонтанка",
    "Медуза", "Новая газета Европа", "The Moscow Times", "Дождь",
    "Радио Свобода", "Настоящее Время",
    # Украинские
    "Украинская правда", "Українська правда", "РБК-Украина", "РБК-Україна",
    "Суспільне", "Суспильне", "Укринформ", "Укрінформ", "УНИАН", "УНІАН",
    "Радіо Свобода", "ЛІГА.net", "Liga.net", "Главком",
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
    # Прямой RSS Медузы: не ждать индексации Google News.
    try:
        response = SESSION.get("https://meduza.io/rss/all", timeout=12)
        response.raise_for_status()
        direct = feedparser.parse(response.content)
        for entry in direct.entries:
            entry["source"] = {"title": "Медуза"}
            entry["direct_feed"] = True
        items.extend(direct.entries)
        print(f"Медуза (прямой RSS): {len(direct.entries)} материалов")
    except (requests.RequestException, ValueError) as exc:
        print(f"Прямой RSS Медузы недоступен: {exc}")
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
    """Use only RSS-provided description, never invent missing facts."""
    raw = text_only(entry.get("summary") or entry.get("description") or "")
    raw = re.sub(r"\s*Читать далее.*$", "", raw, flags=re.I).strip()
    if not raw or raw.casefold() == title.casefold():
        return ""
    # Google News RSS descriptions often repeat linked headlines and HTML
    # rather than containing a usable news summary.
    if "news.google.com" in (entry.get("link") or ""):
        return ""
    if not quality.is_safe_title(raw):
        return ""
    if len(raw) < 65:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", raw)
    short = " ".join(sentences[:3]).strip()
    if len(short) > 440:
        short = short[:440].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return short


def translate_summary(summary):
    if not summary or re.search(r"[а-яё]", summary, re.IGNORECASE):
        return summary
    try:
        translated = GoogleTranslator(source="auto", target="ru").translate(summary)
        if translated and len(translated) <= 700:
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
        print("Достигнут дневной лимит публикаций.")
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
        if not quality.source_allowed(source, PUBLISHERS) or not title or not quality.valid_news_url(link):
            rejected["source"] += 1
            continue
        lower = normalize(title)
        if not any(word in lower for word in RELEVANCE):
            rejected["topic"] += 1
            continue
        if not quality.is_safe_title(title):
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
        local_ids.add(article_id)
        local_titles.append(title)
        candidates.append({
            "id": article_id, "title": title, "original_title": title, "source": source,
            "link": link, "published": published_ts,
            "status": quality.classify(title), "topic": quality.topic(title),
            "direct_feed": bool(entry.get("direct_feed")),
            "summary": extract_summary(entry, title),
        })
    candidates.sort(key=lambda x: x["published"], reverse=True)
    # Prioritize fresh direct-feed updates without monopolizing the channel.
    selected = quality.select_diverse(candidates, limit)
    for item in selected:
        item["title"] = translate_headline(item["title"])
        item["summary"] = translate_summary(item.get("summary", ""))
    print(f"Кандидатов: {len(candidates)}; выбрано: {len(selected)}; отфильтровано: {rejected}")
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


def make_caption(item, credit=None):
    title = html.escape(item["title"][:230])
    source = html.escape(item["source"][:80])
    url = html.escape(item["link"], quote=True)
    topic = html.escape(item.get("topic", "События"))
    label = html.escape(item["status"])
    published = datetime.fromtimestamp(
        item["published"], timezone.utc
    ).strftime("%d.%m.%Y · %H:%M UTC")
    summary = html.escape(item.get("summary", "")[:440])
    details = (summary + "\n\n") if summary else ""
    result = (
        f"🔴 <b>{topic.upper()}</b>\n\n"
        f"<b>{title}</b>\n\n"
        f"{details}"
        f"🗞 <b>Источник:</b> {source}\n"
        f"ℹ️ {label}\n"
        f"🕒 {published}\n\n"
        f'🔗 <a href="{url}">Читать материал</a>\n'
        f"📢 @golos_svo_news"
    )
    if item.get("original_title") != item["title"]:
        result += "\n🌐 Заголовок автоматически переведён с иностранного языка."
    if credit:
        author, photo_url = credit
        result += (
            "\n📷 Архивная иллюстрация, не фото события: "
            f'<a href="{html.escape(photo_url, quote=True)}">{html.escape(author[:90])}</a>'
        )
    if len(text_only(result)) > 950:
        raise ValueError("Слишком длинная подпись для Telegram")
    return result


def send_post(item):
    photo = commons_photo(item["title"])
    if photo:
        image_file, credit, photo_url = photo
        caption = make_caption(item, (credit, photo_url))
    else:
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
            return
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
    for item in selected:
        send_post(item)
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


if __name__ == "__main__":
    main()
