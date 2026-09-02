import asyncio
import hashlib
import html
import logging
import os
import re

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from olx_parser import parse_ad_details, parse_search_page, short_query_label
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
dp = Dispatcher(storage=MemoryStorage())


class CommentStates(StatesGroup):
    waiting = State()


def allowed(user_id: int) -> bool:
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS


def esc(s: str) -> str:
    return html.escape(s or "")


def normalize_title(t: str) -> str:
    t = (t or "").lower()
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def make_fp(details) -> str:
    """
    Fingerprint an ad by its content, not its OLX ad id, so a re-published
    listing (new id, same content) is still recognized as the same ad for
    dislikes/comments. Best-effort: normalized title + phone (or seller
    name if phone wasn't extracted) + price digits.
    """
    base = "|".join([
        normalize_title(details.title),
        (details.phone or details.contact_name or "").strip(),
        re.sub(r"\D", "", details.price or ""),
    ])
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]


BOT_COMMANDS = [
    BotCommand(command="group_add", description="Создать группу поисков"),
    BotCommand(command="group_list", description="Список групп"),
    BotCommand(command="group_remove", description="Удалить группу"),
    BotCommand(command="add", description="Добавить поиски в группу"),
    BotCommand(command="list", description="Показать активные поиски"),
    BotCommand(command="remove", description="Удалить один поиск по id"),
    BotCommand(command="check", description="Проверить все поиски прямо сейчас"),
    BotCommand(command="latest", description="Прислать последнее объявление по поиску"),
    BotCommand(command="favorites", description="Показать избранное"),
    BotCommand(command="help", description="Помощь"),
]


@dp.message(Command("start", "help"))
async def cmd_help(message: Message):
    if not allowed(message.from_user.id):
        return
    await message.answer(
        "Сначала создай группу поисков, потом добавляй в неё ссылки:\n\n"
        "/group_add <название> - создать группу\n"
        "/group_list - список групп\n"
        "/group_remove <id группы> - удалить группу со всеми её поисками\n\n"
        "/add <id группы> <ссылка1> [ссылка2] ... - добавить один или "
        "несколько поисков olx.uz в группу\n"
        "/list - показать активные поиски по группам\n"
        "/remove <id поиска> - удалить один поиск\n"
        "/check - проверить все поиски прямо сейчас, не дожидаясь таймера\n"
        "/latest <id поиска> - прислать самое свежее объявление по поиску, "
        "чтобы убедиться что парсер жив\n"
        "/favorites - показать сохранённое в избранном\n\n"
        "Если объявление подходит под несколько поисков в одной группе - "
        "пришлю его один раз. Под каждым объявлением есть кнопки: "
        "⭐ избранное, 👎 больше не показывать (и его переподачи тоже), "
        "💬 комментарий (сохранится и появится снова, если объявление "
        "попадётся ещё раз).\n\n"
        f"Опрос идёт каждые {POLL_INTERVAL_SECONDS} сек, запросы к olx.uz "
        "разносятся случайной паузой 3-5 сек, чтобы не долбить сайт."
    )


# ---------------- groups ----------------

@dp.message(Command("group_add"))
async def cmd_group_add(message: Message):
    if not allowed(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Пришли: /group_add <название>, например /group_add Квартиры")
        return
    name = parts[1].strip()
    group_id = storage.add_group(message.from_user.id, name)
    await message.answer(f"Группа «{name}» создана, id {group_id}. Теперь: /add {group_id} <ссылка>")


@dp.message(Command("group_list"))
async def cmd_group_list(message: Message):
    if not allowed(message.from_user.id):
        return
    groups = storage.list_groups(message.from_user.id)
    if not groups:
        await message.answer("Групп пока нет. Создай: /group_add <название>")
        return
    lines = [f"{g['id']}: {g['name']}" for g in groups]
    await message.answer("\n".join(lines))


@dp.message(Command("group_remove"))
async def cmd_group_remove(message: Message):
    if not allowed(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer("Пришли: /group_remove <id группы из /group_list>")
        return
    group_id = int(parts[1].strip())
    ok = storage.remove_group(group_id, message.from_user.id)
    await message.answer("Группа и все её поиски удалены." if ok else "Не найдено (или чужая группа).")


# ---------------- searches ----------------

@dp.message(Command("add"))
async def cmd_add(message: Message):
    if not allowed(message.from_user.id):
        return
    parts = message.text.split()
    args = parts[1:]
    if len(args) < 2 or not args[0].isdigit():
        await message.answer(
            "Пришли: /add <id группы> <ссылка1> [ссылка2] ...\n"
            "Группу сначала создай через /group_add."
        )
        return
    group_id = int(args[0])
    group = storage.get_group(group_id, message.from_user.id)
    if not group:
        await message.answer("Такой группы нет (или она не твоя). Сначала /group_add.")
        return
    urls = [u for u in args[1:] if "olx.uz" in u]
    skipped = len(args[1:]) - len(urls)
    if not urls:
        await message.answer("Не нашёл ни одной ссылки на olx.uz в сообщении.")
        return

    status = await message.answer(f"Добавляю {len(urls)} поиск(ов) в группу «{group['name']}»...")
    summary_lines = []
    async with aiohttp.ClientSession() as session:
        for url in urls:
            search_id = storage.add_search(group_id, url)
            try:
                ads = await parse_search_page(session, url)
            except Exception:
                log.exception("baseline fetch failed for %s", url)
                ads = []
            for ad in ads:
                storage.mark_seen(group_id, ad.ad_id)
            summary_lines.append(
                f"{search_id}: {short_query_label(url)} - сейчас {len(ads)} объявлений (не будут присланы)"
            )

    text = "\n".join(summary_lines)
    if skipped:
        text += f"\n\nПропущено {skipped} аргумент(ов) - не похоже на ссылку olx.uz."
    await status.edit_text(text)


@dp.message(Command("list"))
async def cmd_list(message: Message):
    if not allowed(message.from_user.id):
        return
    searches = storage.list_searches_for_user(message.from_user.id)
    if not searches:
        await message.answer("Нет активных поисков. Начни с /group_add, потом /add.")
        return

    groups_order = []
    groups_map = {}
    for s in searches:
        gid = s["group_id"]
        if gid not in groups_map:
            groups_map[gid] = {"name": s["group_name"], "items": []}
            groups_order.append(gid)
        groups_map[gid]["items"].append(s)

    lines = []
    for gid in groups_order:
        g = groups_map[gid]
        lines.append(f"<b>Группа {gid}: {esc(g['name'])}</b> (увидено {storage.count_seen(gid)})")
        for s in g["items"]:
            lines.append(f"  {s['id']}: {esc(short_query_label(s['url']))}")
        lines.append("")

    await message.answer("\n".join(lines).strip(), parse_mode="HTML")


@dp.message(Command("remove"))
async def cmd_remove(message: Message):
    if not allowed(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer("Пришли: /remove <id поиска из /list>")
        return
    search_id = int(parts[1].strip())
    ok = storage.remove_search(search_id, message.from_user.id)
    await message.answer("Удалено." if ok else "Не найдено (или чужой поиск).")


# ---------------- checking ----------------

async def process_new_ad(session: aiohttp.ClientSession, chat_id: int, user_id: int, group_id: int, ad) -> str:
    """Fetch details for one new ad, apply dislike filtering, send if not
    filtered, mark it seen for the group. Returns 'sent', 'skipped' or
    'failed'."""
    try:
        details = await parse_ad_details(session, ad)
    except Exception:
        log.exception("failed to fetch details for %s", ad.url)
        storage.mark_seen(group_id, ad.ad_id)
        return "failed"

    fp = make_fp(details)
    storage.cache_ad(fp, details.ad_id, details.title, details.price, details.url)

    if storage.is_disliked(user_id, fp):
        storage.mark_seen(group_id, ad.ad_id)
        return "skipped"

    try:
        await send_ad(chat_id, details, fp)
    except Exception:
        log.exception("failed to send ad %s to %s", ad.url, chat_id)
        storage.mark_seen(group_id, ad.ad_id)
        return "failed"

    storage.mark_seen(group_id, ad.ad_id)
    return "sent"


@dp.message(Command("check"))
async def cmd_check(message: Message):
    if not allowed(message.from_user.id):
        return
    searches = storage.list_searches_for_user(message.from_user.id)
    if not searches:
        await message.answer("Нет активных поисков.")
        return
    status = await message.answer("Проверяю...")
    sent = skipped = failed = 0
    async with aiohttp.ClientSession() as session:
        for s in searches:
            try:
                ads = await parse_search_page(session, s["url"])
            except Exception:
                log.exception("manual check failed for %s (%s)", s["id"], s["url"])
                failed += 1
                continue
            new_ads = [a for a in ads if not storage.is_seen(s["group_id"], a.ad_id)]
            for ad in reversed(new_ads):
                result = await process_new_ad(session, message.chat.id, message.from_user.id, s["group_id"], ad)
                if result == "sent":
                    sent += 1
                elif result == "skipped":
                    skipped += 1
                else:
                    failed += 1
                await asyncio.sleep(0.5)
    await status.edit_text(
        f"Готово. Новых: {sent}. Пропущено (не интересно): {skipped}. Ошибок: {failed}."
    )


@dp.message(Command("latest"))
async def cmd_latest(message: Message):
    if not allowed(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer("Пришли: /latest <id поиска из /list>")
        return
    search_id = int(parts[1].strip())
    searches = storage.list_searches_for_user(message.from_user.id)
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
    fp = make_fp(details)
    storage.cache_ad(fp, details.ad_id, details.title, details.price, details.url)
    await send_ad(message.chat.id, details, fp)
    if latest.posted:
        await message.answer(f"Метка даты с карточки поиска: {esc(latest.posted)}", parse_mode="HTML")


# ---------------- favorites / dislike / comment ----------------

@dp.message(Command("favorites"))
async def cmd_favorites(message: Message):
    if not allowed(message.from_user.id):
        return
    favs = storage.list_favorites(message.from_user.id)
    if not favs:
        await message.answer("Избранное пусто.")
        return
    lines = []
    for f in favs:
        price = f" — {esc(f['price'])}" if f["price"] else ""
        lines.append(f'<a href="{esc(f["url"])}">{esc(f["title"]) or f["url"]}</a>{price}')
    await message.answer("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@dp.callback_query(F.data.startswith("fav:"))
async def cb_fav(query: CallbackQuery):
    if not allowed(query.from_user.id):
        await query.answer()
        return
    fp = query.data.split(":", 1)[1]
    cached = storage.get_cached_ad(fp)
    if not cached:
        await query.answer("Информация об объявлении устарела.", show_alert=True)
        return
    storage.add_favorite(query.from_user.id, fp, cached["ad_id"], cached["title"], cached["price"], cached["url"])
    await query.answer("Добавлено в избранное ⭐")


@dp.callback_query(F.data.startswith("dis:"))
async def cb_dislike(query: CallbackQuery):
    if not allowed(query.from_user.id):
        await query.answer()
        return
    fp = query.data.split(":", 1)[1]
    storage.add_dislike(query.from_user.id, fp)
    await query.answer("Больше не покажу это объявление (и его переподачи) 👎")


@dp.callback_query(F.data.startswith("com:"))
async def cb_comment(query: CallbackQuery, state: FSMContext):
    if not allowed(query.from_user.id):
        await query.answer()
        return
    fp = query.data.split(":", 1)[1]
    existing = storage.get_comment(query.from_user.id, fp)
    await state.set_state(CommentStates.waiting)
    await state.update_data(fp=fp)
    prompt = "Пришли текст комментария к этому объявлению."
    if existing:
        prompt = f"Текущий комментарий: {esc(existing)}\n\nПришли новый текст, чтобы заменить его."
    await query.message.reply(prompt, parse_mode="HTML")
    await query.answer()


# ---------------- sending ----------------

def build_caption(details, comment: str = None) -> str:
    """Single message per ad, structured like the ad itself: title + price
    up top, description as the body, saved comment (if any), then phone
    number and source link at the bottom."""
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

    if comment:
        parts.append(f"💬 Твой комментарий: {esc(comment)}")

    footer_bits = []
    if details.phone:
        footer_bits.append(f"<u>{esc(details.phone)}</u>")
    else:
        footer_bits.append("номер не удалось извлечь")
    footer_bits.append(f'<a href="{esc(details.url)}">olx.uz</a>')
    parts.append(" • ".join(footer_bits))

    return "\n\n".join(parts)


def build_keyboard(fp: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⭐ Избранное", callback_data=f"fav:{fp}"),
        InlineKeyboardButton(text="👎 Не интересно", callback_data=f"dis:{fp}"),
        InlineKeyboardButton(text="💬 Комментарий", callback_data=f"com:{fp}"),
    ]])


async def send_ad(chat_id: int, details, fp: str):
    comment = storage.get_comment(chat_id, fp)
    caption = build_caption(details, comment)
    keyboard = build_keyboard(fp)

    if details.photos:
        media = [InputMediaPhoto(media=u) for u in details.photos[:10]]
        media[0].caption = caption
        media[0].parse_mode = "HTML"
        try:
            await bot.send_media_group(chat_id, media)
            # Telegram does not allow inline keyboards on media groups, so
            # the action buttons go in a small follow-up message
            await bot.send_message(chat_id, "Действия к объявлению выше 👆", reply_markup=keyboard)
            return
        except Exception:
            log.exception("send_media_group failed, falling back to text for %s", details.url)

    if len(caption) > 4000:
        caption = caption[:4000] + "…"
    await bot.send_message(
        chat_id, caption, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=False
    )


# ---------------- polling ----------------

async def poll_once():
    searches = storage.get_all_active_searches()
    if not searches:
        log.info("poll: no active searches")
        return
    sent = skipped = failed = 0
    async with aiohttp.ClientSession() as session:
        for s in searches:
            try:
                ads = await parse_search_page(session, s["url"])
            except Exception:
                log.exception("failed to fetch search %s (%s)", s["search_id"], s["url"])
                failed += 1
                continue
            new_ads = [a for a in ads if not storage.is_seen(s["group_id"], a.ad_id)]
            for ad in reversed(new_ads):
                result = await process_new_ad(session, s["user_id"], s["user_id"], s["group_id"], ad)
                if result == "sent":
                    sent += 1
                elif result == "skipped":
                    skipped += 1
                else:
                    failed += 1
                await asyncio.sleep(0.5)  # gentle with telegram, olx pacing is inside _get
    log.info(
        "poll done: %d searches checked, %d new ads sent, %d skipped (disliked), %d failed",
        len(searches), sent, skipped, failed,
    )


async def main():
    await bot.set_my_commands(BOT_COMMANDS)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(poll_once, "interval", seconds=POLL_INTERVAL_SECONDS)
    scheduler.start()
    log.info("polling every %s seconds", POLL_INTERVAL_SECONDS)
    await dp.start_polling(bot)


# NOTE: this catch-all must stay registered after every /command handler
# above, so real commands typed while a comment is pending still work.
@dp.message(CommentStates.waiting)
async def save_comment(message: Message, state: FSMContext):
    data = await state.get_data()
    fp = data.get("fp")
    await state.clear()
    if not fp or not message.text:
        return
    storage.upsert_comment(message.from_user.id, fp, message.text.strip())
    await message.answer("Комментарий сохранён 💬")


if __name__ == "__main__":
    asyncio.run(main())
