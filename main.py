# bot.py
# -*- coding: utf-8 -*-

import os
import re
import asyncio
from datetime import datetime, timedelta, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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

# --------- تنظیمات از محیط ---------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
DATABASE_URL = os.environ.get("DATABASE_URL", "")
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "SLSHEXED")  # بدون @

# ---------- ثوابت ----------
TRIGGERS = {"نجوا", "درگوشی", "سکرت"}
WHISPER_LIMIT_MIN = 3  # ← 3 دقیقه
GUIDE_DELETE_AFTER_SEC = 180  # ← متن راهنمای گروه بعد از 3 دقیقه پاک می‌شود
ALERT_SNIPPET = 190  # طول امن برای Alert

# ---------- وضعیت ساده برای ارسال همگانی ----------
broadcast_wait_for_banner = set()  # user_idهایی که منتظر بنر هستند

# ---------- ابزارک‌های عمومی ----------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def sanitize(name: str) -> str:
    return (name or "کاربر").replace("<", "").replace(">", "")

def mention_html(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{sanitize(name)}</a>'

def group_link_title(title: str) -> str:
    return sanitize(title or "گروه")

async def safe_delete(bot, chat_id: int, message_id: int, attempts: int = 3, delay: float = 0.6):
    """حذف مطمئن با چند تلاش."""
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

# ---------- دیتابیس ----------
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
  status TEXT NOT NULL DEFAULT 'sent',  -- 'sent' | 'read'
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
  guide_message_id INTEGER
);

CREATE TABLE IF NOT EXISTS watchers (
  group_id BIGINT NOT NULL,
  watcher_id BIGINT NOT NULL,
  PRIMARY KEY (group_id, watcher_id)
);
"""

ALTER_SQL = """
ALTER TABLE pending ADD COLUMN IF NOT EXISTS guide_message_id INTEGER;
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
    """نام کاربر از DB؛ درصورت نبود، تلاش از get_chat."""
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

# ---------- عضویت اجباری (fail-closed) ----------
async def is_member_required_channel(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(f"@{CHANNEL_USERNAME}", user_id)
        return getattr(member, "status", "") in ("member", "administrator", "creator")
    except Exception:
        return False  # اگر نشد چک کنیم، اجازه نمی‌دهیم

def start_keyboard_pre():
    # قبل از تایید عضویت: دکمه «عضو شدم» نشان داده می‌شود
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("عضو شدم ✅", callback_data="checksub")],
        [InlineKeyboardButton("افزودن ربات به گروه ➕", url="https://t.me/DareGushi_BOT?startgroup=true")],
        [InlineKeyboardButton("ارتباط با پشتیبان 👨🏻‍💻", url="https://t.me/SOULSOWNERBOT")],
    ])

def start_keyboard_post():
    # بعد از تایید عضویت: بدون «عضو شدم»
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("افزودن ربات به گروه ➕", url="https://t.me/DareGushi_BOT?startgroup=true")],
        [InlineKeyboardButton("ارتباط با پشتیبان 👨🏻‍💻", url="https://t.me/SOULSOWNERBOT")],
    ])

START_TEXT = (
    "سلام! 👋\n\n"
    "برای استفاده ابتدا عضو کانال عمومی شوید:\n"
    f"👉 @${'{'}CHANNEL_USERNAME{'}'}\n\n"
    "بعد روی «عضو شدم ✅» بزنید."
)

INTRO_TEXT = (
    "به «درگوشی» خوش آمدید!\n\n"
    "در گروه‌ها روی پیام فرد هدف **Reply** کنید و یکی از کلمات «نجوا / درگوشی / سکرت» را بفرستید؛ "
    "متن نجوا را در خصوصی ارسال کنید. فقط فرستنده و گیرنده می‌توانند نجوا را ببینند. "
    "مهلت ارسال: ۳ دقیقه."
)

async def nudge_join(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    try:
        await context.bot.send_message(
            uid,
            f"برای استفاده از بات، ابتدا عضو @{CHANNEL_USERNAME} شوید و سپس «عضو شدم ✅» را بزنید.",
            reply_markup=start_keyboard_pre()
        )
    except Exception:
        pass

# ---------- /start ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    await upsert_user(update.effective_user)
    # اگر از قبل عضو است، مستقیماً منوی بعد از عضویت را بده
    if await is_member_required_channel(context, update.effective_user.id):
        await update.message.reply_text(INTRO_TEXT, reply_markup=start_keyboard_post())
    else:
        await update.message.reply_text(
            START_TEXT.replace("${CHANNEL_USERNAME}", CHANNEL_USERNAME),
            reply_markup=start_keyboard_pre()
        )

async def on_checksub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    user = update.effective_user
    ok = await is_member_required_channel(context, user.id)
    if ok:
        await update.callback_query.answer("عضویت تایید شد ✅", show_alert=False)
        await update.callback_query.message.reply_text(
            INTRO_TEXT,
            reply_markup=start_keyboard_post()  # ← بدون دکمه «عضو شدم»
        )
    else:
        await update.callback_query.answer("هنوز عضویت تایید نیست. لطفاً عضو شوید و دوباره امتحان کنید.", show_alert=True)

# ---------- تشخیص تریگر در گروه ----------
async def group_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    await upsert_chat(chat)
    await upsert_user(user)

    text = (msg.text or msg.caption or "").strip()
    if msg.reply_to_message is None or text not in TRIGGERS:
        return

    # الزام عضویت پیش از شروع
    if not await is_member_required_channel(context, user.id):
        await safe_delete(context.bot, chat.id, msg.message_id)
        await nudge_join(update, context, user.id)
        return

    target = msg.reply_to_message.from_user
    if target is None or target.is_bot:
        return

    await upsert_user(target)

    # ثبت پندینگ
    expires = now_utc() + timedelta(minutes=WHISPER_LIMIT_MIN)
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO pending (sender_id, group_id, receiver_id, created_at, expires_at, guide_message_id)
               VALUES ($1,$2,$3,NOW(),$4,NULL)
               ON CONFLICT (sender_id) DO UPDATE SET
                 group_id=EXCLUDED.group_id, receiver_id=EXCLUDED.receiver_id,
                 created_at=NOW(), expires_at=$4;""",
            user.id, chat.id, target.id, expires
        )

    # پیام راهنما (reply به پیام هدف) + حذف زمان‌دار
    guide = await context.bot.send_message(
        chat_id=chat.id,
        text=("لطفاً متن نجوای خود را در خصوصی ربات ارسال کنید: @DareGushi_BOT\n"
              f"حداکثر زمان: {WHISPER_LIMIT_MIN} دقیقه."),
        reply_to_message_id=msg.reply_to_message.message_id
    )
    # ذخیره message_id راهنما در pending برای پاک‌سازی بعدی
    async with pool.acquire() as con:
        await con.execute("UPDATE pending SET guide_message_id=$1 WHERE sender_id=$2;", guide.message_id, user.id)

    # زمان‌بندی حذف خودکار راهنما
    context.job_queue.run_once(delete_job, when=GUIDE_DELETE_AFTER_SEC, data=(chat.id, guide.message_id))

    # حذف پیام تریگر کاربر
    await safe_delete(context.bot, chat.id, msg.message_id)

    # پیام خصوصی به فرستنده
    try:
        await context.bot.send_message(
            user.id,
            f"نجوا برای {mention_html(target.id, target.first_name)} در گروه «{group_link_title(chat.title)}»\n"
            f"تا {WHISPER_LIMIT_MIN} دقیقهٔ آینده، متن خود را ارسال کنید.",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

# ---------- دریافت متن نجوا در خصوصی ----------
async def private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    user = update.effective_user
    await upsert_user(user)

    # فقط PV مالک: آمار/ارسال‌همگانی/مدیریت گزارش
    if user.id == ADMIN_ID and (update.message.text or "").strip() == "ارسال همگانی":
        broadcast_wait_for_banner.add(user.id)
        await update.message.reply_text("بنر تبلیغی (متن/عکس/ویدیو/فایل) را ارسال کنید؛ به همهٔ کاربران و گروه‌ها *فوروارد* خواهد شد.")
        return

    if user.id == ADMIN_ID and (update.message.text or "").strip() == "آمار":
        async with pool.acquire() as con:
            users_count = await con.fetchval("SELECT COUNT(*) FROM users;")
            groups_count = await con.fetchval("SELECT COUNT(*) FROM chats WHERE type IN ('group','supergroup');")
            whispers_count = await con.fetchval("SELECT COUNT(*) FROM whispers;")
        await update.message.reply_text(
            f"👥 کاربران: {users_count}\n👥 گروه‌ها: {groups_count}\n✉️ کل نجواها: {whispers_count}"
        )
        return

    if user.id == ADMIN_ID:
        txt = (update.message.text or "").strip()
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

    # اگر مدیر منتظر بنر است، آن را فوروارد کن به همه
    if user.id == ADMIN_ID and user.id in broadcast_wait_for_banner:
        broadcast_wait_for_banner.discard(user.id)
        await update.message.reply_text("در حال ارسال همگانی (Forward)…")
        await do_broadcast(context, update)  # ← forward
        return

    # عضویت اجباریِ محکم برای همهٔ کاربران قبل از ارسال نجوا
    if not await is_member_required_channel(context, user.id):
        await update.message.reply_text(
            START_TEXT.replace("${CHANNEL_USERNAME}", CHANNEL_USERNAME),
            reply_markup=start_keyboard_pre()
        )
        return

    # پیدا کردن پندینگ
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT * FROM pending WHERE sender_id=$1 AND expires_at>NOW();",
            user.id
        )
    if not row:
        await update.message.reply_text("فعلاً درخواست نجوا ندارید. ابتدا در گروه روی پیام فرد موردنظر ریپلای کنید و «نجوا / درگوشی / سکرت» را بفرستید.")
        return

    # ثبت نجوا و ارسال اعلام در گروه
    text = update.message.text or update.message.caption or ""
    group_id = int(row["group_id"])
    receiver_id = int(row["receiver_id"])
    sender_id = int(row["sender_id"])
    guide_message_id = int(row["guide_message_id"]) if row["guide_message_id"] else None

    # حذف پندینگ
    async with pool.acquire() as con:
        await con.execute("DELETE FROM pending WHERE sender_id=$1;", sender_id)

    # نام‌ها برای منشن
    sender_name = await get_name_for(sender_id, fallback="فرستنده")
    receiver_name = await get_name_for(receiver_id, fallback="گیرنده")

    # ارسال پیام در گروه
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
            reply_markup=keyboard
        )

        # ثبت در DB
        async with pool.acquire() as con:
            await con.fetchval(
                """INSERT INTO whispers (group_id, sender_id, receiver_id, text, status, message_id)
                   VALUES ($1,$2,$3,$4,'sent',$5) RETURNING id;""",
                group_id, sender_id, receiver_id, text, sent.message_id
            )

        # پاک کردن پیام راهنما اگر هنوز وجود دارد
        if guide_message_id:
            await safe_delete(context.bot, group_id, guide_message_id)

        # اطلاع به فرستنده در خصوصی
        await update.message.reply_text("نجوا ارسال شد ✅")

        # گزارش محرمانه با اسم و منشن
        await secret_report(context, group_id, sender_id, receiver_id, text, group_title,
                            sender_name, receiver_name)

    except Exception:
        await update.message.reply_text("خطا در ارسال نجوا. لطفاً دوباره تلاش کنید.")
        return

# ---------- گزارش محرمانه (با اسم و منشن) ----------
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

# ---------- کلیک دکمه «نمایش پیام» ----------
async def on_show_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    user = update.effective_user

    # عضویت اجباری حتی برای نمایش پیام
    if not await is_member_required_channel(context, user.id):
        await cq.answer("برای دیدن نجوا باید عضو کانال باشید. PV را چک کنید.", show_alert=True)
        await nudge_join(update, context, user.id)
        return

    try:
        _, group_id, sender_id, receiver_id = cq.data.split(":")
        group_id = int(group_id)
        sender_id = int(sender_id)
        receiver_id = int(receiver_id)
    except Exception:
        return

    # مجوز: فرستنده، گیرنده، یا مالک (نامحسوس)
    allowed = (user.id in (sender_id, receiver_id)) or (user.id == ADMIN_ID)

    async with pool.acquire() as con:
        w = await con.fetchrow(
            "SELECT id, text, status, message_id FROM whispers WHERE group_id=$1 AND sender_id=$2 AND receiver_id=$3 ORDER BY id DESC LIMIT 1;",
            group_id, sender_id, receiver_id
        )

    if not w:
        await cq.answer("پیام یافت نشد.", show_alert=True)
        return

    if allowed:
        text = w["text"]
        alert_text = text if len(text) <= ALERT_SNIPPET else (text[:ALERT_SNIPPET] + " …")
        await cq.answer(text=alert_text, show_alert=True)

        # اگر متن بلند بود نسخه کامل در PV همان خواننده/مالک
        if len(text) > ALERT_SNIPPET:
            try:
                await context.bot.send_message(user.id, f"متن کامل نجوا:\n{text}")
            except Exception:
                pass

        # فقط وضعیت را ثبت می‌کنیم؛ **هیچ ادیتی در گروه انجام نمی‌شود**
        if w["status"] != "read":
            async with pool.acquire() as con:
                await con.execute("UPDATE whispers SET status='read' WHERE id=$1;", int(w["id"]))
    else:
        await cq.answer("این پیام فقط برای فرستنده و گیرنده قابل نمایش است.", show_alert=True)

# ---------- ارسال همگانی (Forward) ----------
async def do_broadcast(context: ContextTypes.DEFAULT_TYPE, update: Update):
    msg = update.message
    async with pool.acquire() as con:
        user_ids = [int(r["user_id"]) for r in await con.fetch("SELECT user_id FROM users;")]
        group_ids = [int(r["chat_id"]) for r in await con.fetch("SELECT chat_id FROM chats WHERE type IN ('group','supergroup');")]

    total = 0
    for uid in user_ids + group_ids:
        try:
            # به جای copy_message از forward_message استفاده می‌کنیم
            await context.bot.forward_message(chat_id=uid, from_chat_id=msg.chat_id, message_id=msg.message_id)
            total += 1
            await asyncio.sleep(0.05)
        except Exception:
            continue

    await msg.reply_text(f"ارسال همگانی (Forward) پایان یافت. ({total} مقصد)")

# ---------- هندلرهای پایه دیگر ----------
async def any_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await upsert_chat(update.effective_chat)
        if update.effective_user:
            await upsert_user(update.effective_user)

# ---------- راه‌اندازی ----------
def main():
    if not BOT_TOKEN or not DATABASE_URL or not ADMIN_ID:
        raise SystemExit("BOT_TOKEN / DATABASE_URL / ADMIN_ID تنظیم نشده‌اند.")

    global app
    app = Application.builder().token(BOT_TOKEN).build()

    app.post_init = lambda _: init_db()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_checksub, pattern="^checksub$"))

    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & filters.REPLY & filters.TEXT & (~filters.COMMAND),
        group_trigger
    ))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, any_group_message), group=2)

    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (~filters.COMMAND), private_text))

    app.add_handler(CallbackQueryHandler(on_show_cb, pattern=r"^show:\-?\d+:\d+:\d+$"))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
