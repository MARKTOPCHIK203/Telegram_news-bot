#!/usr/bin/env python3
"""Manual diagnostics: does not publish anything. Never prints the bot token."""
import os
import requests

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
SESSION = requests.Session()


def api(method, **params):
    try:
        response = SESSION.post(
            f"https://api.telegram.org/bot{TOKEN}/{method}",
            json=params, timeout=20
        )
        payload = response.json()
        if not payload.get("ok"):
            desc = str(payload.get("description", "unknown Telegram error"))
            print(f"  Telegram API {method}: HTTP {response.status_code}: {desc.replace(TOKEN, '[hidden]')}")
            return None
        return payload.get("result")
    except (requests.RequestException, ValueError) as exc:
        print(f"  Telegram API {method}: ошибка сети/ответа: {type(exc).__name__}")
        return None


def describe(chat):
    kind = chat.get("type", "?")
    title = chat.get("title") or chat.get("username") or "(без названия)"
    cid = chat.get("id")
    # The Actions logs of a PUBLIC repository are visible to others.
    print(f"  Тип: {kind}; название: {title}; ID: {cid}")
    return kind


def main():
    print("=== Telegram diagnostics (ничего не публикует) ===")
    if not TOKEN:
        raise SystemExit("Секрет TELEGRAM_BOT_TOKEN отсутствует.")
    me = api("getMe")
    if not me:
        raise SystemExit("Токен бота не работает. Обнови TELEGRAM_BOT_TOKEN.")
    print(f"Бот: @{me.get('username', '?')} — токен работает.")
    if not CHAT_ID:
        print("Секрет TELEGRAM_CHAT_ID отсутствует.")
    elif CHAT_ID.startswith("http") or CHAT_ID.startswith("t.me/"):
        print("НЕВЕРНЫЙ TELEGRAM_CHAT_ID: вставлена ссылка. Нужен @username канала или отрицательный ID -100...")
    elif CHAT_ID.lstrip("-").isdigit() and int(CHAT_ID) >= 0:
        print("НЕВЕРНЫЙ TELEGRAM_CHAT_ID: положительное число не является ID канала.")
    elif not CHAT_ID.startswith("@") and not CHAT_ID.lstrip("-").isdigit():
        print("Проверь TELEGRAM_CHAT_ID: ожидается @username или -100...")
    else:
        print("Проверяю заданный TELEGRAM_CHAT_ID...")
        chat = api("getChat", chat_id=CHAT_ID)
        if chat:
            kind = describe(chat)
            if kind != "channel":
                print("ОШИБКА: это не канал Telegram. Для канала нужен его chat ID.")
            else:
                member = api("getChatMember", chat_id=CHAT_ID, user_id=me["id"])
                if member:
                    status = member.get("status")
                    can_post = member.get("can_post_messages")
                    print(f"Права бота: статус={status}, can_post_messages={can_post}")
                    if status == "creator" or (status == "administrator" and can_post):
                        print("OK: бот может публиковать в этом канале.")
                    else:
                        print("ОШИБКА: дай боту право публиковать сообщения в канале.")
    print("\nИщу каналы в последних обновлениях бота...")
    updates = api("getUpdates", offset=-100, limit=100, timeout=0,
                  allowed_updates=["channel_post", "edited_channel_post", "my_chat_member"])
    channels = {}
    if updates is None:
        print("Не удалось получить обновления. Возможно, у бота настроен webhook.")
    else:
        for update in updates:
            event = (update.get("channel_post") or update.get("edited_channel_post") or
                     update.get("my_chat_member") or {})
            chat = event.get("chat", {})
            if chat.get("type") == "channel":
                channels[chat["id"]] = chat
        if channels:
            print("Найдены каналы. ВНИМАНИЕ: этот журнал публичного репозитория доступен другим.")
            for chat in channels.values():
                describe(chat)
        else:
            print("Каналы пока не обнаружены. Опубликуй новое обычное сообщение в канале,")
            print("где бот — администратор, и запусти диагностику ещё раз.")
    print("=== Готово. Токен нигде не выводился. ===")


if __name__ == "__main__":
    main()
