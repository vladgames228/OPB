import asyncio
import html
import logging
import os

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import InputMediaPhoto, Message
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


@dp.message(Command("start", "help"))
async def cmd_help(message: Message):
    if not allowed(message.from_user.id):
        return
    await message.answer(
        "Команды:\n"
        "/add <ссылка на поиск olx.uz> - добавить отслеживание\n"
        "/list - показать активные поиски\n"
        "/remove <id> - удалить поиск\n\n"
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


def build_caption(details) -> str:
    parts = []
    if details.title:
        parts.append(f"<b>{esc(details.title)}</b>")
    if details.price:
        parts.append(f"💰 <b>{esc(details.price)}</b>")
    if details.location:
        parts.append(f"📍 {esc(details.location)}")
    if details.description:
        desc = details.description
        if len(desc) > 800:
            desc = desc[:800] + "…"
        parts.append(esc(desc))
    contact_bits = []
    if details.contact_name:
        contact_bits.append(esc(details.contact_name))
    if details.phone:
        contact_bits.append(f"<u>{esc(details.phone)}</u>")
    if contact_bits:
        parts.append("☎️ Контакт: " + " | ".join(contact_bits))
    else:
        parts.append("☎️ Контакт: не удалось извлечь, см. ссылку ниже")
    parts.append(f'🔗 <a href="{esc(details.url)}">Открыть объявление на olx.uz</a>')
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
        return
    async with aiohttp.ClientSession() as session:
        for s in searches:
            try:
                ads = await parse_search_page(session, s["url"])
            except Exception:
                log.exception("failed to fetch search %s (%s)", s["id"], s["url"])
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
                except Exception:
                    log.exception("failed to send ad %s to %s", ad.url, s["user_id"])
                storage.mark_seen(s["id"], ad.ad_id)
                await asyncio.sleep(1)  # be gentle with both olx and telegram


async def main():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(poll_once, "interval", seconds=POLL_INTERVAL_SECONDS, next_run_time=None)
    scheduler.start()
    log.info("polling every %s seconds", POLL_INTERVAL_SECONDS)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
