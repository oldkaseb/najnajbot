# main.py
# -*- coding: utf-8 -*-

import os
import re
import asyncio
from datetime import datetime, timedelta, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageEntity,
)
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    CommandHandler,
    filters,
)
import asyncpg

# --------- ENV ---------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
DATABASE_URL = os.environ.get("DATABASE_URL", "")

CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "SLSHEXED")
CHANNEL_USERNAME_2 = os.environ.get("CHANNEL_USERNAME_2", "dr_gooshad")

def _norm(ch: str) -> str:
    return ch.replace("@", "").strip()

MANDATORY_CHANNELS = []
if _norm(CHANNEL_USERNAME):
    MANDATORY_CHANNELS.append(_norm(CHANNEL_USERNAME))
if _norm(CHANNEL_USERNAME_2) and _norm(CHANNEL_USERNAME_2).lower() != _norm(CHANNEL_USERNAME).lower():
    MANDATORY_CHANNELS.append(_norm(CHANNEL_USERNAME_2))

# ---------- CONST ----------
TRIGGERS = {"نجوا", "درگوشی", "سکرت"}
WHISPER_LIMIT_MIN = 5
GUIDE_DELETE_AFTER_SEC = 180
ALERT_SNIPPET = 190

broadcast_wait_for_banner = set()
forward_wait: dict[int, int] = {}

BOT_USERNAME = ""
BOT_MENTION = ""

# ---------- Helpers ----------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def sanitize(name: str) -> str:
    return (name or "کاربر").replace("<", "").replace(">", "")

def mention_html(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{sanitize(name)}</a>'

def group_link_title(title: str) -> str:
    return sanitize(title or "گروه")

async def safe_delete(bot, chat_id: int, message_id: int, attempts: int = 3, delay: float = 0.6):
    for _ in range(attempts):
        try:
            await bot.delete_message(chat_id, message_id)
            return True
        except Exception:
            await asyncio.sleep(delay)
    return False

async def delete_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id, message_id = context.job.data
    try:
        await context.bot.delete_message(chat_id, message_id)
    except Exception:
        pass

def _channels_text():
    return "، ".join([f"@{ch}" for ch in MANDATORY_CHANNELS]) if MANDATORY_CHANNELS else "—"

# ---------- DB ----------
pool: asyncpg.Pool = None

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS users (
  user_id BIGINT PRIMARY KEY,
  username TEXT,
  first_name TEXT,
  last_seen TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS chats (
  chat_id BIGINT PRIMARY KEY,
  title TEXT,
  type TEXT,
  last_seen TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS whispers (
  id BIGSERIAL PRIMARY KEY,
  group_id BIGINT NOT NULL,
  sender_id BIGINT NOT NULL,
  receiver_id BIGINT NOT NULL,
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'sent',
  created_at TIMESTAMPTZ DEFAULT NOW(),
  message_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_whispers_group ON whispers(group_id);
CREATE INDEX IF NOT EXISTS idx_whispers_sr ON whispers(sender_id, receiver_id);

CREATE TABLE IF NOT EXISTS pending (
  sender_id BIGINT PRIMARY KEY,
  group_id BIGINT NOT NULL,
  receiver_id BIGINT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  guide_message_id INTEGER,
  target_message_id INTEGER
);

CREATE TABLE IF NOT EXISTS watchers (
  group_id BIGINT NOT NULL,
  watcher_id BIGINT NOT NULL,
  PRIMARY KEY (group_id, watcher_id)
);
"""

ALTER_SQL = """
ALTER TABLE pending ADD COLUMN IF NOT EXISTS guide_message_id INTEGER;
ALTER TABLE pending ADD COLUMN IF NOT EXISTS target_message_id INTEGER;
"""

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as con:
        await con.execute(CREATE_SQL)
        await con.execute(ALTER_SQL)

async def upsert_user(u):
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO users (user_id, username, first_name, last_seen)
               VALUES ($1,$2,$3,NOW())
               ON CONFLICT (user_id) DO UPDATE SET
                 username=EXCLUDED.username, first_name=EXCLUDED.first_name, last_seen=NOW();""",
            u.id, u.username, u.first_name or u.full_name
        )

async def upsert_chat(c):
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO chats (chat_id, title, type, last_seen)
               VALUES ($1,$2,$3,NOW())
               ON CONFLICT (chat_id) DO UPDATE SET
                 title=EXCLUDED.title, type=EXCLUDED.type, last_seen=NOW();""",
            c.id, getattr(c, "title", None), c.type
        )

async def get_name_for(user_id: int, fallback: str = "کاربر") -> str:
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT COALESCE(NULLIF(first_name,''), NULLIF(username,'')) AS n FROM users WHERE user_id=$1;",
            user_id
        )
    if row and row["n"]:
        return str(row["n"])
    try:
        return sanitize((await app.bot.get_chat(user_id)).first_name)  # type: ignore
    except Exception:
        return sanitize(fallback)

# ---------- Membership ----------
async def is_member_required_channel(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    if not MANDATORY_CHANNELS:
        return True
    try:
        for ch in MANDATORY_CHANNELS:
            m = await context.bot.get_chat_member(f"@{ch}", user_id)
            if getattr(m, "status", "") not in ("member", "administrator", "creator"):
                return False
        return True
    except Exception:
        # اگر نتوانستیم چک کنیم، آزاد
        return True

def start_keyboard_pre():
    rows = [[InlineKeyboardButton("عضو شدم ✅", callback_data="checksub")]]
    for ch in MANDATORY_CHANNELS:
        rows.append([InlineKeyboardButton(f"عضویت در @{ch}", url=f"https://t.me/{ch}")])
    rows.append([InlineKeyboardButton("افزودن ربات به گروه ➕", url=f"https://t.me/{BOT_USERNAME}?startgroup=true")])
    rows.append([InlineKeyboardButton("ارتباط با پشتیبان 👨🏻‍💻", url="https://t.me/SOULSOWNERBOT")])
    return InlineKeyboardMarkup(rows)

def start_keyboard_post():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("افزودن ربات به گروه ➕", url=f"https://t.me/{BOT_USERNAME}?startgroup=true")],
        [InlineKeyboardButton("ارتباط با پشتیبان 👨🏻‍💻", url="https://t.me/SOULSOWNERBOT")],
    ])

START_TEXT = (
    "سلام! 👋\n\n"
    "برای استفاده، لطفاً ابتدا عضو کانال(های) زیر شوید:\n"
    f"👉 {_channels_text()}\n\n"
    "سپس روی «عضو شدم ✅» بزنید."
)

INTRO_TEXT = (
    "به «درگوشی» خوش آمدید!\n\n"
    "روش ۱) روی پیام هدف Reply کنید و یکی از «نجوا / درگوشی / سکرت» را بفرستید؛ سپس متن را در پیوی ارسال کنید.\n"
    "روش ۲) داخل گروه بنویسید:  {bot} متنِ‌نجوا @username  (بدون ریپلای)\n"
    f"⏳ مهلت ارسال متن در پیوی: {WHISPER_LIMIT_MIN} دقیقه."
)

HELP_TEXT = (
    "راهنما:\n\n"
    "• روش ریپلای: روی پیام فرد هدف Reply کنید و «نجوا / درگوشی / سکرت» را بفرستید؛ بعد متن نجوا را در پیوی بفرستید.\n"
    "• روش سریع:  {bot} متنِ‌نجوا @username  → نجوا همان‌جا ساخته می‌شود.\n"
    "• فقط پیام متنی پذیرفته می‌شود.\n"
    "• نمایش پیام فقط برای فرستنده و گیرنده است."
)

async def maybe_send_waiting_pm(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    async with pool.acquire() as con:
        row = await con.fetchrow("SELECT expires_at FROM pending WHERE sender_id=$1 AND expires_at>NOW();", user_id)
    if row:
        left = int((row["expires_at"] - now_utc()).total_seconds() // 60) + 1
        await context.bot.send_message(
            user_id,
            f"⏳ در انتظار متن نجوا… (مهلت حدود {left} دقیقه)\n"
            "پیام *متنی* خود را همین‌جا ارسال کنید.",
            parse_mode=ParseMode.MARKDOWN
        )

async def send_pm_or_prompt_in_group(context: ContextTypes.DEFAULT_TYPE, user, group_msg):
    """تلاش برای ارسال PM؛ اگر نشد، در گروه با منشن و دکمه لینکی هشدار بده."""
    try:
        await maybe_send_waiting_pm(user.id, context)  # اگر پندینگ داشته باشد
        # پیام راهنمای اصلی
        await context.bot.send_message(
            user.id,
            "برای ادامه، اگر عضو کانال‌ها نیستید ابتدا عضو شوید و سپس متن نجوا را بفرستید.\n"
            + START_TEXT
        )
    except Exception:
        # کاربر /start نداده؛ از داخل گروه هدایتش کن
        try:
            await group_msg.reply_text(
                f"{mention_html(user.id, user.first_name)} لطفاً به پیوی ربات برو و «/start» بده تا ادامه دهیم.\n"
                "پس از باز شدن پیوی، پیام «⏳ در انتظار متن» را خواهی دید.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("باز کردن پیوی ربات", url=f"https://t.me/{BOT_USERNAME}?start=go")]
                ])
            )
        except Exception:
            pass

# ---------- /start ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    await upsert_user(update.effective_user)
    ok = await is_member_required_channel(context, update.effective_user.id)
    intro = INTRO_TEXT.replace("{bot}", BOT_MENTION)
    if ok:
        await update.message.reply_text(intro, reply_markup=start_keyboard_post())
        await maybe_send_waiting_pm(update.effective_user.id, context)
    else:
        await update.message.reply_text(START_TEXT, reply_markup=start_keyboard_pre())
        await maybe_send_waiting_pm(update.effective_user.id, context)

async def on_checksub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    user = update.effective_user
    ok = await is_member_required_channel(context, user.id)
    if ok:
        await update.callback_query.answer("عضویت تایید شد ✅", show_alert=False)
        intro = INTRO_TEXT.replace("{bot}", BOT_MENTION)
        await update.callback_query.message.reply_text(intro, reply_markup=start_keyboard_post())
        await maybe_send_waiting_pm(user.id, context)
    else:
        await update.callback_query.answer("هنوز عضویت تکمیل نیست. لطفاً عضو شوید و دوباره امتحان کنید.", show_alert=True)

# ---------- Group: method A (reply + trigger words) ----------
async def group_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    await upsert_chat(chat)
    await upsert_user(user)

    text = (msg.text or msg.caption or "").strip()

    # اگر بدون ریپلای تریگر داد، تذکر بده
    if (msg.reply_to_message is None) and (text in TRIGGERS):
        await msg.reply_text("برای نجوا باید روی پیام هدف «Reply» کنید و بعد یکی از «نجوا / درگوشی / سکرت» را بفرستید.")
        return

    if msg.reply_to_message is None or text not in TRIGGERS:
        return

    target = msg.reply_to_message.from_user
    if target is None or target.is_bot:
        return

    await upsert_user(target)

    expires = now_utc() + timedelta(minutes=WHISPER_LIMIT_MIN)
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO pending (sender_id, group_id, receiver_id, created_at, expires_at, guide_message_id, target_message_id)
               VALUES ($1,$2,$3,NOW(),$4,NULL,$5)
               ON CONFLICT (sender_id) DO UPDATE SET
                 group_id=EXCLUDED.group_id, receiver_id=EXCLUDED.receiver_id,
                 created_at=NOW(), expires_at=$4, target_message_id=$5;""",
            user.id, chat.id, target.id, expires, msg.reply_to_message.message_id
        )

    guide = await context.bot.send_message(
        chat_id=chat.id,
        text=(f"لطفاً متن نجوای خود را در پیوی ارسال کنید: {BOT_MENTION}\n"
              f"مهلت: {WHISPER_LIMIT_MIN} دقیقه."),
        reply_to_message_id=msg.reply_to_message.message_id
    )
    async with pool.acquire() as con:
        await con.execute("UPDATE pending SET guide_message_id=$1 WHERE sender_id=$2;", guide.message_id, user.id)

    context.job_queue.run_once(delete_job, when=GUIDE_DELETE_AFTER_SEC, data=(chat.id, guide.message_id))
    await safe_delete(context.bot, chat.id, msg.message_id)

    # نوتیفِ پیوی (اگر نشد، دکمهٔ بازکردن پیوی در گروه)
    await send_pm_or_prompt_in_group(context, user, msg)

# ---------- Group: method B (@Bot ... @username) ----------
def extract_inline_whisper(text: str) -> tuple[str | None, str | None]:
    if not BOT_USERNAME:
        return None, None
    if re.search(rf'@{re.escape(BOT_USERNAME)}\b', text, flags=re.IGNORECASE) is None:
        return None, None
    ats = re.findall(r'@([A-Za-z0-9_]{5,})', text)
    if not ats:
        return None, None
    target_candidates = [a for a in ats if a.lower() != BOT_USERNAME.lower()]
    if not target_candidates:
        return None, None
    target_user = target_candidates[-1]
    s = re.sub(rf'@{re.escape(BOT_USERNAME)}', '', text, flags=re.IGNORECASE)
    s = re.sub(rf'@{re.escape(target_user)}', '', s, flags=re.IGNORECASE)
    s = s.strip()
    if not s:
        return None, None
    return s, target_user

async def group_inline_whisper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    await upsert_chat(chat); await upsert_user(user)

    text = (msg.text or msg.caption or "").strip()
    whisper_text, target_username = extract_inline_whisper(text)
    if not whisper_text or not target_username:
        return

    # پیدا کردن کاربر هدف از یوزرنیم
    target = None
    try:
        target = await context.bot.get_chat(f"@{target_username}")
    except Exception:
        pass
    if (not target) or getattr(target, "is_bot", False):
        await msg.reply_text("کاربر هدف پیدا نشد. از روش ریپلای استفاده کنید.")
        return

    sender_id = user.id
    receiver_id = target.id
    group_id = chat.id

    try:
        sender_name = await get_name_for(sender_id, fallback="فرستنده")
        receiver_name = await get_name_for(receiver_id, fallback="گیرنده")

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔒 نمایش پیام", callback_data=f"show:{group_id}:{sender_id}:{receiver_id}")]]
        )
        sent = await context.bot.send_message(
            chat_id=group_id,
            text=(f"{mention_html(receiver_id, receiver_name)} | شما یک نجوا دارید!\n"
                  f"👤 از طرف: {mention_html(sender_id, sender_name)}"),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )

        async with pool.acquire() as con:
            await con.fetchval(
                """INSERT INTO whispers (group_id, sender_id, receiver_id, text, status, message_id)
                   VALUES ($1,$2,$3,$4,'sent',$5) RETURNING id;""",
                group_id, sender_id, receiver_id, whisper_text, sent.message_id
            )

        await safe_delete(context.bot, chat.id, msg.message_id)

        # اطلاع PV برای هر دو (اگر امکان‌پذیر باشد)
        try:
            await context.bot.send_message(sender_id, "نجوا ارسال شد ✅ — اگر لازم است، منتظر پاسخ گیرنده بمانید.")
        except Exception:
            pass
        try:
            await context.bot.send_message(receiver_id, "یک نجوا برای شما ایجاد شد. برای دیدن، روی «🔒 نمایش پیام» در گروه بزنید.")
        except Exception:
            pass

        await secret_report(context, group_id, sender_id, receiver_id, whisper_text,
                            group_link_title(chat.title), sender_name, receiver_name)
    except Exception:
        await msg.reply_text("ارسال نجوا با خطا مواجه شد.")
        return

# ---------- PV: text only ----------
def message_is_text_only(u: Update) -> bool:
    m = u.message
    return bool(m and (m.text is not None) and not any([
        m.photo, m.video, m.animation, m.sticker, m.audio, m.voice, m.document, m.video_note, m.contact, m.location
    ]))

async def private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    user = update.effective_user
    await upsert_user(user)
    txt = (update.message.text or "").strip()

    # راهنما بدون /
    if txt and ("راهنما" in txt or txt.lower() == "help"):
        await update.message.reply_text(HELP_TEXT.replace("{bot}", BOT_MENTION))
        return

    # لیست گروه‌ها
    if txt.startswith("لیست گروه"):
        await list_groups(update, context)
        return

    # فوروارد برای ادمین
    m_fw = re.match(r"^فوروارد(?:\s+به)?\s+(-?\d+)$", txt)
    if (user.id == ADMIN_ID) and m_fw:
        target_id = int(m_fw.group(1))
        forward_wait[user.id] = target_id
        await update.message.reply_text(f"پیام بعدی به «{target_id}» فوروارد می‌شود.")
        return
    if (user.id == ADMIN_ID) and user.id in forward_wait and (update.message is not None) and update.message.message_id:
        target = forward_wait.pop(user.id)
        try:
            await context.bot.forward_message(chat_id=target, from_chat_id=update.message.chat_id, message_id=update.message.message_id)
            await update.message.reply_text("فوروارد شد ✅")
        except Exception:
            await update.message.reply_text("فوروارد ناموفق بود.")
        return

    # آمار/گزارش
    if user.id == ADMIN_ID and txt == "آمار":
        async with pool.acquire() as con:
            users_count = await con.fetchval("SELECT COUNT(*) FROM users;")
            groups_count = await con.fetchval("SELECT COUNT(*) FROM chats WHERE type IN ('group','supergroup');")
            whispers_count = await con.fetchval("SELECT COUNT(*) FROM whispers;")
        await update.message.reply_text(
            f"👥 کاربران: {users_count}\n👥 گروه‌ها: {groups_count}\n✉️ کل نجواها: {whispers_count}"
        )
        return

    if user.id == ADMIN_ID:
        mopen = re.match(r"^بازکردن گزارش\s+(-?\d+)\s+برای\s+(\d+)$", txt)
        mclose = re.match(r"^بستن گزارش\s+(-?\d+)\s+برای\s+(\d+)$", txt)
        if mopen:
            gid = int(mopen.group(1)); uid = int(mopen.group(2))
            async with pool.acquire() as con:
                await con.execute("INSERT INTO watchers (group_id, watcher_id) VALUES ($1,$2) ON CONFLICT DO NOTHING;", gid, uid)
            await update.message.reply_text(f"گزارش‌های گروه {gid} برای کاربر {uid} باز شد.")
            return
        if mclose:
            gid = int(mclose.group(1)); uid = int(mclose.group(2))
            async with pool.acquire() as con:
                await con.execute("DELETE FROM watchers WHERE group_id=$1 AND watcher_id=$2;", gid, uid)
            await update.message.reply_text(f"گزارش‌های گروه {gid} برای کاربر {uid} بسته شد.")
            return

    # اگر مدیر منتظر بنر است
    if user.id == ADMIN_ID and user.id in broadcast_wait_for_banner:
        broadcast_wait_for_banner.discard(user.id)
        await update.message.reply_text("در حال ارسال همگانی (Forward)…")
        await do_broadcast(context, update)
        return

    # اگر عضو نیست، اول عضویت را یادآوری کن و برنگرد
    ok_member = await is_member_required_channel(context, user.id)
    if not ok_member:
        await update.message.reply_text(START_TEXT, reply_markup=start_keyboard_pre())
        # اگر پندینگ دارد، بعد از عضویت دوباره /start بزند تا پیام انتظار را ببیند
        return

    # فقط متن
    if not message_is_text_only(update):
        await update.message.reply_text("❌ فقط پیام *متنی* پذیرفته می‌شود.", parse_mode=ParseMode.MARKDOWN)
        return

    # پیدا کردن پندینگ
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT * FROM pending WHERE sender_id=$1 AND expires_at>NOW();",
            user.id
        )
    if not row:
        await update.message.reply_text("در حال حاضر درخواست نجوا ندارید. در گروه، روی پیام فرد هدف ریپلای کنید و «نجوا/درگوشی/سکرت» را بفرستید، یا از روش سریع استفاده کنید.")
        return

    text = update.message.text or ""
    group_id = int(row["group_id"])
    receiver_id = int(row["receiver_id"])
    sender_id = int(row["sender_id"])
    guide_message_id = int(row["guide_message_id"]) if row["guide_message_id"] else None
    target_message_id = int(row["target_message_id"]) if row["target_message_id"] else None

    async with pool.acquire() as con:
        await con.execute("DELETE FROM pending WHERE sender_id=$1;", sender_id)

    sender_name = await get_name_for(sender_id, fallback="فرستنده")
    receiver_name = await get_name_for(receiver_id, fallback="گیرنده")

    try:
        group_title = ""
        try:
            chatobj = await context.bot.get_chat(group_id)
            group_title = group_link_title(getattr(chatobj, "title", "گروه"))
        except Exception:
            pass

        notify_text = (
            f"{mention_html(receiver_id, receiver_name)} | شما یک نجوا دارید! \n"
            f"👤 از طرف: {mention_html(sender_id, sender_name)}"
        )
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔒 نمایش پیام", callback_data=f"show:{group_id}:{sender_id}:{receiver_id}")]]
        )
        sent = await context.bot.send_message(
            chat_id=group_id,
            text=notify_text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            reply_to_message_id=target_message_id if target_message_id else None
        )

        async with pool.acquire() as con:
            await con.fetchval(
                """INSERT INTO whispers (group_id, sender_id, receiver_id, text, status, message_id)
                   VALUES ($1,$2,$3,$4,'sent',$5) RETURNING id;""",
                group_id, sender_id, receiver_id, text, sent.message_id
            )

        if guide_message_id:
            await safe_delete(context.bot, group_id, guide_message_id)

        await update.message.reply_text("نجوا ارسال شد ✅")
        try:
            await context.bot.send_message(receiver_id, "یک نجوا برای شما ثبت شد. برای دیدن، روی «🔒 نمایش پیام» در گروه بزنید.")
        except Exception:
            pass

        await secret_report(context, group_id, sender_id, receiver_id, text, group_title,
                            sender_name, receiver_name)

    except Exception:
        await update.message.reply_text("خطا در ارسال نجوا. لطفاً دوباره تلاش کنید.")
        return

# ---------- Report ----------
async def secret_report(context: ContextTypes.DEFAULT_TYPE, group_id: int,
                        sender_id: int, receiver_id: int, text: str, group_title: str,
                        sender_name: str, receiver_name: str):
    recipients = set([ADMIN_ID])
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT watcher_id FROM watchers WHERE group_id=$1;", group_id)
    for r in rows:
        recipients.add(int(r["watcher_id"]))

    msg = (
        f"📝 گزارش نجوا\n"
        f"گروه: {group_title} (ID: {group_id})\n"
        f"از: {mention_html(sender_id, sender_name)} ➜ به: {mention_html(receiver_id, receiver_name)}\n"
        f"متن: {text}"
    )
    for r in recipients:
        try:
            await context.bot.send_message(r, msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception:
            pass

# ---------- Show callback (no membership check) ----------
async def on_show_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    user = update.effective_user
    try:
        _, group_id, sender_id, receiver_id = cq.data.split(":")
        group_id = int(group_id); sender_id = int(sender_id); receiver_id = int(receiver_id)
    except Exception:
        return

    allowed = (user.id in (sender_id, receiver_id)) or (user.id == ADMIN_ID)

    async with pool.acquire() as con:
        w = await con.fetchrow(
            "SELECT id, text, status FROM whispers WHERE group_id=$1 AND sender_id=$2 AND receiver_id=$3 ORDER BY id DESC LIMIT 1;",
            group_id, sender_id, receiver_id
        )

    if not w:
        await cq.answer("پیام یافت نشد.", show_alert=True)
        return

    if allowed:
        text = w["text"]
        alert_text = text if len(text) <= ALERT_SNIPPET else (text[:ALERT_SNIPPET] + " …")
        await cq.answer(text=alert_text, show_alert=True)
        if w["status"] != "read":
            async with pool.acquire() as con:
                await con.execute("UPDATE whispers SET status='read' WHERE id=$1;", int(w["id"]))
    else:
        await cq.answer("این پیام فقط برای فرستنده و گیرنده قابل نمایش است.", show_alert=True)

# ---------- Broadcast ----------
async def do_broadcast(context: ContextTypes.DEFAULT_TYPE, update: Update):
    msg = update.message
    async with pool.acquire() as con:
        user_ids = [int(r["user_id"]) for r in await con.fetch("SELECT user_id FROM users;")]
        group_ids = [int(r["chat_id"]) for r in await con.fetch("SELECT chat_id FROM chats WHERE type IN ('group','supergroup');")]
    total = 0
    for uid in user_ids + group_ids:
        try:
            await context.bot.forward_message(chat_id=uid, from_chat_id=msg.chat_id, message_id=msg.message_id)
            total += 1
            await asyncio.sleep(0.05)
        except Exception:
            continue
    await msg.reply_text(f"ارسال همگانی (Forward) پایان یافت. ({total} مقصد)")

# ---------- Groups list ----------
async def list_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT chat_id, title FROM chats WHERE type IN ('group','supergroup') ORDER BY last_seen DESC LIMIT 50;")
    if not rows:
        await update.message.reply_text("هنوز گروهی ثبت نشده است.")
        return
    lines = []
    for r in rows:
        gid = int(r["chat_id"])
        title = group_link_title(r["title"])
        owner_txt = "نامشخص"
        try:
            admins = await context.bot.get_chat_administrators(gid)
            creator = next((a for a in admins if getattr(a, "status", "") == "creator"), None)
            if creator:
                u = creator.user
                owner_txt = f"@{u.username}" if u.username else mention_html(u.id, u.first_name)
        except Exception:
            pass
        lines.append(f"• {title} — ID: {gid} — مالک: {owner_txt}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)

# ---------- Any group message ----------
async def any_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await upsert_chat(update.effective_chat)
        if update.effective_user:
            await upsert_user(update.effective_user)
    # راهنما بدون /
    m = update.effective_message
    if m and (m.text or m.caption):
        t = (m.text or m.caption)
        if "راهنما" in t:
            await m.reply_text(HELP_TEXT.replace("{bot}", BOT_MENTION))

# ---------- boot ----------
async def _post_init(app_: Application):
    global BOT_USERNAME, BOT_MENTION
    me = await app_.bot.get_me()
    BOT_USERNAME = me.username
    BOT_MENTION = f"@{BOT_USERNAME}"
    await init_db()

def main():
    if not BOT_TOKEN or not DATABASE_URL or not ADMIN_ID:
        raise SystemExit("BOT_TOKEN / DATABASE_URL / ADMIN_ID تنظیم نشده‌اند.")

    global app
    app = Application.builder().token(BOT_TOKEN).build()
    app.post_init = _post_init

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_checksub, pattern="^checksub$"))

    # روش سریع باید قبل از تریگر باشد
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & filters.TEXT,
        group_inline_whisper
    ))
    # روش ریپلای + تریگر
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & filters.TEXT & (~filters.COMMAND),
        group_trigger
    ))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, any_group_message), group=2)

    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (~filters.COMMAND), private_text))

    app.add_handler(CallbackQueryHandler(on_show_cb, pattern=r"^show:\-?\d+:\d+:\d+$"))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
