import asyncio
import logging
import os
import re
import secrets
import tempfile
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from telegram import Update, InputSticker, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)
from telegram.error import TelegramError
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("fstik-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
DATABASE_URL = os.environ["DATABASE_URL"]
PUBLIC_URL = os.environ["RENDER_EXTERNAL_URL"].rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET") or secrets.token_urlsafe(32)
MAX_STICKER_BYTES = 256 * 1024
MAX_INPUT_BYTES = 20 * 1024 * 1024  # Telegram cloud Bot API download ceiling
TMP_DIR = Path("/tmp/fstik")
TMP_DIR.mkdir(parents=True, exist_ok=True)

pool = ConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=4,
                      kwargs={"row_factory": dict_row}, open=False)
app = FastAPI(title="Private Telegram Animated Sticker Bot")
tg_app: Application | None = None
bot_username = ""

# Short-lived conversation state; durable pack data stays in PostgreSQL.
WAIT_TITLE = "wait_title"
WAIT_EMOJI = "wait_emoji"
WAIT_RENAME = "wait_rename"
WAIT_MEDIA = "wait_media"
WAIT_SHAPE = "wait_shape"
SHAPES = {"square", "rectangle", "rounded", "circle", "oval", "heart", "star"}


def init_db():
    with pool.connection() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            telegram_user_id BIGINT PRIMARY KEY, draft_pack_title TEXT,
            active_pack_id BIGINT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.execute("""CREATE TABLE IF NOT EXISTS packs (
            id BIGSERIAL PRIMARY KEY, owner_id BIGINT NOT NULL, title TEXT NOT NULL,
            short_name TEXT NOT NULL UNIQUE, sticker_count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        conn.execute("""CREATE TABLE IF NOT EXISTS sticker_items (
            id BIGSERIAL PRIMARY KEY,
            pack_id BIGINT NOT NULL REFERENCES packs(id) ON DELETE CASCADE,
            telegram_file_id TEXT NOT NULL, source_type TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        # Additive migrations only: preserve existing users, packs and records.
        conn.execute("ALTER TABLE packs ADD COLUMN IF NOT EXISTS emoji TEXT NOT NULL DEFAULT '✨'")
        conn.execute("ALTER TABLE packs ADD COLUMN IF NOT EXISTS shape TEXT NOT NULL DEFAULT 'square'")
        conn.execute("ALTER TABLE sticker_items ADD COLUMN IF NOT EXISTS file_unique_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS sticker_items_pack_idx ON sticker_items(pack_id)")
        conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS sticker_items_unique_idx
            ON sticker_items(pack_id, file_unique_id) WHERE file_unique_id IS NOT NULL""")
        conn.commit()


def ensure_user(uid):
    with pool.connection() as c:
        c.execute("INSERT INTO users (telegram_user_id) VALUES (%s) ON CONFLICT DO NOTHING", (uid,))
        c.commit()


def get_user(uid):
    ensure_user(uid)
    with pool.connection() as c:
        return c.execute("SELECT * FROM users WHERE telegram_user_id=%s", (uid,)).fetchone()


def update_user(uid, **fields):
    ensure_user(uid)
    allowed = {"draft_pack_title", "active_pack_id"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    sql = ", ".join(f"{k}=%s" for k in fields) + ", updated_at=NOW()"
    with pool.connection() as c:
        c.execute(f"UPDATE users SET {sql} WHERE telegram_user_id=%s", (*fields.values(), uid))
        c.commit()


def create_pack(uid, title, short_name, emoji, shape="square"):
    with pool.connection() as c:
        row = c.execute("""INSERT INTO packs(owner_id,title,short_name,emoji,shape)
            VALUES(%s,%s,%s,%s,%s) RETURNING id""", (uid, title, short_name, emoji, shape)).fetchone()
        c.commit()
        return row["id"]


def get_pack(pack_id, uid):
    if not pack_id:
        return None
    with pool.connection() as c:
        return c.execute("SELECT * FROM packs WHERE id=%s AND owner_id=%s", (pack_id, uid)).fetchone()


def list_packs(uid):
    with pool.connection() as c:
        return c.execute("SELECT * FROM packs WHERE owner_id=%s ORDER BY created_at DESC", (uid,)).fetchall()


def list_stickers(pack_id):
    with pool.connection() as c:
        return c.execute("SELECT * FROM sticker_items WHERE pack_id=%s ORDER BY id", (pack_id,)).fetchall()


def save_sticker(pack_id, sticker, source_type):
    with pool.connection() as c:
        c.execute("""INSERT INTO sticker_items(pack_id,telegram_file_id,source_type,file_unique_id)
            VALUES(%s,%s,%s,%s) ON CONFLICT (pack_id,file_unique_id)
            WHERE file_unique_id IS NOT NULL DO NOTHING""",
            (pack_id, sticker.file_id, source_type, sticker.file_unique_id))
        c.execute("UPDATE packs SET sticker_count=(SELECT count(*) FROM sticker_items WHERE pack_id=%s) WHERE id=%s",
                  (pack_id, pack_id))
        c.commit()


def remove_sticker_record(item_id, pack_id):
    with pool.connection() as c:
        c.execute("DELETE FROM sticker_items WHERE id=%s AND pack_id=%s", (item_id, pack_id))
        c.execute("UPDATE packs SET sticker_count=(SELECT count(*) FROM sticker_items WHERE pack_id=%s) WHERE id=%s",
                  (pack_id, pack_id))
        c.commit()


def delete_pack_record(pack_id, uid):
    with pool.connection() as c:
        c.execute("DELETE FROM packs WHERE id=%s AND owner_id=%s", (pack_id, uid))
        c.execute("UPDATE users SET active_pack_id=NULL WHERE telegram_user_id=%s AND active_pack_id=%s",
                  (uid, pack_id))
        c.commit()


def set_pack_shape(pack_id, uid, shape):
    if shape not in SHAPES:
        return
    with pool.connection() as c:
        c.execute("UPDATE packs SET shape=%s WHERE id=%s AND owner_id=%s", (shape, pack_id, uid))
        c.commit()

def rename_pack_record(pack_id, uid, title):
    with pool.connection() as c:
        c.execute("UPDATE packs SET title=%s WHERE id=%s AND owner_id=%s", (title, pack_id, uid))
        c.commit()


def safe_slug(title):
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", title.strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    if not slug or not slug[0].isalpha():
        slug = "pack_" + slug
    return slug[:18]


def is_owner(update):
    u, ch = update.effective_user, update.effective_chat
    return bool(u and ch and u.id == OWNER_ID and ch.type == "private")


async def deny(update):
    if update.effective_message:
        await update.effective_message.reply_text("🔒 This bot is private and restricted to its owner.")


def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Create Pack", callback_data="create"),
         InlineKeyboardButton("📚 My Packs", callback_data="packs")],
        [InlineKeyboardButton("🎯 Select Active Pack", callback_data="select"),
         InlineKeyboardButton("⚙️ Help", callback_data="help")],
    ])


async def show_menu(message, text="🎬 *FSticker Control Panel*\nChoose an action:"):
    await message.reply_text(text, parse_mode="Markdown", reply_markup=main_keyboard())


async def start(update, context):
    if not is_owner(update):
        return await deny(update)
    context.user_data.pop("flow", None)
    await show_menu(update.effective_message,
        "👋 *Welcome to FSticker*\nSend a GIF/video after selecting or creating a pack.\n\nChoose an action:")


async def help_cmd(update, context):
    if not is_owner(update):
        return await deny(update)
    await update.effective_message.reply_text(
        "Create a pack, choose its emoji once, then send GIFs/videos. "
        "Each conversion is added automatically and the finished sticker is sent back here.\n"
        "Videos are trimmed to their first 3 seconds. Use the buttons to manage packs and stickers.",
        reply_markup=main_keyboard())


async def refresh_pack_stickers(bot, pack):
    """Reconcile DB with Telegram's authoritative sticker-set contents."""
    sticker_set = await bot.get_sticker_set(pack["short_name"])
    actual = {s.file_unique_id: s for s in sticker_set.stickers}
    with pool.connection() as c:
        known = c.execute("SELECT file_unique_id FROM sticker_items WHERE pack_id=%s AND file_unique_id IS NOT NULL",
                          (pack["id"],)).fetchall()
        known_ids = {r["file_unique_id"] for r in known}
        # Remove stale records (e.g. stickers deleted outside this bot).
        c.execute("DELETE FROM sticker_items WHERE pack_id=%s AND file_unique_id IS NOT NULL AND file_unique_id <> ALL(%s)",
                  (pack["id"], list(actual.keys()) or [""]))
        for unique_id, sticker in actual.items():
            if unique_id not in known_ids:
                c.execute("""INSERT INTO sticker_items(pack_id,telegram_file_id,source_type,file_unique_id)
                    VALUES(%s,%s,%s,%s)""", (pack["id"], sticker.file_id, "existing/synced", unique_id))
            else:
                c.execute("UPDATE sticker_items SET telegram_file_id=%s WHERE pack_id=%s AND file_unique_id=%s",
                          (sticker.file_id, pack["id"], unique_id))
        # Old versions stored placeholder strings instead of real Telegram sticker IDs.
        c.execute("DELETE FROM sticker_items WHERE pack_id=%s AND file_unique_id IS NULL", (pack["id"],))
        c.execute("UPDATE packs SET sticker_count=%s WHERE id=%s", (len(actual), pack["id"]))
        c.commit()
    return sticker_set


async def show_packs(query, uid, mode="manage"):
    packs = list_packs(uid)
    if not packs:
        await query.edit_message_text("You have no packs yet. Press Create Pack.", reply_markup=main_keyboard())
        return
    rows = []
    for p in packs:
        label = f"{p['title']} ({p['sticker_count']})"
        action = "choose" if mode == "select" else "open"
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"{action}:{p['id']}")])
    rows.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="home")])
    await query.edit_message_text("📚 *Your sticker packs*\nChoose a pack:", parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(rows))


async def show_pack_detail(query, pack):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Set Active", callback_data=f"active:{pack['id']}")],
        [InlineKeyboardButton("🖊 Edit Title", callback_data=f"rename:{pack['id']}"),
         InlineKeyboardButton("✂️ Shape", callback_data=f"shapeedit:{pack['id']}")],
        [InlineKeyboardButton("🗑 Delete Stickers", callback_data=f"stickers:{pack['id']}")],
        [InlineKeyboardButton("❌ Delete Entire Pack", callback_data=f"delpack:{pack['id']}")],
        [InlineKeyboardButton("⬅️ My Packs", callback_data="packs")],
    ])
    await query.edit_message_text(
        f"📦 *{pack['title']}*\nEmoji: {pack['emoji']}\nShape: {pack.get('shape', 'square')}\nStickers recorded: {pack['sticker_count']}\n"
        f"[Open pack](https://t.me/addstickers/{pack['short_name']})",
        parse_mode="Markdown", reply_markup=kb, disable_web_page_preview=True)


def shape_keyboard(target):
    labels = [("Square", "square"), ("Rectangle", "rectangle"), ("Rounded", "rounded"),
              ("Circle", "circle"), ("Oval", "oval"), ("Heart", "heart"), ("Star", "star")]
    rows = [[InlineKeyboardButton(label, callback_data=f"setshape:{target}:{value}")] for label, value in labels]
    rows.append([InlineKeyboardButton("Cancel", callback_data="home" if target == "new" else f"open:{target}")])
    return InlineKeyboardMarkup(rows)


async def callback_handler(update, context):
    q = update.callback_query
    if not is_owner(update):
        await q.answer("Not authorised", show_alert=True)
        return
    await q.answer()
    data = q.data
    if data == "home":
        context.user_data.pop("flow", None)
        await q.edit_message_text("🎬 *FSticker Control Panel*\nChoose an action:", parse_mode="Markdown", reply_markup=main_keyboard())
    elif data == "create":
        context.user_data["flow"] = WAIT_TITLE
        await q.message.reply_text("Send the new sticker pack title (up to 64 characters).")
    elif data == "packs":
        await show_packs(q, OWNER_ID)
    elif data == "select":
        await show_packs(q, OWNER_ID, "select")
    elif data == "help":
        await q.message.reply_text("Create a pack, select it as active, then send GIFs/videos. Converted stickers are added automatically and sent back to you.", reply_markup=main_keyboard())
    elif data == "shape_new":
        await q.edit_message_text("Choose the default shape for this new pack:", reply_markup=shape_keyboard("new"))
    elif data.startswith("shapeedit:"):
        pack_id = int(data.split(":", 1)[1])
        pack = get_pack(pack_id, OWNER_ID)
        if pack:
            await q.edit_message_text(f"Choose the shape for *{pack['title']}*. This applies to stickers added from now on.", parse_mode="Markdown", reply_markup=shape_keyboard(str(pack_id)))
    elif data.startswith("setshape:"):
        _, target, shape = data.split(":", 2)
        if shape not in SHAPES:
            return
        if target == "new":
            context.user_data["pack_shape"] = shape
            context.user_data["flow"] = WAIT_MEDIA
            await q.edit_message_text(f"✅ Default shape: *{shape}*. Now send the first GIF or video to create the pack.", parse_mode="Markdown")
        else:
            pack_id = int(target)
            pack = get_pack(pack_id, OWNER_ID)
            if pack:
                set_pack_shape(pack_id, OWNER_ID, shape)
                await q.edit_message_text(f"✅ Shape changed to *{shape}*. It will apply to new stickers.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to pack", callback_data=f"open:{pack_id}")]]))
    elif data.startswith("delsticker:"):
        _, p_id, item_id = data.split(":")
        pack_id, item_id = int(p_id), int(item_id)
        pack = get_pack(pack_id, OWNER_ID)
        if not pack:
            await q.edit_message_text("Pack not found.")
            return
        with pool.connection() as c:
            item = c.execute("SELECT * FROM sticker_items WHERE id=%s AND pack_id=%s", (item_id, pack_id)).fetchone()
        if not item or not item["file_unique_id"]:
            await q.edit_message_text("I couldn't identify that sticker safely. Reopen Manage Stickers to sync the pack.")
            return
        try:
            await context.bot.delete_sticker_from_set(item["telegram_file_id"])
        except TelegramError as exc:
            await q.edit_message_text(f"Telegram couldn't delete the sticker: {exc}")
            return
        remove_sticker_record(item_id, pack_id)
        await q.edit_message_text("✅ Sticker deleted.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Manage remaining stickers", callback_data=f"stickers:{pack_id}")],
            [InlineKeyboardButton("Back to pack", callback_data=f"open:{pack_id}")]]))
    elif ":" in data:
        action, raw_id = data.split(":", 1)
        try:
            pack_id = int(raw_id)
        except ValueError:
            return
        pack = get_pack(pack_id, OWNER_ID)
        if not pack:
            await q.edit_message_text("That pack no longer exists.", reply_markup=main_keyboard())
            return
        if action in ("open", "choose"):
            if action == "choose":
                update_user(OWNER_ID, active_pack_id=pack_id, draft_pack_title=None)
                await q.edit_message_text(f"✅ Active pack: *{pack['title']}*\nNow send a GIF or video.", parse_mode="Markdown",
                                          reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Main Menu", callback_data="home")]]))
            else:
                await show_pack_detail(q, pack)
        elif action == "active":
            update_user(OWNER_ID, active_pack_id=pack_id, draft_pack_title=None)
            await q.edit_message_text(f"✅ *{pack['title']}* is now your active pack. Send a GIF or video to add stickers.",
                                      parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Main Menu", callback_data="home")]]))
        elif action == "rename":
            context.user_data["flow"] = WAIT_RENAME
            context.user_data["rename_pack_id"] = pack_id
            await q.message.reply_text(f"Send the new title for *{pack['title']}*.", parse_mode="Markdown")
        elif action == "stickers":
            try:
                await refresh_pack_stickers(context.bot, pack)
            except TelegramError as exc:
                await q.edit_message_text(f"Telegram couldn't load this pack: {exc}")
                return
            items = list_stickers(pack_id)
            if not items:
                await q.edit_message_text("This pack has no stickers.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"open:{pack_id}")]]))
                return
            rows = [[InlineKeyboardButton(f"🗑 Delete sticker {i+1}", callback_data=f"delsticker:{pack_id}:{it['id']}")] for i, it in enumerate(items)]
            rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"open:{pack_id}")])
            await q.edit_message_text("Choose the sticker to delete:", reply_markup=InlineKeyboardMarkup(rows))
        elif action == "delpack":
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Yes, delete pack", callback_data=f"confirmdel:{pack_id}"),
                                        InlineKeyboardButton("Cancel", callback_data=f"open:{pack_id}")]])
            await q.edit_message_text(f"⚠️ Delete *{pack['title']}* and all its stickers? This cannot be undone.", parse_mode="Markdown", reply_markup=kb)
        elif action == "confirmdel":
            try:
                await context.bot.delete_sticker_set(pack["short_name"])
            except TelegramError as exc:
                await q.edit_message_text(f"Telegram did not delete the pack: {exc}\nYour database record was kept.")
                return
            delete_pack_record(pack_id, OWNER_ID)
            await q.edit_message_text("✅ Sticker pack deleted.", reply_markup=main_keyboard())
    elif data.startswith("confirmdel:"):
        # handled in the colon branch above
        pass


def get_media(message):
    if message.animation:
        return message.animation, "animation"
    if message.video:
        return message.video, "video"
    if message.document:
        mime = (message.document.mime_type or "").lower()
        name = (message.document.file_name or "").lower()
        if mime == "image/gif" or name.endswith(".gif"):
            return message.document, "GIF"
        if mime.startswith("video/") or name.endswith((".mp4", ".mov", ".mkv", ".webm")):
            return message.document, "video"
    return None, None


async def convert_to_webm(source: Path, output: Path, shape="square"):
    """Encode the first three seconds to a transparent 512x512 VP9 sticker."""
    shape = shape if shape in SHAPES else "square"
    # Alpha-mask expressions use normalized coordinates (0..1) on the 512 canvas.
    masks = {
        "rounded": "if(gte(X,28)*lte(X,W-29)*gte(Y,28)*lte(Y,H-29),255,if(lte(pow(max(28-X,0),2)+pow(max(X-(W-29),0),2)+pow(max(28-Y,0),2)+pow(max(Y-(H-29),0),2),784),255,0))",
        "circle": "if(lte(pow(X-W/2,2)+pow(Y-H/2,2),pow(min(W,H)/2,2)),255,0)",
        "oval": "if(lte(pow((X-W/2)/(W/2),2)+pow((Y-H/2)/(H/2),2),1),255,0)",
        "heart": "if(lte(pow(pow(X-W/2,2)+pow(Y-H*0.38,2),0.5),W*0.22)+gte(Y,H*0.48)*lte(abs(X-W/2),W*0.48*(1-(Y-H*0.48)/(H*0.52))),255,0)",
        "star": "if(gte(cos(atan2(Y-H/2,X-W/2))*cos(atan2(Y-H/2,X-W/2))*0+1,0),if(lte(hypot(X-W/2,Y-H/2),min(W,H)*0.48*(0.62+0.38*cos(5*atan2(Y-H/2,X-W/2)))),255,0),0)",
    }
    vf = ("fps=30,scale=512:512:force_original_aspect_ratio=decrease:flags=lanczos,"
          "pad=512:512:(ow-iw)/2:(oh-ih)/2:color=black@0,format=rgba")
    if shape in masks:
        vf += f",geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{masks[shape]}'"
    vf += ",format=yuva420p"
    last_error = ""
    for bitrate in ("450k", "320k", "230k", "160k", "110k", "75k"):
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
               "-t", "3", "-an", "-vf", vf, "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
               "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", "600k", "-deadline", "good",
               "-cpu-used", "5", "-auto-alt-ref", "0", "-metadata:s:v:0", "alpha_mode=1",
               "-f", "webm", str(output)]
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(proc.communicate(), timeout=100)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            last_error = "Encoding took too long on the free hosting instance."
            continue
        if proc.returncode == 0 and output.exists():
            size = output.stat().st_size
            if size <= MAX_STICKER_BYTES:
                return
            last_error = f"Compressed output was {size // 1024} KB."
        else:
            last_error = err.decode("utf-8", "ignore")[-500:]
    raise ValueError("Could not encode this clip under Telegram's 256 KB sticker limit. Try a simpler clip. " + last_error)


async def handle_text(update, context):
    if not is_owner(update):
        return
    flow = context.user_data.get("flow")
    text = (update.effective_message.text or "").strip()
    if not flow or text.startswith("/"):
        return
    if flow == WAIT_TITLE:
        if not text or len(text) > 64:
            await update.effective_message.reply_text("Please send a title between 1 and 64 characters.")
            return
        context.user_data["draft_title"] = text
        context.user_data["flow"] = WAIT_EMOJI
        await update.effective_message.reply_text("Now send ONE emoji to use for every sticker in this pack (for example 😂).")
    elif flow == WAIT_EMOJI:
        # Accept one visible emoji grapheme; Telegram validates association when creating sticker.
        emoji = text
        if len(emoji) > 16 or not any(ord(ch) > 127 for ch in emoji):
            await update.effective_message.reply_text("Please send one emoji, such as 😂 or ❤️.")
            return
        context.user_data["pack_emoji"] = emoji
        context.user_data["flow"] = WAIT_SHAPE
        await update.effective_message.reply_text(
            f"📦 Emoji saved: {emoji}. Choose the default sticker shape:", reply_markup=shape_keyboard("new"))
    elif flow == WAIT_RENAME:
        if len(text) > 64:
            await update.effective_message.reply_text("Title must be 64 characters or fewer.")
            return
        pack_id = context.user_data.get("rename_pack_id")
        pack = get_pack(pack_id, OWNER_ID)
        if not pack:
            context.user_data.pop("flow", None)
            await update.effective_message.reply_text("Pack not found.", reply_markup=main_keyboard())
            return
        try:
            await context.bot.set_sticker_set_title(pack["short_name"], text)
            rename_pack_record(pack_id, OWNER_ID, text)
            await update.effective_message.reply_text("✅ Pack title updated.", reply_markup=main_keyboard())
        except TelegramError as exc:
            await update.effective_message.reply_text(f"Telegram couldn't change the title: {exc}")
        context.user_data.pop("flow", None)


async def handle_media(update, context):
    if not is_owner(update):
        return await deny(update)
    msg = update.effective_message
    media, source_type = get_media(msg)
    if not media:
        return
    state = get_user(OWNER_ID)
    pack = get_pack(state["active_pack_id"], OWNER_ID) if state["active_pack_id"] else None
    creating = context.user_data.get("flow") == WAIT_MEDIA and context.user_data.get("draft_title")
    if not creating and not pack:
        await msg.reply_text("First choose Create Pack or select an Active Pack from the menu.", reply_markup=main_keyboard())
        return
    file_size = getattr(media, "file_size", None)
    if file_size and file_size > MAX_INPUT_BYTES:
        await msg.reply_text("❌ This file is larger than 20 MB. Please send a smaller GIF/video.")
        return
    status = await msg.reply_text("⏳ Downloading and converting your video into an animated sticker…")
    with tempfile.TemporaryDirectory(dir=TMP_DIR) as td:
        source, output = Path(td) / "input", Path(td) / "sticker.webm"
        try:
            tg_file = await context.bot.get_file(media.file_id, read_timeout=120, write_timeout=120, connect_timeout=30, pool_timeout=30)
            await tg_file.download_to_drive(custom_path=str(source), read_timeout=120, write_timeout=120, connect_timeout=30, pool_timeout=30)
            if source.stat().st_size > MAX_INPUT_BYTES:
                raise ValueError("This file is larger than the 20 MB input limit.")
            shape = context.user_data.get("pack_shape", "square") if creating else pack.get("shape", "square")
            await convert_to_webm(source, output, shape)
            if creating:
                title = context.user_data["draft_title"]
                emoji = context.user_data["pack_emoji"]
                base = safe_slug(title)
                short_name = f"{base}_by_{bot_username.lower()}"
                sticker = InputSticker(sticker=InputFile(output.read_bytes(), filename="sticker.webm"), format="video", emoji_list=[emoji])
                for attempt in range(6):
                    try:
                        await context.bot.create_new_sticker_set(user_id=OWNER_ID, name=short_name,
                            title=title, stickers=[sticker])
                        break
                    except TelegramError as exc:
                        if "OCCUPIED" in str(exc).upper() or "ALREADY OCCUPIED" in str(exc).upper():
                            short_name = f"{base}_{secrets.token_hex(2)}_by_{bot_username.lower()}"
                            continue
                        raise
                else:
                    raise RuntimeError("Couldn't find an available sticker pack name.")
                pack_id = create_pack(OWNER_ID, title, short_name, emoji, context.user_data.get("pack_shape", "square"))
                update_user(OWNER_ID, active_pack_id=pack_id, draft_pack_title=None)
                context.user_data.pop("flow", None)
                context.user_data.pop("draft_title", None)
                context.user_data.pop("pack_emoji", None)
                context.user_data.pop("pack_shape", None)
                pack = get_pack(pack_id, OWNER_ID)
            else:
                sticker = InputSticker(sticker=InputFile(output.read_bytes(), filename="sticker.webm"), format="video", emoji_list=[pack["emoji"]])
                await context.bot.add_sticker_to_set(user_id=OWNER_ID, name=pack["short_name"], sticker=sticker)
            # Telegram is the source of truth for file IDs; sync and send the exact new sticker.
            sticker_set = await context.bot.get_sticker_set(pack["short_name"])
            known = {x["file_unique_id"] for x in list_stickers(pack["id"]) if x["file_unique_id"]}
            new_stickers = [s for s in sticker_set.stickers if s.file_unique_id not in known]
            if new_stickers:
                new_sticker = new_stickers[-1]
            else:
                # For first sticker, DB is empty; otherwise Telegram may deduplicate identical media.
                new_sticker = sticker_set.stickers[-1]
            save_sticker(pack["id"], new_sticker, source_type)
            await status.edit_text(f"✅ Added to *{pack['title']}*.", parse_mode="Markdown")
            await context.bot.send_sticker(chat_id=OWNER_ID, sticker=new_sticker.file_id)
            await context.bot.send_message(chat_id=OWNER_ID,
                text=f"Sticker uploaded successfully.\nPack: {pack['title']}\nhttps://t.me/addstickers/{pack['short_name']}")
        except Exception as exc:
            log.exception("Sticker processing failed")
            await status.edit_text("❌ " + (str(exc)[:850] or "Sticker processing failed."))


async def cancel_cmd(update, context):
    if not is_owner(update):
        return await deny(update)
    context.user_data.pop("flow", None)
    context.user_data.pop("draft_title", None)
    context.user_data.pop("pack_emoji", None)
    context.user_data.pop("pack_shape", None)
    await update.effective_message.reply_text("Cancelled.", reply_markup=main_keyboard())


async def done_cmd(update, context):
    if not is_owner(update):
        return await deny(update)
    update_user(OWNER_ID, active_pack_id=None, draft_pack_title=None)
    context.user_data.pop("flow", None)
    await update.effective_message.reply_text("No active pack selected.", reply_markup=main_keyboard())


async def on_startup():
    global tg_app, bot_username
    pool.open(wait=True)
    init_db()
    from telegram.request import HTTPXRequest
    request = HTTPXRequest(connection_pool_size=16, connect_timeout=30, read_timeout=120, write_timeout=120, pool_timeout=30)
    tg_app = Application.builder().token(BOT_TOKEN).request(request).get_updates_request(request).build()
    await tg_app.initialize()
    me = await tg_app.bot.get_me()
    bot_username = me.username or ""
    if not bot_username:
        raise RuntimeError("Could not determine bot username.")
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("help", help_cmd))
    tg_app.add_handler(CommandHandler("cancel", cancel_cmd))
    tg_app.add_handler(CommandHandler("done", done_cmd))
    tg_app.add_handler(CallbackQueryHandler(callback_handler))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    tg_app.add_handler(MessageHandler(filters.VIDEO | filters.ANIMATION | filters.Document.ALL, handle_media))
    await tg_app.start()
    await tg_app.bot.set_webhook(url=f"{PUBLIC_URL}/telegram/{WEBHOOK_SECRET}",
        secret_token=WEBHOOK_SECRET, allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    log.info("Bot @%s webhook configured", bot_username)


@app.on_event("startup")
async def startup_event():
    await on_startup()


@app.on_event("shutdown")
async def shutdown_event():
    if tg_app:
        await tg_app.bot.delete_webhook()
        await tg_app.stop()
        await tg_app.shutdown()
    pool.close()


@app.get("/")
async def health():
    return {"ok": True, "service": "private-animated-sticker-bot"}


@app.post("/telegram/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if not secrets.compare_digest(secret, WEBHOOK_SECRET):
        raise HTTPException(status_code=404)
    header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not secrets.compare_digest(header_secret, WEBHOOK_SECRET):
        raise HTTPException(status_code=403)
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    message = update.effective_message
    is_media = bool(message and (message.animation or message.video or message.document))
    if is_media:
        # Do not keep Telegram's webhook request open while downloading/encoding media.
        tg_app.create_task(tg_app.process_update(update), update=f"media update {update.update_id}")
    else:
        await tg_app.process_update(update)
    return JSONResponse({"ok": True})
