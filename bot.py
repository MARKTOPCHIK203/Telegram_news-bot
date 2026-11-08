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
from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
DRY_RUN = os.getenv("DRY_RUN", "0").lower() in ("1", "true", "yes")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
MAX_POSTS_PER_RUN = 2
MAX_POSTS_PER_DAY = 30
MIN_AGE_MINUTES = 60
MAX_AGE_HOURS = 8
USER_AGENT = "TelegramNewsBot/1.0 (public news digest; contact via repository)"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})

# Google News RSS — бесплатная подборка заголовков. Не выдавать её за фактчекинг.
QUERIES = [
    "Украина Россия фронт when:1d",
    "Украина Россия обстрел беспилотники when:1d",
    "Украина Россия переговоры when:1d",
    "СВО Украина Россия события when:1d",
]

# Отбор узнаваемых редакций, включая источники с разных сторон конфликта.
# Их публикация не означает независимого подтверждения сообщения.
PUBLISHERS = [
    "Reuters", "Associated Press", "AP News", "BBC", "DW",
    "РБК", "ТАСС", "Интерфакс", "Коммерсантъ", "Ведомости",
    "Украинская правда", "РБК-Украина", "РБК-Україна",
    "Суспільне", "Суспильне", "Укринформ", "Укрінформ",
    "Радио Свобода", "Настоящее Время", "Медуза",
    "Новая газета Европа", "The Moscow Times", "Euronews",
    "РИА Новости", "Известия", "France 24", "УНИАН",
]
RELEVANCE = (
    "украин", "росси", "сво", "фронт", "донец", "харьков", "киев",
    "київ", "запорож", "беспилот", "дрон", "ракет", "обстрел",
    "взрыв", "атак", "войн", "переговор", "пво", "всу", "энергет",
)
# Не публикуем оперативные наводки, точные координаты и расположение подразделений.
TACTICAL = (
    "координат", "геолокац", "местоположени подразделени",
    "дислокаци", "маршрут колонн", "точка запуска", "точка пуска",
    "местонахождени расчет", "наводк на цель", "позиции пво",
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
    for query in QUERIES:
        url = "https://news.google.com/rss/search?" + urlencode({
            "q": query, "hl": "ru", "gl": "RU", "ceid": "RU:ru"
        })
        try:
            response = SESSION.get(url, timeout=18)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            items.extend(feed.entries)
            print(f"RSS: {query}: {len(feed.entries)} материалов")
        except (requests.RequestException, ValueError) as exc:
            print(f"RSS недоступен: {query}: {exc}")
    return items


def normalize(value):
    return re.sub(r"[^а-яёa-z0-9]+", " ", value.casefold()).strip()


def allowed_source(source):
    return bool(source) and any(name.casefold() in source.casefold() for name in PUBLISHERS)


def classify(title):
    lower = title.casefold()
    if any(word in lower for word in ("слух", "неподтвержд", "не проверен", "предположительно")):
        return "⚠️ Неподтверждённое сообщение"
    if any(word in lower for word in ("заявил", "заявила", "сообщило минобороны", "по данным генштаба")):
        return "📣 Заявление стороны"
    return "📰 Сообщение СМИ (независимо не проверено)"


def choose_news(entries, state):
    now = time.time()
    already = {post["id"] for post in state["posts"] if "id" in post}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_count = sum(1 for post in state["posts"] if post.get("date") == today)
    limit = min(MAX_POSTS_PER_RUN, max(0, MAX_POSTS_PER_DAY - today_count))
    if limit == 0:
        print("Достигнут дневной лимит постов.")
        return []

    candidates = []
    local_ids = set()
    for entry in entries:
        source = text_only(entry.get("source", {}).get("title", ""))
        title = text_only(entry.get("title", ""))
        if title.endswith(" - " + source):
            title = title[:-(len(source) + 3)].strip()
        link = entry.get("link", "")
        if not allowed_source(source) or not title or not link.startswith("https://"):
            continue
        lower = normalize(title)
        if not any(word in lower for word in RELEVANCE):
            continue
        if any(word in lower for word in TACTICAL) or COORDINATES.search(title):
            continue
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        if not published:
            continue
        published_ts = calendar.timegm(published)
        age = now - published_ts
        if not (MIN_AGE_MINUTES * 60 <= age <= MAX_AGE_HOURS * 3600):
            continue
        article_id = hashlib.sha256((source.casefold() + "|" + lower).encode()).hexdigest()[:24]
        if article_id in already or article_id in local_ids:
            continue
        local_ids.add(article_id)
        candidates.append({
            "id": article_id, "title": title, "source": source,
            "link": link, "published": published_ts,
            "status": classify(title),
        })
    candidates.sort(key=lambda x: x["published"], reverse=True)
    return candidates[:limit]


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
            if not any(x in license_lower for x in ("cc by", "cc0", "public domain")):
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
    draw.text((88, 72), "НОВОСТНОЙ ДАЙДЖЕСТ", font=font(25, True), fill="#f0f4f8")
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
    published = datetime.fromtimestamp(item["published"], timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    result = (
        f"<b>{title}</b>\n\n"
        f"Источник сообщения: {source}. Автоматическая подборка заголовков; "
        f"изложенные сведения независимо не проверены.\n\n"
        f"{html.escape(item['status'])}\n"
        f"Опубликовано: {published}\n"
        f'🔗 <a href="{url}">Читать первоисточник</a>'
    )
    if credit:
        author, photo_url = credit
        result += (
            f'\n📷 Иллюстративное архивное фото (не место события): '
            f'<a href="{html.escape(photo_url, quote=True)}">{html.escape(author[:90])}</a>'
        )
    else:
        result += "\n🖼 Собственная новостная карточка, не фотография события"
    # Не обрезаем HTML посередине тега <a>: Telegram отвечает 400.
    # Ограничение Telegram (1024 символа) относится к видимому тексту,
    # а не к длине URL внутри атрибута href.
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
                raise RuntimeError(f"Telegram: ошибка сети при отправке: {type(exc).__name__}") from exc
            time.sleep(3)


def main():
    if not DRY_RUN and (not BOT_TOKEN or not CHAT_ID):
        raise SystemExit("Настрой TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в GitHub Secrets.")
    state = get_state()
    now = time.time()
    state["posts"] = [
        x for x in state["posts"]
        if isinstance(x, dict) and now - float(x.get("ts", 0)) < 30 * 86400
    ]
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
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d")
        })
        # Сохраняем после каждого успешного поста.
        save_state(state)
        print(f"Отправлено: {item['source']}: {item['title']}")
        time.sleep(2)
    save_state(state)


if __name__ == "__main__":
    main()
