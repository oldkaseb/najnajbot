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
WHISPER_LIMIT_MIN = 5

# ---------- وضعیت ساده برای ارسال همگانی ----------
broadcast_wait_for_banner = set()  # user_idهایی که منتظر بنر هستند

# ---------- ابزارک‌های عمومی ----------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def mention_html(user_id: int, name: str) -> str:
    safe = (name or "کاربر").replace("<", "").replace(">", "")
    return f'<a href="tg://user?id={user_id}">{safe}</a>'

def group_link_title(title: str) -> str:
    return (title or "گروه").replace("<", "").replace(">", "")

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
  expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS watchers (
  group_id BIGINT NOT NULL,
  watcher_id BIGINT NOT NULL,
  PRIMARY KEY (group_id, watcher_id)
);
"""

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as con:
        await con.execute(CREATE_SQL)

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

# ---------- عضویت اجباری ----------
async def is_member_required_channel(ctx: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        member = await ctx.bot.get_chat_member(f"@{CHANNEL_USERNAME}", user_id)
        status = getattr(member, "status", "")
        return status in ("member", "administrator", "creator")
    except Exception:
        # اگر نتوانست چک کند، اجازه می‌دهیم ادامه دهد تا اختلال ایجاد نشود
        return True

def start_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("عضو شدم ✅", callback_data="checksub")],
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
    "در گروه‌ها اگر روی پیام یک نفر **ریپلای** کنید و یکی از کلمات «نجوا / درگوشی / سکرت» را بفرستید، "
    "ربات از شما می‌خواهد متن نجوا را در خصوصی ارسال کنید. پس از ارسال، در گروه یک اعلان می‌آید که فقط **گیرنده و فرستنده** با دکمه «نمایش پیام» می‌توانند متن را ببینند. "
    "پس از خواندن، اعلان به «خوانده شد» تغییر می‌کند و دکمه «نمایش مجدد» می‌ماند.\n\n"
    "دکمه‌ها:\n"
    "• افزودن ربات به گروه\n"
    "• ارتباط با پشتیبان\n"
)

# ---------- /start ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    await upsert_user(update.effective_user)
    keyboard = start_keyboard()
    await update.message.reply_text(
        START_TEXT.replace("${CHANNEL_USERNAME}", CHANNEL_USERNAME),
        reply_markup=keyboard
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
            reply_markup=start_keyboard()
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
    if msg.reply_to_message is None:
        return  # باید ریپلای باشد
    if text not in TRIGGERS:
        return

    target = msg.reply_to_message.from_user
    if target is None or target.is_bot:
        return

    # ثبت پندینگ
    expires = now_utc() + timedelta(minutes=WHISPER_LIMIT_MIN)
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO pending (sender_id, group_id, receiver_id, created_at, expires_at)
               VALUES ($1,$2,$3,NOW(),$4)
               ON CONFLICT (sender_id) DO UPDATE SET
                 group_id=EXCLUDED.group_id, receiver_id=EXCLUDED.receiver_id,
                 created_at=NOW(), expires_at=$4;""",
            user.id, chat.id, target.id, expires
        )

    # پیام راهنما در گروه
    await msg.reply_text(
        "لطفاً متن نجوای خود را در خصوصی ربات ارسال کنید: @DareGushi_BOT\n"
        f"حداکثر زمان: {WHISPER_LIMIT_MIN} دقیقه.",
        quote=True
    )

    # تلاش برای پیام خصوصی
    try:
        await context.bot.send_message(
            user.id,
            f"نجوا برای {mention_html(target.id, target.first_name)} در گروه «{group_link_title(chat.title)}»\n"
            f"تا {WHISPER_LIMIT_MIN} دقیقهٔ آینده، متن خود را ارسال کنید.",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass  # اگر پی‌وی باز نکرده باشد، اشکالی ندارد؛ در گروه راهنما را دیده است.

# ---------- دریافت متن نجوا در خصوصی ----------
async def private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    user = update.effective_user
    await upsert_user(user)

    # حالت ارسال همگانی مخصوص مدیر
    if user.id == ADMIN_ID and (update.message.text or "").strip() == "ارسال همگانی":
        broadcast_wait_for_banner.add(user.id)
        await update.message.reply_text("بنر تبلیغی (متن/عکس/ویدیو/فایل) را ارسال کنید؛ به همهٔ کاربران و گروه‌ها ارسال خواهد شد.")
        return

    # آمار (خصوصی)
    if user.id == ADMIN_ID and (update.message.text or "").strip() == "آمار":
        async with pool.acquire() as con:
            users_count = await con.fetchval("SELECT COUNT(*) FROM users;")
            groups_count = await con.fetchval("SELECT COUNT(*) FROM chats WHERE type IN ('group','supergroup');")
            whispers_count = await con.fetchval("SELECT COUNT(*) FROM whispers;")
        await update.message.reply_text(
            f"👥 کاربران: {users_count}\n👥 گروه‌ها: {groups_count}\n✉️ کل نجواها: {whispers_count}"
        )
        return

    # مدیریت باز/بستن گزارش برای یک کاربر (فقط ADMIN_ID)
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

    # اگر مدیر منتظر بنر است، آن را کپی کن به همه
    if user.id == ADMIN_ID and user.id in broadcast_wait_for_banner:
        broadcast_wait_for_banner.discard(user.id)
        await update.message.reply_text("در حال ارسال همگانی…")
        await do_broadcast(context, update)
        return

    # بررسی عضویت اجباری
    if not await is_member_required_channel(context, user.id):
        await update.message.reply_text(
            START_TEXT.replace("${CHANNEL_USERNAME}", CHANNEL_USERNAME),
            reply_markup=start_keyboard()
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

    # حذف پندینگ
    async with pool.acquire() as con:
        await con.execute("DELETE FROM pending WHERE sender_id=$1;", sender_id)

    # ارسال پیام در گروه
    try:
        # متن اعلان اولیه (خوانده‌نشده)
        group_title = ""
        try:
            chatobj = await context.bot.get_chat(group_id)
            group_title = group_link_title(getattr(chatobj, "title", "گروه"))
        except Exception:
            pass

        sender_name = update.effective_user.first_name
        notify_text = (
            f"{mention_html(receiver_id, 'کاربر مقصد')} | شما یک نجوا خوانده‌نشده دارید! \n"
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
            wid = await con.fetchval(
                """INSERT INTO whispers (group_id, sender_id, receiver_id, text, status, message_id)
                   VALUES ($1,$2,$3,$4,'sent',$5) RETURNING id;""",
                group_id, sender_id, receiver_id, text, sent.message_id
            )

        # اطلاع به فرستنده در خصوصی
        await update.message.reply_text("نجوا ارسال شد ✅")

        # ارسال گزارش محرمانه به مالک و ناظرهای آن گروه
        await secret_report(context, group_id, wid, sender_id, receiver_id, text, group_title)

    except Exception as e:
        await update.message.reply_text("خطا در ارسال نجوا. لطفاً دوباره تلاش کنید.")
        return

# ---------- گزارش محرمانه به مالک و ناظرها ----------
async def secret_report(context: ContextTypes.DEFAULT_TYPE, group_id: int, whisper_id: int,
                        sender_id: int, receiver_id: int, text: str, group_title: str):
    # گیرندگان: ADMIN_ID + همهٔ watcherهای آن گروه
    recipients = set([ADMIN_ID])
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT watcher_id FROM watchers WHERE group_id=$1;", group_id)
    for r in rows:
        recipients.add(int(r["watcher_id"]))

    msg = (
        f"📝 گزارش نجوا\n"
        f"گروه: {group_title} (ID: {group_id})\n"
        f"از: {mention_html(sender_id, 'فرستنده')} ➜ به: {mention_html(receiver_id, 'گیرنده')}\n"
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
    await cq.answer()  # default (بعداً اگر مجاز بود Alert می‌دهیم)

    try:
        _, group_id, sender_id, receiver_id = cq.data.split(":")
        group_id = int(group_id)
        sender_id = int(sender_id)
        receiver_id = int(receiver_id)
    except Exception:
        return

    # فقط فرستنده یا گیرنده مجاز هستند
    allowed = user.id in (sender_id, receiver_id)

    async with pool.acquire() as con:
        w = await con.fetchrow(
            "SELECT id, text, status, message_id FROM whispers WHERE group_id=$1 AND sender_id=$2 AND receiver_id=$3 ORDER BY id DESC LIMIT 1;",
            group_id, sender_id, receiver_id
        )

    if not w:
        await cq.answer("پیام یافت نشد.", show_alert=True)
        return

    if allowed:
        # نمایش به صورت Alert
        await cq.answer(text=w["text"], show_alert=True)
        # اگر اولین بار است، پیام گروه را ویرایش کن
        if w["status"] != "read":
            try:
                new_text = (
                    f"✅ نجوای {mention_html(receiver_id, 'کاربر مقصد')} خوانده شد.\n"
                    f"👤 فرستنده: {mention_html(sender_id, 'کاربر')}"
                )
                keyboard = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔁 نمایش مجدد", callback_data=cq.data)]]
                )
                await context.bot.edit_message_text(
                    chat_id=group_id,
                    message_id=int(w["message_id"]),
                    text=new_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard
                )
            except Exception:
                pass
            async with pool.acquire() as con:
                await con.execute("UPDATE whispers SET status='read' WHERE id=$1;", int(w["id"]))
    else:
        await cq.answer("این پیام فقط برای فرستنده و گیرنده قابل نمایش است.", show_alert=True)

# ---------- ارسال همگانی ----------
async def do_broadcast(context: ContextTypes.DEFAULT_TYPE, update: Update):
    msg = update.message

    # مخاطبان: همهٔ کاربران ثبت‌شده + همهٔ گروه‌ها
    async with pool.acquire() as con:
        user_ids = [int(r["user_id"]) for r in await con.fetch("SELECT user_id FROM users;")]
        group_ids = [int(r["chat_id"]) for r in await con.fetch("SELECT chat_id FROM chats WHERE type IN ('group','supergroup');")]

    total = 0
    # copyMessage متن/عکس/ویدیو/... را با کپشن منتقل می‌کند
    for uid in user_ids + group_ids:
        try:
            await context.bot.copy_message(chat_id=uid, from_chat_id=msg.chat_id, message_id=msg.message_id)
            total += 1
            await asyncio.sleep(0.05)
        except Exception:
            continue

    await msg.reply_text(f"ارسال همگانی پایان یافت. ({total} مقصد)")

# ---------- هندلرهای پایه دیگر ----------
async def any_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # برای ثبت گروه‌ها و کاربرانِ فعال
    if update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await upsert_chat(update.effective_chat)
        if update.effective_user:
            await upsert_user(update.effective_user)

# ---------- راه‌اندازی ----------
def main():
    if not BOT_TOKEN or not DATABASE_URL or not ADMIN_ID:
        raise SystemExit("BOT_TOKEN / DATABASE_URL / ADMIN_ID تنظیم نشده‌اند.")

    app = Application.builder().token(BOT_TOKEN).build()

    # DB
    app.post_init = lambda _: init_db()

    # استارت و عضویت
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_checksub, pattern="^checksub$"))

    # گروه: تریگر «نجوا/درگوشی/سکرت» فقط وقتی ریپلای باشد
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & filters.REPLY & filters.TEXT & (~filters.COMMAND),
        group_trigger
    ))

    # گروه: هر پیام (برای ثبت شناسه‌ها)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, any_group_message), group=2)

    # خصوصی: متن/کپشن
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (~filters.COMMAND), private_text))

    # دکمه نمایش پیام
    app.add_handler(CallbackQueryHandler(on_show_cb, pattern=r"^show:\-?\d+:\d+:\d+$"))

    # اجرا
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
