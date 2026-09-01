import asyncio
import html
import logging
import os

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import BotCommand, InputMediaPhoto, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from olx_parser import parse_ad_details, parse_search_page
from storage import Storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_USER_IDS = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()}
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "180"))
DB_PATH = os.environ.get("DB_PATH", "data/olx.sqlite3")

os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

storage = Storage(DB_PATH)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def allowed(user_id: int) -> bool:
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS


def esc(s: str) -> str:
    return html.escape(s or "")


BOT_COMMANDS = [
    BotCommand(command="add", description="Добавить поиск olx.uz"),
    BotCommand(command="list", description="Показать активные поиски"),
    BotCommand(command="remove", description="Удалить поиск по id"),
    BotCommand(command="check", description="Проверить все поиски прямо сейчас"),
    BotCommand(command="latest", description="Прислать последнее объявление по поиску"),
    BotCommand(command="help", description="Помощь"),
]


@dp.message(Command("start", "help"))
async def cmd_help(message: Message):
    if not allowed(message.from_user.id):
        return
    await message.answer(
        "Команды:\n"
        "/add <ссылка на поиск olx.uz> - добавить отслеживание\n"
        "/list - показать активные поиски\n"
        "/remove <id> - удалить поиск\n"
        "/check - проверить все поиски прямо сейчас, не дожидаясь таймера\n"
        "/latest <id> - прислать самое свежее объявление по поиску "
        "(не помечает его как увиденное), чтобы убедиться что парсер жив\n\n"
        "После /add бот будет проверять новые объявления каждые "
        f"{POLL_INTERVAL_SECONDS} сек и присылать их сюда."
    )


@dp.message(Command("add"))
async def cmd_add(message: Message):
    if not allowed(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or "olx.uz" not in parts[1]:
        await message.answer("Пришли: /add https://www.olx.uz/...")
        return
    url = parts[1].strip()
    search_id = storage.add_search(message.from_user.id, url)

    # baseline: mark everything currently on page 1 as seen so we don't spam
    # the user with the entire existing listing history on first add
    async with aiohttp.ClientSession() as session:
        try:
            ads = await parse_search_page(session, url)
        except Exception as e:
            log.exception("baseline fetch failed for %s", url)
            ads = []
    for ad in ads:
        storage.mark_seen(search_id, ad.ad_id)

    await message.answer(
        f"Добавлено (id {search_id}). Найдено сейчас {len(ads)} объявлений - "
        "они не будут присланы, бот пришлёт только то, что появится новое."
    )


@dp.message(Command("list"))
async def cmd_list(message: Message):
    if not allowed(message.from_user.id):
        return
    searches = storage.list_searches(message.from_user.id)
    if not searches:
        await message.answer("Нет активных поисков.")
        return
    lines = [f"{s['id']}: {s['url']} (отслежено {storage.count_seen(s['id'])})" for s in searches]
    await message.answer("\n".join(lines))


@dp.message(Command("remove"))
async def cmd_remove(message: Message):
    if not allowed(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer("Пришли: /remove <id из /list>")
        return
    search_id = int(parts[1].strip())
    ok = storage.remove_search(search_id, message.from_user.id)
    await message.answer("Удалено." if ok else "Не найдено (или чужой поиск).")


@dp.message(Command("check"))
async def cmd_check(message: Message):
    if not allowed(message.from_user.id):
        return
    searches = storage.list_searches(message.from_user.id)
    if not searches:
        await message.answer("Нет активных поисков.")
        return
    status = await message.answer("Проверяю...")
    total_new = 0
    async with aiohttp.ClientSession() as session:
        for s in searches:
            try:
                ads = await parse_search_page(session, s["url"])
            except Exception:
                log.exception("manual check failed for %s (%s)", s["id"], s["url"])
                continue
            new_ads = [a for a in ads if not storage.is_seen(s["id"], a.ad_id)]
            for ad in reversed(new_ads):
                try:
                    details = await parse_ad_details(session, ad)
                    await send_ad(message.chat.id, details)
                except Exception:
                    log.exception("manual check: failed to send %s", ad.url)
                storage.mark_seen(s["id"], ad.ad_id)
                total_new += 1
                await asyncio.sleep(1)
    await status.edit_text(f"Готово. Новых объявлений: {total_new}.")


@dp.message(Command("latest"))
async def cmd_latest(message: Message):
    if not allowed(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer("Пришли: /latest <id из /list>")
        return
    search_id = int(parts[1].strip())
    searches = storage.list_searches(message.from_user.id)
    s = next((x for x in searches if x["id"] == search_id), None)
    if not s:
        await message.answer("Не найдено (или чужой поиск).")
        return
    async with aiohttp.ClientSession() as session:
        try:
            ads = await parse_search_page(session, s["url"])
        except Exception:
            log.exception("latest fetch failed for %s", s["url"])
            await message.answer("Не удалось загрузить страницу поиска.")
            return
        if not ads:
            await message.answer("Объявлений не найдено (парсер вернул 0).")
            return
        latest = ads[0]
        try:
            details = await parse_ad_details(session, latest)
        except Exception:
            log.exception("latest details failed for %s", latest.url)
            await message.answer("Нашёл объявление, но не смог загрузить детали.")
            return
    posted = f"\n🕒 {esc(latest.posted)}" if latest.posted else ""
    await send_ad(message.chat.id, details)
    if posted:
        await message.answer(f"Метка даты с карточки поиска:{posted}", parse_mode="HTML")


def build_caption(details) -> str:
    """
    Single message per ad, structured like the ad itself:
    title + price up top, description as the body, phone number and source
    link at the bottom.
    """
    head = f"<b>{esc(details.title)}</b>" if details.title else ""
    if details.price:
        head = f"{head}\n💰 <b>{esc(details.price)}</b>" if head else f"💰 <b>{esc(details.price)}</b>"
    if details.location:
        head = f"{head}\n📍 {esc(details.location)}" if head else f"📍 {esc(details.location)}"

    parts = [p for p in [head] if p]

    if details.description:
        desc = details.description
        if len(desc) > 800:
            desc = desc[:800] + "…"
        parts.append(esc(desc))

    footer_bits = []
    if details.phone:
        footer_bits.append(f"<u>{esc(details.phone)}</u>")
    else:
        footer_bits.append("номер не удалось извлечь")
    footer_bits.append(f'<a href="{esc(details.url)}">olx.uz</a>')
    parts.append(" • ".join(footer_bits))

    return "\n\n".join(parts)


async def send_ad(chat_id: int, details):
    caption = build_caption(details)
    if details.photos:
        media = [InputMediaPhoto(media=u) for u in details.photos[:10]]
        media[0].caption = caption
        media[0].parse_mode = "HTML"
        try:
            await bot.send_media_group(chat_id, media)
            return
        except Exception:
            log.exception("send_media_group failed, falling back to text for %s", details.url)
    if len(caption) > 4000:
        caption = caption[:4000] + "…"
    await bot.send_message(chat_id, caption, parse_mode="HTML", disable_web_page_preview=False)


async def poll_once():
    searches = storage.get_all_active_searches()
    if not searches:
        log.info("poll: no active searches")
        return
    total_new = 0
    failed = 0
    async with aiohttp.ClientSession() as session:
        for s in searches:
            try:
                ads = await parse_search_page(session, s["url"])
            except Exception:
                log.exception("failed to fetch search %s (%s)", s["id"], s["url"])
                failed += 1
                continue
            new_ads = [a for a in ads if not storage.is_seen(s["id"], a.ad_id)]
            # oldest-looking-first isn't reliable from listing order alone,
            # send in the order returned by the page (newest-first on OLX)
            for ad in reversed(new_ads):
                try:
                    details = await parse_ad_details(session, ad)
                except Exception:
                    log.exception("failed to fetch details for %s", ad.url)
                    storage.mark_seen(s["id"], ad.ad_id)
                    continue
                try:
                    await send_ad(s["user_id"], details)
                    total_new += 1
                except Exception:
                    log.exception("failed to send ad %s to %s", ad.url, s["user_id"])
                storage.mark_seen(s["id"], ad.ad_id)
                await asyncio.sleep(1)  # be gentle with both olx and telegram
    log.info(
        "poll done: %d searches checked, %d new ads sent, %d searches failed",
        len(searches), total_new, failed,
    )


async def main():
    await bot.set_my_commands(BOT_COMMANDS)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(poll_once, "interval", seconds=POLL_INTERVAL_SECONDS)
    scheduler.start()
    log.info("polling every %s seconds", POLL_INTERVAL_SECONDS)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
