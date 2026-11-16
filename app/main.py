import asyncio
import logging
import os
import re
import secrets
import tempfile
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from telegram import Update, InputFile, InputSticker
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)
from telegram.error import TelegramError
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("fstik-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
DATABASE_URL = os.environ["DATABASE_URL"]
PUBLIC_URL = os.environ["RENDER_EXTERNAL_URL"].rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET") or secrets.token_urlsafe(32)
PORT = int(os.environ.get("PORT", "10000"))

MAX_STICKER_BYTES = 256 * 1024
TMP_DIR = Path("/tmp/fstik")
TMP_DIR.mkdir(parents=True, exist_ok=True)

# psycopg uses libpq's sslmode=require from Neon connection strings.
pool = ConnectionPool(
    conninfo=DATABASE_URL,
    min_size=1,
    max_size=4,
    kwargs={"row_factory": dict_row},
    open=False,
)

app = FastAPI(title="Private Telegram Animated Sticker Bot")
telegram_app: Application | None = None
bot_username = ""


def init_db():
    with pool.connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_user_id BIGINT PRIMARY KEY,
                draft_pack_title TEXT,
                active_pack_id BIGINT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS packs (
                id BIGSERIAL PRIMARY KEY,
                owner_id BIGINT NOT NULL,
                title TEXT NOT NULL,
                short_name TEXT NOT NULL UNIQUE,
                sticker_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sticker_items (
                id BIGSERIAL PRIMARY KEY,
                pack_id BIGINT NOT NULL REFERENCES packs(id) ON DELETE CASCADE,
                telegram_file_id TEXT NOT NULL,
                source_type TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.commit()


def ensure_user(user_id: int):
    with pool.connection() as conn:
        conn.execute(
            """INSERT INTO users (telegram_user_id) VALUES (%s)
               ON CONFLICT (telegram_user_id) DO NOTHING""",
            (user_id,)
        )
        conn.commit()


def get_user(user_id: int):
    ensure_user(user_id)
    with pool.connection() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE telegram_user_id=%s", (user_id,)
        ).fetchone()


def set_user_state(
    user_id: int,
    *,
    draft_title=None,
    active_pack_id=None,
    update_draft=False,
    update_active=False
):
    ensure_user(user_id)
    fields, values = [], []
    if update_draft:
        fields.append("draft_pack_title=%s")
        values.append(draft_title)
    if update_active:
        fields.append("active_pack_id=%s")
        values.append(active_pack_id)
    fields.append("updated_at=NOW()")
    values.append(user_id)
    with pool.connection() as conn:
        conn.execute(
            f"UPDATE users SET {', '.join(fields)} WHERE telegram_user_id=%s",
            tuple(values)
        )
        conn.commit()


def create_pack(owner_id: int, title: str, short_name: str) -> int:
    with pool.connection() as conn:
        row = conn.execute(
            """INSERT INTO packs (owner_id,title,short_name)
               VALUES (%s,%s,%s) RETURNING id""",
            (owner_id, title, short_name)
        ).fetchone()
        conn.commit()
        return row["id"]


def get_pack(pack_id: int, owner_id: int):
    with pool.connection() as conn:
        return conn.execute(
            "SELECT * FROM packs WHERE id=%s AND owner_id=%s",
            (pack_id, owner_id)
        ).fetchone()


def find_pack(owner_id: int, query: str):
    q = query.strip()
    with pool.connection() as conn:
        return conn.execute(
            """SELECT * FROM packs WHERE owner_id=%s
               AND (short_name=%s OR lower(title)=lower(%s))
               ORDER BY created_at DESC LIMIT 1""",
            (owner_id, q, q)
        ).fetchone()


def list_packs(owner_id: int):
    with pool.connection() as conn:
        return conn.execute(
            "SELECT * FROM packs WHERE owner_id=%s ORDER BY created_at DESC",
            (owner_id,)
        ).fetchall()


def record_sticker(pack_id: int, file_id: str, source_type: str):
    with pool.connection() as conn:
        conn.execute(
            """INSERT INTO sticker_items (pack_id,telegram_file_id,source_type)
               VALUES (%s,%s,%s)""",
            (pack_id, file_id, source_type)
        )
        conn.execute(
            "UPDATE packs SET sticker_count=sticker_count+1 WHERE id=%s",
            (pack_id,)
        )
        conn.commit()


def safe_slug(title: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", title.strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    if not slug or not slug[0].isalpha():
        slug = "pack_" + slug
    return slug[:18]


def authorised(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    return bool(user and chat and user.id == OWNER_ID and chat.type == "private")


async def deny(update: Update):
    if update.effective_message:
        await update.effective_message.reply_text(
            "🔒 This is a private sticker bot. It is restricted to its owner."
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return await deny(update)
    await update.message.reply_text(
        "👋 Welcome to your private animated sticker maker!\n\n"
        "Send a GIF or video and I'll convert it into a Telegram video sticker.\n\n"
        "Commands:\n"
        "/newpack Name — create a new pack\n"
        "/add PackName — select an existing pack\n"
        "/packs — list your packs\n"
        "/done — stop adding to the active pack\n"
        "/cancel — cancel the current action\n"
        "/help — show help\n\n"
        "Only you can use this bot. No custom emoji features."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return await deny(update)
    await update.message.reply_text(
        "/newpack My Reactions — start a new animated sticker pack\n"
        "Then send GIFs or videos one by one.\n\n"
        "/add My Reactions — select an existing pack\n"
        "/packs — list packs and links\n"
        "/done — stop adding to a pack\n"
        "/cancel — cancel a pending new pack\n\n"
        "Input is converted to VP9 WebM video stickers. The first 3 seconds "
        "are used. Telegram's 256 KB limit applies."
    )


async def newpack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return await deny(update)
    title = " ".join(context.args).strip()
    if not title:
        await update.message.reply_text("Usage: /newpack My Sticker Pack")
        return
    if len(title) > 64:
        await update.message.reply_text("Pack title must be 64 characters or fewer.")
        return
    set_user_state(
        OWNER_ID,
        draft_title=title,
        update_draft=True,
        active_pack_id=None,
        update_active=True
    )
    await update.message.reply_text(
        f"📦 New pack: *{title}*\n\nNow send me a GIF or video for its first sticker.",
        parse_mode="Markdown"
    )


async def add_pack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return await deny(update)
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Usage: /add PackName")
        return
    pack = find_pack(OWNER_ID, query)
    if not pack:
        await update.message.reply_text("I couldn't find that pack. Use /packs.")
        return
    set_user_state(
        OWNER_ID,
        draft_title=None,
        update_draft=True,
        active_pack_id=pack["id"],
        update_active=True
    )
    await update.message.reply_text(
        f"✅ Active pack: *{pack['title']}*\nSend a GIF or video to add a sticker.",
        parse_mode="Markdown"
    )


async def packs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return await deny(update)
    packs = list_packs(OWNER_ID)
    if not packs:
        await update.message.reply_text(
            "You haven't created any sticker packs yet. Use /newpack Name."
        )
        return
    lines = ["📚 *YOUR STICKER PACKS*", ""]
    for p in packs:
        lines.append(
            f"• [{p['title']}](https://t.me/addstickers/{p['short_name']}) "
            f"— {p['sticker_count']} sticker(s)"
        )
        lines.append(f"  Short name: `{p['short_name']}`")
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return await deny(update)
    set_user_state(
        OWNER_ID,
        draft_title=None,
        update_draft=True,
        active_pack_id=None,
        update_active=True
    )
    await update.message.reply_text("✅ Finished. No pack is currently active.")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return await deny(update)
    set_user_state(OWNER_ID, draft_title=None, update_draft=True)
    await update.message.reply_text("Cancelled the pending new-pack action.")


def get_media(message):
    if message.animation:
        return message.animation, "GIF/animation"
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


async def convert_to_webm(source: Path, output: Path):
    # Telegram video sticker requirements: VP9/WebM, no audio, max 3 sec,
    # 512x512 canvas, <=256 KiB. Transparent input alpha is retained where possible.
    last_error = ""
    for bitrate in ("450k", "300k", "200k", "140k"):
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source), "-t", "3", "-an",
            "-vf",
            "fps=30,scale=512:512:force_original_aspect_ratio=decrease,"
            "pad=512:512:(ow-iw)/2:(oh-ih)/2:color=black@0,format=yuva420p",
            "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
            "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", "600k",
            "-deadline", "good", "-cpu-used", "5", "-auto-alt-ref", "0",
            "-metadata:s:v:0", "alpha_mode=1",
            "-f", "webm", str(output)
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0 and output.exists():
            if output.stat().st_size <= MAX_STICKER_BYTES:
                return
            last_error = f"Encoded file is {output.stat().st_size // 1024} KB"
        else:
            last_error = stderr.decode("utf-8", "ignore")[-700:]
    raise ValueError(
        "I couldn't compress this media below Telegram's 256 KB sticker limit. "
        "Try a shorter or lower-resolution GIF/video. " + last_error
    )


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return await deny(update)

    message = update.message
    media, source_type = get_media(message)
    if not media:
        await message.reply_text("Please send a GIF or video. Use /help for instructions.")
        return

    state = get_user(OWNER_ID)
    draft_title = state["draft_pack_title"]
    pack = get_pack(state["active_pack_id"], OWNER_ID) if state["active_pack_id"] else None

    if not draft_title and not pack:
        await message.reply_text("First use /newpack Pack Name, or /add PackName.")
        return

    status = await message.reply_text("⏳ Downloading and converting your animation…")

    with tempfile.TemporaryDirectory(dir=TMP_DIR) as temp:
        tempdir = Path(temp)
        source = tempdir / "source_media"
        output = tempdir / "sticker.webm"

        try:
            tg_file = await context.bot.get_file(media.file_id)
            await tg_file.download_to_drive(custom_path=str(source))
            await convert_to_webm(source, output)

            if draft_title:
                base = safe_slug(draft_title)
                username = bot_username.lower()
                short_name = f"{base}_by_{username}"

                # Telegram names are globally unique; add a suffix if already taken.
                for _ in range(5):
                    try:
                        with output.open("rb") as sticker_file:
                            sticker = InputSticker(
                                sticker=InputFile(sticker_file, filename="sticker.webm"),
                                format="video",
                                emoji_list=["✨"]
                            )
                            await context.bot.create_new_sticker_set(
                                user_id=OWNER_ID,
                                name=short_name,
                                title=draft_title,
                                stickers=[sticker]
                            )
                        break
                    except TelegramError as exc:
                        if (
                            "OCCUPIED" in str(exc).upper()
                            or "ALREADY OCCUPIED" in str(exc).upper()
                        ):
                            short_name = (
                                f"{base}_{secrets.token_hex(2)}_by_{username}"
                            )
                            continue
                        raise
                else:
                    raise RuntimeError("Could not find an available sticker-pack name.")

                pack_id = create_pack(OWNER_ID, draft_title, short_name)
                set_user_state(
                    OWNER_ID,
                    draft_title=None,
                    update_draft=True,
                    active_pack_id=pack_id,
                    update_active=True
                )
                record_sticker(pack_id, "created-in-sticker-set", source_type)

                await status.edit_text(
                    f"✅ Pack created: *{draft_title}*\n"
                    f"https://t.me/addstickers/{short_name}\n\n"
                    "Send another GIF/video to add more stickers, or /done.",
                    parse_mode="Markdown",
                    disable_web_page_preview=True
                )
            else:
                with output.open("rb") as sticker_file:
                    sticker = InputSticker(
                        sticker=InputFile(sticker_file, filename="sticker.webm"),
                        format="video",
                        emoji_list=["✨"]
                    )
                    await context.bot.add_sticker_to_set(
                        user_id=OWNER_ID,
                        name=pack["short_name"],
                        sticker=sticker
                    )

                record_sticker(pack["id"], "added-to-sticker-set", source_type)
                await status.edit_text(
                    f"✅ Sticker added to *{pack['title']}*.\n"
                    f"Total stickers recorded: {pack['sticker_count'] + 1}",
                    parse_mode="Markdown"
                )

        except Exception as exc:
            log.exception("Sticker processing failed")
            msg = str(exc)
            await status.edit_text(
                "❌ " + (msg[:900] if msg else "Sticker processing failed.")
            )


async def on_startup():
    global telegram_app, bot_username

    pool.open(wait=True)
    init_db()

    telegram_app = Application.builder().token(BOT_TOKEN).build()
    await telegram_app.initialize()
    await telegram_app.start()

    me = await telegram_app.bot.get_me()
    bot_username = me.username or ""
    if not bot_username:
        raise RuntimeError("Could not determine bot username from Telegram.")

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("help", help_cmd))
    telegram_app.add_handler(CommandHandler("newpack", newpack))
    telegram_app.add_handler(CommandHandler("add", add_pack))
    telegram_app.add_handler(CommandHandler("packs", packs_cmd))
    telegram_app.add_handler(CommandHandler("done", done_cmd))
    telegram_app.add_handler(CommandHandler("cancel", cancel_cmd))
    telegram_app.add_handler(MessageHandler(
        filters.VIDEO | filters.ANIMATION | filters.Document.ALL,
        handle_media
    ))

    await telegram_app.bot.set_webhook(
        url=f"{PUBLIC_URL}/telegram/{WEBHOOK_SECRET}",
        secret_token=WEBHOOK_SECRET,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )
    log.info("Bot @%s webhook configured", bot_username)


@app.on_event("startup")
async def startup_event():
    await on_startup()


@app.on_event("shutdown")
async def shutdown_event():
    global telegram_app
    if telegram_app:
        await telegram_app.bot.delete_webhook()
        await telegram_app.stop()
        await telegram_app.shutdown()
    pool.close()


@app.get("/")
async def health():
    return {"ok": True, "service": "private-animated-sticker-bot"}


@app.post("/telegram/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if not secrets.compare_digest(secret, WEBHOOK_SECRET):
        raise HTTPException(status_code=404)

    header_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token", ""
    )
    if not secrets.compare_digest(header_secret, WEBHOOK_SECRET):
        raise HTTPException(status_code=403)

    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return JSONResponse({"ok": True})
