import os
import io
import json
import time
import random
import sqlite3
import logging
import asyncio
import threading
from urllib.parse import quote
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from html import escape

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError, BadRequest
from telegram.ext import (
    Application, ApplicationHandlerStop, CommandHandler, MessageHandler,
    CallbackQueryHandler, TypeHandler, ContextTypes, filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)
# جلوگیری از چاپ توکن توی لاگ‌ها (آدرس درخواست‌ها شامل توکن هست)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ================= تنظیمات =================
# توکن رو توی محیط (Environment Variable) روی هاستت بذار، نه توی کد
BOT_TOKEN = "8725147253:AAG2YcCL6AQ7c12kYPzRxAKvhSZwq1taIw4"
ADMIN_ID = 5591238725
ADMIN_ID_2 = 6270844351
# گپی که پیام حراجی‌ها (Flash Sale) توش ارسال میشه
ANNOUNCE_CHAT_ID = int(os.environ.get("ANNOUNCE_CHAT_ID", "-1003431951834"))

DB_FILE = "grim_valley.db"
MAX_CARS = 3
HOUSE_VALUE = 2500
PAGE_SIZE = 7                 # تعداد آیتم در هر صفحه

DAILY_MIN, DAILY_MAX = 10, 20  # جایزه روزانه
WORK_MIN, WORK_MAX = 10, 50    # دستور /work
WORK_COOLDOWN = 48 * 3600      # هر ۴۸ ساعت (۲ روز) یک بار
TRADE_TAX_PERCENT = 15         # مالیات هر ترید
RICH_GOAL = 10_000             # مدال ثروتمند
FLASH_HOURS = 24               # مدت حراجی
FLASH_DISCOUNT = 50            # درصد تخفیف حراجی
STREAK_STEP = 1                # پاداش استریک: هر روز پشت‌سرهم +۱ دلار
STREAK_CAP = 6                 # حداکثر پاداش استریک (یعنی +۶ دلار از روز هفتم به بعد)
WISHLIST_MAX = 5               # حداکثر آیتم‌های لیست آرزو
SELL_DAILY_CAP = 750           # حداکثر پولی که یه بازیکن توی ۲۴ ساعت از «فروش به سرور» می‌گیره
ORDER_PENDING_MAX = 5          # حداکثر سفارش تحویل‌نشده همزمان برای هر بازیکن
SELL_PENDING_MAX = 5           # حداکثر درخواست فروش در انتظار همزمان
BUY_QTY_MAX = 10               # حداکثر تعداد توی یک خرید («چند تا؟»)
REF_INVITER_REWARD = 100       # جایزه دعوت‌کننده (بعد از تأیید ادمین)
REF_INVITEE_REWARD = 50        # جایزه دعوت‌شده (بعد از تأیید ادمین)
REF_MAX = 10                   # حداکثر دعوت جایزه‌دار برای هر نفر
TICKET_MAX_OPEN = 3            # حداکثر تیکت بدون جواب برای هر نفر
REAL_COOLDOWN = 120            # فاصله درخواست‌های خرید پولی (ثانیه)

# ضد اسپم: بیشتر از RATE_MAX عمل در RATE_WINDOW ثانیه => RATE_BLOCK ثانیه بلاک
RATE_WINDOW = 10
RATE_MAX = 8
RATE_BLOCK = 30

# دسته‌های پیش‌فرض (ادمین می‌تونه از پنل دسته‌های جدید هم اضافه کنه)
CATEGORIES = {
    "car": "🚗 خودرو",
    "resource": "💎 منابع",
    "attachment": "📦 باربند",
    "weapon": "🔫 اسلحه",
    "food": "🍖 غذا",
    "backpack": "🎒 کوله‌پشتی",
    "tool": "🛠 ابزار",
}

MEDAL_RICH = "💰 ثروتمند"
MEDAL_DRIVER = "🚗 راننده تازه‌کار"
MEDAL_REF = "🤝 دعوت‌کننده"

# اسم‌های فارسی دسته‌ها برای دستور /اعلام
CAT_ALIASES = {
    "ماشین": "car", "خودرو": "car", "منبع": "resource", "منابع": "resource",
    "باربند": "attachment", "اسلحه": "weapon", "غذا": "food",
    "کوله": "backpack", "کوله‌پشتی": "backpack", "ابزار": "tool",
}

WORK_JOBS = [
    "⛏ تو معدن کار کردی و {a}$ گرفتی!",
    "🚕 به عنوان تاکسی‌ران {a}$ درآوردی.",
    "🚚 بار کامیون جابه‌جا کردی و {a}$ گرفتی!",
    "🌾 توی مزرعه کار کردی و {a}$ دستمزد گرفتی.",
    "🔧 یه ماشین رو تعمیر کردی و {a}$ گرفتی!",
    "🎣 ماهی گرفتی و فروختی؛ {a}$ درآوردی.",
    "📦 توی انبار شیفت دادی و {a}$ گرفتی.",
    "🪓 هیزم شکستی و فروختی؛ {a}$ نصیبت شد!",
]

DIV = "━━━━━━━━━━━━━━━━"
# ==========================================

flask_app = Flask(__name__)


@flask_app.route("/")
def home():
    return "Trader Bot is running! 🎮"


def run_flask():
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))


# ================= دیتابیس =================
@contextmanager
def db():
    conn = sqlite3.connect(DB_FILE, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _add_col(conn, table, col, ddl):
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def init_db():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS players (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            cash INTEGER NOT NULL DEFAULT 0,
            has_house INTEGER NOT NULL DEFAULT 0,
            cars TEXT NOT NULL DEFAULT '[]',
            last_daily TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            buy_price INTEGER NOT NULL,
            sell_price INTEGER NOT NULL DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS panels (
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            owner_id INTEGER NOT NULL,
            created INTEGER NOT NULL,
            PRIMARY KEY (chat_id, message_id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY,
            key TEXT NOT NULL UNIQUE,
            label TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS factions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            leader_id INTEGER,
            bank INTEGER NOT NULL DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS faction_members (
            user_id INTEGER PRIMARY KEY,
            faction_id INTEGER NOT NULL,
            joined INTEGER NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            reply TEXT,
            created INTEGER NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            item_name TEXT NOT NULL,
            category TEXT,
            price INTEGER NOT NULL DEFAULT 0,
            real_price TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created INTEGER NOT NULL,
            done_at INTEGER
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS wishlist (
            user_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            notified INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, item_id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS referrals (
            invitee_id INTEGER PRIMARY KEY,
            inviter_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            inviter_paid INTEGER NOT NULL DEFAULT 0,
            created INTEGER NOT NULL,
            done_at INTEGER
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS buy_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price INTEGER NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS sell_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            item_name TEXT NOT NULL,
            qty INTEGER NOT NULL,
            unit_price INTEGER NOT NULL,
            total INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created INTEGER NOT NULL,
            done_at INTEGER
        )""")
        if get_setting(conn, "buy_seeded") is None:
            # قیمت‌های اولیه خرید از بازیکن؛ از پنل ادمین قابل تغییرن
            for name, price in [("نقره (تکه‌شده)", 10), ("انگشتر نقره", 5), ("طلا (تکه‌شده)", 20),
                                ("انگشتر طلا", 10), ("یاقوت کبود", 50), ("زمرد", 70),
                                ("یاقوت سرخ", 90), ("الماس", 130)]:
                conn.execute("INSERT INTO buy_items (name, price) VALUES (?,?)", (name, price))
            set_setting(conn, "buy_seeded", "1")
        conn.execute("""CREATE TABLE IF NOT EXISTS bans (
            user_id INTEGER PRIMARY KEY,
            reason TEXT,
            created INTEGER NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS market (
            key TEXT PRIMARY KEY,
            price INTEGER NOT NULL,
            prev INTEGER,
            updated INTEGER NOT NULL DEFAULT 0
        )""")
        # مهاجرت دیتابیس‌های قدیمی
        _add_col(conn, "players", "last_work", "INTEGER NOT NULL DEFAULT 0")
        _add_col(conn, "players", "streak", "INTEGER NOT NULL DEFAULT 0")
        _add_col(conn, "orders", "qty", "INTEGER NOT NULL DEFAULT 1")
        _add_col(conn, "players", "joined", "INTEGER NOT NULL DEFAULT 0")
        _add_col(conn, "players", "last_seen", "INTEGER NOT NULL DEFAULT 0")
        # تأیید ورود: کاربرهای قدیمی پیش‌فرض ۱ (تأییدشده) می‌مونن؛ فقط کاربر جدید با ۰ ساخته میشه
        _add_col(conn, "players", "approved", "INTEGER NOT NULL DEFAULT 1")
        _add_col(conn, "buy_items", "market", "TEXT")
        _add_col(conn, "buy_items", "factor", "INTEGER NOT NULL DEFAULT 100")
        _add_col(conn, "buy_items", "prev_price", "INTEGER")
        _add_col(conn, "items", "sale_price", "INTEGER")
        conn.execute("INSERT OR IGNORE INTO market (key, price, prev, updated) VALUES ('gold', 20, NULL, 0)")
        conn.execute("INSERT OR IGNORE INTO market (key, price, prev, updated) VALUES ('silver', 10, NULL, 0)")
        # وصل کردن آیتم‌های پیش‌فرض به بازار (تکه = ۱۰۰٪، انگشتر = ۵۰٪)
        for nm, mk, fc in [("طلا (تکه‌شده)", "gold", 100), ("انگشتر طلا", "gold", 50),
                           ("نقره (تکه‌شده)", "silver", 100), ("انگشتر نقره", "silver", 50)]:
            conn.execute("UPDATE buy_items SET market=?, factor=? WHERE name=? AND market IS NULL", (mk, fc, nm))
        _add_col(conn, "items", "announce", "INTEGER NOT NULL DEFAULT 0")
        _add_col(conn, "players", "medals", "TEXT NOT NULL DEFAULT '[]'")
        _add_col(conn, "items", "photo_id", "TEXT")
        _add_col(conn, "items", "is_real", "INTEGER NOT NULL DEFAULT 0")
        _add_col(conn, "items", "real_price", "TEXT")
        _add_col(conn, "items", "sale_until", "INTEGER NOT NULL DEFAULT 0")
        conn.execute("DELETE FROM panels WHERE created < ?", (int(time.time()) - 7 * 86400,))


# ================= ابزارها =================
def is_admin(uid):
    return uid in (ADMIN_ID, ADMIN_ID_2)


def normalize_digits(text):
    table = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    return str(text).translate(table)


def parse_amount(text):
    if not text:
        return None
    s = normalize_digits(text).replace(",", "").replace("،", "").replace(" ", "")
    if not s or not s.isascii() or not s.isdigit():
        return None
    return int(s)


def _json_load(raw, default):
    try:
        v = json.loads(raw or "")
        return v if isinstance(v, type(default)) else default
    except Exception:
        return default


def get_cars(player):
    return _json_load(player["cars"], [])


def get_medals(player):
    return _json_load(player["medals"], [])


def ensure_player(conn, uid, username="player"):
    conn.execute("INSERT OR IGNORE INTO players (user_id, username, joined) VALUES (?,?,?)",
                 (uid, username, int(time.time())))


def user_label(user):
    return user.username or user.first_name or "پلیر"


def fmt_duration(sec):
    sec = max(0, int(sec))
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    parts = []
    if d:
        parts.append(f"{d} روز")
    if h:
        parts.append(f"{h} ساعت")
    if m and not d:
        parts.append(f"{m} دقیقه")
    return " و ".join(parts) or "کمتر از یک دقیقه"


def bar(value, goal, width=10):
    filled = max(0, min(width, value * width // goal))
    return "▰" * filled + "▱" * (width - filled)


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))


def cat_map():
    """دسته‌های پیش‌فرض + دسته‌هایی که ادمین ساخته."""
    m = dict(CATEGORIES)
    try:
        with db() as conn:
            for r in conn.execute("SELECT key, label FROM categories ORDER BY id"):
                m[r["key"]] = r["label"]
    except sqlite3.Error:
        pass
    return m


# ---------- حراجی ----------
def on_sale(it):
    return (not it["is_real"]) and (it["sale_until"] or 0) > time.time()


def eff_price(it):
    """قیمت فعلی: اگه قیمت موقت فعال باشه همون، وگرنه قیمت اصلی (بعد از انقضا خودکار برمی‌گرده)."""
    if on_sale(it):
        sp = it["sale_price"]
        return sp if sp is not None else it["buy_price"] * (100 - FLASH_DISCOUNT) // 100
    return it["buy_price"]


def dir_arrow(new, old):
    return "🔴⬇️" if new < old else ("🟢⬆️" if new > old else "⚪➖")


def price_label(it):
    if it["is_real"]:
        return f"💶 {it['real_price']}"
    if on_sale(it):
        p = eff_price(it)
        return f"🔥{p:,}$" if p < it["buy_price"] else f"📈{p:,}$"
    return f"{it['buy_price']:,}$"


# ---------- صفحه‌بندی ----------
def paginate(items, page):
    total = max(1, -(-len(items) // PAGE_SIZE))
    page = min(max(page, 0), total - 1)
    return items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE], page, total


def page_nav(prefix, page, total):
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"{prefix}{page - 1}"))
    if page < total - 1:
        row.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"{prefix}{page + 1}"))
    return row


def page_of_item(it, by_category=True):
    with db() as conn:
        if by_category:
            n = conn.execute("SELECT COUNT(*) FROM items WHERE category=? AND id<?",
                             (it["category"], it["id"])).fetchone()[0]
        else:
            n = conn.execute("SELECT COUNT(*) FROM items WHERE id<?", (it["id"],)).fetchone()[0]
    return n // PAGE_SIZE


# ---------- کیبوردها ----------
def main_menu(admin=False):
    rows = [
        [InlineKeyboardButton("💼 دارایی‌های من", callback_data="my_assets"),
         InlineKeyboardButton("🛒 فروشگاه", callback_data="shop")],
        [InlineKeyboardButton("🛠 کار کردن", callback_data="work"),
         InlineKeyboardButton("🎁 جایزه روزانه", callback_data="daily")],
        [InlineKeyboardButton("🏴 فکشن من", callback_data="my_faction"),
         InlineKeyboardButton("🏆 لیدربورد", callback_data="leaderboard")],
        [InlineKeyboardButton("📦 سفارش‌های من", callback_data="my_orders"),
         InlineKeyboardButton("🎫 پشتیبانی", callback_data="tickets")],
        [InlineKeyboardButton("⭐ لیست آرزو", callback_data="wishlist"),
         InlineKeyboardButton("🤝 دعوت دوستان", callback_data="invite")],
        [InlineKeyboardButton("💎 فروش به سرور", callback_data="sell"),
         InlineKeyboardButton("❓ راهنما", callback_data="help")],
        [InlineKeyboardButton("❌ بستن منو", callback_data="close_panel")],
    ]
    if admin:
        rows.append([InlineKeyboardButton("🛠️ پنل مدیریت", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)


def back_main():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")]])


def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ افزودن آیتم", callback_data="admin_add"),
         InlineKeyboardButton("📋 لیست آیتم‌ها", callback_data="admin_list")],
        [InlineKeyboardButton("🗂 مدیریت دسته‌ها", callback_data="cm"),
         InlineKeyboardButton("🏴 فکشن‌ها", callback_data="fcl_0")],
        [InlineKeyboardButton("📦 سفارش‌های باز", callback_data="ol_0"),
         InlineKeyboardButton("🎫 تیکت‌های باز", callback_data="tkl_0")],
        [InlineKeyboardButton("💎 قیمت‌های خرید", callback_data="bm"),
         InlineKeyboardButton("📥 درخواست‌های فروش", callback_data="sq_0")],
        [InlineKeyboardButton("📈 قیمت بازار", callback_data="mk"),
         InlineKeyboardButton("👥 کاربران", callback_data="ul_0")],
        [InlineKeyboardButton("🤝 دعوت‌ها", callback_data="rl_0"),
         InlineKeyboardButton("💾 بکاپ", callback_data="admin_backup")],
        [InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu"),
         InlineKeyboardButton("❌ بستن", callback_data="close_panel")],
    ])


def cancel_markup():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="cancel_flow")]])


def cancel_player_markup():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="cancel_p")]])


def category_markup():
    kb = [[InlineKeyboardButton(label, callback_data="addcat_" + key)] for key, label in cat_map().items()]
    kb.append([InlineKeyboardButton("❌ لغو", callback_data="cancel_flow")])
    return InlineKeyboardMarkup(kb)


# ---------- مالکیت منوها ----------
def remember_panel(msg, owner_id):
    """ثبت اینکه این منو مال کیه (فقط در گروه‌ها لازمه)."""
    if msg is None or msg.chat.type == "private":
        return
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO panels (chat_id, message_id, owner_id, created) VALUES (?,?,?,?)",
            (msg.chat_id, msg.message_id, owner_id, int(time.time())),
        )


def panel_owner(chat_id, message_id):
    with db() as conn:
        row = conn.execute(
            "SELECT owner_id FROM panels WHERE chat_id=? AND message_id=?", (chat_id, message_id)
        ).fetchone()
    return row["owner_id"] if row else None


async def reply_panel(update: Update, text, markup=None):
    sent = await update.effective_message.reply_text(text, reply_markup=markup)
    remember_panel(sent, update.effective_user.id)
    return sent


async def send_panel(context, chat_id, owner_id, text, markup=None, photo=None):
    sent = None
    if photo:
        try:
            sent = await context.bot.send_photo(chat_id=chat_id, photo=photo,
                                                caption=text[:1024], reply_markup=markup)
        except TelegramError as e:
            logger.warning("send_photo failed: %s", e)
    if sent is None:
        sent = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
    remember_panel(sent, owner_id)
    return sent


async def safe_delete(message):
    try:
        await message.delete()
    except TelegramError:
        pass


async def show(query, context, text, markup=None, photo=None):
    """نمایش صفحه جدید: تا جای ممکن همون پیام ادیت میشه (پیام جدید پایین چت نمیاد)."""
    msg = query.message
    if not photo and not msg.photo:
        try:
            await query.edit_message_text(text, reply_markup=markup)
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
            logger.warning("edit failed, resending: %s", e)
        except TelegramError as e:
            logger.warning("edit failed, resending: %s", e)
    await safe_delete(msg)
    await send_panel(context, msg.chat_id, query.from_user.id, text, markup, photo)


# ================= ضد اسپم =================
_hits = {}


async def rate_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or is_admin(user.id):
        return
    now = time.monotonic()
    if len(_hits) > 5000:
        for k in [k for k, v in _hits.items() if now > v["blocked_until"] and not v["times"]]:
            _hits.pop(k, None)
    st = _hits.setdefault(user.id, {"times": deque(), "blocked_until": 0.0})

    if now < st["blocked_until"]:
        if update.callback_query:
            try:
                await update.callback_query.answer("⏳ آهسته‌تر! چند ثانیه صبر کن.")
            except TelegramError:
                pass
        raise ApplicationHandlerStop

    times = st["times"]
    while times and now - times[0] > RATE_WINDOW:
        times.popleft()
    times.append(now)

    if len(times) > RATE_MAX:
        st["blocked_until"] = now + RATE_BLOCK
        times.clear()
        try:
            if update.callback_query:
                await update.callback_query.answer(f"⏳ اسپم نکن! {RATE_BLOCK} ثانیه بلاکی.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text(f"⏳ اسپم نکن! {RATE_BLOCK} ثانیه نمی‌تونی استفاده کنی.")
        except TelegramError:
            pass
        raise ApplicationHandlerStop


# ================= مدال‌ها =================
def check_medals(conn, uid):
    """مدال‌های خودکار رو چک می‌کنه و مدال‌های تازه‌ی گرفته‌شده رو برمی‌گردونه."""
    p = conn.execute("SELECT cash, cars, medals FROM players WHERE user_id=?", (uid,)).fetchone()
    if not p:
        return []
    medals = get_medals(p)
    new = []
    if p["cash"] >= RICH_GOAL and MEDAL_RICH not in medals:
        new.append(MEDAL_RICH)
    if get_cars(p) and MEDAL_DRIVER not in medals:
        new.append(MEDAL_DRIVER)
    if MEDAL_REF not in medals:
        n_ref = conn.execute("SELECT COUNT(*) FROM referrals WHERE inviter_id=? AND status='confirmed'",
                             (uid,)).fetchone()[0]
        if n_ref:
            new.append(MEDAL_REF)
    if new:
        conn.execute("UPDATE players SET medals=? WHERE user_id=?",
                     (json.dumps(medals + new, ensure_ascii=False), uid))
    return new


def check_wishlist(conn, uid):
    """آیتم‌های آرزویی که تازه قابل خریدن شدن رو برمی‌گردونه."""
    p = conn.execute("SELECT cash FROM players WHERE user_id=?", (uid,)).fetchone()
    if not p:
        return []
    out = []
    rows = conn.execute(
        "SELECT w.notified AS notified, i.* FROM wishlist w JOIN items i ON i.id=w.item_id "
        "WHERE w.user_id=? AND i.is_real=0", (uid,)).fetchall()
    for r in rows:
        price = eff_price(r)
        if p["cash"] >= price and not r["notified"]:
            out.append(r)
            conn.execute("UPDATE wishlist SET notified=1 WHERE user_id=? AND item_id=?", (uid, r["id"]))
        elif p["cash"] < price and r["notified"]:
            conn.execute("UPDATE wishlist SET notified=0 WHERE user_id=? AND item_id=?", (uid, r["id"]))
    return out


async def refresh_medals(context, uid):
    """بعد از هر تغییر پول: مدال‌های جدید و آیتم‌های آرزوی قابل‌خرید رو به بازیکن اطلاع میده."""
    try:
        with db() as conn:
            new = check_medals(conn, uid)
            wished = check_wishlist(conn, uid)
    except sqlite3.Error as e:
        logger.error("medal/wishlist check failed: %s", e)
        return
    for m in new:
        try:
            await context.bot.send_message(
                chat_id=uid, text=f"🏅 تبریک! مدال جدید گرفتی:\n\n✨ {m} ✨\n\nتوی دارایی‌هات می‌تونی ببینیش.")
        except TelegramError:
            pass
    for it in wished:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=f"⭐ پولت به آیتم آرزوت رسید!\n{DIV}\n🛍 {it['name']} — {eff_price(it):,}$\nالان می‌تونی بخریش 🎉",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🛒 برو به صفحه آیتم", callback_data="pick_" + str(it["id"]))]]))
        except TelegramError:
            pass


# ================= فکشن (توابع پایه) =================
def faction_of(conn, uid):
    return conn.execute(
        "SELECT f.id, f.name, f.leader_id, f.bank, m.joined FROM faction_members m "
        "JOIN factions f ON f.id=m.faction_id WHERE m.user_id=?", (uid,)
    ).fetchone()


def faction_by_name(conn, name):
    return conn.execute("SELECT * FROM factions WHERE name=? COLLATE NOCASE", (name.strip(),)).fetchone()


def remove_member(conn, uid):
    conn.execute("UPDATE factions SET leader_id=NULL WHERE leader_id=?", (uid,))
    conn.execute("DELETE FROM faction_members WHERE user_id=?", (uid,))


def create_faction(name):
    name = (name or "").strip()
    if not name or len(name) > 30:
        return False, "❌ اسم فکشن باید بین ۱ تا ۳۰ حرف باشه."
    with db() as conn:
        if faction_by_name(conn, name):
            return False, "❌ فکشنی با این اسم از قبل هست."
        conn.execute("INSERT INTO factions (name) VALUES (?)", (name,))
    return True, f"✅ فکشن «{name}» ساخته شد.\nحالا روی پیام پلیرها ریپلای کن و بنویس:\n+faction {name}"


# ================= دارایی =================
def asset_text(conn, player):
    cars = get_cars(player)
    medals = get_medals(player)
    car_value = 0
    for name in cars:
        row = conn.execute(
            "SELECT buy_price FROM items WHERE name=? AND category='car' AND is_real=0 ORDER BY id LIMIT 1",
            (name,)
        ).fetchone()
        if row:
            car_value += row["buy_price"]
    house_value = HOUSE_VALUE if player["has_house"] else 0
    total = player["cash"] + car_value + house_value
    fac = faction_of(conn, player["user_id"])

    lines = [f"💼 دارایی‌های {player['username'] or 'پلیر'}"]
    lines.append("🏅 " + " | ".join(medals) if medals else "🏅 هنوز مدالی نگرفته")
    lines.append(DIV)
    lines.append(f"💰 پول نقد: {player['cash']:,} $")
    lines.append(f"🏠 خانه: {'✅ دارد' if player['has_house'] else '❌ ندارد'}")
    lines.append(f"🚗 ماشین‌ها ({len(cars)}/{MAX_CARS}):")
    lines.extend([f"   • {c}" for c in cars] if cars else ["   • هیچ ماشینی نداره"])
    if fac:
        lines.append(f"🏴 فکشن: {fac['name']}" + (" 👑" if fac["leader_id"] == player["user_id"] else ""))
    lines.append(DIV)
    lines.append(f"🚙 ارزش ماشین‌ها: {car_value:,} $")
    lines.append(f"🏠 ارزش خانه: {house_value:,} $")
    lines.append(f"💎 کل دارایی: {total:,} $")
    if player["cash"] < RICH_GOAL and MEDAL_RICH not in medals:
        pct = player["cash"] * 100 // RICH_GOAL
        lines.append(f"\n🎯 تا مدال «{MEDAL_RICH}»: {bar(player['cash'], RICH_GOAL)} {pct}%")
    return "\n".join(lines)


def main_text(user):
    with db() as conn:
        p = conn.execute("SELECT cash FROM players WHERE user_id=?", (user.id,)).fetchone()
        fac = faction_of(conn, user.id)
    lines = ["🎮 تریدر | گریم ولی", DIV, f"👋 سلام {user_label(user)}!"]
    if p:
        lines.append(f"💰 موجودی: {p['cash']:,} $")
    if fac:
        lines.append(f"🏴 فکشن: {fac['name']}")
    lines += [DIV, "از منوی زیر انتخاب کن 👇"]
    return "\n".join(lines)


def help_text(admin):
    lines = [
        "❓ راهنمای تریدر", DIV,
        "🎮 /start — منوی اصلی",
        f"🛠 /work — کار کردن (هر {WORK_COOLDOWN // 3600} ساعت، {WORK_MIN} تا {WORK_MAX}$)",
        f"🎁 جایزه روزانه — {DAILY_MIN} تا {DAILY_MAX}$ (از منو)",
        "🤝 /trade @یوزرنیم مبلغ — انتقال پول",
        f"🧾 از هر ترید {TRADE_TAX_PERCENT}٪ مالیات کم میشه.",
        "↩️ به‌جای @یوزرنیم می‌تونی روی پیام طرف ریپلای کنی.",
        f"💎 فروش به سرور: آیتم‌های خاص (طلا، نقره، سنگ‌های قیمتی...) رو به سرور بفروش؛ سقف دریافت {SELL_DAILY_CAP}$ در هر ۲۴ ساعت.",
        f"🔢 توی صفحه آیتم «چند تا؟» رو بزن تا چندتایی بخری (یه سفارش میشه). حداکثر {ORDER_PENDING_MAX} سفارش تحویل‌نشده همزمان.",
        "⭐ لیست آرزو: آیتم دلخواهت رو ذخیره کن؛ وقتی پولت رسید بات خبرت می‌کنه.",
        "🤝 دعوت دوستان: لینک اختصاصی بگیر و بعد از ورود دوستت به سرور جایزه بگیر.",
        f"🔥 استریک: هر روز پشت‌سرهم جایزه بگیری پاداشت بیشتر میشه (تا +{STREAK_CAP * STREAK_STEP}$).",
        "📦 هر خرید یه سفارش میشه؛ بعد از تحویل توی بازی بهت پیام میاد (وضعیتش توی «سفارش‌های من»).",
        "🏴 فکشن، 🎫 تیکت و 🏆 لیدربورد از منوی اصلی.",
    ]
    if admin:
        lines += [
            DIV, "🛠 دستورات ادمین:",
            "/admin /additem /listitems /wealth /backup /restore",
            "/newfaction نام — ساخت فکشن",
            "📦 سفارش‌ها: از پنل مدیریت (دکمه تحویل/لغو هم زیر پیام هر سفارش هست)",
            "/flash آیدی_یا_اسم — حراجی ۵۰٪ برای ۲۴ ساعت | /endflash آیدی_یا_اسم",
            "قیمت موقت دلخواه (کمتر/بیشتر): پنل ← لیست آیتم‌ها ← آیتم ← «قیمت کمتر/بیشتر»",
            DIV, "↩️ ریپلای روی پیام پلیر:",
            "+200 / -200 — پول | +house / -house",
            "+car اسم / -car اسم — ماشین",
            "+medal متن / -medal متن — مدال دستی",
            "/medal @یوزرنیم متن | /unmedal @یوزرنیم متن — مدال بدون ریپلای",
            "💎 قیمت‌های خرید، 📈 قیمت بازار (نوسان طلا و نقره) و 👥 کاربران: از پنل مدیریت",
            "/ban @user دلیل | /unban @user | /kick @user | /user @user — مسدودی، رفع مسدودی، کیک از گپ، پروفایل",
            "/announce ماشین یا /اعلام ماشین — روشن/خاموش اعلام خرید یه دسته توی گپ",
            "+faction نام / -faction — فکشن",
            "+leader / -leader — لیدر فکشن",
            "🆕 هر کاربر جدید قبل از استفاده از بات باید توسط ادمین تأیید بشه (پیامش برات میاد).",
        ]
    return "\n".join(lines)


# ================= تأیید کاربر جدید =================
def is_approved(conn, uid):
    if is_admin(uid):
        return True
    row = conn.execute("SELECT approved FROM players WHERE user_id=?", (uid,)).fetchone()
    return bool(row and row["approved"])


async def notify_new_user(context, user):
    uname = f"@{escape(user.username)}" if user.username else "ندارد"
    msg = (f"🆕 <b>کاربر جدید می‌خواد وارد بات بشه</b>\n{DIV}\n"
           f"👤 <a href=\"tg://user?id={user.id}\">{escape(user.full_name)}</a>\n"
           f"🔗 یوزرنیم: {uname}\n🆔 آیدی: <code>{user.id}</code>\n\n"
           f"تأیید می‌کنی؟")
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تأیید", callback_data=f"nuy_{user.id}"),
        InlineKeyboardButton("❌ رد (مسدود)", callback_data=f"nun_{user.id}"),
    ]])
    for admin in (ADMIN_ID, ADMIN_ID_2):
        try:
            await context.bot.send_message(chat_id=admin, text=msg, parse_mode=ParseMode.HTML,
                                           reply_markup=markup)
        except Exception as e:
            logger.error("Failed to notify new user to %s: %s", admin, e)


# ================= منوی پلیر =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    context.user_data.clear()
    user = update.effective_user
    name = user_label(user)
    payload = (context.args[0] if context.args else "") or ""
    inviter = None
    is_new = False
    with db() as conn:
        existed = conn.execute("SELECT 1 FROM players WHERE user_id=?", (user.id,)).fetchone() is not None
        if not existed:
            is_new = True
            conn.execute("INSERT OR IGNORE INTO players (user_id, username, joined, approved) VALUES (?,?,?,?)",
                         (user.id, name, int(time.time()), 1 if is_admin(user.id) else 0))
        conn.execute("UPDATE players SET username=? WHERE user_id=?", (name, user.id))
        approved = is_approved(conn, user.id)
        if is_new and payload.startswith("ref_") and payload[4:].isdigit() and int(payload[4:]) != user.id:
            inviter = conn.execute("SELECT user_id, username FROM players WHERE user_id=?",
                                   (int(payload[4:]),)).fetchone()
            if inviter:
                conn.execute("INSERT OR IGNORE INTO referrals (invitee_id, inviter_id, created) VALUES (?,?,?)",
                             (user.id, inviter["user_id"], int(time.time())))
    if not approved:
        await update.message.reply_text(
            "⏳ درخواست ورودت برای ادمین ارسال شد.\nبعد از تأیید بهت پیام میاد؛ لطفاً صبر کن.")
        if is_new:
            await notify_new_user(context, user)
        return
    extra = f"\n\n🤝 با دعوت {inviter['username'] or 'یه دوست'} اومدی!" if inviter else ""
    await reply_panel(update, main_text(user) + extra, main_menu(is_admin(user.id)))
    if inviter:
        await notify_referral(context, user, inviter)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_panel(update, help_text(is_admin(update.effective_user.id)), back_main())


async def show_assets(query, context):
    with db() as conn:
        player = conn.execute("SELECT * FROM players WHERE user_id=?", (query.from_user.id,)).fetchone()
        text = asset_text(conn, player) if player else "❌ اول /start بزن تا ثبت‌نام بشی."
    await show(query, context, text, back_main())


# ================= فروشگاه =================
async def show_shop(query, context):
    with db() as conn:
        rows = conn.execute("SELECT category, COUNT(*) AS n FROM items GROUP BY category").fetchall()
    if not rows:
        await show(query, context, "🛒 فروشگاه فعلاً خالی است.", back_main())
        return
    cmap = cat_map()
    order = list(cmap)
    rows = sorted(rows, key=lambda r: order.index(r["category"]) if r["category"] in order else 99)
    keyboard = [[InlineKeyboardButton(f"{cmap.get(r['category'], r['category'])} ({r['n']})",
                                      callback_data=f"cat_{r['category']}_0")] for r in rows]
    keyboard.append([InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")])
    await show(query, context, f"🛒 فروشگاه تریدر\n{DIV}\nیک دسته رو انتخاب کن 👇", InlineKeyboardMarkup(keyboard))


async def show_category(query, context, category, page=0):
    with db() as conn:
        items = conn.execute("SELECT * FROM items WHERE category=? ORDER BY id", (category,)).fetchall()
    if not items:
        await show(query, context, "📦 این دسته فعلاً خالی است.", back_main())
        return
    chunk, page, total = paginate(items, page)
    # هر آیتم توی یه ردیف جدا (ستونی)، حداکثر ۷ آیتم توی هر صفحه
    keyboard = []
    for it in chunk:
        icon = "🖼 " if it["photo_id"] else ""
        label = f"{icon}{it['name']} | {price_label(it)}"
        keyboard.append([InlineKeyboardButton(label[:60], callback_data="pick_" + str(it["id"]))])
    nav = page_nav(f"cat_{category}_", page, total)
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔙 دسته‌بندی‌ها", callback_data="shop")])
    keyboard.append([InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")])
    title = cat_map().get(category, category)
    if total > 1:
        title += f"  (صفحه {page + 1} از {total})"
    await show(query, context, f"{title}\n{DIV}\nیک آیتم رو انتخاب کن 👇", InlineKeyboardMarkup(keyboard))


async def show_item(query, context, item_id, note=None):
    with db() as conn:
        it = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not it:
        await show(query, context, "❌ آیتم پیدا نشد.", back_main())
        return
    lines = [f"🛍 {it['name']}", DIV, f"🏷️ دسته: {cat_map().get(it['category'], it['category'])}"]
    if it["is_real"]:
        lines.append(f"💵 قیمت: {price_label(it)}")
        lines.append("\n💶 این آیتم با پول واقعیه و به دارایی بازی‌ات ربطی نداره. ادمین باهات هماهنگ می‌کنه.")
        buy_btn = InlineKeyboardButton("💶 درخواست خرید", callback_data="rq_" + str(it["id"]))
    else:
        if on_sale(it):
            left = it["sale_until"] - time.time()
            p, o = eff_price(it), it["buy_price"]
            lines.append(f"💵 قیمت: {dir_arrow(p, o)} {p:,}$  (قبلاً {o:,}$)")
            lines.append(("🔥 حراجی!" if p < o else "📢 قیمت موقت!") + f" — {fmt_duration(left)} مونده")
        else:
            lines.append(f"💵 قیمت: {it['buy_price']:,}$")
        buy_btn = InlineKeyboardButton(f"🛒 خرید ({eff_price(it):,}$)", callback_data="buy_" + str(it["id"]))
    if note:
        lines.append("\n" + note)
    rows = [[buy_btn]]
    if not it["is_real"]:
        rows[0].append(InlineKeyboardButton("🔢 چند تا؟", callback_data="bq_" + str(it["id"])))
        with db() as conn:
            wished = conn.execute("SELECT 1 FROM wishlist WHERE user_id=? AND item_id=?",
                                  (query.from_user.id, item_id)).fetchone()
        rows.append([InlineKeyboardButton("🗑 حذف از لیست آرزو" if wished else "⭐ افزودن به لیست آرزو",
                                          callback_data="wl_" + str(item_id))])
    rows.append([InlineKeyboardButton("🔙 برگشت", callback_data=f"cat_{it['category']}_{page_of_item(it)}"),
                 InlineKeyboardButton("🏠 منوی اصلی", callback_data="main_menu")])
    markup = InlineKeyboardMarkup(rows)
    await show(query, context, "\n".join(lines), markup, photo=it["photo_id"])


ORDER_STATUS = {"pending": "⏳ در انتظار تحویل", "delivered": "✅ تحویل شد", "cancelled": "❌ لغو شد"}


def create_order(conn, uid, username, item, price, qty=1):
    """price = کل مبلغ کم‌شده (برای برگشت پول موقع لغو)."""
    real = bool(item["is_real"])
    cur = conn.execute(
        "INSERT INTO orders (user_id, username, item_name, category, price, real_price, qty, created)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (uid, username, item["name"], item["category"], 0 if real else price,
         item["real_price"] if real else None, 1 if real else qty, int(time.time())),
    )
    return cur.lastrowid


def order_name(o):
    return f"{o['item_name']} ×{o['qty']}" if o["qty"] > 1 else o["item_name"]


def order_price_text(o):
    return f"💶 {o['real_price']}" if o["real_price"] else f"{o['price']:,} $"


def order_admin_markup(oid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تحویل داده شد", callback_data=f"od_{oid}"),
        InlineKeyboardButton("❌ لغو سفارش", callback_data=f"oc_{oid}"),
    ]])


def try_buy(uid, item_id, username="player", qty=1):
    """خرید اتمیک + ثبت یک سفارش. خروجی: (وضعیت, آیتم, موجودی/تعداد سفارش باز, کل قیمت, کد سفارش)"""
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not item or item["is_real"]:
            return "no_item", None, 0, 0, 0
        total = eff_price(item) * qty
        player = conn.execute("SELECT * FROM players WHERE user_id=?", (uid,)).fetchone()
        if not player:
            return "no_player", item, 0, total, 0
        pending = conn.execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND status='pending'",
                               (uid,)).fetchone()[0]
        if pending >= ORDER_PENDING_MAX:
            return "too_many", item, pending, total, 0
        cars = get_cars(player)
        if item["category"] == "car" and len(cars) + qty > MAX_CARS:
            return "car_full", item, player["cash"], total, 0
        if player["cash"] < total:
            return "poor", item, player["cash"], total, 0
        conn.execute("UPDATE players SET cash=cash-? WHERE user_id=?", (total, uid))
        if item["category"] == "car":
            cars.extend([item["name"]] * qty)
            conn.execute("UPDATE players SET cars=? WHERE user_id=?", (json.dumps(cars), uid))
        oid = create_order(conn, uid, username, item, total, qty)
        return "ok", item, player["cash"] - total, total, oid


async def notify_admins(context, user, item, price=None, oid=None, qty=1):
    real = bool(item["is_real"])
    uname = f"@{escape(user.username)}" if user.username else "ندارد"
    if real:
        head = "💶 <b>درخواست خرید پولی!</b>"
        price_line = f"💶 قیمت: {escape(item['real_price'] or '')}"
        note = "\n\n📞 باید با این پلیر هماهنگ کنی و پیگیری کنی. (چیزی از دارایی بازی‌اش کم نشده)"
    else:
        head = "🛒 <b>سفارش جدید!</b>"
        total = eff_price(item) * qty if price is None else price
        unit = total // qty if qty else total
        price_line = f"💰 قیمت کل: {total:,} $"
        if qty > 1:
            price_line += f" ({qty} × {unit:,}$)"
        if unit != item["buy_price"]:
            price_line += f" (قیمت موقت — قیمت اصلی هر عدد {item['buy_price']:,}$)"
        note = "\n\n📦 بعد از تحویل توی بازی، دکمه «تحویل داده شد» رو بزن."
    msg = (
        f"{head}\n{DIV}\n"
        + (f"🔢 سفارش: #{oid}\n" if oid else "") +
        f"👤 نام: <a href=\"tg://user?id={user.id}\">{escape(user.full_name)}</a>\n"
        f"🔗 یوزرنیم: {uname}\n"
        f"🆔 آیدی: <code>{user.id}</code>\n"
        f"📦 آیتم: {escape(item['name'])}" + (f" ×{qty}" if qty > 1 else "") + "\n"
        f"🏷️ دسته: {escape(cat_map().get(item['category'], item['category']))}\n"
        f"{price_line}\n\n"
        f"⏰ زمان: {datetime.now().strftime('%Y-%m-%d %H:%M')}{note}"
    )
    markup = order_admin_markup(oid) if oid else None
    for admin in (ADMIN_ID, ADMIN_ID_2):
        try:
            if item["photo_id"]:
                await context.bot.send_photo(chat_id=admin, photo=item["photo_id"], caption=msg,
                                             parse_mode=ParseMode.HTML, reply_markup=markup)
            else:
                await context.bot.send_message(chat_id=admin, text=msg, parse_mode=ParseMode.HTML,
                                               reply_markup=markup)
        except Exception as e:
            logger.error("Failed to notify admin %s: %s", admin, e)


_real_cooldown = {}


async def confirm_real(query, context, item_id):
    with db() as conn:
        it = conn.execute("SELECT * FROM items WHERE id=? AND is_real=1", (item_id,)).fetchone()
    if not it:
        await show(query, context, "❌ آیتم پیدا نشد.", back_main())
        return
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ بله، درخواست بده", callback_data="rqy_" + str(item_id)),
         InlineKeyboardButton("❌ نه", callback_data="pick_" + str(item_id))],
    ])
    await show(query, context,
               f"💶 می‌خوای «{it['name']}» رو به قیمت {it['real_price']} بخری؟\n{DIV}\n"
               f"با تأیید، درخواستت برای ادمین ارسال میشه و ادمین باهات هماهنگ می‌کنه.", markup)


async def send_real_request(query, context, item_id):
    user = query.from_user
    with db() as conn:
        it = conn.execute("SELECT * FROM items WHERE id=? AND is_real=1", (item_id,)).fetchone()
    if not it:
        await show(query, context, "❌ آیتم پیدا نشد.", back_main())
        return
    now = time.monotonic()
    if len(_real_cooldown) > 5000:
        _real_cooldown.clear()
    if now - _real_cooldown.get((user.id, item_id), -REAL_COOLDOWN) < REAL_COOLDOWN:
        await show(query, context, "⏳ درخواستت قبلاً ارسال شده. منتظر پیام ادمین باش.", back_main())
        return
    with db() as conn:
        n_pending = conn.execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND status='pending'",
                                 (user.id,)).fetchone()[0]
    if n_pending >= ORDER_PENDING_MAX:
        await show(query, context,
                   f"⏳ {n_pending} سفارش تحویل‌نشده داری (حداکثر {ORDER_PENDING_MAX}).\n"
                   f"صبر کن تحویل بگیری، بعد درخواست جدید بده.", back_main())
        return
    _real_cooldown[(user.id, item_id)] = now
    with db() as conn:
        oid = create_order(conn, user.id, user_label(user), it, 0)
    await notify_admins(context, user, it, oid=oid)
    await show(query, context,
               f"✅ درخواست #{oid} برای «{it['name']}» ثبت شد.\n📩 ادمین به‌زودی باهات تماس می‌گیره.",
               back_main())


async def purchase(context, user, item_id, qty=1):
    """منطق مشترک خرید (دکمه یا «چند تا؟»). خروجی: (متن, کیبورد, موفق؟)"""
    status, item, cash, price, oid = try_buy(user.id, item_id, user_label(user), qty)
    orders_kb = InlineKeyboardMarkup([[InlineKeyboardButton("📦 سفارش‌های من", callback_data="my_orders"),
                                       InlineKeyboardButton("🔙 منو", callback_data="main_menu")]])
    if status == "no_item":
        return "❌ آیتم پیدا نشد.", back_main(), False
    if status == "no_player":
        return "❌ اول /start بزن.", back_main(), False
    if status == "too_many":
        return (f"⏳ {cash} سفارش تحویل‌نشده داری (حداکثر {ORDER_PENDING_MAX}).\n"
                f"صبر کن ادمین تحویلشون بده، بعد سفارش جدید بزن."), orders_kb, False
    if status == "car_full":
        return f"❌ جای کافی برای {qty} ماشین نداری (سقف ماشین‌هات {MAX_CARS} عدد).\nاول یکی رو بفروش.", back_main(), False
    if status == "poor":
        return f"❌ پولت کمه!\n{DIV}\n💰 داری: {cash:,} $\n💵 نیاز داری: {price:,} $", back_main(), False

    await notify_admins(context, user, item, price, oid, qty)
    if should_announce(item):
        await announce_purchase(context, user, item, price, qty)
    extra = "🚗 به لیست ماشین‌هات اضافه شد.\n" if item["category"] == "car" else ""
    text = (f"🎉 خرید موفق!\n{DIV}\n"
            f"✅ {item['name']}" + (f" ×{qty}" if qty > 1 else "") +
            f"\n💰 {price:,} $ کسر شد — موجودی: {cash:,} $\n{extra}\n"
            f"📦 سفارش #{oid} ثبت شد. ادمین توی بازی تحویلت میده و بعدش همین‌جا بهت پیام میاد.")
    return text, orders_kb, True


async def buy_item(query, context, item_id, qty=1):
    text, markup, ok = await purchase(context, query.from_user, item_id, qty)
    await show(query, context, text, markup)
    if ok:
        await refresh_medals(context, query.from_user.id)


async def start_buy_qty(query, context, item_id):
    with db() as conn:
        it = conn.execute("SELECT * FROM items WHERE id=? AND is_real=0", (item_id,)).fetchone()
    if not it:
        await show(query, context, "❌ آیتم پیدا نشد.", back_main())
        return
    context.user_data.update(pstep="buy_qty", buy_item=item_id)
    await show(query, context,
               f"🔢 چند تا «{it['name']}» می‌خوای؟\n{DIV}\n💵 قیمت هر عدد: {eff_price(it):,}$\n"
               f"عددی بین ۱ تا {BUY_QTY_MAX} بنویس (همه توی یه سفارش ثبت میشن):", cancel_player_markup())


# ================= لیست آرزو =================
async def toggle_wish(query, context, item_id):
    uid = query.from_user.id
    note = None
    with db() as conn:
        it = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not it or it["is_real"]:
            note = "❌ این آیتم قابل افزودن به لیست آرزو نیست."
        elif conn.execute("SELECT 1 FROM wishlist WHERE user_id=? AND item_id=?", (uid, item_id)).fetchone():
            conn.execute("DELETE FROM wishlist WHERE user_id=? AND item_id=?", (uid, item_id))
            note = "🗑 از لیست آرزو حذف شد."
        else:
            n = conn.execute("SELECT COUNT(*) FROM wishlist WHERE user_id=?", (uid,)).fetchone()[0]
            if n >= WISHLIST_MAX:
                note = f"❌ لیست آرزوت پره (حداکثر {WISHLIST_MAX} آیتم). اول یکی رو حذف کن."
            else:
                p = conn.execute("SELECT cash FROM players WHERE user_id=?", (uid,)).fetchone()
                can_now = 1 if p and p["cash"] >= eff_price(it) else 0
                conn.execute("INSERT INTO wishlist (user_id, item_id, notified) VALUES (?,?,?)", (uid, item_id, can_now))
                note = "⭐ به لیست آرزو اضافه شد! وقتی پولت بهش برسه خبرت می‌کنم."
    await show_item(query, context, item_id, note)


async def remove_wish(query, context, item_id):
    with db() as conn:
        conn.execute("DELETE FROM wishlist WHERE user_id=? AND item_id=?", (query.from_user.id, item_id))
    await show_wishlist(query, context)


async def show_wishlist(query, context):
    uid = query.from_user.id
    with db() as conn:
        p = conn.execute("SELECT cash FROM players WHERE user_id=?", (uid,)).fetchone()
        rows = conn.execute("SELECT i.* FROM wishlist w JOIN items i ON i.id=w.item_id "
                            "WHERE w.user_id=? ORDER BY w.rowid", (uid,)).fetchall()
    cash = p["cash"] if p else 0
    if not rows:
        await show(query, context,
                   f"⭐ لیست آرزو\n{DIV}\nهنوز آیتمی توی لیست آرزوت نیست.\n"
                   f"توی فروشگاه روی هر آیتم «⭐ افزودن به لیست آرزو» رو بزن تا پیشرفتت رو ببینی.",
                   back_main())
        return
    lines = ["⭐ لیست آرزو", DIV, f"💰 موجودی تو: {cash:,} $", ""]
    kb = []
    for it in rows:
        price = max(eff_price(it), 1)
        pct = min(100, cash * 100 // price)
        lines.append(f"🛍 {it['name']} — {price:,}$" + (" 🔥" if on_sale(it) else ""))
        lines.append(f"   {bar(min(cash, price), price)} {pct}%" + ("  ✅ می‌تونی بخریش!" if cash >= price else
                                                                   f"  (کم داری: {price - cash:,}$)"))
        kb.append([InlineKeyboardButton(f"🛍 {it['name']}"[:40], callback_data="pick_" + str(it["id"])),
                   InlineKeyboardButton("🗑", callback_data="wlr_" + str(it["id"]))])
    kb.append([InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")])
    await show(query, context, "\n".join(lines), InlineKeyboardMarkup(kb))


async def notify_wishers(context, it):
    """وقتی قیمت یه آیتم موقتاً کم میشه، به کسایی که توی لیست آرزوشون دارن پیوی می‌فرسته."""
    if eff_price(it) >= it["buy_price"]:
        return
    hours = max(1, round((it["sale_until"] - time.time()) / 3600))
    with db() as conn:
        ids = [r["user_id"] for r in conn.execute(
            "SELECT user_id FROM wishlist WHERE item_id=? LIMIT 100", (it["id"],))]
    for uid in ids:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=(f"🔥 قیمت آیتم آرزوت پایین اومد!\n{DIV}\n🛍 {it['name']}\n"
                      f"🔴⬇️ {it['buy_price']:,}$ ➜ {eff_price(it):,}$ (فقط {hours} ساعت)"),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🛒 برو به صفحه آیتم", callback_data="pick_" + str(it["id"]))]]))
        except TelegramError:
            pass
        await asyncio.sleep(0.05)


# ================= اعلام خرید توی گپ =================
def should_announce(item):
    if item["is_real"]:
        return False
    if item["announce"]:
        return True
    with db() as conn:
        cats = _json_load(get_setting(conn, "announce_cats", "[]"), [])
    return item["category"] in cats


async def announce_purchase(context, user, item, price, qty=1):
    text = (f"🎉 {user_label(user)} همین الان «{item['name']}»" + (f" ×{qty}" if qty > 1 else "") + f" رو خرید!\n{DIV}\n"
            f"🏷️ {cat_map().get(item['category'], item['category'])}\n💰 {price:,} $\n\n"
            f"🛒 تو هم از فروشگاه بات خرید کن!")
    try:
        if item["photo_id"]:
            await context.bot.send_photo(chat_id=ANNOUNCE_CHAT_ID, photo=item["photo_id"], caption=text[:1024])
        else:
            await context.bot.send_message(chat_id=ANNOUNCE_CHAT_ID, text=text)
    except Exception as e:
        logger.error("Purchase announce failed: %s", e)


def resolve_category(arg):
    a = (arg or "").strip().lower()
    cmap = cat_map()
    if a in CAT_ALIASES and CAT_ALIASES[a] in cmap:
        return CAT_ALIASES[a]
    if a in cmap:
        return a
    if a:
        for key, label in cmap.items():
            if a in label.lower():
                return key
    return None


async def _announce_cmd(update, arg):
    if not is_admin(update.effective_user.id):
        return
    cmap = cat_map()
    with db() as conn:
        cats = _json_load(get_setting(conn, "announce_cats", "[]"), [])
        key = resolve_category(arg) if arg else None
        if arg and key is None:
            await update.message.reply_text("❌ دسته‌ای با این اسم پیدا نشد.\nمثال: /اعلام ماشین")
            return
        if key:
            if key in cats:
                cats.remove(key)
                msg = f"⬜ اعلام خرید «{cmap[key]}» خاموش شد."
            else:
                cats.append(key)
                msg = f"✅ اعلام خرید «{cmap[key]}» روشن شد؛ هر خرید این دسته توی گپ اعلام میشه."
            set_setting(conn, "announce_cats", json.dumps(cats))
            await update.message.reply_text(msg)
            return
    lines = ["📣 اعلام خرید توی گپ", DIV]
    lines += [f"{'✅' if k in cats else '⬜'} {label}" for k, label in cmap.items()]
    lines += ["", "روشن/خاموش کردن: /اعلام ماشین  یا  /announce car",
              "برای یه آیتم خاص: توی پنل ← لیست آیتم‌ها ← آیتم ← دکمه «اعلام خرید»."]
    await update.message.reply_text("\n".join(lines))


async def announce_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _announce_cmd(update, " ".join(context.args or []))


async def announce_fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دستور فارسی «/اعلام ماشین» (تلگرام دستور فارسی رو به‌عنوان command نمی‌شناسه، پس متنیه)."""
    parts = (update.message.text or "").split(None, 1)
    await _announce_cmd(update, parts[1] if len(parts) > 1 else "")


# ================= دعوت دوستان =================
REF_STATUS = {"pending": "⏳ در انتظار", "confirmed": "✅ تأیید شد", "rejected": "❌ رد شد"}


async def bot_username(context):
    u = context.bot.username
    if not u:
        u = (await context.bot.get_me()).username
    return u


async def notify_referral(context, user, inviter):
    uname = f"@{escape(user.username)}" if user.username else "ندارد"
    try:
        await context.bot.send_message(
            chat_id=inviter["user_id"],
            text=(f"🎉 {user_label(user)} با لینک دعوت تو وارد بات شد!\n"
                  f"بعد از اینکه وارد سرور بازی بشه و ادمین تأیید کنه، جایزه‌ات رو می‌گیری 🎁"))
    except TelegramError:
        pass
    msg = (f"🤝 <b>دعوت جدید</b>\n{DIV}\n"
           f"👤 <a href=\"tg://user?id={user.id}\">{escape(user.full_name)}</a> | {uname} | <code>{user.id}</code>\n"
           f"📨 دعوت‌کننده: {escape(inviter['username'] or str(inviter['user_id']))} | <code>{inviter['user_id']}</code>\n\n"
           f"بعد از اینکه واقعاً وارد سرور شد، تأیید کن تا جایزه‌ها داده بشه.")
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ وارد سرور شد", callback_data=f"rfy_{user.id}"),
        InlineKeyboardButton("❌ رد", callback_data=f"rfn_{user.id}"),
    ]])
    for admin in (ADMIN_ID, ADMIN_ID_2):
        try:
            await context.bot.send_message(chat_id=admin, text=msg, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception as e:
            logger.error("Failed to notify referral to %s: %s", admin, e)


async def show_invite(query, context):
    uid = query.from_user.id
    link = f"https://t.me/{await bot_username(context)}?start=ref_{uid}"
    with db() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM referrals WHERE inviter_id=? GROUP BY status",
                            (uid,)).fetchall()
    cnt = {r["status"]: r["n"] for r in rows}
    share = "https://t.me/share/url?url=" + quote(link) + "&text=" + quote("🎮 بیا توی گریم ولی با هم بازی کنیم!")
    text = (f"🤝 دعوت دوستان\n{DIV}\n"
            f"لینک اختصاصی تو:\n{link}\n\n"
            f"🎁 هر دوستی که با لینک تو وارد بات بشه و واقعاً وارد سرور بازی بشه (با تأیید ادمین):\n"
            f"   • تو: +{REF_INVITER_REWARD}$\n   • دوستت: +{REF_INVITEE_REWARD}$ هدیه خوش‌آمد\n"
            f"(حداکثر {REF_MAX} دعوت جایزه‌دار)\n\n"
            f"📊 دعوت‌شده‌ها: {sum(cnt.values())} | تأییدشده: {cnt.get('confirmed', 0)} | "
            f"در انتظار: {cnt.get('pending', 0)}")
    await show(query, context, text, InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 ارسال برای دوستان", url=share)],
        [InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")]]))


def do_confirm_referral(invitee_id):
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        r = conn.execute("SELECT * FROM referrals WHERE invitee_id=?", (invitee_id,)).fetchone()
        if not r:
            return "no", None
        if r["status"] != "pending":
            return "done", r
        paid = conn.execute("SELECT COUNT(*) FROM referrals WHERE inviter_id=? AND status='confirmed' "
                            "AND inviter_paid=1", (r["inviter_id"],)).fetchone()[0] < REF_MAX
        conn.execute("UPDATE players SET cash=cash+? WHERE user_id=?", (REF_INVITEE_REWARD, invitee_id))
        if paid:
            conn.execute("UPDATE players SET cash=cash+? WHERE user_id=?", (REF_INVITER_REWARD, r["inviter_id"]))
        conn.execute("UPDATE referrals SET status='confirmed', inviter_paid=?, done_at=? WHERE invitee_id=?",
                     (1 if paid else 0, int(time.time()), invitee_id))
    return "ok", (r, paid)


async def confirm_referral(query, context, invitee_id):
    status, data = do_confirm_referral(invitee_id)
    back = InlineKeyboardMarkup([[InlineKeyboardButton("🤝 دعوت‌ها", callback_data="rl_0")],
                                 [InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")]])
    if status == "no":
        await show(query, context, "❌ دعوت پیدا نشد.", back)
        return
    if status == "done":
        await show(query, context, f"⚠️ این دعوت قبلاً بررسی شده — وضعیت: {REF_STATUS.get(data['status'], data['status'])}", back)
        return
    r, paid = data
    try:
        await context.bot.send_message(
            chat_id=invitee_id,
            text=f"🎉 ورودت به سرور تأیید شد!\n💵 +{REF_INVITEE_REWARD}$ هدیه خوش‌آمد به حسابت اضافه شد.")
    except TelegramError:
        pass
    try:
        await context.bot.send_message(
            chat_id=r["inviter_id"],
            text=(f"🎉 دوستت وارد سرور شد!\n💵 +{REF_INVITER_REWARD}$ به حسابت اضافه شد." if paid else
                  f"🎉 دوستت وارد سرور شد! (به سقف {REF_MAX} دعوت جایزه‌دار رسیدی، پس جایزه‌ای نداره.)"))
    except TelegramError:
        pass
    await show(query, context, "✅ تأیید شد و جایزه‌ها داده شد." + ("" if paid else "\n(دعوت‌کننده به سقف رسیده بود.)"), back)
    await refresh_medals(context, invitee_id)
    await refresh_medals(context, r["inviter_id"])


async def reject_referral(query, context, invitee_id):
    with db() as conn:
        r = conn.execute("SELECT status FROM referrals WHERE invitee_id=?", (invitee_id,)).fetchone()
        if r and r["status"] == "pending":
            conn.execute("UPDATE referrals SET status='rejected', done_at=? WHERE invitee_id=?",
                         (int(time.time()), invitee_id))
    await show(query, context, "❌ دعوت رد شد." if r else "❌ دعوت پیدا نشد.",
               InlineKeyboardMarkup([[InlineKeyboardButton("🤝 دعوت‌ها", callback_data="rl_0")],
                                     [InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")]]))


async def show_referral_list(query, context, page=0):
    with db() as conn:
        rows = conn.execute(
            "SELECT r.*, a.username AS invitee_name, b.username AS inviter_name FROM referrals r "
            "LEFT JOIN players a ON a.user_id=r.invitee_id LEFT JOIN players b ON b.user_id=r.inviter_id "
            "WHERE r.status='pending' ORDER BY r.created").fetchall()
    if not rows:
        await show(query, context, f"🤝 دعوت‌های در انتظار\n{DIV}\n✅ دعوتی در انتظار تأیید نیست.", admin_menu())
        return
    chunk, page, total = paginate(rows, page)
    kb = [[InlineKeyboardButton(
        f"{r['invitee_name'] or r['invitee_id']} ← {r['inviter_name'] or r['inviter_id']}"[:48],
        callback_data=f"rv_{r['invitee_id']}")] for r in chunk]
    nav = page_nav("rl_", page, total)
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")])
    title = f"🤝 دعوت‌های در انتظار ({len(rows)})"
    if total > 1:
        title += f" — صفحه {page + 1} از {total}"
    await show(query, context, f"{title}\n{DIV}\nروی هر مورد بزن؛ بعد از ورود واقعی به سرور تأیید کن.",
               InlineKeyboardMarkup(kb))


async def show_referral_detail(query, context, invitee_id):
    with db() as conn:
        r = conn.execute(
            "SELECT r.*, a.username AS invitee_name, b.username AS inviter_name FROM referrals r "
            "LEFT JOIN players a ON a.user_id=r.invitee_id LEFT JOIN players b ON b.user_id=r.inviter_id "
            "WHERE r.invitee_id=?", (invitee_id,)).fetchone()
    if not r:
        await show(query, context, "❌ دعوت پیدا نشد.", admin_menu())
        return
    text = (f"🤝 دعوت — {REF_STATUS.get(r['status'], r['status'])}\n{DIV}\n"
            f"👤 دعوت‌شده: {r['invitee_name'] or '-'} | {r['invitee_id']}\n"
            f"📨 دعوت‌کننده: {r['inviter_name'] or '-'} | {r['inviter_id']}\n"
            f"🕒 {datetime.fromtimestamp(r['created']).strftime('%Y-%m-%d %H:%M')}")
    kb = []
    if r["status"] == "pending":
        kb.append([InlineKeyboardButton("✅ وارد سرور شد", callback_data=f"rfy_{invitee_id}"),
                   InlineKeyboardButton("❌ رد", callback_data=f"rfn_{invitee_id}")])
    kb.append([InlineKeyboardButton("🔙 لیست", callback_data="rl_0")])
    await show(query, context, text, InlineKeyboardMarkup(kb))


# ================= فروش به سرور (خرید از بازیکن) =================
SELL_STATUS = {"pending": "⏳ در انتظار", "accepted": "✅ پرداخت شد", "rejected": "❌ رد شد"}


def sell_window(conn, uid):
    """(دریافتی ۲۴ ساعت اخیر, مجموع درخواست‌های در انتظار, قدیمی‌ترین دریافتی داخل پنجره)"""
    now = int(time.time())
    credited = conn.execute(
        "SELECT COALESCE(SUM(total),0) FROM sell_orders WHERE user_id=? AND status='accepted' AND done_at>?",
        (uid, now - 86400)).fetchone()[0]
    pending = conn.execute(
        "SELECT COALESCE(SUM(total),0) FROM sell_orders WHERE user_id=? AND status='pending'", (uid,)).fetchone()[0]
    oldest = conn.execute(
        "SELECT MIN(done_at) FROM sell_orders WHERE user_id=? AND status='accepted' AND done_at>?",
        (uid, now - 86400)).fetchone()[0]
    return credited, pending, oldest


def create_sell_order(uid, username, item_id, qty):
    now = int(time.time())
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        ensure_player(conn, uid, username)
        it = conn.execute("SELECT * FROM buy_items WHERE id=?", (item_id,)).fetchone()
        if not it:
            return "no_item", None
        n_pending = conn.execute("SELECT COUNT(*) FROM sell_orders WHERE user_id=? AND status='pending'",
                                 (uid,)).fetchone()[0]
        if n_pending >= SELL_PENDING_MAX:
            return "too_many", n_pending
        total = it["price"] * qty
        credited, pending, oldest = sell_window(conn, uid)
        remaining = max(0, SELL_DAILY_CAP - credited - pending)
        if total > remaining:
            wait = (oldest + 86400 - now) if oldest else 0
            return "cap", {"remaining": remaining, "max_qty": remaining // it["price"],
                           "wait": max(0, wait), "item": it}
        cur = conn.execute(
            "INSERT INTO sell_orders (user_id, username, item_name, qty, unit_price, total, created)"
            " VALUES (?,?,?,?,?,?,?)", (uid, username, it["name"], qty, it["price"], total, now))
        return "ok", {"id": cur.lastrowid, "item": it, "total": total}


def sell_admin_markup(oid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ دریافت کردم", callback_data=f"sa_{oid}"),
        InlineKeyboardButton("❌ رد", callback_data=f"sr_{oid}"),
    ]])


async def notify_sell(context, user, oid, item, qty, total):
    uname = f"@{escape(user.username)}" if user.username else "ندارد"
    msg = (f"📥 <b>درخواست فروش به سرور</b>\n{DIV}\n"
           f"🔢 درخواست: #{oid}\n"
           f"👤 <a href=\"tg://user?id={user.id}\">{escape(user.full_name)}</a> | {uname} | <code>{user.id}</code>\n"
           f"💎 آیتم: {escape(item['name'])} ×{qty}\n"
           f"💰 مبلغ: {total:,} $ ({item['price']:,}$ × {qty})\n\n"
           f"بعد از اینکه آیتم‌ها رو توی بازی تحویل گرفتی، «دریافت کردم» رو بزن تا پول به حسابش بیاد.")
    for admin in (ADMIN_ID, ADMIN_ID_2):
        try:
            await context.bot.send_message(chat_id=admin, text=msg, parse_mode=ParseMode.HTML,
                                           reply_markup=sell_admin_markup(oid))
        except Exception as e:
            logger.error("Failed to notify sell to %s: %s", admin, e)


async def show_sell_menu(query, context):
    uid = query.from_user.id
    with db() as conn:
        items = conn.execute("SELECT * FROM buy_items ORDER BY id").fetchall()
        credited, pending, oldest = sell_window(conn, uid)
        last = conn.execute("SELECT * FROM sell_orders WHERE user_id=? ORDER BY id DESC LIMIT 3", (uid,)).fetchall()
    if not items:
        await show(query, context, f"💎 فروش به سرور\n{DIV}\nفعلاً سرور چیزی نمی‌خره.", back_main())
        return
    remaining = max(0, SELL_DAILY_CAP - credited - pending)
    lines = ["💎 فروش به سرور", DIV, "سرور این آیتم‌ها رو از تو می‌خره 👇"]
    lines += [price_row(it) for it in items]
    lines += [DIV,
              f"📊 سقف دریافت: {SELL_DAILY_CAP:,}$ در هر ۲۴ ساعت",
              f"   دریافتی: {credited:,}$ | در انتظار: {pending:,}$ | باقی‌مانده: {remaining:,}$"]
    if remaining == 0 and oldest:
        lines.append(f"   ⏳ {fmt_duration(oldest + 86400 - time.time())} دیگه دوباره می‌تونی بفروشی")
    lines += ["", "📝 درخواست بده، آیتم رو توی بازی به ادمین تحویل بده؛ بعد از تأیید پول میاد."]
    if last:
        lines += ["", "🕘 آخرین درخواست‌هات:"]
        lines += [f"   #{o['id']} {o['item_name']} ×{o['qty']} — {o['total']:,}$ {SELL_STATUS.get(o['status'], '')}"
                  for o in last]
    kb, row = [], []
    for it in items:
        row.append(InlineKeyboardButton(f"💎 {it['name']}"[:30], callback_data=f"sl_{it['id']}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")])
    await show(query, context, "\n".join(lines), InlineKeyboardMarkup(kb))


async def start_sell(query, context, item_id):
    with db() as conn:
        it = conn.execute("SELECT * FROM buy_items WHERE id=?", (item_id,)).fetchone()
    if not it:
        await show(query, context, "❌ این آیتم دیگه خریده نمیشه.", back_main())
        return
    context.user_data.update(pstep="sell_qty", sell_item=item_id)
    await show(query, context,
               f"💎 {it['name']}\n{DIV}\n💵 قیمت هر عدد: {it['price']:,}$\n\n"
               f"چند تا می‌فروشی؟ فقط عدد بنویس:", cancel_player_markup())


def do_accept_sell(oid):
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        o = conn.execute("SELECT * FROM sell_orders WHERE id=?", (oid,)).fetchone()
        if not o:
            return "no", None
        if o["status"] != "pending":
            return "done", o
        credited, _pending, _oldest = sell_window(conn, o["user_id"])
        if credited + o["total"] > SELL_DAILY_CAP:
            return "cap", o
        conn.execute("UPDATE players SET cash=cash+? WHERE user_id=?", (o["total"], o["user_id"]))
        conn.execute("UPDATE sell_orders SET status='accepted', done_at=? WHERE id=?", (int(time.time()), oid))
    return "ok", o


def sell_back_markup():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📥 درخواست‌های فروش", callback_data="sq_0")],
                                 [InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")]])


async def accept_sell(query, context, oid):
    status, o = do_accept_sell(oid)
    if status == "no":
        await show(query, context, "❌ درخواست پیدا نشد.", sell_back_markup())
    elif status == "done":
        await show(query, context, f"⚠️ این درخواست قبلاً بررسی شده — {SELL_STATUS.get(o['status'], o['status'])}",
                   sell_back_markup())
    elif status == "cap":
        await show(query, context,
                   f"⚠️ پرداخت این درخواست از سقف ۲۴ ساعته‌ی بازیکن ({SELL_DAILY_CAP:,}$) رد میشه.\n"
                   f"درخواست در انتظار موند؛ بعداً تأیید کن یا رد کن.", sell_back_markup())
    else:
        try:
            await context.bot.send_message(
                chat_id=o["user_id"],
                text=f"✅ درخواست فروش #{oid} تأیید شد!\n{DIV}\n💎 {o['item_name']} ×{o['qty']}\n"
                     f"💵 {o['total']:,}$ به حسابت اضافه شد.")
        except TelegramError:
            pass
        await show(query, context, f"✅ درخواست #{oid} پرداخت شد: {o['total']:,}$ به {o['username'] or o['user_id']}.",
                   sell_back_markup())
        await refresh_medals(context, o["user_id"])


async def reject_sell(query, context, oid):
    with db() as conn:
        o = conn.execute("SELECT * FROM sell_orders WHERE id=?", (oid,)).fetchone()
        done = bool(o and o["status"] == "pending")
        if done:
            conn.execute("UPDATE sell_orders SET status='rejected', done_at=? WHERE id=?", (int(time.time()), oid))
    if not o:
        await show(query, context, "❌ درخواست پیدا نشد.", sell_back_markup())
    elif not done:
        await show(query, context, f"⚠️ این درخواست قبلاً بررسی شده — {SELL_STATUS.get(o['status'], o['status'])}",
                   sell_back_markup())
    else:
        try:
            await context.bot.send_message(chat_id=o["user_id"],
                                           text=f"❌ درخواست فروش #{oid} ({o['item_name']} ×{o['qty']}) رد شد.")
        except TelegramError:
            pass
        await show(query, context, f"❌ درخواست #{oid} رد شد.", sell_back_markup())


async def show_sell_requests(query, context, page=0):
    with db() as conn:
        rows = conn.execute("SELECT * FROM sell_orders WHERE status='pending' ORDER BY id").fetchall()
    if not rows:
        await show(query, context, f"📥 درخواست‌های فروش\n{DIV}\n✅ درخواست در انتظاری نیست.", admin_menu())
        return
    chunk, page, total = paginate(rows, page)
    kb = [[InlineKeyboardButton(
        f"#{o['id']} • {o['username'] or o['user_id']} • {o['item_name']}×{o['qty']}"[:48],
        callback_data=f"sv_{o['id']}")] for o in chunk]
    nav = page_nav("sq_", page, total)
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")])
    title = f"📥 درخواست‌های فروش ({len(rows)})"
    if total > 1:
        title += f" — صفحه {page + 1} از {total}"
    await show(query, context, f"{title}\n{DIV}", InlineKeyboardMarkup(kb))


async def show_sell_detail(query, context, oid):
    with db() as conn:
        o = conn.execute("SELECT * FROM sell_orders WHERE id=?", (oid,)).fetchone()
    if not o:
        await show(query, context, "❌ درخواست پیدا نشد.", admin_menu())
        return
    text = (f"📥 درخواست #{o['id']} — {SELL_STATUS.get(o['status'], o['status'])}\n{DIV}\n"
            f"👤 {o['username'] or '-'} | {o['user_id']}\n💎 {o['item_name']} ×{o['qty']}\n"
            f"💰 {o['total']:,}$ ({o['unit_price']:,}$ × {o['qty']})\n"
            f"🕒 {datetime.fromtimestamp(o['created']).strftime('%Y-%m-%d %H:%M')}")
    kb = []
    if o["status"] == "pending":
        kb.append([InlineKeyboardButton("✅ دریافت کردم", callback_data=f"sa_{oid}"),
                   InlineKeyboardButton("❌ رد", callback_data=f"sr_{oid}")])
    kb.append([InlineKeyboardButton("🔙 لیست", callback_data="sq_0")])
    await show(query, context, text, InlineKeyboardMarkup(kb))


async def show_buy_manage(query, context):
    with db() as conn:
        items = conn.execute("SELECT * FROM buy_items ORDER BY id").fetchall()
    lines = ["💎 قیمت‌های خرید از بازیکن", DIV, f"📊 سقف دریافت هر بازیکن: {SELL_DAILY_CAP:,}$ در ۲۴ ساعت", ""]
    lines += [price_row(it) for it in items] or ["(هنوز آیتمی نیست)"]
    kb = [[InlineKeyboardButton(f"✏️ {it['name']} ({it['price']:,}$)"[:48], callback_data=f"bi_{it['id']}")]
          for it in items]
    kb.append([InlineKeyboardButton("➕ افزودن آیتم خرید", callback_data="bnew")])
    kb.append([InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")])
    await show(query, context, "\n".join(lines), InlineKeyboardMarkup(kb))


async def show_buy_detail(query, context, iid):
    with db() as conn:
        it = conn.execute("SELECT * FROM buy_items WHERE id=?", (iid,)).fetchone()
    if not it:
        await show_buy_manage(query, context)
        return
    mk = f"{MARKET_NAMES[it['market']]} ({it['factor']}٪ قیمت بازار)" if it["market"] in MARKET_NAMES else "ندارد"
    kb = [[InlineKeyboardButton("✏️ قیمت", callback_data=f"bpe_{iid}"),
           InlineKeyboardButton("✏️ اسم", callback_data=f"bne_{iid}")],
          [InlineKeyboardButton("📈 نوسان قیمت: " + (MARKET_NAMES.get(it["market"], "ندارد")),
                                callback_data=f"bmk_{iid}")]]
    if it["market"] in MARKET_NAMES:
        kb.append([InlineKeyboardButton(f"⚖️ نسبت به قیمت بازار: {it['factor']}٪ (تغییر)", callback_data=f"bfc_{iid}")])
    kb += [[InlineKeyboardButton("🗑 حذف", callback_data=f"bdl_{iid}")],
           [InlineKeyboardButton("🔙 لیست", callback_data="bm")]]
    await show(query, context, f"💎 {it['name']}\n{DIV}\n💵 قیمت خرید: {price_row_value(it)}\n📈 نوسان روزانه: {mk}",
               InlineKeyboardMarkup(kb))


# ===================================================================
#  بازار طلا و نقره (نوسان روزانه) + مدیریت کاربران
# ===================================================================
MARKET_NAMES = {"gold": "🥇 طلا", "silver": "🥈 نقره"}
# (حداقل, حداکثر, وزن): هر بار اول یه بازه انتخاب میشه، بعد یه عدد تصادفی داخلش
GOLD_BUCKETS = [(10, 14, 25), (15, 19, 35), (20, 20, 15), (21, 27, 18), (28, 35, 7)]
SILVER_BUCKETS = [(5, 9, 45), (10, 10, 20), (11, 14, 22), (15, 17, 9), (18, 20, 4)]


def trend_arrow(cur, prev):
    if prev is None:
        return ""
    if cur > prev:
        return f"🟢⬆️ +{cur - prev}"
    if cur < prev:
        return f"🔴⬇️ -{prev - cur}"
    return "⚪➖"


def price_row_value(it):
    arrow = trend_arrow(it["price"], it["prev_price"])
    return f"{it['price']:,}$" + (f"  {arrow}" if arrow else "")


def price_row(it):
    return f"• {it['name']} — {price_row_value(it)}"


def roll_price(buckets):
    lo, hi, _w = random.choices(buckets, weights=[b[2] for b in buckets])[0]
    return random.randint(lo, hi)


def set_market_price(conn, key, new, now):
    """قیمت پایه‌ی بازار رو عوض می‌کنه و روی آیتم‌های وصل‌شده اعمال می‌کنه. قیمت قبلی رو برمی‌گردونه."""
    old = conn.execute("SELECT price FROM market WHERE key=?", (key,)).fetchone()["price"]
    conn.execute("UPDATE market SET prev=?, price=?, updated=? WHERE key=?", (old, new, now, key))
    for it in conn.execute("SELECT id, factor, price FROM buy_items WHERE market=?", (key,)).fetchall():
        conn.execute("UPDATE buy_items SET prev_price=?, price=? WHERE id=?",
                     (it["price"], max(1, (new * it["factor"] + 50) // 100), it["id"]))
    return old


def roll_market():
    """قیمت جدید تصادفی طلا و نقره."""
    now = int(time.time())
    out = {}
    with db() as conn:
        for key, buckets in (("gold", GOLD_BUCKETS), ("silver", SILVER_BUCKETS)):
            new = roll_price(buckets)
            out[key] = (set_market_price(conn, key, new, now), new)
    return out


def market_text(title, note=None):
    with db() as conn:
        rows = conn.execute("SELECT * FROM market ORDER BY CASE key WHEN 'gold' THEN 0 ELSE 1 END").fetchall()
        linked = conn.execute("SELECT * FROM buy_items WHERE market IS NOT NULL ORDER BY market DESC, factor DESC").fetchall()
    lines = [title, DIV]
    if note:
        lines += [note, ""]
    for r in rows:
        lines.append(f"{MARKET_NAMES.get(r['key'], r['key'])} (تکه‌شده): {r['price']}$  {trend_arrow(r['price'], r['prev'])}".rstrip())
    last = max((r["updated"] for r in rows), default=0)
    if linked:
        lines += ["", "آیتم‌های وابسته:"] + [price_row(it) for it in linked]
    return "\n".join(lines), last


async def show_market(query, context, note=None):
    text, last = market_text("📈 بازار طلا و نقره", note)
    text += (f"\n\n🕒 آخرین تغییر: {fmt_duration(time.time() - last) + ' پیش' if last else 'هنوز تغییری نداده'}"
             f"\n🎲 بازه‌ها: طلا {GOLD_BUCKETS[0][0]}–{GOLD_BUCKETS[-1][1]}$ | نقره {SILVER_BUCKETS[0][0]}–{SILVER_BUCKETS[-1][1]}$"
             f"\n(قیمت‌های پایین‌تر شانس بیشتری دارن)")
    await show(query, context, text, InlineKeyboardMarkup([
        [InlineKeyboardButton("🎲 ساخت قیمت امروز", callback_data="mkroll")],
        [InlineKeyboardButton("🥇 طلا: قیمت کمتر", callback_data="mkd_gold"),
         InlineKeyboardButton("🥇 طلا: قیمت بیشتر", callback_data="mku_gold")],
        [InlineKeyboardButton("🥈 نقره: قیمت کمتر", callback_data="mkd_silver"),
         InlineKeyboardButton("🥈 نقره: قیمت بیشتر", callback_data="mku_silver")],
        [InlineKeyboardButton("📣 اعلام قیمت‌ها توی گپ", callback_data="mkann")],
        [InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")]]))


async def announce_market(context, note=None, title="📈 قیمت امروز خرید سرور"):
    text, _last = market_text(title, note)
    text += "\n\n💎 برای فروش از منوی بات «فروش به سرور» رو بزن."
    try:
        await context.bot.send_message(chat_id=ANNOUNCE_CHAT_ID, text=text)
        return None
    except Exception as e:
        logger.error("Market announce failed: %s", e)
        return str(e)


# ---------- مسدودسازی / تأیید ورود / پروفایل / حذف ----------
def is_banned(conn, uid):
    return conn.execute("SELECT reason FROM bans WHERE user_id=?", (uid,)).fetchone()


_seen = {}


async def access_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """قبل از همه: کاربر مسدودشده یا تأییدنشده بلاک میشه و آخرین فعالیت ثبت میشه."""
    user = update.effective_user
    if not user:
        return
    now = time.time()
    with db() as conn:
        ban = None if is_admin(user.id) else is_banned(conn, user.id)
        if not ban and now - _seen.get(user.id, 0) > 600:
            conn.execute("UPDATE players SET last_seen=? WHERE user_id=?", (int(now), user.id))
            _seen[user.id] = now
    if not ban:
        if is_admin(user.id):
            return
        # کاربر تأییدنشده (یا ثبت‌نام‌نکرده): فقط /start مجازه
        with db() as conn:
            row = conn.execute("SELECT approved FROM players WHERE user_id=?", (user.id,)).fetchone()
        if row is not None and row["approved"]:
            return
        msg_ = update.effective_message
        txt_ = (msg_.text or "") if msg_ else ""
        if txt_.startswith("/start"):
            return
        pending_text = ("⏳ هنوز ادمین ورودت رو تأیید نکرده. صبر کن." if row is not None
                        else "❌ اول توی پیوی بات /start بزن تا ثبت‌نام بشی.")
        try:
            if update.callback_query:
                await update.callback_query.answer(pending_text, show_alert=True)
            elif msg_ and (msg_.chat.type == "private" or txt_.startswith("/")):
                await msg_.reply_text(pending_text)
        except TelegramError:
            pass
        raise ApplicationHandlerStop
    text = f"🚫 شما از استفاده از این بات مسدود شدید.\nدلیل: {ban['reason'] or 'نامشخص'}"
    try:
        if update.callback_query:
            await update.callback_query.answer(text, show_alert=True)
        elif update.effective_message and (update.effective_message.chat.type == "private"
                                           or (update.effective_message.text or "").startswith("/")):
            await update.effective_message.reply_text(text)
    except TelegramError:
        pass
    raise ApplicationHandlerStop


async def notify_user(context, uid, text):
    try:
        await context.bot.send_message(chat_id=uid, text=text)
        return True
    except TelegramError:
        return False


def do_ban(uid, reason):
    if is_admin(uid):
        return False
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO bans (user_id, reason, created) VALUES (?,?,?)",
                     (uid, (reason or "").strip() or None, int(time.time())))
    return True


def do_unban(uid):
    with db() as conn:
        n = conn.execute("DELETE FROM bans WHERE user_id=?", (uid,)).rowcount
    return n > 0


def new_user_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")]])


async def approve_user(query, context, uid):
    changed = False
    ref = None
    with db() as conn:
        p = conn.execute("SELECT approved FROM players WHERE user_id=?", (uid,)).fetchone()
        if p and not p["approved"]:
            conn.execute("UPDATE players SET approved=1 WHERE user_id=?", (uid,))
            changed = True
            ref = conn.execute(
                "SELECT r.inviter_id AS user_id, pl.username AS username FROM referrals r "
                "LEFT JOIN players pl ON pl.user_id=r.inviter_id "
                "WHERE r.invitee_id=? AND r.status='pending'", (uid,)).fetchone()
    if not p:
        text = "❌ این کاربر پیدا نشد (شاید قبلاً رد شده)."
    elif not changed:
        text = f"⚠️ کاربر {uid} قبلاً تأیید شده بود."
    else:
        text = f"✅ کاربر {uid} تأیید شد."
        await notify_user(context, uid, "✅ ورودت به بات تأیید شد! حالا /start بزن و شروع کن 🎮")
        if ref:
            # اعلان دعوت (برای تأیید ورود به سرور) حالا که کاربر تأیید شد به ادمین‌ها میره
            try:
                chat = await context.bot.get_chat(uid)
                await notify_referral(context, chat, ref)
            except Exception as e:
                logger.error("Referral notify after approval failed: %s", e)
    await show(query, context, text, new_user_back())


async def reject_user(query, context, uid):
    if is_admin(uid):
        await show(query, context, "❌ نمی‌شه ادمین رو رد کرد.", new_user_back())
        return
    with db() as conn:
        p = conn.execute("SELECT approved FROM players WHERE user_id=?", (uid,)).fetchone()
        if p and p["approved"]:
            already = True
        else:
            already = False
            conn.execute("DELETE FROM players WHERE user_id=? AND approved=0", (uid,))
            conn.execute("DELETE FROM referrals WHERE invitee_id=? AND status='pending'", (uid,))
    if already:
        await show(query, context, f"⚠️ کاربر {uid} قبلاً تأیید شده بود؛ برای مسدود کردنش از پنل کاربران استفاده کن.",
                   new_user_back())
        return
    do_ban(uid, "درخواست ورود رد شد")
    await notify_user(context, uid, "❌ درخواست ورودت به بات رد شد.")
    await show(query, context, f"❌ کاربر {uid} رد و مسدود شد.", new_user_back())


def delete_player(uid):
    """همه‌ی اطلاعات بازی کاربر پاک میشه (می‌تونه دوباره /start بزنه). مسدودی پاک نمیشه."""
    if is_admin(uid):
        return None
    now = int(time.time())
    with db() as conn:
        pending = conn.execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND status='pending'", (uid,)).fetchone()[0]
        remove_member(conn, uid)
        conn.execute("DELETE FROM wishlist WHERE user_id=?", (uid,))
        conn.execute("UPDATE orders SET status='cancelled', done_at=? WHERE user_id=? AND status='pending'", (now, uid))
        conn.execute("UPDATE sell_orders SET status='rejected', done_at=? WHERE user_id=? AND status='pending'", (now, uid))
        conn.execute("UPDATE tickets SET status='closed' WHERE user_id=? AND status!='closed'", (uid,))
        conn.execute("DELETE FROM referrals WHERE invitee_id=? AND status='pending'", (uid,))
        conn.execute("UPDATE referrals SET status='rejected', done_at=? WHERE inviter_id=? AND status='pending'", (now, uid))
        deleted = conn.execute("DELETE FROM players WHERE user_id=?", (uid,)).rowcount
    return {"existed": deleted > 0, "pending": pending}


async def kick_from_group(context, uid):
    """کیک از گپ (می‌تونه دوباره عضو بشه). خروجی: متن خطا یا None."""
    if is_admin(uid):
        return "نمی‌شه ادمین رو کیک کرد."
    try:
        await context.bot.ban_chat_member(ANNOUNCE_CHAT_ID, uid)
        await context.bot.unban_chat_member(ANNOUNCE_CHAT_ID, uid, only_if_banned=True)
    except Exception as e:
        logger.error("Kick failed: %s", e)
        return str(e)
    await notify_user(context, uid, "⚠️ تذکر: ادمین تو رو از گپ کیک کرد.\n"
                                    "می‌تونی دوباره عضو بشی؛ لطفاً قوانین رو رعایت کن.")
    return None


async def tg_profile(context, uid):
    info = {"name": None, "username": None, "bio": None, "photo": None}
    try:
        chat = await context.bot.get_chat(uid)
        info["name"] = " ".join(x for x in (chat.first_name, chat.last_name) if x) or None
        info["username"] = chat.username
        info["bio"] = getattr(chat, "bio", None)
    except Exception:
        pass
    try:
        photos = await context.bot.get_user_profile_photos(uid, limit=1)
        if photos.total_count and photos.photos:
            info["photo"] = photos.photos[0][-1].file_id
    except Exception:
        pass
    return info


def fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "نامشخص"


async def show_user_profile(query, context, uid):
    with db() as conn:
        p = conn.execute("SELECT * FROM players WHERE user_id=?", (uid,)).fetchone()
        ban = is_banned(conn, uid)
        stats = {}
        if p:
            stats["orders"] = conn.execute("SELECT COUNT(*), SUM(status='pending') FROM orders WHERE user_id=?", (uid,)).fetchone()
            stats["tickets"] = conn.execute("SELECT COUNT(*) FROM tickets WHERE user_id=?", (uid,)).fetchone()[0]
            stats["sold"] = conn.execute("SELECT COALESCE(SUM(total),0) FROM sell_orders WHERE user_id=? AND status='accepted'",
                                         (uid,)).fetchone()[0]
            stats["fac"] = faction_of(conn, uid)
    info = await tg_profile(context, uid)
    lines = ["👤 پروفایل کاربر", DIV, f"🆔 {uid}"]
    if info["name"]:
        lines.append(f"📛 نام: {info['name']}")
    uname = info["username"] or (p["username"] if p else None)
    if uname:
        lines.append(f"🔗 یوزرنیم: @{uname}" if info["username"] else f"🔗 نام ثبت‌شده: {uname}")
    if info["bio"]:
        lines.append(f"📝 بیو: {info['bio'][:120]}")
    status_txt = f"🚫 مسدود — {ban['reason'] or 'بدون دلیل'}" if ban else (
        "⏳ در انتظار تأیید ورود" if (p and not p["approved"]) else "✅ فعال")
    lines.append("🚦 وضعیت: " + status_txt)
    if p:
        cars = get_cars(p)
        medals = get_medals(p)
        lines += [DIV, f"💰 پول نقد: {p['cash']:,} $", f"🏠 خانه: {'دارد' if p['has_house'] else 'ندارد'}",
                  f"🚗 ماشین‌ها ({len(cars)}/{MAX_CARS}): " + (", ".join(cars) if cars else "—"),
                  "🏅 مدال‌ها: " + (" | ".join(medals) if medals else "—")]
        if stats["fac"]:
            lines.append(f"🏴 فکشن: {stats['fac']['name']}" + (" 👑" if stats["fac"]["leader_id"] == uid else ""))
        lines += [f"🔥 استریک: {p['streak']} روز",
                  f"📦 سفارش‌ها: {stats['orders'][0]} (تحویل‌نشده: {stats['orders'][1] or 0})",
                  f"💎 فروخته به سرور: {stats['sold']:,}$ | 🎫 تیکت‌ها: {stats['tickets']}",
                  f"🗓 عضویت: {fmt_ts(p['joined'])}", f"🕒 آخرین فعالیت: {fmt_ts(p['last_seen'])}"]
    else:
        lines += [DIV, "این کاربر توی بات ثبت‌نام نکرده (یا حذف شده)."]
    kb = []
    if p and not p["approved"] and not ban:
        kb.append([InlineKeyboardButton("✅ تأیید ورود", callback_data=f"nuy_{uid}"),
                   InlineKeyboardButton("❌ رد", callback_data=f"nun_{uid}")])
    kb += [[InlineKeyboardButton("✅ رفع مسدودی" if ban else "🚫 مسدود کردن",
                                 callback_data=f"uub_{uid}" if ban else f"ub_{uid}")],
           [InlineKeyboardButton("👢 کیک از گپ", callback_data=f"uk_{uid}"),
            InlineKeyboardButton("🗑 حذف از بات", callback_data=f"ud_{uid}")],
           [InlineKeyboardButton("🔙 لیست کاربران", callback_data="ul_0")]]
    await show(query, context, "\n".join(lines), InlineKeyboardMarkup(kb), photo=info["photo"])


async def show_user_list(query, context, page=0, banned_only=False):
    with db() as conn:
        if banned_only:
            rows = conn.execute("SELECT b.user_id AS user_id, p.username AS username, p.cash AS cash, 1 AS banned, "
                                "0 AS waiting "
                                "FROM bans b LEFT JOIN players p ON p.user_id=b.user_id ORDER BY b.created DESC").fetchall()
        else:
            rows = conn.execute("SELECT p.user_id AS user_id, p.username AS username, p.cash AS cash, "
                                "(b.user_id IS NOT NULL) AS banned, (p.approved=0) AS waiting FROM players p "
                                "LEFT JOIN bans b ON b.user_id=p.user_id ORDER BY p.approved ASC, "
                                "p.last_seen DESC, p.user_id DESC").fetchall()
    prefix = "ulb_" if banned_only else "ul_"
    chunk, page, total = paginate(rows, page)
    kb = [[InlineKeyboardButton(
        f"{'🚫 ' if r['banned'] else ('⏳ ' if r['waiting'] else '')}{r['username'] or r['user_id']} • {(r['cash'] or 0):,}$"[:48],
        callback_data=f"uv_{r['user_id']}")] for r in chunk]
    nav = page_nav(prefix, page, total)
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("🔎 جستجو", callback_data="usearch"),
               InlineKeyboardButton("✅ همه" if banned_only else "🚫 مسدودها",
                                    callback_data="ul_0" if banned_only else "ulb_0")])
    kb.append([InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")])
    title = ("🚫 کاربران مسدود" if banned_only else "👥 کاربران") + f" ({len(rows)})"
    if total > 1:
        title += f" — صفحه {page + 1} از {total}"
    hint = "(خالیه)" if not rows else "روی هر کاربر بزن تا پروفایلش رو ببینی.\n⏳ = منتظر تأیید ورود"
    await show(query, context, f"{title}\n{DIV}\n" + hint, InlineKeyboardMarkup(kb))


def user_back(uid):
    return InlineKeyboardMarkup([[InlineKeyboardButton("👤 پروفایل", callback_data=f"uv_{uid}"),
                                  InlineKeyboardButton("👥 کاربران", callback_data="ul_0")]])


async def resolve_user_arg(update, args):
    """(uid, label, بقیه‌ی آرگومان‌ها) یا (None, پیام خطا, [])"""
    args = list(args or [])
    reply = update.message.reply_to_message
    if reply and reply.from_user and not (args and (args[0].startswith("@") or args[0].isdigit())):
        return reply.from_user.id, user_label(reply.from_user), args
    if not args:
        return None, "❌ روی پیام کاربر ریپلای کن یا @یوزرنیم / آیدی عددی بنویس.", []
    a = args[0]
    with db() as conn:
        if a.startswith("@"):
            row = conn.execute("SELECT user_id, username FROM players WHERE lower(username)=lower(?)", (a[1:],)).fetchone()
            if not row:
                return None, "❌ کاربری با این یوزرنیم توی بات پیدا نشد.", []
            return row["user_id"], row["username"] or a, args[1:]
        if a.isdigit():
            row = conn.execute("SELECT username FROM players WHERE user_id=?", (int(a),)).fetchone()
            return int(a), (row["username"] if row and row["username"] else a), args[1:]
    return None, "❌ آرگومان نامعتبر.", []


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    uid, label, rest = await resolve_user_arg(update, context.args)
    if uid is None:
        await update.message.reply_text(label + "\nمثال: /ban @user دلیل")
        return
    reason = " ".join(rest)
    if not do_ban(uid, reason):
        await update.message.reply_text("❌ نمی‌شه ادمین رو مسدود کرد.")
        return
    await notify_user(context, uid, f"🚫 دسترسی تو به بات مسدود شد.\nدلیل: {reason or 'نامشخص'}")
    await update.message.reply_text(f"🚫 {label} مسدود شد." + (f"\nدلیل: {reason}" if reason else ""))


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    uid, label, _rest = await resolve_user_arg(update, context.args)
    if uid is None:
        await update.message.reply_text(label + "\nمثال: /unban @user")
        return
    if do_unban(uid):
        await notify_user(context, uid, "✅ مسدودیتت برداشته شد. می‌تونی دوباره از بات استفاده کنی.")
        await update.message.reply_text(f"✅ مسدودیت {label} برداشته شد.")
    else:
        await update.message.reply_text("⚠️ این کاربر مسدود نبود.")


async def kick_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    uid, label, _rest = await resolve_user_arg(update, context.args)
    if uid is None:
        await update.message.reply_text(label + "\nمثال: /kick @user")
        return
    err = await kick_from_group(context, uid)
    await update.message.reply_text(f"👢 {label} از گپ کیک شد (می‌تونه دوباره عضو بشه)." if not err else
                                    f"❌ کیک انجام نشد: {err}\n(بات باید توی گپ ادمین با دسترسی بن باشه)")


async def user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/user @یوزرنیم — خلاصه‌ی پروفایل (متنی)"""
    if not is_admin(update.effective_user.id):
        return
    uid, label, _rest = await resolve_user_arg(update, context.args)
    if uid is None:
        await update.message.reply_text(label + "\nمثال: /user @user")
        return
    with db() as conn:
        p = conn.execute("SELECT * FROM players WHERE user_id=?", (uid,)).fetchone()
        ban = is_banned(conn, uid)
        text = asset_text(conn, p) if p else "این کاربر توی بات ثبت‌نام نکرده."
    await update.message.reply_text(f"🆔 {uid}" + (f" | 🚫 مسدود: {ban['reason'] or 'بدون دلیل'}" if ban else "") + f"\n{text}")


# ================= سفارش‌ها =================
async def show_my_orders(query, context):
    with db() as conn:
        rows = conn.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 8",
                            (query.from_user.id,)).fetchall()
    if not rows:
        text = f"📦 سفارش‌های من\n{DIV}\nهنوز سفارشی نداری. از فروشگاه خرید کن 🛒"
    else:
        lines = ["📦 سفارش‌های من (۸ تای آخر)", DIV]
        for o in rows:
            lines.append(f"#{o['id']} • {order_name(o)} • {order_price_text(o)}\n   {ORDER_STATUS.get(o['status'], o['status'])}")
        text = "\n".join(lines)
    await show(query, context, text, back_main())


async def show_order_list(query, context, page=0):
    with db() as conn:
        rows = conn.execute("SELECT * FROM orders WHERE status='pending' ORDER BY id").fetchall()
    if not rows:
        await show(query, context, f"📦 سفارش‌های باز\n{DIV}\n✅ سفارش تحویل‌نشده‌ای نیست.", admin_menu())
        return
    chunk, page, total = paginate(rows, page)
    kb = [[InlineKeyboardButton(f"#{o['id']} • {o['username'] or o['user_id']} • {order_name(o)}"[:48],
                                callback_data=f"ov_{o['id']}")] for o in chunk]
    nav = page_nav("ol_", page, total)
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")])
    title = f"📦 سفارش‌های تحویل‌نشده ({len(rows)})"
    if total > 1:
        title += f" — صفحه {page + 1} از {total}"
    await show(query, context, f"{title}\n{DIV}", InlineKeyboardMarkup(kb))


async def show_order_detail(query, context, oid):
    with db() as conn:
        o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        await show(query, context, "❌ سفارش پیدا نشد.", admin_menu())
        return
    text = (f"📦 سفارش #{o['id']} — {ORDER_STATUS.get(o['status'], o['status'])}\n{DIV}\n"
            f"👤 {o['username'] or '-'}  |  🆔 {o['user_id']}\n"
            f"🛍 {order_name(o)}\n💵 {order_price_text(o)}\n"
            f"🕒 {datetime.fromtimestamp(o['created']).strftime('%Y-%m-%d %H:%M')}")
    kb = []
    if o["status"] == "pending":
        kb.append([InlineKeyboardButton("✅ تحویل داده شد", callback_data=f"od_{oid}"),
                   InlineKeyboardButton("❌ لغو سفارش", callback_data=f"oc_{oid}")])
    kb.append([InlineKeyboardButton("🔙 لیست سفارش‌ها", callback_data="ol_0")])
    await show(query, context, text, InlineKeyboardMarkup(kb))


async def deliver_order(query, context, oid):
    done = False
    with db() as conn:
        o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if o and o["status"] == "pending":
            conn.execute("UPDATE orders SET status='delivered', done_at=? WHERE id=?", (int(time.time()), oid))
            done = True
    back = InlineKeyboardMarkup([[InlineKeyboardButton("📦 سفارش‌های باز", callback_data="ol_0")],
                                 [InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")]])
    if not o:
        await show(query, context, "❌ سفارش پیدا نشد.", back)
    elif not done:
        await show(query, context, f"⚠️ سفارش #{oid} قبلاً بررسی شده — وضعیت: {ORDER_STATUS.get(o['status'], o['status'])}", back)
    else:
        try:
            await context.bot.send_message(
                chat_id=o["user_id"],
                text=f"📦 سفارش #{oid} تحویل داده شد!\n{DIV}\n✅ {order_name(o)}\nاز خریدت ممنون 🙏 موفق باشی!")
        except TelegramError:
            pass
        await show(query, context, f"✅ سفارش #{oid} ({order_name(o)}) تحویل‌شده ثبت شد و به بازیکن اطلاع داده شد.", back)


def do_cancel_order(oid):
    """لغو سفارش در انتظار + برگشت پول (و حذف ماشین از لیست). خروجی: (وضعیت, سفارش)"""
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not o:
            return "no", None
        if o["status"] != "pending":
            return "done", o
        if not o["real_price"]:
            if o["price"]:
                conn.execute("UPDATE players SET cash=cash+? WHERE user_id=?", (o["price"], o["user_id"]))
            if o["category"] == "car":
                p = conn.execute("SELECT cars FROM players WHERE user_id=?", (o["user_id"],)).fetchone()
                if p:
                    cars = get_cars(p)
                    for _ in range(o["qty"]):
                        if o["item_name"] in cars:
                            cars.remove(o["item_name"])
                    conn.execute("UPDATE players SET cars=? WHERE user_id=?", (json.dumps(cars), o["user_id"]))
        conn.execute("UPDATE orders SET status='cancelled', done_at=? WHERE id=?", (int(time.time()), oid))
    return "ok", o


async def cancel_order(query, context, oid):
    status, o = do_cancel_order(oid)
    back = InlineKeyboardMarkup([[InlineKeyboardButton("📦 سفارش‌های باز", callback_data="ol_0")],
                                 [InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")]])
    if status == "no":
        await show(query, context, "❌ سفارش پیدا نشد.", back)
    elif status == "done":
        await show(query, context, f"⚠️ سفارش #{oid} قبلاً بررسی شده — وضعیت: {ORDER_STATUS.get(o['status'], o['status'])}", back)
    else:
        refund = "" if o["real_price"] else (f"\n💰 {o['price']:,} $ به حسابت برگشت." if o["price"] else "")
        try:
            await context.bot.send_message(chat_id=o["user_id"],
                                           text=f"❌ سفارش #{oid} ({order_name(o)}) لغو شد.{refund}")
        except TelegramError:
            pass
        await show(query, context,
                   f"✅ سفارش #{oid} لغو شد." + ("" if o["real_price"] else f"\n💰 {o['price']:,} $ به بازیکن برگشت."),
                   back)


# ================= لیدربورد / جایزه / کار =================
async def show_leaderboard(query, context):
    with db() as conn:
        rows = conn.execute("SELECT username, cash FROM players WHERE approved=1 ORDER BY cash DESC LIMIT 10").fetchall()
    if not rows:
        text = "🏆 هنوز کسی در لیدربورد نیست."
    else:
        icons = ["🥇", "🥈", "🥉"]
        lines = ["🏆 ثروتمندترین‌های گریم ولی", DIV]
        for i, r in enumerate(rows):
            icon = icons[i] if i < 3 else f"{i + 1}."
            lines.append(f"{icon} {r['username'] or 'پلیر'} — {r['cash']:,} $")
        text = "\n".join(lines)
    await show(query, context, text, back_main())


async def give_daily(query, context):
    uid = query.from_user.id
    now = datetime.now(timezone.utc).date()
    today, yesterday = now.isoformat(), (now - timedelta(days=1)).isoformat()
    ok = False
    with db() as conn:
        player = conn.execute("SELECT * FROM players WHERE user_id=?", (uid,)).fetchone()
        if not player:
            text = "❌ اول /start بزن."
        elif player["last_daily"] == today:
            text = (f"⏳ امروز جایزه‌ات رو گرفتی.\n🔥 استریک فعلی: {player['streak']} روز\n"
                    f"فردا دوباره بیا تا استریکت نشکنه! 🌙")
        else:
            streak = player["streak"] + 1 if player["last_daily"] == yesterday else 1
            bonus = min(streak - 1, STREAK_CAP) * STREAK_STEP
            reward = random.randint(DAILY_MIN, DAILY_MAX) + bonus
            conn.execute("UPDATE players SET cash=cash+?, last_daily=?, streak=? WHERE user_id=?",
                         (reward, today, streak, uid))
            text = (f"🎁 جایزه روزانه\n{DIV}\n💵 {reward} $ به حسابت اضافه شد ✅\n"
                    f"🔥 استریک: {streak} روز پشت‌سرهم" + (f" (پاداش استریک: +{bonus}$)" if bonus else ""))
            if bonus < STREAK_CAP * STREAK_STEP:
                text += "\n⬆️ هر روز که بیای پاداشت بیشتر میشه!"
            ok = True
    await show(query, context, text, back_main())
    if ok:
        await refresh_medals(context, uid)


def do_work(uid, name):
    now = int(time.time())
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        ensure_player(conn, uid, name)
        p = conn.execute("SELECT last_work FROM players WHERE user_id=?", (uid,)).fetchone()
        left = (p["last_work"] or 0) + WORK_COOLDOWN - now
        if left > 0:
            return False, f"😮‍💨 هنوز خسته‌ای!\n{DIV}\n⏳ {fmt_duration(left)} دیگه می‌تونی دوباره کار کنی."
        amount = random.randint(WORK_MIN, WORK_MAX)
        conn.execute("UPDATE players SET cash=cash+?, last_work=? WHERE user_id=?", (amount, now, uid))
    return True, f"🛠 کار روزانه\n{DIV}\n" + random.choice(WORK_JOBS).format(a=amount) + \
        f"\n\n⏳ دفعه بعد: {fmt_duration(WORK_COOLDOWN)} دیگه"


async def show_work(query, context):
    ok, text = do_work(query.from_user.id, user_label(query.from_user))
    await show(query, context, text, back_main())
    if ok:
        await refresh_medals(context, query.from_user.id)


async def work_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ok, text = do_work(user.id, user_label(user))
    await reply_panel(update, text, back_main())
    if ok:
        await refresh_medals(context, user.id)


# ================= ترید =================
def do_trade(sender_id, target_id, amount):
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        s = conn.execute("SELECT * FROM players WHERE user_id=?", (sender_id,)).fetchone()
        t = conn.execute("SELECT * FROM players WHERE user_id=?", (target_id,)).fetchone()
        if not s:
            return False, "❌ اول /start بزن."
        if not t:
            return False, "❌ این پلیر هنوز ثبت‌نام نکرده (باید /start بزنه)."
        if not t["approved"]:
            return False, "❌ ورود این پلیر هنوز توسط ادمین تأیید نشده."
        if s["cash"] < amount:
            return False, f"❌ پولت کافی نیست!\n💰 داری: {s['cash']:,} $"
        tax = amount * TRADE_TAX_PERCENT // 100
        got = amount - tax
        conn.execute("UPDATE players SET cash=cash-? WHERE user_id=?", (amount, sender_id))
        conn.execute("UPDATE players SET cash=cash+? WHERE user_id=?", (got, target_id))
        return True, {"amount": amount, "tax": tax, "got": got}


TRADE_USAGE = (
    "🤝 نحوه استفاده (فقط انتقال پول):\n"
    "/trade @یوزرنیم 500\n"
    "یا روی پیام طرف ریپلای کن و فقط /trade 500 بزن."
)


async def trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user
    args = list(context.args or [])
    target_id, target_name, error = None, None, None

    with db() as conn:
        ensure_player(conn, user.id, user_label(user))
        if args and args[0].startswith("@"):
            row = conn.execute("SELECT user_id, username FROM players WHERE lower(username)=lower(?)",
                               (args[0][1:],)).fetchone()
            args = args[1:]
            if row:
                target_id, target_name = row["user_id"], row["username"]
            else:
                error = "❌ پلیری با این یوزرنیم پیدا نشد (باید /start زده باشه)."
        elif msg.reply_to_message and msg.reply_to_message.from_user:
            t = msg.reply_to_message.from_user
            row = conn.execute("SELECT user_id, username FROM players WHERE user_id=?", (t.id,)).fetchone()
            if row:
                target_id, target_name = row["user_id"], row["username"] or user_label(t)
            else:
                error = "❌ این پلیر هنوز ثبت‌نام نکرده (باید /start بزنه)."
        else:
            error = TRADE_USAGE

    if error:
        await msg.reply_text(error)
        return
    if target_id == user.id:
        await msg.reply_text("❌ نمی‌تونی به خودت ترید بدی!")
        return
    amount = parse_amount(args[0]) if args else None
    if amount is None or amount <= 0:
        await msg.reply_text(TRADE_USAGE)
        return
    if len(args) > 1:
        await msg.reply_text("❌ ترید آیتم وجود نداره؛ فقط انتقال پول ممکنه.\n" + TRADE_USAGE)
        return

    ok, res = do_trade(user.id, target_id, amount)
    if not ok:
        await msg.reply_text(res)
        return

    what = (f"💵 مبلغ: {res['amount']:,} $\n🧾 مالیات ({TRADE_TAX_PERCENT}٪): {res['tax']:,} $\n"
            f"✅ دریافتی: {res['got']:,} $")
    await msg.reply_text(f"🤝 معامله انجام شد!\n{DIV}\n📤 {user_label(user)}  ➜  📥 {target_name}\n{what}")
    await refresh_medals(context, target_id)


# ================= فکشن (پلیر) =================
def fb_deposit(uid, amount):
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        fac = faction_of(conn, uid)
        if not fac:
            return False, "❌ عضو هیچ فکشنی نیستی."
        cash = conn.execute("SELECT cash FROM players WHERE user_id=?", (uid,)).fetchone()["cash"]
        if cash < amount:
            return False, f"❌ پولت کافی نیست!\n💰 داری: {cash:,} $"
        conn.execute("UPDATE players SET cash=cash-? WHERE user_id=?", (amount, uid))
        conn.execute("UPDATE factions SET bank=bank+? WHERE id=?", (amount, fac["id"]))
        bank = conn.execute("SELECT bank FROM factions WHERE id=?", (fac["id"],)).fetchone()["bank"]
    return True, f"✅ {amount:,} $ به صندوق «{fac['name']}» واریز شد.\n🏦 موجودی صندوق: {bank:,} $"


def fb_withdraw(uid, amount):
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        fac = faction_of(conn, uid)
        if not fac:
            return False, "❌ عضو هیچ فکشنی نیستی."
        if fac["leader_id"] != uid:
            return False, "❌ فقط لیدر فکشن می‌تونه برداشت کنه."
        if fac["bank"] < amount:
            return False, f"❌ صندوق اینقدر پول نداره!\n🏦 موجودی صندوق: {fac['bank']:,} $"
        conn.execute("UPDATE factions SET bank=bank-? WHERE id=?", (amount, fac["id"]))
        conn.execute("UPDATE players SET cash=cash+? WHERE user_id=?", (amount, uid))
        bank = fac["bank"] - amount
    return True, f"✅ {amount:,} $ از صندوق «{fac['name']}» برداشت شد.\n🏦 موجودی صندوق: {bank:,} $"


async def show_my_faction(query, context):
    uid = query.from_user.id
    with db() as conn:
        fac = faction_of(conn, uid)
        members = []
        if fac:
            members = conn.execute(
                "SELECT m.user_id, p.username FROM faction_members m "
                "LEFT JOIN players p ON p.user_id=m.user_id WHERE m.faction_id=? ORDER BY m.joined",
                (fac["id"],)).fetchall()
    if not fac:
        await show(query, context,
                   f"🏴 فکشن\n{DIV}\nتو عضو هیچ فکشنی نیستی.\nبرای عضو شدن به ادمین سرور پیام بده.",
                   back_main())
        return
    is_leader = fac["leader_id"] == uid
    leader = next((r for r in members if r["user_id"] == fac["leader_id"]), None)
    lines = [f"🏴 فکشن {fac['name']}", DIV,
             f"👑 لیدر: {(leader['username'] or leader['user_id']) if leader else 'ندارد'}",
             f"🏦 صندوق: {fac['bank']:,} $", f"👥 اعضا ({len(members)}):"]
    for r in members[:15]:
        mark = " 👑" if r["user_id"] == fac["leader_id"] else ""
        lines.append(f"   • {r['username'] or r['user_id']}{mark}")
    if len(members) > 15:
        lines.append(f"   … و {len(members) - 15} نفر دیگر")
    rows = [[InlineKeyboardButton("💰 واریز به صندوق", callback_data="fbd")]]
    if is_leader:
        rows.append([InlineKeyboardButton("🏦 برداشت از صندوق", callback_data="fbw")])
    rows.append([InlineKeyboardButton("🚪 ترک فکشن", callback_data="fl")])
    rows.append([InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")])
    await show(query, context, "\n".join(lines), InlineKeyboardMarkup(rows))


def faction_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏴 فکشن من", callback_data="my_faction"),
                                  InlineKeyboardButton("🔙 منو", callback_data="main_menu")]])


# ================= تیکت =================
def ticket_admin_markup(tid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("💬 پاسخ", callback_data=f"tkr_{tid}"),
        InlineKeyboardButton("✅ بستن", callback_data=f"tkc_{tid}"),
    ]])


async def notify_ticket(context, user, tid, text):
    uname = f"@{escape(user.username)}" if user.username else "ندارد"
    msg = (
        f"🎫 <b>تیکت جدید #{tid}</b>\n{DIV}\n"
        f"👤 <a href=\"tg://user?id={user.id}\">{escape(user.full_name)}</a>\n"
        f"🔗 {uname}  |  🆔 <code>{user.id}</code>\n{DIV}\n{escape(text)}"
    )
    for admin in (ADMIN_ID, ADMIN_ID_2):
        try:
            await context.bot.send_message(chat_id=admin, text=msg, parse_mode=ParseMode.HTML,
                                           reply_markup=ticket_admin_markup(tid))
        except Exception as e:
            logger.error("Failed to notify ticket to %s: %s", admin, e)


def ticket_status_label(status):
    return {"open": "🟢 باز", "answered": "💬 پاسخ داده شد", "closed": "⚫ بسته"}.get(status, status)


async def show_tickets_menu(query, context):
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 تیکت جدید", callback_data="tk_new"),
         InlineKeyboardButton("📂 تیکت‌های من", callback_data="tk_mine")],
        [InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")],
    ])
    await show(query, context,
               f"🎫 پشتیبانی\n{DIV}\nمشکل یا سوالی داری؟ یه تیکت بفرست تا ادمین‌ها جواب بدن.", markup)


async def start_ticket(query, context):
    uid = query.from_user.id
    with db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM tickets WHERE user_id=? AND status='open'", (uid,)).fetchone()[0]
    if n >= TICKET_MAX_OPEN:
        await show(query, context,
                   f"⏳ {n} تیکت بدون پاسخ داری.\nصبر کن ادمین‌ها جواب بدن بعد تیکت جدید بفرست.",
                   InlineKeyboardMarkup([[InlineKeyboardButton("📂 تیکت‌های من", callback_data="tk_mine")],
                                         [InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")]]))
        return
    context.user_data["pstep"] = "ticket"
    await show(query, context,
               f"📝 تیکت جدید\n{DIV}\nمشکل یا سوالت رو توی یه پیام بنویس و بفرست (حداکثر ۸۰۰ حرف):",
               cancel_player_markup())


async def show_my_tickets(query, context):
    with db() as conn:
        rows = conn.execute("SELECT * FROM tickets WHERE user_id=? ORDER BY id DESC LIMIT 5",
                            (query.from_user.id,)).fetchall()
    if not rows:
        text = f"📂 تیکت‌های من\n{DIV}\nهنوز تیکتی نفرستادی."
    else:
        lines = ["📂 تیکت‌های من (۵ تای آخر)", DIV]
        for t in rows:
            lines.append(f"#{t['id']} {ticket_status_label(t['status'])}\n   📝 {t['text'][:80]}")
            if t["reply"]:
                lines.append(f"   ↳ 💬 {t['reply'][:120]}")
        text = "\n".join(lines)
    await show(query, context, text, InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 تیکت جدید", callback_data="tk_new")],
        [InlineKeyboardButton("🔙 پشتیبانی", callback_data="tickets")]]))


async def show_ticket_list(query, context, page=0):
    with db() as conn:
        rows = conn.execute("SELECT * FROM tickets WHERE status='open' ORDER BY id").fetchall()
    if not rows:
        await show(query, context, f"🎫 تیکت‌های باز\n{DIV}\n✅ تیکت بازی نیست.", admin_menu())
        return
    chunk, page, total = paginate(rows, page)
    kb = [[InlineKeyboardButton(f"#{t['id']} • {t['username'] or t['user_id']} • {t['text'][:20]}"[:48],
                                callback_data=f"tkv_{t['id']}")] for t in chunk]
    nav = page_nav("tkl_", page, total)
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")])
    title = f"🎫 تیکت‌های باز ({len(rows)})"
    if total > 1:
        title += f" — صفحه {page + 1} از {total}"
    await show(query, context, f"{title}\n{DIV}", InlineKeyboardMarkup(kb))


async def show_ticket_detail(query, context, tid):
    with db() as conn:
        t = conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
    if not t:
        await show(query, context, "❌ تیکت پیدا نشد.", admin_menu())
        return
    text = (f"🎫 تیکت #{t['id']} — {ticket_status_label(t['status'])}\n{DIV}\n"
            f"👤 {t['username'] or '-'}  |  🆔 {t['user_id']}\n"
            f"🕒 {datetime.fromtimestamp(t['created']).strftime('%Y-%m-%d %H:%M')}\n{DIV}\n{t['text']}")
    if t["reply"]:
        text += f"\n{DIV}\n💬 پاسخ شما:\n{t['reply']}"
    kb = []
    if t["status"] != "closed":
        kb.append([InlineKeyboardButton("💬 پاسخ", callback_data=f"tkr_{tid}"),
                   InlineKeyboardButton("✅ بستن", callback_data=f"tkc_{tid}")])
    kb.append([InlineKeyboardButton("🔙 لیست تیکت‌ها", callback_data="tkl_0")])
    await show(query, context, text, InlineKeyboardMarkup(kb))


async def close_ticket(query, context, tid):
    with db() as conn:
        t = conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
        if t and t["status"] != "closed":
            conn.execute("UPDATE tickets SET status='closed' WHERE id=?", (tid,))
    if not t:
        await show(query, context, "❌ تیکت پیدا نشد.", admin_menu())
        return
    if t["status"] != "closed":
        try:
            await context.bot.send_message(chat_id=t["user_id"], text=f"✅ تیکت #{tid} شما بسته شد. ممنون 🙏")
        except TelegramError:
            pass
    await show(query, context, f"✅ تیکت #{tid} بسته شد.",
               InlineKeyboardMarkup([[InlineKeyboardButton("🎫 تیکت‌های باز", callback_data="tkl_0")],
                                     [InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")]]))


# ================= جریان‌های متنیِ پلیر (تیکت / صندوق فکشن) =================
async def handle_player_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data
    step = ud.get("pstep")
    user = update.effective_user
    text = update.message.text.strip()

    if step == "ticket":
        if not text or len(text) > 800:
            await reply_panel(update, "❌ متن تیکت باید بین ۱ تا ۸۰۰ حرف باشه.", cancel_player_markup())
            return
        with db() as conn:
            n = conn.execute("SELECT COUNT(*) FROM tickets WHERE user_id=? AND status='open'",
                             (user.id,)).fetchone()[0]
            if n >= TICKET_MAX_OPEN:
                ud.clear()
                await reply_panel(update, "⏳ تیکت‌های بدون پاسخت زیاده. صبر کن جواب بدن.", back_main())
                return
            cur = conn.execute("INSERT INTO tickets (user_id, username, text, created) VALUES (?,?,?,?)",
                               (user.id, user_label(user), text, int(time.time())))
            tid = cur.lastrowid
        ud.clear()
        await notify_ticket(context, user, tid, text)
        await reply_panel(update, f"✅ تیکت #{tid} ثبت شد!\n📩 به‌زودی ادمین‌ها جواب میدن.", back_main())
        return

    if step == "buy_qty":
        qty = parse_amount(text)
        if qty is None or qty < 1 or qty > BUY_QTY_MAX:
            await reply_panel(update, f"❌ یه عدد بین ۱ تا {BUY_QTY_MAX} بنویس.", cancel_player_markup())
            return
        item_id = ud.get("buy_item")
        ud.clear()
        out, markup, ok = await purchase(context, user, item_id, qty)
        await reply_panel(update, out, markup)
        if ok:
            await refresh_medals(context, user.id)
        return

    if step == "sell_qty":
        qty = parse_amount(text)
        if qty is None or qty <= 0 or qty > 1000:
            await reply_panel(update, "❌ یه عدد بین ۱ تا ۱۰۰۰ بنویس.", cancel_player_markup())
            return
        item_id = ud.get("sell_item")
        ud.clear()
        status, data = create_sell_order(user.id, user_label(user), item_id, qty)
        back = InlineKeyboardMarkup([[InlineKeyboardButton("💎 فروش به سرور", callback_data="sell"),
                                      InlineKeyboardButton("🔙 منو", callback_data="main_menu")]])
        if status == "no_item":
            await reply_panel(update, "❌ این آیتم دیگه خریده نمیشه.", back)
        elif status == "too_many":
            await reply_panel(update, f"⏳ {data} درخواست فروش در انتظار داری (حداکثر {SELL_PENDING_MAX}).\n"
                                      f"صبر کن ادمین تأییدشون کنه، بعد درخواست جدید بده.", back)
        elif status == "cap":
            msg = (f"❌ این درخواست از سقف دریافت روزانه ({SELL_DAILY_CAP}$) بیشتره.\n{DIV}\n"
                   f"💵 هنوز می‌تونی تا {data['remaining']:,}$ بفروشی")
            if data["max_qty"]:
                msg += f" (حداکثر {data['max_qty']} تا «{data['item']['name']}»)"
            if data["wait"]:
                msg += f"\n⏳ بعد از {fmt_duration(data['wait'])} سقفت دوباره باز میشه."
            elif data["remaining"] == 0:
                msg += "\n⏳ منتظر تأیید درخواست‌های قبلیت باش."
            await reply_panel(update, msg, back)
        else:
            await notify_sell(context, user, data["id"], data["item"], qty, data["total"])
            await reply_panel(
                update,
                f"✅ درخواست فروش #{data['id']} ثبت شد!\n{DIV}\n"
                f"💎 {data['item']['name']} ×{qty}\n💰 {data['total']:,}$\n\n"
                f"📦 آیتم‌ها رو توی بازی به ادمین تحویل بده. بعد از تأیید ادمین پول به حسابت میاد.\n"
                f"(اگه اشتباه ثبت کردی به ادمین بگو رد کنه.)", back)
        return

    if step in ("fb_dep", "fb_wd"):
        amount = parse_amount(text)
        if amount is None or amount <= 0:
            await reply_panel(update, "❌ یه عدد مثبت بنویس. مثال: 500", cancel_player_markup())
            return
        ud.clear()
        ok, res = fb_deposit(user.id, amount) if step == "fb_dep" else fb_withdraw(user.id, amount)
        await reply_panel(update, res, faction_back())
        if ok:
            await refresh_medals(context, user.id)


# ================= افزودن / ویرایش آیتم (ادمین) =================
ADD_NAME_PROMPT = "✏️ مرحله ۱ از ۵:\nاسم آیتم رو بنویس:\nمثال: Chevy Step Van"


async def additem_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ فقط ادمین.")
        return
    context.user_data.clear()
    context.user_data["flow_chat"] = update.message.chat_id
    context.user_data["step"] = "name"
    await reply_panel(update, ADD_NAME_PROMPT, cancel_markup())


async def finish_add(context, chat_id, owner_id, photo_id):
    ud = context.user_data
    name, buy, category = ud.get("item_name"), ud.get("item_buy"), ud.get("item_cat")
    real_price = ud.get("item_real_price") if ud.get("item_real") else None
    if not (name and category) or buy is None or (ud.get("item_real") and not real_price):
        ud.clear()
        await send_panel(context, chat_id, owner_id, "❌ اطلاعات ناقص بود. دوباره شروع کن.", admin_menu())
        return
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO items (name, category, buy_price, sell_price, photo_id, is_real, real_price)"
            " VALUES (?,?,?,0,?,?,?)",
            (name, category, buy, photo_id, 1 if real_price else 0, real_price),
        )
        new_id = cur.lastrowid
    ud.clear()
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 لیست آیتم‌ها", callback_data="admin_list")],
        [InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")],
    ])
    price_txt = f"💶 {real_price} (پولی)" if real_price else f"{buy:,} $"
    text = (f"✅ آیتم ثبت شد!\n{DIV}\n🆔 کد: {new_id}\n📛 اسم: {name}\n"
            f"🏷️ دسته: {cat_map().get(category, category)}\n💵 قیمت: {price_txt}\n"
            f"🖼 عکس: {'دارد' if photo_id else 'ندارد'}")
    await send_panel(context, chat_id, owner_id, text, markup, photo=photo_id)


def item_back_markup(item_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 صفحه آیتم", callback_data="item_" + str(item_id)),
         InlineKeyboardButton("📋 لیست", callback_data="admin_list")],
    ])


async def handle_admin_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data
    text = update.message.text.strip()
    step, edit_step = ud.get("step"), ud.get("edit_step")
    try:
        if step == "name":
            if not text or len(text) > 60:
                await reply_panel(update, "❌ یه اسم معتبر (حداکثر ۶۰ حرف) بنویس.", cancel_markup())
                return
            ud["item_name"] = text
            ud["step"] = "type"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💰 با پول بازی", callback_data="addtype_game")],
                [InlineKeyboardButton("💶 پولی (پول واقعی)", callback_data="addtype_real")],
                [InlineKeyboardButton("❌ لغو", callback_data="cancel_flow")],
            ])
            await reply_panel(update, "✏️ مرحله ۲ از ۵:\nاین آیتم با چی خریده میشه؟", kb)
        elif step == "price":
            if ud.get("item_real"):
                price_text = normalize_digits(text).strip()
                if not price_text or len(price_text) > 30:
                    await reply_panel(update, "❌ قیمت رو با واحد بنویس (حداکثر ۳۰ حرف). مثال: 20 یورو",
                                      cancel_markup())
                    return
                ud["item_real_price"] = price_text
                ud["item_buy"] = 0
            else:
                value = parse_amount(text)
                if value is None or value <= 0:
                    await reply_panel(update, "❌ یه عدد مثبت بنویس. مثال: 1500", cancel_markup())
                    return
                ud["item_buy"] = value
            ud["step"] = "category"
            await reply_panel(update, "✏️ مرحله ۴ از ۵:\nدسته‌بندی رو انتخاب کن:", category_markup())
        elif step in ("type", "category", "photo"):
            await reply_panel(update, "👆 از دکمه‌های همین مرحله استفاده کن"
                              + (" یا عکس بفرست." if step == "photo" else "."), cancel_markup())
        elif step == "pamt":
            ch = parse_change(text)
            with db() as conn:
                it = conn.execute("SELECT * FROM items WHERE id=?", (ud.get("p_item"),)).fetchone()
            if not it or it["is_real"]:
                ud.clear()
                await reply_panel(update, "❌ آیتم پیدا نشد.", admin_menu())
                return
            new = apply_change(it["buy_price"], ud.get("p_dir"), *ch) if ch else None
            if new is None:
                await reply_panel(update, "❌ مقدار نامعتبر.\n" + CHANGE_HINT, cancel_markup())
                return
            ud["p_new"] = new
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("۶ ساعت", callback_data="ph_6"), InlineKeyboardButton("۱۲ ساعت", callback_data="ph_12")],
                [InlineKeyboardButton("۱ روز", callback_data="ph_24"), InlineKeyboardButton("۲ روز", callback_data="ph_48"),
                 InlineKeyboardButton("۳ روز", callback_data="ph_72")],
                [InlineKeyboardButton("❌ لغو", callback_data="cancel_flow")]])
            await reply_panel(update, f"{dir_arrow(new, it['buy_price'])} «{it['name']}»: {it['buy_price']:,}$ ➜ {new:,}$\n"
                                      f"این قیمت برای چند ساعت باشه؟\n(بعدش خودکار به {it['buy_price']:,}$ برمی‌گرده)", kb)
        elif step == "mkamt":
            key = ud.get("mk_key")
            ch = parse_change(text)
            with db() as conn:
                cur = conn.execute("SELECT price FROM market WHERE key=?", (key,)).fetchone()
            new = apply_change(cur["price"], ud.get("mk_dir"), *ch) if cur and ch else None
            if new is None:
                await reply_panel(update, "❌ مقدار نامعتبر.\n" + CHANGE_HINT, cancel_markup())
                return
            ud.clear()
            with db() as conn:
                old = set_market_price(conn, key, new, int(time.time()))
            err = await announce_market(context, f"{MARKET_NAMES[key]}: {dir_arrow(new, old)} {old}$ ➜ {new}$",
                                        "📢 تغییر قیمت خرید سرور")
            await reply_panel(
                update,
                f"✅ قیمت {MARKET_NAMES[key]} از {old}$ به {new}$ تغییر کرد.\n"
                f"(تا تغییر بعدی همین‌طور می‌مونه.)\n" +
                ("📣 توی گپ اعلام شد." if not err else f"⚠️ اعلام توی گپ ناموفق بود: {err}"),
                InlineKeyboardMarkup([[InlineKeyboardButton("📈 بازار", callback_data="mk")]]))
        elif step == "banreason":
            uid_t = ud.get("ban_uid")
            reason = "" if text == "-" else text[:200]
            ud.clear()
            if not do_ban(uid_t, reason):
                await reply_panel(update, "❌ نمی‌شه ادمین رو مسدود کرد.", user_back(uid_t))
            else:
                await notify_user(context, uid_t, f"🚫 دسترسی تو به بات مسدود شد.\nدلیل: {reason or 'نامشخص'}")
                await reply_panel(update, f"🚫 کاربر {uid_t} مسدود شد." + (f"\nدلیل: {reason}" if reason else ""),
                                  user_back(uid_t))
        elif step == "usearch":
            q_ = text.lstrip("@").strip()
            with db() as conn:
                if q_.isdigit():
                    row = conn.execute("SELECT user_id, username FROM players WHERE user_id=?", (int(q_),)).fetchone()
                    found = (int(q_), row["username"] if row else None)
                else:
                    row = conn.execute("SELECT user_id, username FROM players WHERE lower(username)=lower(?)",
                                       (q_,)).fetchone()
                    found = (row["user_id"], row["username"]) if row else None
            if not found:
                await reply_panel(update, "❌ کاربری پیدا نشد. یوزرنیم یا آیدی عددی بنویس:", cancel_markup())
                return
            ud.clear()
            await reply_panel(update, f"✅ پیدا شد: {found[1] or found[0]}",
                              InlineKeyboardMarkup([[InlineKeyboardButton("👤 باز کردن پروفایل", callback_data=f"uv_{found[0]}")]]))
        elif step == "bname":
            if not text or len(text) > 40:
                await reply_panel(update, "❌ اسم باید بین ۱ تا ۴۰ حرف باشه.", cancel_markup())
                return
            ud["b_name"] = text
            ud["step"] = "bprice"
            await reply_panel(update, "💎 قیمت خرید (دلار) رو فقط عدد بنویس:", cancel_markup())
        elif step == "bprice":
            value = parse_amount(text)
            if value is None or value <= 0:
                await reply_panel(update, "❌ یه عدد مثبت بنویس.", cancel_markup())
                return
            name = ud.get("b_name")
            with db() as conn:
                conn.execute("INSERT INTO buy_items (name, price) VALUES (?,?)", (name, value))
            ud.clear()
            await reply_panel(update, f"✅ «{name}» با قیمت خرید {value:,}$ اضافه شد.",
                              InlineKeyboardMarkup([[InlineKeyboardButton("💎 قیمت‌های خرید", callback_data="bm")]]))
        elif edit_step == "bedit_price":
            value = parse_amount(text)
            if value is None or value <= 0:
                await reply_panel(update, "❌ یه عدد مثبت بنویس.", cancel_markup())
                return
            with db() as conn:
                conn.execute("UPDATE buy_items SET prev_price=price, price=? WHERE id=?", (value, ud.get("edit_id")))
            iid = ud.get("edit_id")
            ud.clear()
            await reply_panel(update, f"✅ قیمت خرید به {value:,}$ تغییر کرد.", InlineKeyboardMarkup(
                [[InlineKeyboardButton("📄 صفحه آیتم", callback_data=f"bi_{iid}"),
                  InlineKeyboardButton("💎 لیست", callback_data="bm")]]))
        elif edit_step == "bedit_name":
            if not text or len(text) > 40:
                await reply_panel(update, "❌ اسم باید بین ۱ تا ۴۰ حرف باشه.", cancel_markup())
                return
            with db() as conn:
                conn.execute("UPDATE buy_items SET name=? WHERE id=?", (text, ud.get("edit_id")))
            iid = ud.get("edit_id")
            ud.clear()
            await reply_panel(update, f"✅ اسم به «{text}» تغییر کرد.", InlineKeyboardMarkup(
                [[InlineKeyboardButton("📄 صفحه آیتم", callback_data=f"bi_{iid}"),
                  InlineKeyboardButton("💎 لیست", callback_data="bm")]]))
        elif step == "newcat":
            if not text or len(text) > 30:
                await reply_panel(update, "❌ اسم دسته باید بین ۱ تا ۳۰ حرف باشه.", cancel_markup())
                return
            if text.lower() in (v.lower() for v in cat_map().values()):
                await reply_panel(update, "❌ دسته‌ای با این اسم هست.", cancel_markup())
                return
            with db() as conn:
                n = conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM categories").fetchone()[0]
                conn.execute("INSERT INTO categories (id, key, label) VALUES (?,?,?)", (n, f"c{n}", text))
            ud.clear()
            await reply_panel(update, f"✅ دسته «{text}» ساخته شد.\nحالا موقع افزودن آیتم می‌تونی انتخابش کنی.",
                              InlineKeyboardMarkup([[InlineKeyboardButton("🗂 مدیریت دسته‌ها", callback_data="cm")],
                                                    [InlineKeyboardButton("➕ افزودن آیتم", callback_data="admin_add")]]))
        elif step == "newfac":
            ok, res = create_faction(text)
            if ok:
                ud.clear()
            await reply_panel(update, res, InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏴 فکشن‌ها", callback_data="fcl_0")]]) if ok else cancel_markup())
        elif step == "tk_reply":
            tid = ud.get("tk_id")
            with db() as conn:
                t = conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
            if not t:
                ud.clear()
                await reply_panel(update, "❌ تیکت پیدا نشد.", admin_menu())
                return
            try:
                await context.bot.send_message(
                    chat_id=t["user_id"],
                    text=f"🎫 پاسخ تیکت #{tid}\n{DIV}\n{text}\n{DIV}\nسوال دیگه‌ای داشتی تیکت جدید بفرست 🙌")
            except TelegramError:
                await reply_panel(update, "⚠️ نتونستم پیام رو بفرستم (کاربر بات رو بلاک کرده).", cancel_markup())
                return
            with db() as conn:
                conn.execute("UPDATE tickets SET status='answered', reply=? WHERE id=? AND status!='closed'",
                             (text[:1000], tid))
            ud.clear()
            await reply_panel(update, f"✅ پاسخ تیکت #{tid} ارسال شد.", InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ بستن تیکت", callback_data=f"tkc_{tid}"),
                 InlineKeyboardButton("🎫 لیست", callback_data="tkl_0")]]))
        elif edit_step == "edit_price":
            item_id = ud.get("edit_id")
            with db() as conn:
                it = conn.execute("SELECT is_real FROM items WHERE id=?", (item_id,)).fetchone()
            if it and it["is_real"]:
                price_text = normalize_digits(text).strip()
                if not price_text or len(price_text) > 30:
                    await reply_panel(update, "❌ قیمت رو با واحد بنویس. مثال: 20 یورو", cancel_markup())
                    return
                with db() as conn:
                    conn.execute("UPDATE items SET real_price=? WHERE id=?", (price_text, item_id))
                ud.clear()
                await reply_panel(update, f"✅ قیمت به {price_text} تغییر کرد.", item_back_markup(item_id))
            else:
                value = parse_amount(text)
                if value is None or value <= 0:
                    await reply_panel(update, "❌ یه عدد معتبر بنویس.", cancel_markup())
                    return
                with db() as conn:
                    conn.execute("UPDATE items SET buy_price=? WHERE id=?", (value, item_id))
                ud.clear()
                await reply_panel(update, f"✅ قیمت به {value:,} $ تغییر کرد.", item_back_markup(item_id))
        elif edit_step == "edit_name":
            if not text or len(text) > 60:
                await reply_panel(update, "❌ اسم معتبر بنویس (حداکثر ۶۰ حرف).", cancel_markup())
                return
            item_id = ud.get("edit_id")
            with db() as conn:
                conn.execute("UPDATE items SET name=? WHERE id=?", (text, item_id))
            ud.clear()
            await reply_panel(update, f"✅ اسم به «{text}» تغییر کرد.", item_back_markup(item_id))
        elif edit_step == "edit_photo":
            await reply_panel(update, "🖼 یه عکس بفرست (نه متن).", cancel_markup())
    except Exception as e:
        logger.error("Error in admin flow: %s", e, exc_info=True)
        await update.message.reply_text("⚠️ خطایی پیش اومد. دوباره تلاش کن.")


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id) or not update.message or not update.message.photo:
        return
    ud = context.user_data
    if ud.get("flow_chat") not in (None, update.message.chat_id):
        return
    file_id = update.message.photo[-1].file_id
    if ud.get("step") == "photo":
        await finish_add(context, update.message.chat_id, user.id, file_id)
    elif ud.get("edit_step") == "edit_photo":
        item_id = ud.get("edit_id")
        with db() as conn:
            conn.execute("UPDATE items SET photo_id=? WHERE id=?", (file_id, item_id))
        ud.clear()
        await reply_panel(update, "✅ عکس آیتم تغییر کرد.", item_back_markup(item_id))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تنها هندلر متن: جریان‌های پلیر، جریان‌های ادمین، بعد دستورات ریپلای ادمین."""
    if not update.message or not update.message.text:
        return
    ud = context.user_data
    uid = update.effective_user.id
    # عملیات نیمه‌تموم فقط توی همون چتی که شروع شده ادامه پیدا می‌کنه (نه توی گروه یا چت دیگه)
    flow_here = ud.get("flow_chat") in (None, update.message.chat_id)
    if ud.get("pstep") and flow_here:
        await handle_player_flow(update, context)
        return
    if not is_admin(uid):
        return
    if (ud.get("step") or ud.get("edit_step")) and flow_here:
        await handle_admin_flow(update, context)
    elif update.message.reply_to_message:
        await admin_reply(update, context)


# ================= پنل ادمین: لیست / جزئیات / حذف =================
def build_item_list(items, page=0):
    chunk, page, total = paginate(items, page)
    keyboard, row = [], []
    for it in chunk:
        label = f"{it['name']} | {price_label(it)}"
        row.append(InlineKeyboardButton(label[:45], callback_data="item_" + str(it["id"])))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    nav = page_nav("alp_", page, total)
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("➕ افزودن آیتم", callback_data="admin_add")])
    keyboard.append([InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")])
    title = "📋 لیست آیتم‌ها — برای ویرایش روی یک آیتم بزن:"
    if total > 1:
        title += f"\n(صفحه {page + 1} از {total})"
    return title, InlineKeyboardMarkup(keyboard)


async def cmd_list_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    with db() as conn:
        items = conn.execute("SELECT * FROM items ORDER BY id").fetchall()
    if not items:
        await reply_panel(update, "📋 فروشگاه خالی است.", admin_menu())
        return
    text, markup = build_item_list(items)
    await reply_panel(update, text, markup)


async def show_admin_list(query, context, page=0):
    with db() as conn:
        items = conn.execute("SELECT * FROM items ORDER BY id").fetchall()
    if not items:
        await show(query, context, "📋 فروشگاه خالی است.\nبرای افزودن دکمه زیر رو بزن.", admin_menu())
        return
    text, markup = build_item_list(items, page)
    await show(query, context, text, markup)


async def show_item_detail(query, context, item_id):
    with db() as conn:
        it = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not it:
        await show(query, context, "❌ آیتم پیدا نشد.", admin_menu())
        return
    lines = [
        f"🆔 کد: {it['id']}",
        f"📛 اسم: {it['name']}",
        f"🏷️ دسته: {cat_map().get(it['category'], it['category'])}",
        f"📌 نوع: {'پولی (پول واقعی)' if it['is_real'] else 'بازی'}",
        f"💵 قیمت: {price_label(it)}",
        f"🖼 عکس: {'دارد' if it['photo_id'] else 'ندارد'}",
    ]
    i = str(it["id"])
    rows = [
        [InlineKeyboardButton("✏️ قیمت", callback_data="eb_" + i),
         InlineKeyboardButton("✏️ اسم", callback_data="en_" + i)],
        [InlineKeyboardButton("🖼 تغییر عکس", callback_data="ep_" + i)],
    ]
    if not it["is_real"]:
        if on_sale(it):
            p, o = eff_price(it), it["buy_price"]
            lines.append(f"⏱ قیمت موقت: {dir_arrow(p, o)} {p:,}$ (قبلاً {o:,}$) — "
                         f"{fmt_duration(it['sale_until'] - time.time())} مونده؛ بعدش خودکار برمی‌گرده")
        rows.append([InlineKeyboardButton("📉 قیمت کمتر (موقت)", callback_data="pd_" + i),
                     InlineKeyboardButton("📈 قیمت بیشتر (موقت)", callback_data="pu_" + i)])
        if on_sale(it):
            rows.append([InlineKeyboardButton("🛑 برگردوندن قیمت اصلی", callback_data="fe_" + i)])
    if not it["is_real"]:
        rows.append([InlineKeyboardButton(
            f"📣 اعلام خرید توی گپ: {'✅ روشن' if it['announce'] else '⬜ خاموش'}", callback_data="an_" + i)])
    rows.append([InlineKeyboardButton("🗑️ حذف آیتم", callback_data="del_" + i)])
    rows.append([InlineKeyboardButton("📋 لیست", callback_data=f"alp_{page_of_item(it, False)}"),
                 InlineKeyboardButton("🔙 پنل", callback_data="admin_panel")])
    await show(query, context, "\n".join(lines), InlineKeyboardMarkup(rows), photo=it["photo_id"])


async def confirm_delete(query, context, item_id):
    with db() as conn:
        it = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not it:
        await show(query, context, "❌ آیتم پیدا نشد.", admin_menu())
        return
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ بله، حذف کن", callback_data="delyes_" + str(item_id)),
         InlineKeyboardButton("❌ نه", callback_data="item_" + str(item_id))],
    ])
    await show(query, context, f"⚠️ مطمئنی «{it['name']}» حذف بشه؟\nاین کار برگشت‌پذیر نیست.", markup)


async def do_delete(query, context, item_id):
    with db() as conn:
        conn.execute("DELETE FROM items WHERE id=?", (item_id,))
    await show(query, context, "🗑️ آیتم حذف شد.", admin_menu())


# ================= حراجی ناگهانی (Flash Sale) =================
def find_items(conn, arg):
    arg = (arg or "").strip()
    if not arg:
        return []
    if arg.isdigit():
        r = conn.execute("SELECT * FROM items WHERE id=?", (int(arg),)).fetchone()
        return [r] if r else []
    rows = conn.execute("SELECT * FROM items WHERE lower(name)=lower(?)", (arg,)).fetchall()
    if not rows:
        rows = conn.execute("SELECT * FROM items WHERE lower(name) LIKE ?", (f"%{arg.lower()}%",)).fetchall()
    return rows


def set_temp_price(item_id, new_price, hours):
    """قیمت موقت: بعد از hours ساعت خودکار به قیمت اصلی برمی‌گرده. خروجی: (وضعیت, آیتم)"""
    with db() as conn:
        it = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not it:
            return "no", None
        if it["is_real"]:
            return "real", it
        conn.execute("UPDATE items SET sale_until=?, sale_price=? WHERE id=?",
                     (int(time.time()) + int(hours) * 3600, int(new_price), item_id))
        it = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    return "ok", it


def set_flash(item_id):
    """میان‌بر /flash: ۵۰٪ تخفیف برای ۲۴ ساعت."""
    with db() as conn:
        it = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not it:
        return "no", None
    if it["is_real"]:
        return "real", it
    if on_sale(it):
        return "already", it
    return set_temp_price(item_id, max(1, it["buy_price"] * (100 - FLASH_DISCOUNT) // 100), FLASH_HOURS)


def end_flash(item_id):
    with db() as conn:
        conn.execute("UPDATE items SET sale_until=0, sale_price=NULL WHERE id=?", (item_id,))


def parse_change(text):
    """'20' یا '20%' => ('pct', 20) | '50$' یا '50 دلار' => ('usd', 50)"""
    t = normalize_digits(text).replace("٪", "%").replace(" ", "")
    unit = "pct"
    if t.endswith("%"):
        t = t[:-1]
    elif t.endswith("$"):
        unit, t = "usd", t[:-1]
    elif t.endswith("دلار"):
        unit, t = "usd", t[:-4]
    if not t.isdigit() or int(t) <= 0:
        return None
    return unit, int(t)


def apply_change(price, direction, unit, value):
    """قیمت جدید رو برمی‌گردونه (یا None اگه مقدار نامعتبره)."""
    if unit == "pct":
        if (direction == "down" and value > 90) or (direction == "up" and value > 300):
            return None
        new = (price * ((100 - value) if direction == "down" else (100 + value)) + 50) // 100
    else:
        new = price - value if direction == "down" else price + value
    return new if new >= 1 and new != price else None


CHANGE_HINT = ("مثال: 20 یا 20% (درصد) — یا 50$ (مبلغ دلاری)\n"
               "حداکثر ۹۰٪ کاهش و ۳۰۰٪ افزایش؛ قیمت باید بیشتر از صفر بمونه.")


async def announce_flash(context, it):
    """پیام Broadcast توی گپ برای کم یا زیاد شدن قیمت. اگه خطا بود متنش رو برمی‌گردونه."""
    old, new = it["buy_price"], eff_price(it)
    hours = max(1, round((it["sale_until"] - time.time()) / 3600))
    pct = abs(new - old) * 100 // max(old, 1)
    if new < old:
        text = (f"🔥⚡ حراجی! ⚡🔥\n{DIV}\n"
                f"🔥 قیمت {it['name']} برای {hours} ساعت آینده پایین اومد!\n\n"
                f"{dir_arrow(new, old)} {old:,}$ ➜ {new:,}$ ({pct}٪ کمتر)\n"
                f"⏳ فقط {hours} ساعت فرصت داری!\n\n🛒 برای خرید به فروشگاه ربات برو.")
    else:
        text = (f"📢 تغییر قیمت\n{DIV}\n"
                f"📢 قیمت {it['name']} برای {hours} ساعت آینده بالا رفت!\n\n"
                f"{dir_arrow(new, old)} {old:,}$ ➜ {new:,}$ ({pct}٪ بیشتر)\n"
                f"⏳ بعد از {hours} ساعت دوباره به قیمت قبلی برمی‌گرده.")
    try:
        if it["photo_id"]:
            await context.bot.send_photo(chat_id=ANNOUNCE_CHAT_ID, photo=it["photo_id"], caption=text[:1024])
        else:
            await context.bot.send_message(chat_id=ANNOUNCE_CHAT_ID, text=text)
        return None
    except Exception as e:
        logger.error("Price announce failed: %s", e)
        return str(e)


async def flash_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    arg = " ".join(context.args or [])
    with db() as conn:
        rows = find_items(conn, arg)
    if not arg:
        await update.message.reply_text("⚡ استفاده: /flash آیدی_آیتم یا اسم آیتم\nمثال: /flash 12 یا /flash BMW M5")
        return
    if not rows:
        await update.message.reply_text("❌ آیتمی پیدا نشد.")
        return
    if len(rows) > 1:
        lst = "\n".join(f"• {r['id']} — {r['name']}" for r in rows[:10])
        await update.message.reply_text(f"🔎 چند آیتم پیدا شد؛ با آیدی دقیق بزن:\n{lst}")
        return
    status, it = set_flash(rows[0]["id"])
    if status == "real":
        await update.message.reply_text("❌ حراجی فقط برای آیتم‌های بازی هست، نه پولی.")
    elif status == "already":
        await update.message.reply_text(
            f"⚠️ این آیتم الان توی حراجیه ({fmt_duration(it['sale_until'] - time.time())} مونده).\n"
            f"برای پایان: /endflash {it['id']}")
    elif status == "ok":
        err = await announce_flash(context, it)
        await notify_wishers(context, it)
        await update.message.reply_text(
            f"✅ حراجی «{it['name']}» شروع شد.\n" +
            ("📣 پیام به گپ ارسال شد." if not err else f"⚠️ ارسال به گپ ناموفق بود:\n{err}\n(بات باید توی گپ عضو باشه)"))
    else:
        await update.message.reply_text("❌ آیتم پیدا نشد.")


async def endflash_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    arg = " ".join(context.args or [])
    with db() as conn:
        rows = find_items(conn, arg)
    if len(rows) != 1:
        await update.message.reply_text("❌ استفاده: /endflash آیدی_آیتم")
        return
    end_flash(rows[0]["id"])
    await update.message.reply_text(f"🛑 حراجی «{rows[0]['name']}» تموم شد.")


# ================= دسته‌ها / فکشن‌ها (پنل ادمین) =================
async def show_cat_manage(query, context):
    cmap = cat_map()
    with db() as conn:
        counts = {r["category"]: r["n"] for r in
                  conn.execute("SELECT category, COUNT(*) AS n FROM items GROUP BY category")}
    lines = ["🗂 مدیریت دسته‌ها", DIV]
    kb = [[InlineKeyboardButton("➕ افزودن دسته جدید", callback_data="cmadd")]]
    for key, label in cmap.items():
        custom = key not in CATEGORIES
        lines.append(f"{label} — {counts.get(key, 0)} آیتم" + (" (سفارشی)" if custom else ""))
        if custom:
            kb.append([InlineKeyboardButton(f"🗑 حذف «{label}»", callback_data="cmdel_" + key)])
    kb.append([InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")])
    await show(query, context, "\n".join(lines), InlineKeyboardMarkup(kb))


async def delete_category(query, context, key):
    if key in CATEGORIES:
        await show_cat_manage(query, context)
        return
    with db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM items WHERE category=?", (key,)).fetchone()[0]
        if n == 0:
            conn.execute("DELETE FROM categories WHERE key=?", (key,))
    if n:
        await show(query, context, f"❌ این دسته {n} آیتم داره. اول آیتم‌هاش رو حذف کن.",
                   InlineKeyboardMarkup([[InlineKeyboardButton("🗂 مدیریت دسته‌ها", callback_data="cm")]]))
    else:
        await show_cat_manage(query, context)


async def show_faction_list(query, context, page=0):
    with db() as conn:
        rows = conn.execute(
            "SELECT f.*, (SELECT COUNT(*) FROM faction_members m WHERE m.faction_id=f.id) AS n "
            "FROM factions f ORDER BY f.id").fetchall()
    chunk, page, total = paginate(rows, page)
    kb = [[InlineKeyboardButton(f"🏴 {r['name']} ({r['n']} عضو)"[:48], callback_data=f"fcv_{r['id']}")]
          for r in chunk]
    nav = page_nav("fcl_", page, total)
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("➕ فکشن جدید", callback_data="fcnew")])
    kb.append([InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")])
    text = (f"🏴 فکشن‌ها\n{DIV}\n"
            "برای اضافه/حذف پلیر، روی پیامش ریپلای کن و بنویس:\n"
            "+faction اسم_فکشن  |  -faction\n"
            "+leader  |  -leader  (تعیین/حذف لیدر)")
    if not rows:
        text += "\n\n(هنوز فکشنی نساختی)"
    await show(query, context, text, InlineKeyboardMarkup(kb))


async def show_faction_detail(query, context, fid):
    with db() as conn:
        f = conn.execute("SELECT * FROM factions WHERE id=?", (fid,)).fetchone()
        members = []
        if f:
            members = conn.execute(
                "SELECT m.user_id, p.username FROM faction_members m LEFT JOIN players p ON p.user_id=m.user_id "
                "WHERE m.faction_id=? ORDER BY m.joined", (fid,)).fetchall()
    if not f:
        await show_faction_list(query, context)
        return
    lines = [f"🏴 {f['name']}", DIV, f"🏦 صندوق: {f['bank']:,} $", f"👥 اعضا ({len(members)}):"]
    for r in members[:25]:
        lines.append(f"   • {r['username'] or r['user_id']}" + (" 👑" if r["user_id"] == f["leader_id"] else ""))
    if not members:
        lines.append("   (خالی)")
    if f["leader_id"] is None:
        lines.append("\n⚠️ لیدر ندارد — با ریپلای و +leader تعیین کن.")
    kb = [[InlineKeyboardButton("🗑 حذف فکشن", callback_data=f"fcd_{fid}")],
          [InlineKeyboardButton("🔙 لیست فکشن‌ها", callback_data="fcl_0")]]
    await show(query, context, "\n".join(lines), InlineKeyboardMarkup(kb))


async def newfaction_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    ok, res = create_faction(" ".join(context.args or []))
    await update.message.reply_text(res if ok or context.args else "استفاده: /newfaction اسم_فکشن")


# ================= دستورات ادمین با ریپلای =================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ دسترسی غیرمجاز.")
        return
    await reply_panel(update, f"🛠️ پنل مدیریت تریدر\n{DIV}", admin_menu())


def medal_change(conn, target, label, give):
    label = (label or "").strip()
    if give and (not label or len(label) > 30):
        return "❌ متن مدال باید بین ۱ تا ۳۰ حرف باشه. مثال: +medal 🏆 قهرمان"
    row = conn.execute("SELECT medals FROM players WHERE user_id=?", (target,)).fetchone()
    medals = get_medals(row)
    if give:
        if label in medals:
            return "⚠️ این مدال رو از قبل داره."
        medals.append(label)
        msg = f"🏅 مدال «{label}» داده شد."
    else:
        if label not in medals:
            return "❌ این مدال رو نداره.\nمدال‌هاش: " + (" | ".join(medals) or "—")
        medals.remove(label)
        msg = f"✅ مدال «{label}» گرفته شد."
    conn.execute("UPDATE players SET medals=? WHERE user_id=?", (json.dumps(medals, ensure_ascii=False), target))
    return msg


async def _medal_cmd(update, context, give):
    if not is_admin(update.effective_user.id):
        return
    args = list(context.args or [])
    cmd = "/medal" if give else "/unmedal"
    if len(args) < 2 or not args[0].startswith("@"):
        await update.message.reply_text(f"استفاده: {cmd} @یوزرنیم متن_مدال\nمثال: {cmd} @ali 🏆 قهرمان")
        return
    with db() as conn:
        row = conn.execute("SELECT user_id FROM players WHERE lower(username)=lower(?)", (args[0][1:],)).fetchone()
        res = medal_change(conn, row["user_id"], " ".join(args[1:]), give) if row else \
            "❌ پلیری با این یوزرنیم پیدا نشد (باید /start زده باشه)."
    await update.message.reply_text(res)


async def medal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _medal_cmd(update, context, True)


async def unmedal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _medal_cmd(update, context, False)


def apply_admin_command(target, target_name, text):
    """اجرای دستور ریپلای. متن جواب رو برمی‌گردونه (یا None اگه دستور نبود)."""
    low = text.lower()
    if text[0] not in "+-":
        return None
    arg = text.split(" ", 1)[1].strip() if " " in text else ""

    with db() as conn:
        ensure_player(conn, target, target_name)

        if low in ("+house", "+خونه", "+خانه"):
            conn.execute("UPDATE players SET has_house=1 WHERE user_id=?", (target,))
            return "✅ خانه ثبت شد."
        if low in ("-house", "-خونه", "-خانه"):
            conn.execute("UPDATE players SET has_house=0 WHERE user_id=?", (target,))
            return "✅ خانه حذف شد."
        if low.startswith("+car ") or low.startswith("+ماشین "):
            name = arg
            if not name:
                return "❌ اسم ماشین رو بنویس."
            row = conn.execute("SELECT cars FROM players WHERE user_id=?", (target,)).fetchone()
            cars = get_cars(row)
            if len(cars) >= MAX_CARS:
                return f"❌ سقف {MAX_CARS} ماشین پر شده!"
            cars.append(name)
            conn.execute("UPDATE players SET cars=? WHERE user_id=?", (json.dumps(cars), target))
            return f"✅ {name} اضافه شد. ({len(cars)}/{MAX_CARS})"

        if low.startswith("-car") or low.startswith("-ماشین"):
            name = arg
            row = conn.execute("SELECT cars FROM players WHERE user_id=?", (target,)).fetchone()
            cars = get_cars(row)
            if not cars:
                return "❌ این پلیر ماشینی نداره."
            if name:
                found = next((i for i, c in enumerate(cars) if c.lower() == name.lower()), None)
                if found is None:
                    return "❌ این ماشین توی لیستش نیست.\nلیستش:\n" + "\n".join(f"• {c}" for c in cars)
                removed = cars.pop(found)
            else:
                removed = cars.pop()
            conn.execute("UPDATE players SET cars=? WHERE user_id=?", (json.dumps(cars), target))
            return f"✅ {removed} حذف شد. ({len(cars)}/{MAX_CARS} باقی‌مانده)"

        # ----- مدال -----
        if low.startswith(("+medal ", "+مدال ")):
            return medal_change(conn, target, arg, True)
        if low.startswith(("-medal ", "-مدال ")):
            return medal_change(conn, target, arg, False)

        # ----- فکشن -----
        if low.startswith(("+faction ", "+فکشن ", "+فاکشن ")):
            fac = faction_by_name(conn, arg)
            if not fac:
                return "❌ فکشنی با این اسم نیست. اول با /newfaction بساز."
            remove_member(conn, target)
            conn.execute("INSERT INTO faction_members (user_id, faction_id, joined) VALUES (?,?,?)",
                         (target, fac["id"], int(time.time())))
            return f"✅ {target_name} به فکشن «{fac['name']}» اضافه شد."
        if low in ("-faction", "-فکشن", "-فاکشن"):
            fac = faction_of(conn, target)
            if not fac:
                return "❌ این پلیر عضو هیچ فکشنی نیست."
            remove_member(conn, target)
            return f"✅ {target_name} از فکشن «{fac['name']}» حذف شد."
        if low in ("+leader", "+لیدر"):
            fac = faction_of(conn, target)
            if not fac:
                return "❌ این پلیر عضو هیچ فکشنی نیست."
            conn.execute("UPDATE factions SET leader_id=? WHERE id=?", (target, fac["id"]))
            return f"👑 {target_name} لیدر فکشن «{fac['name']}» شد."
        if low in ("-leader", "-لیدر"):
            fac = faction_of(conn, target)
            if not fac or fac["leader_id"] != target:
                return "❌ این پلیر لیدر نیست."
            conn.execute("UPDATE factions SET leader_id=NULL WHERE id=?", (fac["id"],))
            return f"✅ لیدری {target_name} برداشته شد."

        # ----- پول -----
        if len(text) >= 2:
            amount = parse_amount(text[1:])
            if amount is None or amount <= 0:
                return "❌ عدد معتبر بنویس. مثال: +200"
            cash = conn.execute("SELECT cash FROM players WHERE user_id=?", (target,)).fetchone()["cash"]
            if text[0] == "+":
                conn.execute("UPDATE players SET cash=cash+? WHERE user_id=?", (amount, target))
                return f"✅ {amount:,} $ اضافه شد.\n💰 موجودی جدید: {cash + amount:,} $"
            if cash < amount:
                return f"❌ اینقدر پول نداره! موجودیش: {cash:,} $"
            conn.execute("UPDATE players SET cash=cash-? WHERE user_id=?", (amount, target))
            return f"✅ {amount:,} $ کم شد.\n💰 موجودی جدید: {cash - amount:,} $"
    return None


async def admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    replied = msg.reply_to_message
    if not replied or not replied.from_user:
        return
    text = (msg.text or "").strip()
    if not text:
        return
    target = replied.from_user.id
    result = apply_admin_command(target, user_label(replied.from_user), text)
    if result:
        await msg.reply_text(result)
        await refresh_medals(context, target)


async def wealth_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ دسترسی غیرمجاز.")
        return
    replied = update.message.reply_to_message
    if not replied or not replied.from_user:
        await update.message.reply_text("❌ روی پیام پلیر ریپلای کن و /wealth بزن.")
        return
    with db() as conn:
        player = conn.execute("SELECT * FROM players WHERE user_id=?", (replied.from_user.id,)).fetchone()
        if not player:
            await update.message.reply_text("❌ این پلیر ثبت‌نام نکرده. اول ازش بخواه /start بزنه.")
            return
        text = asset_text(conn, player)
    await update.message.reply_text(text)


# ================= بکاپ / بازیابی =================
async def send_backup(chat_id, context):
    with open(DB_FILE, "rb") as f:
        data = io.BytesIO(f.read())
    await context.bot.send_document(
        chat_id=chat_id, document=data, filename="grim_valley_backup.db",
        caption="💾 بکاپ کامل تریدر\n\nاین فایل رو نگه دار.\nبرای برگرداندنش، روی همین فایل ریپلای کن و /restore بزن."
    )


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await send_backup(update.effective_message.chat_id, context)


async def restore_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    msg = update.message
    if not msg.reply_to_message or not msg.reply_to_message.document:
        await msg.reply_text("❌ روی فایل بکاپ (.db) ریپلای کن و /restore بزن.")
        return
    doc = msg.reply_to_message.document
    if not (doc.file_name or "").endswith(".db"):
        await msg.reply_text("❌ فایل باید پسوند .db داشته باشه.")
        return
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await msg.reply_text("❌ فایل خیلی حجیمه.")
        return
    tmp = "restore_tmp.db"
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(custom_path=tmp)
        check = sqlite3.connect(tmp)
        try:
            check.execute("SELECT COUNT(*) FROM players").fetchone()
            check.execute("SELECT COUNT(*) FROM items").fetchone()
            ok = check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            check.close()
        if not ok:
            raise ValueError("invalid database")
        os.replace(tmp, DB_FILE)
        init_db()  # اگه بکاپ قدیمی بود، جدول‌ها و ستون‌های جدید اضافه میشن
        await msg.reply_text("✅ بکاپ بازیابی شد. همه پول‌ها، ماشین‌ها و آیتم‌ها برگشتن.")
    except Exception as e:
        logger.error("Restore failed: %s", e)
        if os.path.exists(tmp):
            os.remove(tmp)
        await msg.reply_text("❌ این فایل بکاپ معتبر نیست.")


# ================= لغو =================
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # فقط وقتی واقعاً عملیاتی در جریانه جواب بده
    if any(context.user_data.get(k) for k in ("step", "edit_step", "pstep")):
        context.user_data.clear()
        await update.message.reply_text("❌ عملیات لغو شد.")


# ================= دیسپچر دکمه‌ها =================
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    msg = q.message
    if msg is None:
        await q.answer()
        return
    uid = q.from_user.id

    # فقط صاحب منو می‌تونه دکمه‌هاش رو بزنه (توی گروه)
    if msg.chat.type != "private":
        owner = panel_owner(msg.chat_id, msg.message_id)
        if owner is None:
            await q.answer("⌛ این منو قدیمی شده. دوباره /start بزن.", show_alert=True)
            return
        if owner != uid:
            await q.answer("🚫 این منو برای تو نیست. برای خودت /start بزن.", show_alert=True)
            return
    await q.answer()

    data = q.data or ""
    admin = is_admin(uid)
    num = data.split("_", 1)[1] if "_" in data else ""
    num = int(num) if num.isdigit() else None

    # با هر کلیک روی منو، حالت‌های نیمه‌تموم (تیکت، افزودن آیتم، ...) پاک میشن
    # (به‌جز دکمه‌های داخل خودِ مراحل افزودن آیتم)
    if not data.startswith(("addtype_", "addcat_", "add_skip_photo", "ph_")):
        context.user_data.clear()
        # هر عملیات فقط توی همون چتی ادامه پیدا می‌کنه که توش شروع شده
        context.user_data["flow_chat"] = msg.chat_id

    # ----- پلیر -----
    if data == "main_menu":
        await show(q, context, main_text(q.from_user), main_menu(admin))
    elif data == "close_panel":
        await safe_delete(msg)
    elif data == "cancel_p":
        await show(q, context, main_text(q.from_user), main_menu(admin))
    elif data == "my_assets":
        await show_assets(q, context)
    elif data == "shop":
        await show_shop(q, context)
    elif data == "leaderboard":
        await show_leaderboard(q, context)
    elif data == "daily":
        await give_daily(q, context)
    elif data == "work":
        await show_work(q, context)
    elif data == "help":
        await show(q, context, help_text(admin), back_main())
    elif data == "my_faction":
        await show_my_faction(q, context)
    elif data == "tickets":
        await show_tickets_menu(q, context)
    elif data == "tk_new":
        await start_ticket(q, context)
    elif data == "tk_mine":
        await show_my_tickets(q, context)
    elif data == "my_orders":
        await show_my_orders(q, context)
    elif data == "sell":
        await show_sell_menu(q, context)
    elif data.startswith("sl_") and num is not None:
        await start_sell(q, context, num)
    elif data == "wishlist":
        await show_wishlist(q, context)
    elif data == "invite":
        await show_invite(q, context)
    elif data.startswith("wlr_") and num is not None:
        await remove_wish(q, context, num)
    elif data.startswith("wl_") and num is not None:
        await toggle_wish(q, context, num)
    elif data == "fbd":
        with db() as conn:
            in_fac = faction_of(conn, uid) is not None
        if in_fac:
            context.user_data["pstep"] = "fb_dep"
            await show(q, context, "💰 مبلغ واریز به صندوق فکشن رو فقط عدد بنویس:", cancel_player_markup())
        else:
            await show(q, context, "❌ عضو هیچ فکشنی نیستی.", back_main())
    elif data == "fbw":
        with db() as conn:
            fac = faction_of(conn, uid)
        if fac and fac["leader_id"] == uid:
            context.user_data["pstep"] = "fb_wd"
            await show(q, context, f"🏦 موجودی صندوق: {fac['bank']:,} $\nمبلغ برداشت رو فقط عدد بنویس:",
                       cancel_player_markup())
        else:
            await show(q, context, "❌ فقط لیدر فکشن می‌تونه برداشت کنه.", back_main())
    elif data == "fl":
        with db() as conn:
            fac = faction_of(conn, uid)
        if not fac:
            await show(q, context, "❌ عضو هیچ فکشنی نیستی.", back_main())
        else:
            warn = "\n\n⚠️ تو لیدری؛ با رفتنت فکشن بدون لیدر میشه." if fac["leader_id"] == uid else ""
            await show(q, context, f"🚪 مطمئنی می‌خوای از فکشن «{fac['name']}» بری؟{warn}",
                       InlineKeyboardMarkup([[InlineKeyboardButton("✅ بله، برم", callback_data="fly"),
                                              InlineKeyboardButton("❌ نه", callback_data="my_faction")]]))
    elif data == "fly":
        with db() as conn:
            fac = faction_of(conn, uid)
            if fac:
                remove_member(conn, uid)
        await show(q, context,
                   f"🚪 از فکشن «{fac['name']}» خارج شدی." if fac else "❌ عضو هیچ فکشنی نیستی.", back_main())
    elif data.startswith("cat_"):
        rest = data[4:]
        cat, sep, pg = rest.rpartition("_")
        if not sep or not pg.isdigit():
            cat, pg = rest, "0"
        await show_category(q, context, cat, int(pg))
    elif data.startswith("pick_") and num is not None:
        await show_item(q, context, num)
    elif data.startswith("buy_") and num is not None:
        await buy_item(q, context, num)
    elif data.startswith("bq_") and num is not None:
        await start_buy_qty(q, context, num)
    elif data.startswith("rqy_") and num is not None:
        await send_real_request(q, context, num)
    elif data.startswith("rq_") and num is not None:
        await confirm_real(q, context, num)

    # ----- ادمین -----
    elif not admin:
        return
    elif data == "admin_panel":
        await show(q, context, f"🛠️ پنل مدیریت تریدر\n{DIV}", admin_menu())
    elif data == "cancel_flow":
        await show(q, context, f"❌ عملیات لغو شد.\n\n🛠️ پنل مدیریت تریدر\n{DIV}", admin_menu())
    elif data == "admin_backup":
        await send_backup(msg.chat_id, context)
    elif data == "admin_list":
        await show_admin_list(q, context)
    elif data.startswith("alp_") and num is not None:
        await show_admin_list(q, context, num)
    elif data == "admin_add":
        context.user_data["step"] = "name"
        await show(q, context, ADD_NAME_PROMPT, cancel_markup())
    elif data.startswith("addtype_"):
        if context.user_data.get("step") == "type" and data[8:] in ("game", "real"):
            real = data[8:] == "real"
            context.user_data["item_real"] = real
            context.user_data["step"] = "price"
            if real:
                prompt = "✏️ مرحله ۳ از ۵:\nقیمت پولی رو با واحدش بنویس:\nمثال: 20 یورو"
            else:
                prompt = "✏️ مرحله ۳ از ۵:\nقیمت رو فقط عدد بنویس:\nمثال: 1500"
            await show(q, context, prompt, cancel_markup())
    elif data.startswith("addcat_"):
        cat = data[7:]
        if context.user_data.get("step") == "category" and cat in cat_map():
            context.user_data["item_cat"] = cat
            context.user_data["step"] = "photo"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭ بدون عکس", callback_data="add_skip_photo")],
                [InlineKeyboardButton("❌ لغو", callback_data="cancel_flow")],
            ])
            await show(q, context, "🖼 مرحله ۵ از ۵:\nیه عکس برای آیتم بفرست (اختیاریه):", kb)
    elif data == "add_skip_photo":
        if context.user_data.get("step") == "photo":
            await safe_delete(msg)
            await finish_add(context, msg.chat_id, uid, None)
    elif data.startswith("item_") and num is not None:
        await show_item_detail(q, context, num)
    elif data.startswith("delyes_") and num is not None:
        await do_delete(q, context, num)
    elif data.startswith("del_") and num is not None:
        await confirm_delete(q, context, num)
    elif data.startswith("eb_") and num is not None:
        context.user_data.update(edit_step="edit_price", edit_id=num)
        await show(q, context, "💵 قیمت جدید رو بنویس (برای آیتم بازی عدد، برای پولی با واحد):", cancel_markup())
    elif data.startswith("en_") and num is not None:
        context.user_data.update(edit_step="edit_name", edit_id=num)
        await show(q, context, "✏️ اسم جدید آیتم رو بنویس:", cancel_markup())
    elif data.startswith("ep_") and num is not None:
        context.user_data.update(edit_step="edit_photo", edit_id=num)
        await show(q, context, "🖼 عکس جدید آیتم رو بفرست:", cancel_markup())
    elif data.startswith("fs_") and num is not None:
        status, it = set_flash(num)
        if status == "ok":
            err = await announce_flash(context, it)
            await notify_wishers(context, it)
            if err:
                await context.bot.send_message(
                    chat_id=msg.chat_id, text=f"⚠️ ارسال پیام حراجی به گپ ناموفق بود:\n{err}\n(بات باید توی گپ عضو باشه)")
        elif status == "real":
            await context.bot.send_message(chat_id=msg.chat_id, text="❌ حراجی فقط برای آیتم‌های بازیه.")
        await show_item_detail(q, context, num)
    elif data.startswith("fe_") and num is not None:
        end_flash(num)
        await show_item_detail(q, context, num)

    # دسته‌ها
    elif data == "cm":
        await show_cat_manage(q, context)
    elif data == "cmadd":
        context.user_data["step"] = "newcat"
        await show(q, context, "🗂 اسم دسته جدید رو بنویس (می‌تونی اول اسمش ایموجی بذاری):\nمثال: 🧪 دارو",
                   cancel_markup())
    elif data.startswith("cmdel_"):
        await delete_category(q, context, data[6:])

    # فکشن‌ها
    elif data.startswith("fcl_") and num is not None:
        await show_faction_list(q, context, num)
    elif data == "fcnew":
        context.user_data["step"] = "newfac"
        await show(q, context, "🏴 اسم فکشن جدید رو بنویس:", cancel_markup())
    elif data.startswith("fcv_") and num is not None:
        await show_faction_detail(q, context, num)
    elif data.startswith("fcdy_") and num is not None:
        with db() as conn:
            conn.execute("DELETE FROM faction_members WHERE faction_id=?", (num,))
            conn.execute("DELETE FROM factions WHERE id=?", (num,))
        await show_faction_list(q, context)
    elif data.startswith("fcd_") and num is not None:
        await show(q, context, "⚠️ مطمئنی این فکشن حذف بشه؟\nاعضا آزاد میشن و پول صندوق از بین میره.",
                   InlineKeyboardMarkup([[InlineKeyboardButton("✅ بله، حذف کن", callback_data=f"fcdy_{num}"),
                                          InlineKeyboardButton("❌ نه", callback_data=f"fcv_{num}")]]))

    # تیکت‌ها
    elif data.startswith("tkl_") and num is not None:
        await show_ticket_list(q, context, num)
    elif data.startswith("tkv_") and num is not None:
        await show_ticket_detail(q, context, num)
    elif data.startswith("tkc_") and num is not None:
        await close_ticket(q, context, num)
    elif data.startswith("tkr_") and num is not None:
        context.user_data.update(step="tk_reply", tk_id=num)
        await send_panel(context, msg.chat_id, uid, f"✍️ پاسخ تیکت #{num} رو بنویس و بفرست:", cancel_markup())

    # سفارش‌ها
    elif data.startswith("ol_") and num is not None:
        await show_order_list(q, context, num)
    elif data.startswith("ov_") and num is not None:
        await show_order_detail(q, context, num)
    elif data.startswith("od_") and num is not None:
        await deliver_order(q, context, num)
    elif data.startswith("oc_") and num is not None:
        await cancel_order(q, context, num)

    # اعلام خرید / دعوت‌ها
    elif data.startswith("an_") and num is not None:
        with db() as conn:
            conn.execute("UPDATE items SET announce = 1 - announce WHERE id=?", (num,))
        await show_item_detail(q, context, num)
    elif data.startswith("rl_") and num is not None:
        await show_referral_list(q, context, num)
    elif data.startswith("rv_") and num is not None:
        await show_referral_detail(q, context, num)
    elif data.startswith("rfy_") and num is not None:
        await confirm_referral(q, context, num)
    elif data.startswith("rfn_") and num is not None:
        await reject_referral(q, context, num)

    # تأیید / رد کاربر جدید
    elif data.startswith("nuy_") and num is not None:
        await approve_user(q, context, num)
    elif data.startswith("nun_") and num is not None:
        await reject_user(q, context, num)

    # خرید از بازیکن (ادمین)
    elif data == "bm":
        await show_buy_manage(q, context)
    elif data.startswith("bi_") and num is not None:
        await show_buy_detail(q, context, num)
    elif data == "bnew":
        context.user_data["step"] = "bname"
        await show(q, context, "💎 اسم آیتمی که سرور می‌خره رو بنویس (مثلاً: الماس):", cancel_markup())
    elif data.startswith("bpe_") and num is not None:
        context.user_data.update(edit_step="bedit_price", edit_id=num)
        await show(q, context, "💵 قیمت خرید جدید رو فقط عدد بنویس:", cancel_markup())
    elif data.startswith("bne_") and num is not None:
        context.user_data.update(edit_step="bedit_name", edit_id=num)
        await show(q, context, "✏️ اسم جدید رو بنویس:", cancel_markup())
    elif data.startswith("bdl_") and num is not None:
        await show(q, context, "⚠️ مطمئنی این آیتم از لیست خرید حذف بشه؟",
                   InlineKeyboardMarkup([[InlineKeyboardButton("✅ بله", callback_data=f"bdy_{num}"),
                                          InlineKeyboardButton("❌ نه", callback_data=f"bi_{num}")]]))
    elif data.startswith("bdy_") and num is not None:
        with db() as conn:
            conn.execute("DELETE FROM buy_items WHERE id=?", (num,))
        await show_buy_manage(q, context)
    elif data.startswith("sq_") and num is not None:
        await show_sell_requests(q, context, num)
    elif data.startswith("sv_") and num is not None:
        await show_sell_detail(q, context, num)
    elif data.startswith("sa_") and num is not None:
        await accept_sell(q, context, num)
    elif data.startswith("sr_") and num is not None:
        await reject_sell(q, context, num)

    # قیمت موقت آیتم (کمتر / بیشتر)
    elif data.startswith(("pd_", "pu_")) and num is not None:
        down = data.startswith("pd_")
        with db() as conn:
            it = conn.execute("SELECT * FROM items WHERE id=?", (num,)).fetchone()
        if not it or it["is_real"]:
            await show(q, context, "❌ آیتم پیدا نشد (یا پولیه).", admin_menu())
        else:
            context.user_data.update(step="pamt", p_item=num, p_dir="down" if down else "up")
            await show(q, context,
                       f"{'📉' if down else '📈'} «{it['name']}» — قیمت اصلی: {it['buy_price']:,}$\n{DIV}\n"
                       f"چقدر می‌خوای از قیمتش {'کم' if down else 'زیاد'} کنی؟\n{CHANGE_HINT}", cancel_markup())
    elif data.startswith("ph_") and num in (6, 12, 24, 48, 72):
        ud_ = context.user_data
        item_id, new = ud_.get("p_item"), ud_.get("p_new")
        if not item_id or not new:
            await show(q, context, "⌛ این مرحله منقضی شده؛ از صفحه آیتم دوباره شروع کن.", admin_menu())
        else:
            ud_.clear()
            ud_["flow_chat"] = msg.chat_id
            status, it = set_temp_price(item_id, new, num)
            if status != "ok":
                await show(q, context, "❌ آیتم پیدا نشد (یا پولیه).", admin_menu())
            else:
                err = await announce_flash(context, it)
                await notify_wishers(context, it)
                if err:
                    await context.bot.send_message(
                        chat_id=msg.chat_id, text=f"⚠️ اعلام توی گپ ناموفق بود:\n{err}\n(بات باید توی گپ عضو باشه)")
                await show_item_detail(q, context, item_id)

    # بازار طلا و نقره
    elif data.startswith(("mkd_", "mku_")) and data.split("_", 1)[1] in MARKET_NAMES:
        key = data.split("_", 1)[1]
        down = data.startswith("mkd_")
        with db() as conn:
            cur = conn.execute("SELECT price FROM market WHERE key=?", (key,)).fetchone()
        context.user_data.update(step="mkamt", mk_key=key, mk_dir="down" if down else "up")
        await show(q, context,
                   f"{'📉' if down else '📈'} {MARKET_NAMES[key]} — قیمت فعلی: {cur['price']}$\n{DIV}\n"
                   f"چقدر می‌خوای {'کم' if down else 'زیاد'} بشه؟\n{CHANGE_HINT}\n"
                   f"(این قیمت تا تغییر بعدی خودت ثابت می‌مونه و خودکار برنمی‌گرده.)", cancel_markup())
    elif data == "mk":
        await show_market(q, context)
    elif data == "mkroll":
        res = roll_market()
        await show_market(q, context, "✅ قیمت‌های امروز ساخته شد: " + " | ".join(
            f"{MARKET_NAMES[k]} {o}$ ➜ {n}$" for k, (o, n) in res.items()))
    elif data == "mkann":
        err = await announce_market(context)
        await show_market(q, context, "📣 قیمت‌ها توی گپ اعلام شد." if not err else f"⚠️ ارسال به گپ ناموفق بود: {err}")
    elif data.startswith("bmk_") and num is not None:
        with db() as conn:
            cur = conn.execute("SELECT market FROM buy_items WHERE id=?", (num,)).fetchone()
            nxt = {None: "gold", "gold": "silver", "silver": None}.get(cur["market"] if cur else None)
            conn.execute("UPDATE buy_items SET market=? WHERE id=?", (nxt, num))
        await show_buy_detail(q, context, num)
    elif data.startswith("bfc_") and num is not None:
        with db() as conn:
            conn.execute("UPDATE buy_items SET factor = CASE WHEN factor=100 THEN 50 ELSE 100 END WHERE id=?", (num,))
        await show_buy_detail(q, context, num)

    # مدیریت کاربران
    elif data.startswith("ulb_") and num is not None:
        await show_user_list(q, context, num, banned_only=True)
    elif data.startswith("ul_") and num is not None:
        await show_user_list(q, context, num)
    elif data == "usearch":
        context.user_data["step"] = "usearch"
        await show(q, context, "🔎 یوزرنیم (با یا بدون @) یا آیدی عددی کاربر رو بنویس:", cancel_markup())
    elif data.startswith("uv_") and num is not None:
        await show_user_profile(q, context, num)
    elif data.startswith("ub_") and num is not None:
        context.user_data.update(step="banreason", ban_uid=num)
        await show(q, context, "🚫 دلیل مسدودیت رو بنویس (یا - بفرست برای بدون دلیل):", cancel_markup())
    elif data.startswith("uub_") and num is not None:
        if do_unban(num):
            await notify_user(context, num, "✅ مسدودیتت برداشته شد. می‌تونی دوباره از بات استفاده کنی.")
        await show(q, context, f"✅ مسدودیت کاربر {num} برداشته شد.", user_back(num))
    elif data.startswith("uky_") and num is not None:
        err = await kick_from_group(context, num)
        await show(q, context, f"👢 کاربر {num} از گپ کیک شد (می‌تونه دوباره عضو بشه)." if not err else
                   f"❌ کیک انجام نشد: {err}\n(بات باید توی گپ ادمین با دسترسی بن باشه)", user_back(num))
    elif data.startswith("uk_") and num is not None:
        await show(q, context, f"👢 کاربر {num} از گپ اعلامیه‌ها کیک بشه؟\nمی‌تونه دوباره عضو بشه و یه پیام تذکر هم می‌گیره.",
                   InlineKeyboardMarkup([[InlineKeyboardButton("✅ بله، کیک کن", callback_data=f"uky_{num}"),
                                          InlineKeyboardButton("❌ نه", callback_data=f"uv_{num}")]]))
    elif data.startswith("udy_") and num is not None:
        res = delete_player(num)
        await show(q, context, "❌ نمی‌شه ادمین رو حذف کرد." if res is None else
                   f"🗑 کاربر {num} از بات حذف شد (پول، ماشین‌ها، مدال‌ها و عضویت فکشنش پاک شد)."
                   + ("\n🚫 مسدودیتش (اگه داشت) سر جاشه." if res["existed"] else "\n(توی بات ثبت‌نام نکرده بود.)"),
                   InlineKeyboardMarkup([[InlineKeyboardButton("👥 کاربران", callback_data="ul_0")]]))
    elif data.startswith("ud_") and num is not None:
        await show(q, context, f"⚠️ مطمئنی کاربر {num} کامل از بات حذف بشه؟\n"
                               f"پول، ماشین‌ها، مدال‌ها، لیست آرزو و عضویت فکشنش پاک میشه و سفارش‌های بازش لغو میشن (بدون برگشت پول).\n"
                               f"می‌تونه دوباره /start بزنه، مگر اینکه مسدود باشه.",
                   InlineKeyboardMarkup([[InlineKeyboardButton("✅ بله، حذف کن", callback_data=f"udy_{num}"),
                                          InlineKeyboardButton("❌ نه", callback_data=f"uv_{num}")]]))


# ================= خطاها =================
async def on_error(update, context):
    logger.error("Update caused error: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ یه مشکلی پیش اومد. دوباره تلاش کن.")
        except TelegramError:
            pass


# ================= اجرا =================
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN تنظیم نشده. اون رو به‌عنوان Environment Variable بذار.")
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    # ضد اسپم؛ قبل از همه هندلرها
    app.add_handler(TypeHandler(Update, access_check), group=-2)   # مسدودی‌ها + تأیید ورود + آخرین فعالیت
    app.add_handler(TypeHandler(Update, rate_limit), group=-1)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("work", work_command))
    app.add_handler(CommandHandler("trade", trade_command))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("additem", additem_start))
    app.add_handler(CommandHandler("listitems", cmd_list_items))
    app.add_handler(CommandHandler("wealth", wealth_command))
    app.add_handler(CommandHandler("newfaction", newfaction_command))
    app.add_handler(CommandHandler("flash", flash_command))
    app.add_handler(CommandHandler("endflash", endflash_command))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("restore", restore_backup))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("kick", kick_command))
    app.add_handler(CommandHandler("user", user_command))
    app.add_handler(CommandHandler("announce", announce_command))
    app.add_handler(CommandHandler("medal", medal_command))
    app.add_handler(CommandHandler("unmedal", unmedal_command))
    # «/اعلام ماشین»: تلگرام دستور فارسی رو command حساب نمی‌کنه، پس با Regex می‌گیریمش
    app.add_handler(MessageHandler(filters.Regex(r"^/اعلام(\s|$)"), announce_fa))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
