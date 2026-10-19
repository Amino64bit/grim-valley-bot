import os
import io
import json
import time
import random
import sqlite3
import logging
import threading
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
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

# ================= تنظیمات =================
# توکن رو توی محیط (Environment Variable) روی هاستت بذار، نه توی کد
BOT_TOKEN = os."8725147253:AAGyHLZQOED6YFCij-cV-_XrY9fgdFgczXk"
ADMIN_ID = 5591238725
ADMIN_ID_2 = 6270844351
# گپی که پیام حراجی‌ها (Flash Sale) توش ارسال میشه
ANNOUNCE_CHAT_ID = int(os.environ.get("ANNOUNCE_CHAT_ID", "-1003431951834"))

DB_FILE = "grim_valley.db"
MAX_CARS = 3
HOUSE_VALUE = 2500
REPAIR_COST = 400
PAGE_SIZE = 5                 # تعداد آیتم در هر صفحه

DAILY_MIN, DAILY_MAX = 10, 20  # جایزه روزانه
WORK_MIN, WORK_MAX = 10, 50    # دستور /work
WORK_COOLDOWN = 48 * 3600      # هر ۴۸ ساعت (۲ روز) یک بار
TRADE_TAX_PERCENT = 15         # مالیات هر ترید
RICH_GOAL = 10_000             # مدال ثروتمند
FLASH_HOURS = 24               # مدت حراجی
FLASH_DISCOUNT = 50            # درصد تخفیف حراجی
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
        # مهاجرت دیتابیس‌های قدیمی
        _add_col(conn, "players", "last_work", "INTEGER NOT NULL DEFAULT 0")
        _add_col(conn, "players", "inv", "TEXT NOT NULL DEFAULT '{}'")
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


def get_inv(player):
    return _json_load(player["inv"], {})


def get_medals(player):
    return _json_load(player["medals"], [])


def ensure_player(conn, uid, username="player"):
    conn.execute("INSERT OR IGNORE INTO players (user_id, username) VALUES (?,?)", (uid, username))


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
    if on_sale(it):
        return it["buy_price"] * (100 - FLASH_DISCOUNT) // 100
    return it["buy_price"]


def price_label(it):
    if it["is_real"]:
        return f"💶 {it['real_price']}"
    if on_sale(it):
        return f"🔥{eff_price(it):,}$"
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
        [InlineKeyboardButton("🎫 پشتیبانی", callback_data="tickets"),
         InlineKeyboardButton("❓ راهنما", callback_data="help")],
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
        [InlineKeyboardButton("🎫 تیکت‌های باز", callback_data="tkl_0"),
         InlineKeyboardButton("💾 بکاپ", callback_data="admin_backup")],
        [InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")],
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
    if new:
        conn.execute("UPDATE players SET medals=? WHERE user_id=?",
                     (json.dumps(medals + new, ensure_ascii=False), uid))
    return new


async def refresh_medals(context, uid):
    try:
        with db() as conn:
            new = check_medals(conn, uid)
    except sqlite3.Error as e:
        logger.error("medal check failed: %s", e)
        return
    for m in new:
        try:
            await context.bot.send_message(
                chat_id=uid, text=f"🏅 تبریک! مدال جدید گرفتی:\n\n✨ {m} ✨\n\nتوی دارایی‌هات می‌تونی ببینیش.")
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
    inv = get_inv(player)
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
    if inv:
        lines.append("🎒 انبار:")
        lines.extend(f"   • {k} ×{v}" for k, v in inv.items())
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
        "🤝 /trade @یوزرنیم تعداد اسم_آیتم — انتقال آیتم از انبار",
        f"🧾 از هر ترید {TRADE_TAX_PERCENT}٪ مالیات کم میشه.",
        "↩️ به‌جای @یوزرنیم می‌تونی روی پیام طرف ریپلای کنی.",
        "🏴 فکشن، 🎫 تیکت و 🏆 لیدربورد از منوی اصلی.",
    ]
    if admin:
        lines += [
            DIV, "🛠 دستورات ادمین:",
            "/admin /additem /listitems /wealth /backup /restore",
            "/newfaction نام — ساخت فکشن",
            "/flash آیدی_یا_اسم — حراجی ۲۴ ساعته | /endflash آیدی_یا_اسم",
            DIV, "↩️ ریپلای روی پیام پلیر:",
            "+200 / -200 — پول | +house / -house | repair",
            "+car اسم / -car اسم — ماشین",
            "+item اسم [تعداد] / -item اسم [تعداد] — انبار",
            "+medal متن / -medal متن — مدال دستی",
            "+faction نام / -faction — فکشن",
            "+leader / -leader — لیدر فکشن",
        ]
    return "\n".join(lines)


# ================= منوی پلیر =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    context.user_data.clear()
    user = update.effective_user
    name = user_label(user)
    with db() as conn:
        ensure_player(conn, user.id, name)
        conn.execute("UPDATE players SET username=? WHERE user_id=?", (name, user.id))
    await reply_panel(update, main_text(user), main_menu(is_admin(user.id)))


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
    keyboard, row = [], []
    for it in chunk:
        icon = "🖼 " if it["photo_id"] else ""
        label = f"{icon}{it['name']} | {price_label(it)}"
        row.append(InlineKeyboardButton(label[:48], callback_data="pick_" + str(it["id"])))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    nav = page_nav(f"cat_{category}_", page, total)
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔙 دسته‌بندی‌ها", callback_data="shop")])
    keyboard.append([InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")])
    title = cat_map().get(category, category)
    if total > 1:
        title += f"  (صفحه {page + 1} از {total})"
    await show(query, context, f"{title}\n{DIV}\nیک آیتم رو انتخاب کن 👇", InlineKeyboardMarkup(keyboard))


async def show_item(query, context, item_id):
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
            lines.append(f"💵 قیمت: 🔥 {eff_price(it):,}$  (قبلاً {it['buy_price']:,}$)")
            lines.append(f"⚡ حراجی {FLASH_DISCOUNT}٪ — {fmt_duration(left)} مونده!")
        else:
            lines.append(f"💵 قیمت: {it['buy_price']:,}$")
        buy_btn = InlineKeyboardButton(f"🛒 خرید ({eff_price(it):,}$)", callback_data="buy_" + str(it["id"]))
    markup = InlineKeyboardMarkup([
        [buy_btn],
        [InlineKeyboardButton("🔙 برگشت", callback_data=f"cat_{it['category']}_{page_of_item(it)}"),
         InlineKeyboardButton("🏠 منوی اصلی", callback_data="main_menu")],
    ])
    await show(query, context, "\n".join(lines), markup, photo=it["photo_id"])


def try_buy(uid, item_id):
    """خرید اتمیک. خروجی: (وضعیت, آیتم, موجودی, قیمت نهایی)"""
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not item or item["is_real"]:
            return "no_item", None, 0, 0
        price = eff_price(item)
        player = conn.execute("SELECT * FROM players WHERE user_id=?", (uid,)).fetchone()
        if not player:
            return "no_player", item, 0, price
        cars = get_cars(player)
        if item["category"] == "car" and len(cars) >= MAX_CARS:
            return "car_full", item, player["cash"], price
        if player["cash"] < price:
            return "poor", item, player["cash"], price
        conn.execute("UPDATE players SET cash=cash-? WHERE user_id=?", (price, uid))
        if item["category"] == "car":
            cars.append(item["name"])
            conn.execute("UPDATE players SET cars=? WHERE user_id=?", (json.dumps(cars), uid))
        else:
            inv = get_inv(player)
            inv_add(inv, item["name"], 1)
            conn.execute("UPDATE players SET inv=? WHERE user_id=?", (json.dumps(inv, ensure_ascii=False), uid))
        return "ok", item, player["cash"] - price, price


def inv_key(inv, name):
    n = (name or "").strip().lower()
    for k in inv:
        if k.lower() == n:
            return k
    return None


def inv_add(inv, name, qty):
    k = inv_key(inv, name) or name.strip()
    inv[k] = inv.get(k, 0) + qty


def inv_sub(inv, name, qty):
    k = inv_key(inv, name)
    if k is None or inv[k] < qty:
        return False
    inv[k] -= qty
    if inv[k] <= 0:
        del inv[k]
    return True


async def notify_admins(context, user, item, price=None):
    real = bool(item["is_real"])
    uname = f"@{escape(user.username)}" if user.username else "ندارد"
    if real:
        head = "💶 <b>درخواست خرید پولی!</b>"
        price_line = f"💶 قیمت: {escape(item['real_price'] or '')}"
        note = "\n\n📞 باید با این پلیر هماهنگ کنی و پیگیری کنی. (چیزی از دارایی بازی‌اش کم نشده)"
    else:
        head = "🛒 <b>سفارش جدید!</b>"
        price = eff_price(item) if price is None else price
        price_line = f"💰 قیمت: {price:,} $" + (f" (🔥 حراجی، قیمت اصلی {item['buy_price']:,}$)"
                                                 if price != item["buy_price"] else "")
        note = ""
    msg = (
        f"{head}\n{DIV}\n"
        f"👤 نام: <a href=\"tg://user?id={user.id}\">{escape(user.full_name)}</a>\n"
        f"🔗 یوزرنیم: {uname}\n"
        f"🆔 آیدی: <code>{user.id}</code>\n"
        f"📦 آیتم: {escape(item['name'])}\n"
        f"🏷️ دسته: {escape(cat_map().get(item['category'], item['category']))}\n"
        f"{price_line}\n\n"
        f"⏰ زمان: {datetime.now().strftime('%Y-%m-%d %H:%M')}{note}"
    )
    for admin in (ADMIN_ID, ADMIN_ID_2):
        try:
            if item["photo_id"]:
                await context.bot.send_photo(chat_id=admin, photo=item["photo_id"],
                                             caption=msg, parse_mode=ParseMode.HTML)
            else:
                await context.bot.send_message(chat_id=admin, text=msg, parse_mode=ParseMode.HTML)
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
    _real_cooldown[(user.id, item_id)] = now
    await notify_admins(context, user, it)
    await show(query, context,
               f"✅ درخواست خرید «{it['name']}» برای ادمین ارسال شد.\n📩 ادمین به‌زودی باهات تماس می‌گیره.",
               back_main())


async def buy_item(query, context, item_id):
    user = query.from_user
    status, item, cash, price = try_buy(user.id, item_id)

    if status == "no_item":
        await show(query, context, "❌ آیتم پیدا نشد.", back_main())
        return
    if status == "no_player":
        await show(query, context, "❌ اول /start بزن.", back_main())
        return
    if status == "car_full":
        await show(query, context, f"❌ سقف ماشین‌هات ({MAX_CARS} عدد) پر شده.\nاول یکی رو بفروش.", back_main())
        return
    if status == "poor":
        await show(query, context,
                   f"❌ پولت کمه!\n{DIV}\n💰 داری: {cash:,} $\n💵 نیاز داری: {price:,} $", back_main())
        return

    await notify_admins(context, user, item, price)
    extra = "🚗 به لیست ماشین‌هات اضافه شد." if item["category"] == "car" else \
        "🎒 به انبارت اضافه شد (با /trade می‌تونی بدیش به بقیه)."
    await show(query, context,
               f"🎉 خرید موفق!\n{DIV}\n"
               f"✅ {item['name']}\n💰 {price:,} $ کسر شد — موجودی: {cash:,} $\n{extra}\n\n"
               f"📦 برای تحویل گرفتن به ادمین سرور پیام بده.", back_main())
    await refresh_medals(context, user.id)


# ================= لیدربورد / جایزه / کار =================
async def show_leaderboard(query, context):
    with db() as conn:
        rows = conn.execute("SELECT username, cash FROM players ORDER BY cash DESC LIMIT 10").fetchall()
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
    today = datetime.now(timezone.utc).date().isoformat()
    ok = False
    with db() as conn:
        player = conn.execute("SELECT * FROM players WHERE user_id=?", (uid,)).fetchone()
        if not player:
            text = "❌ اول /start بزن."
        elif player["last_daily"] == today:
            text = "⏳ امروز جایزه‌ات رو گرفتی.\nفردا دوباره بیا! 🌙"
        else:
            reward = random.randint(DAILY_MIN, DAILY_MAX)
            conn.execute("UPDATE players SET cash=cash+?, last_daily=? WHERE user_id=?", (reward, today, uid))
            text = f"🎁 جایزه روزانه\n{DIV}\n💵 {reward} $ به حسابت اضافه شد ✅"
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
def do_trade(sender_id, target_id, amount, item_name):
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        s = conn.execute("SELECT * FROM players WHERE user_id=?", (sender_id,)).fetchone()
        t = conn.execute("SELECT * FROM players WHERE user_id=?", (target_id,)).fetchone()
        if not s:
            return False, "❌ اول /start بزن."
        if not t:
            return False, "❌ این پلیر هنوز ثبت‌نام نکرده (باید /start بزنه)."
        tax = amount * TRADE_TAX_PERCENT // 100
        got = amount - tax
        if not item_name:
            if s["cash"] < amount:
                return False, f"❌ پولت کافی نیست!\n💰 داری: {s['cash']:,} $"
            conn.execute("UPDATE players SET cash=cash-? WHERE user_id=?", (amount, sender_id))
            conn.execute("UPDATE players SET cash=cash+? WHERE user_id=?", (got, target_id))
            return True, {"kind": "money", "amount": amount, "tax": tax, "got": got}
        inv = get_inv(s)
        key = inv_key(inv, item_name)
        if key is None or inv[key] < amount:
            have = "\n".join(f"• {k} ×{v}" for k, v in inv.items()) or "• انبارت خالیه"
            return False, f"❌ به اندازه کافی «{item_name}» نداری.\n🎒 انبارت:\n{have}"
        inv_sub(inv, key, amount)
        tinv = get_inv(t)
        inv_add(tinv, key, got)
        conn.execute("UPDATE players SET inv=? WHERE user_id=?", (json.dumps(inv, ensure_ascii=False), sender_id))
        conn.execute("UPDATE players SET inv=? WHERE user_id=?", (json.dumps(tinv, ensure_ascii=False), target_id))
        return True, {"kind": "item", "name": key, "amount": amount, "tax": tax, "got": got}


TRADE_USAGE = (
    "🤝 نحوه استفاده:\n"
    "/trade @یوزرنیم 500 — انتقال پول\n"
    "/trade @یوزرنیم 3 Diamond — انتقال آیتم از انبار\n"
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
    item_name = " ".join(args[1:]).strip()

    ok, res = do_trade(user.id, target_id, amount, item_name)
    if not ok:
        await msg.reply_text(res)
        return

    if res["kind"] == "money":
        what = (f"💵 مبلغ: {res['amount']:,} $\n🧾 مالیات ({TRADE_TAX_PERCENT}٪): {res['tax']:,} $\n"
                f"✅ دریافتی: {res['got']:,} $")
    else:
        what = (f"📦 آیتم: {res['name']}\n🔢 تعداد: ×{res['amount']}\n🧾 مالیات ({TRADE_TAX_PERCENT}٪): "
                f"×{res['tax']}\n✅ دریافتی: ×{res['got']}")
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
    file_id = update.message.photo[-1].file_id
    if ud.get("step") == "photo":
        await finish_add(context, update.effective_chat.id, user.id, file_id)
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
    if ud.get("pstep"):
        await handle_player_flow(update, context)
        return
    if not is_admin(uid):
        return
    if ud.get("step") or ud.get("edit_step"):
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
            lines.append(f"🔥 در حراجی — {fmt_duration(it['sale_until'] - time.time())} مونده")
            rows.append([InlineKeyboardButton("🛑 پایان حراجی", callback_data="fe_" + i)])
        else:
            rows.append([InlineKeyboardButton(f"⚡ حراجی {FLASH_DISCOUNT}٪ ({FLASH_HOURS} ساعت)",
                                              callback_data="fs_" + i)])
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


def set_flash(item_id):
    with db() as conn:
        it = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not it:
            return "no", None
        if it["is_real"]:
            return "real", it
        if on_sale(it):
            return "already", it
        conn.execute("UPDATE items SET sale_until=? WHERE id=?", (int(time.time()) + FLASH_HOURS * 3600, item_id))
        it = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    return "ok", it


def end_flash(item_id):
    with db() as conn:
        conn.execute("UPDATE items SET sale_until=0 WHERE id=?", (item_id,))


async def announce_flash(context, it):
    """پیام Broadcast توی گپ. اگه خطا بود متنش رو برمی‌گردونه."""
    text = (
        f"🔥⚡ حراجی ناگهانی! ⚡🔥\n{DIV}\n"
        f"🔥 حراجی! قیمت {it['name']} برای {FLASH_HOURS} ساعت آینده {FLASH_DISCOUNT}٪ تخفیف خورد!\n\n"
        f"💵 قیمت قبلی: {it['buy_price']:,}$\n"
        f"🏷 قیمت جدید: {eff_price(it):,}$\n"
        f"⏳ فقط {FLASH_HOURS} ساعت فرصت داری!\n\n"
        f"🛒 برای خرید به فروشگاه ربات برو."
    )
    try:
        if it["photo_id"]:
            await context.bot.send_photo(chat_id=ANNOUNCE_CHAT_ID, photo=it["photo_id"], caption=text[:1024])
        else:
            await context.bot.send_message(chat_id=ANNOUNCE_CHAT_ID, text=text)
        return None
    except Exception as e:
        logger.error("Flash announce failed: %s", e)
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


def split_name_qty(arg):
    tokens = arg.split()
    if len(tokens) > 1:
        q = parse_amount(tokens[-1])
        if q is not None and q > 0:
            return " ".join(tokens[:-1]), q
    return arg.strip(), 1


def apply_admin_command(target, target_name, text):
    """اجرای دستور ریپلای. متن جواب رو برمی‌گردونه (یا None اگه دستور نبود)."""
    low = text.lower()
    if not (text[0] in "+-" or low in ("repair", "تعمیر")):
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
        if low in ("repair", "تعمیر", "+repair"):
            cash = conn.execute("SELECT cash FROM players WHERE user_id=?", (target,)).fetchone()["cash"]
            if cash < REPAIR_COST:
                return f"❌ پولش برای تعمیر ({REPAIR_COST} $) کافی نیست."
            conn.execute("UPDATE players SET cash=cash-? WHERE user_id=?", (REPAIR_COST, target))
            return f"🔧 هزینه تعمیر: {REPAIR_COST} $ کم شد."

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

        # ----- انبار -----
        if low.startswith(("+item ", "+آیتم ")):
            name, qty = split_name_qty(arg)
            if not name:
                return "❌ مثال: +item Diamond 5"
            inv = get_inv(conn.execute("SELECT inv FROM players WHERE user_id=?", (target,)).fetchone())
            inv_add(inv, name, qty)
            conn.execute("UPDATE players SET inv=? WHERE user_id=?",
                         (json.dumps(inv, ensure_ascii=False), target))
            return f"✅ {qty}× {name} به انبارش اضافه شد."
        if low.startswith(("-item ", "-آیتم ")):
            name, qty = split_name_qty(arg)
            inv = get_inv(conn.execute("SELECT inv FROM players WHERE user_id=?", (target,)).fetchone())
            if not inv_sub(inv, name, qty):
                have = "\n".join(f"• {k} ×{v}" for k, v in inv.items()) or "• انبارش خالیه"
                return f"❌ به اندازه کافی «{name}» نداره.\nانبارش:\n{have}"
            conn.execute("UPDATE players SET inv=? WHERE user_id=?",
                         (json.dumps(inv, ensure_ascii=False), target))
            return f"✅ {qty}× {name} از انبارش کم شد."

        # ----- مدال -----
        if low.startswith(("+medal ", "+مدال ")):
            if not arg or len(arg) > 30:
                return "❌ متن مدال باید بین ۱ تا ۳۰ حرف باشه. مثال: +medal 🏆 قهرمان"
            row = conn.execute("SELECT medals FROM players WHERE user_id=?", (target,)).fetchone()
            medals = get_medals(row)
            if arg in medals:
                return "⚠️ این مدال رو از قبل داره."
            medals.append(arg)
            conn.execute("UPDATE players SET medals=? WHERE user_id=?",
                         (json.dumps(medals, ensure_ascii=False), target))
            return f"🏅 مدال «{arg}» داده شد."
        if low.startswith(("-medal ", "-مدال ")):
            row = conn.execute("SELECT medals FROM players WHERE user_id=?", (target,)).fetchone()
            medals = get_medals(row)
            if arg not in medals:
                return "❌ این مدال رو نداره.\nمدال‌هاش: " + (" | ".join(medals) or "—")
            medals.remove(arg)
            conn.execute("UPDATE players SET medals=? WHERE user_id=?",
                         (json.dumps(medals, ensure_ascii=False), target))
            return f"✅ مدال «{arg}» گرفته شد."

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
    if context.user_data:
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
    if not data.startswith(("addtype_", "addcat_", "add_skip_photo")):
        context.user_data.clear()

    # ----- پلیر -----
    if data == "main_menu":
        await show(q, context, main_text(q.from_user), main_menu(admin))
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

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
import os
import io
import json
import time
import random
import sqlite3
import logging
import threading
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
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

# ================= تنظیمات =================
# توکن رو توی محیط (Environment Variable) روی هاستت بذار، نه توی کد
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = 5591238725
ADMIN_ID_2 = 6270844351
# گپی که پیام حراجی‌ها (Flash Sale) توش ارسال میشه
ANNOUNCE_CHAT_ID = int(os.environ.get("ANNOUNCE_CHAT_ID", "-1003431951834"))

DB_FILE = "grim_valley.db"
MAX_CARS = 3
HOUSE_VALUE = 2500
REPAIR_COST = 400
PAGE_SIZE = 5                 # تعداد آیتم در هر صفحه

DAILY_MIN, DAILY_MAX = 10, 20  # جایزه روزانه
WORK_MIN, WORK_MAX = 10, 50    # دستور /work
WORK_COOLDOWN = 48 * 3600      # هر ۴۸ ساعت (۲ روز) یک بار
TRADE_TAX_PERCENT = 15         # مالیات هر ترید
RICH_GOAL = 10_000             # مدال ثروتمند
FLASH_HOURS = 24               # مدت حراجی
FLASH_DISCOUNT = 50            # درصد تخفیف حراجی
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
        # مهاجرت دیتابیس‌های قدیمی
        _add_col(conn, "players", "last_work", "INTEGER NOT NULL DEFAULT 0")
        _add_col(conn, "players", "inv", "TEXT NOT NULL DEFAULT '{}'")
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


def get_inv(player):
    return _json_load(player["inv"], {})


def get_medals(player):
    return _json_load(player["medals"], [])


def ensure_player(conn, uid, username="player"):
    conn.execute("INSERT OR IGNORE INTO players (user_id, username) VALUES (?,?)", (uid, username))


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
    if on_sale(it):
        return it["buy_price"] * (100 - FLASH_DISCOUNT) // 100
    return it["buy_price"]


def price_label(it):
    if it["is_real"]:
        return f"💶 {it['real_price']}"
    if on_sale(it):
        return f"🔥{eff_price(it):,}$"
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
        [InlineKeyboardButton("🎫 پشتیبانی", callback_data="tickets"),
         InlineKeyboardButton("❓ راهنما", callback_data="help")],
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
        [InlineKeyboardButton("🎫 تیکت‌های باز", callback_data="tkl_0"),
         InlineKeyboardButton("💾 بکاپ", callback_data="admin_backup")],
        [InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")],
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
    if new:
        conn.execute("UPDATE players SET medals=? WHERE user_id=?",
                     (json.dumps(medals + new, ensure_ascii=False), uid))
    return new


async def refresh_medals(context, uid):
    try:
        with db() as conn:
            new = check_medals(conn, uid)
    except sqlite3.Error as e:
        logger.error("medal check failed: %s", e)
        return
    for m in new:
        try:
            await context.bot.send_message(
                chat_id=uid, text=f"🏅 تبریک! مدال جدید گرفتی:\n\n✨ {m} ✨\n\nتوی دارایی‌هات می‌تونی ببینیش.")
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
    inv = get_inv(player)
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
    if inv:
        lines.append("🎒 انبار:")
        lines.extend(f"   • {k} ×{v}" for k, v in inv.items())
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
        "🤝 /trade @یوزرنیم تعداد اسم_آیتم — انتقال آیتم از انبار",
        f"🧾 از هر ترید {TRADE_TAX_PERCENT}٪ مالیات کم میشه.",
        "↩️ به‌جای @یوزرنیم می‌تونی روی پیام طرف ریپلای کنی.",
        "🏴 فکشن، 🎫 تیکت و 🏆 لیدربورد از منوی اصلی.",
    ]
    if admin:
        lines += [
            DIV, "🛠 دستورات ادمین:",
            "/admin /additem /listitems /wealth /backup /restore",
            "/newfaction نام — ساخت فکشن",
            "/flash آیدی_یا_اسم — حراجی ۲۴ ساعته | /endflash آیدی_یا_اسم",
            DIV, "↩️ ریپلای روی پیام پلیر:",
            "+200 / -200 — پول | +house / -house | repair",
            "+car اسم / -car اسم — ماشین",
            "+item اسم [تعداد] / -item اسم [تعداد] — انبار",
            "+medal متن / -medal متن — مدال دستی",
            "+faction نام / -faction — فکشن",
            "+leader / -leader — لیدر فکشن",
        ]
    return "\n".join(lines)


# ================= منوی پلیر =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    context.user_data.clear()
    user = update.effective_user
    name = user_label(user)
    with db() as conn:
        ensure_player(conn, user.id, name)
        conn.execute("UPDATE players SET username=? WHERE user_id=?", (name, user.id))
    await reply_panel(update, main_text(user), main_menu(is_admin(user.id)))


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
    keyboard, row = [], []
    for it in chunk:
        icon = "🖼 " if it["photo_id"] else ""
        label = f"{icon}{it['name']} | {price_label(it)}"
        row.append(InlineKeyboardButton(label[:48], callback_data="pick_" + str(it["id"])))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    nav = page_nav(f"cat_{category}_", page, total)
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔙 دسته‌بندی‌ها", callback_data="shop")])
    keyboard.append([InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")])
    title = cat_map().get(category, category)
    if total > 1:
        title += f"  (صفحه {page + 1} از {total})"
    await show(query, context, f"{title}\n{DIV}\nیک آیتم رو انتخاب کن 👇", InlineKeyboardMarkup(keyboard))


async def show_item(query, context, item_id):
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
            lines.append(f"💵 قیمت: 🔥 {eff_price(it):,}$  (قبلاً {it['buy_price']:,}$)")
            lines.append(f"⚡ حراجی {FLASH_DISCOUNT}٪ — {fmt_duration(left)} مونده!")
        else:
            lines.append(f"💵 قیمت: {it['buy_price']:,}$")
        buy_btn = InlineKeyboardButton(f"🛒 خرید ({eff_price(it):,}$)", callback_data="buy_" + str(it["id"]))
    markup = InlineKeyboardMarkup([
        [buy_btn],
        [InlineKeyboardButton("🔙 برگشت", callback_data=f"cat_{it['category']}_{page_of_item(it)}"),
         InlineKeyboardButton("🏠 منوی اصلی", callback_data="main_menu")],
    ])
    await show(query, context, "\n".join(lines), markup, photo=it["photo_id"])


def try_buy(uid, item_id):
    """خرید اتمیک. خروجی: (وضعیت, آیتم, موجودی, قیمت نهایی)"""
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not item or item["is_real"]:
            return "no_item", None, 0, 0
        price = eff_price(item)
        player = conn.execute("SELECT * FROM players WHERE user_id=?", (uid,)).fetchone()
        if not player:
            return "no_player", item, 0, price
        cars = get_cars(player)
        if item["category"] == "car" and len(cars) >= MAX_CARS:
            return "car_full", item, player["cash"], price
        if player["cash"] < price:
            return "poor", item, player["cash"], price
        conn.execute("UPDATE players SET cash=cash-? WHERE user_id=?", (price, uid))
        if item["category"] == "car":
            cars.append(item["name"])
            conn.execute("UPDATE players SET cars=? WHERE user_id=?", (json.dumps(cars), uid))
        else:
            inv = get_inv(player)
            inv_add(inv, item["name"], 1)
            conn.execute("UPDATE players SET inv=? WHERE user_id=?", (json.dumps(inv, ensure_ascii=False), uid))
        return "ok", item, player["cash"] - price, price


def inv_key(inv, name):
    n = (name or "").strip().lower()
    for k in inv:
        if k.lower() == n:
            return k
    return None


def inv_add(inv, name, qty):
    k = inv_key(inv, name) or name.strip()
    inv[k] = inv.get(k, 0) + qty


def inv_sub(inv, name, qty):
    k = inv_key(inv, name)
    if k is None or inv[k] < qty:
        return False
    inv[k] -= qty
    if inv[k] <= 0:
        del inv[k]
    return True


async def notify_admins(context, user, item, price=None):
    real = bool(item["is_real"])
    uname = f"@{escape(user.username)}" if user.username else "ندارد"
    if real:
        head = "💶 <b>درخواست خرید پولی!</b>"
        price_line = f"💶 قیمت: {escape(item['real_price'] or '')}"
        note = "\n\n📞 باید با این پلیر هماهنگ کنی و پیگیری کنی. (چیزی از دارایی بازی‌اش کم نشده)"
    else:
        head = "🛒 <b>سفارش جدید!</b>"
        price = eff_price(item) if price is None else price
        price_line = f"💰 قیمت: {price:,} $" + (f" (🔥 حراجی، قیمت اصلی {item['buy_price']:,}$)"
                                                 if price != item["buy_price"] else "")
        note = ""
    msg = (
        f"{head}\n{DIV}\n"
        f"👤 نام: <a href=\"tg://user?id={user.id}\">{escape(user.full_name)}</a>\n"
        f"🔗 یوزرنیم: {uname}\n"
        f"🆔 آیدی: <code>{user.id}</code>\n"
        f"📦 آیتم: {escape(item['name'])}\n"
        f"🏷️ دسته: {escape(cat_map().get(item['category'], item['category']))}\n"
        f"{price_line}\n\n"
        f"⏰ زمان: {datetime.now().strftime('%Y-%m-%d %H:%M')}{note}"
    )
    for admin in (ADMIN_ID, ADMIN_ID_2):
        try:
            if item["photo_id"]:
                await context.bot.send_photo(chat_id=admin, photo=item["photo_id"],
                                             caption=msg, parse_mode=ParseMode.HTML)
            else:
                await context.bot.send_message(chat_id=admin, text=msg, parse_mode=ParseMode.HTML)
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
    _real_cooldown[(user.id, item_id)] = now
    await notify_admins(context, user, it)
    await show(query, context,
               f"✅ درخواست خرید «{it['name']}» برای ادمین ارسال شد.\n📩 ادمین به‌زودی باهات تماس می‌گیره.",
               back_main())


async def buy_item(query, context, item_id):
    user = query.from_user
    status, item, cash, price = try_buy(user.id, item_id)

    if status == "no_item":
        await show(query, context, "❌ آیتم پیدا نشد.", back_main())
        return
    if status == "no_player":
        await show(query, context, "❌ اول /start بزن.", back_main())
        return
    if status == "car_full":
        await show(query, context, f"❌ سقف ماشین‌هات ({MAX_CARS} عدد) پر شده.\nاول یکی رو بفروش.", back_main())
        return
    if status == "poor":
        await show(query, context,
                   f"❌ پولت کمه!\n{DIV}\n💰 داری: {cash:,} $\n💵 نیاز داری: {price:,} $", back_main())
        return

    await notify_admins(context, user, item, price)
    extra = "🚗 به لیست ماشین‌هات اضافه شد." if item["category"] == "car" else \
        "🎒 به انبارت اضافه شد (با /trade می‌تونی بدیش به بقیه)."
    await show(query, context,
               f"🎉 خرید موفق!\n{DIV}\n"
               f"✅ {item['name']}\n💰 {price:,} $ کسر شد — موجودی: {cash:,} $\n{extra}\n\n"
               f"📦 برای تحویل گرفتن به ادمین سرور پیام بده.", back_main())
    await refresh_medals(context, user.id)


# ================= لیدربورد / جایزه / کار =================
async def show_leaderboard(query, context):
    with db() as conn:
        rows = conn.execute("SELECT username, cash FROM players ORDER BY cash DESC LIMIT 10").fetchall()
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
    today = datetime.now(timezone.utc).date().isoformat()
    ok = False
    with db() as conn:
        player = conn.execute("SELECT * FROM players WHERE user_id=?", (uid,)).fetchone()
        if not player:
            text = "❌ اول /start بزن."
        elif player["last_daily"] == today:
            text = "⏳ امروز جایزه‌ات رو گرفتی.\nفردا دوباره بیا! 🌙"
        else:
            reward = random.randint(DAILY_MIN, DAILY_MAX)
            conn.execute("UPDATE players SET cash=cash+?, last_daily=? WHERE user_id=?", (reward, today, uid))
            text = f"🎁 جایزه روزانه\n{DIV}\n💵 {reward} $ به حسابت اضافه شد ✅"
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
def do_trade(sender_id, target_id, amount, item_name):
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        s = conn.execute("SELECT * FROM players WHERE user_id=?", (sender_id,)).fetchone()
        t = conn.execute("SELECT * FROM players WHERE user_id=?", (target_id,)).fetchone()
        if not s:
            return False, "❌ اول /start بزن."
        if not t:
            return False, "❌ این پلیر هنوز ثبت‌نام نکرده (باید /start بزنه)."
        tax = amount * TRADE_TAX_PERCENT // 100
        got = amount - tax
        if not item_name:
            if s["cash"] < amount:
                return False, f"❌ پولت کافی نیست!\n💰 داری: {s['cash']:,} $"
            conn.execute("UPDATE players SET cash=cash-? WHERE user_id=?", (amount, sender_id))
            conn.execute("UPDATE players SET cash=cash+? WHERE user_id=?", (got, target_id))
            return True, {"kind": "money", "amount": amount, "tax": tax, "got": got}
        inv = get_inv(s)
        key = inv_key(inv, item_name)
        if key is None or inv[key] < amount:
            have = "\n".join(f"• {k} ×{v}" for k, v in inv.items()) or "• انبارت خالیه"
            return False, f"❌ به اندازه کافی «{item_name}» نداری.\n🎒 انبارت:\n{have}"
        inv_sub(inv, key, amount)
        tinv = get_inv(t)
        inv_add(tinv, key, got)
        conn.execute("UPDATE players SET inv=? WHERE user_id=?", (json.dumps(inv, ensure_ascii=False), sender_id))
        conn.execute("UPDATE players SET inv=? WHERE user_id=?", (json.dumps(tinv, ensure_ascii=False), target_id))
        return True, {"kind": "item", "name": key, "amount": amount, "tax": tax, "got": got}


TRADE_USAGE = (
    "🤝 نحوه استفاده:\n"
    "/trade @یوزرنیم 500 — انتقال پول\n"
    "/trade @یوزرنیم 3 Diamond — انتقال آیتم از انبار\n"
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
    item_name = " ".join(args[1:]).strip()

    ok, res = do_trade(user.id, target_id, amount, item_name)
    if not ok:
        await msg.reply_text(res)
        return

    if res["kind"] == "money":
        what = (f"💵 مبلغ: {res['amount']:,} $\n🧾 مالیات ({TRADE_TAX_PERCENT}٪): {res['tax']:,} $\n"
                f"✅ دریافتی: {res['got']:,} $")
    else:
        what = (f"📦 آیتم: {res['name']}\n🔢 تعداد: ×{res['amount']}\n🧾 مالیات ({TRADE_TAX_PERCENT}٪): "
                f"×{res['tax']}\n✅ دریافتی: ×{res['got']}")
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
    file_id = update.message.photo[-1].file_id
    if ud.get("step") == "photo":
        await finish_add(context, update.effective_chat.id, user.id, file_id)
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
    if ud.get("pstep"):
        await handle_player_flow(update, context)
        return
    if not is_admin(uid):
        return
    if ud.get("step") or ud.get("edit_step"):
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
            lines.append(f"🔥 در حراجی — {fmt_duration(it['sale_until'] - time.time())} مونده")
            rows.append([InlineKeyboardButton("🛑 پایان حراجی", callback_data="fe_" + i)])
        else:
            rows.append([InlineKeyboardButton(f"⚡ حراجی {FLASH_DISCOUNT}٪ ({FLASH_HOURS} ساعت)",
                                              callback_data="fs_" + i)])
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


def set_flash(item_id):
    with db() as conn:
        it = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not it:
            return "no", None
        if it["is_real"]:
            return "real", it
        if on_sale(it):
            return "already", it
        conn.execute("UPDATE items SET sale_until=? WHERE id=?", (int(time.time()) + FLASH_HOURS * 3600, item_id))
        it = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    return "ok", it


def end_flash(item_id):
    with db() as conn:
        conn.execute("UPDATE items SET sale_until=0 WHERE id=?", (item_id,))


async def announce_flash(context, it):
    """پیام Broadcast توی گپ. اگه خطا بود متنش رو برمی‌گردونه."""
    text = (
        f"🔥⚡ حراجی ناگهانی! ⚡🔥\n{DIV}\n"
        f"🔥 حراجی! قیمت {it['name']} برای {FLASH_HOURS} ساعت آینده {FLASH_DISCOUNT}٪ تخفیف خورد!\n\n"
        f"💵 قیمت قبلی: {it['buy_price']:,}$\n"
        f"🏷 قیمت جدید: {eff_price(it):,}$\n"
        f"⏳ فقط {FLASH_HOURS} ساعت فرصت داری!\n\n"
        f"🛒 برای خرید به فروشگاه ربات برو."
    )
    try:
        if it["photo_id"]:
            await context.bot.send_photo(chat_id=ANNOUNCE_CHAT_ID, photo=it["photo_id"], caption=text[:1024])
        else:
            await context.bot.send_message(chat_id=ANNOUNCE_CHAT_ID, text=text)
        return None
    except Exception as e:
        logger.error("Flash announce failed: %s", e)
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


def split_name_qty(arg):
    tokens = arg.split()
    if len(tokens) > 1:
        q = parse_amount(tokens[-1])
        if q is not None and q > 0:
            return " ".join(tokens[:-1]), q
    return arg.strip(), 1


def apply_admin_command(target, target_name, text):
    """اجرای دستور ریپلای. متن جواب رو برمی‌گردونه (یا None اگه دستور نبود)."""
    low = text.lower()
    if not (text[0] in "+-" or low in ("repair", "تعمیر")):
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
        if low in ("repair", "تعمیر", "+repair"):
            cash = conn.execute("SELECT cash FROM players WHERE user_id=?", (target,)).fetchone()["cash"]
            if cash < REPAIR_COST:
                return f"❌ پولش برای تعمیر ({REPAIR_COST} $) کافی نیست."
            conn.execute("UPDATE players SET cash=cash-? WHERE user_id=?", (REPAIR_COST, target))
            return f"🔧 هزینه تعمیر: {REPAIR_COST} $ کم شد."

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

        # ----- انبار -----
        if low.startswith(("+item ", "+آیتم ")):
            name, qty = split_name_qty(arg)
            if not name:
                return "❌ مثال: +item Diamond 5"
            inv = get_inv(conn.execute("SELECT inv FROM players WHERE user_id=?", (target,)).fetchone())
            inv_add(inv, name, qty)
            conn.execute("UPDATE players SET inv=? WHERE user_id=?",
                         (json.dumps(inv, ensure_ascii=False), target))
            return f"✅ {qty}× {name} به انبارش اضافه شد."
        if low.startswith(("-item ", "-آیتم ")):
            name, qty = split_name_qty(arg)
            inv = get_inv(conn.execute("SELECT inv FROM players WHERE user_id=?", (target,)).fetchone())
            if not inv_sub(inv, name, qty):
                have = "\n".join(f"• {k} ×{v}" for k, v in inv.items()) or "• انبارش خالیه"
                return f"❌ به اندازه کافی «{name}» نداره.\nانبارش:\n{have}"
            conn.execute("UPDATE players SET inv=? WHERE user_id=?",
                         (json.dumps(inv, ensure_ascii=False), target))
            return f"✅ {qty}× {name} از انبارش کم شد."

        # ----- مدال -----
        if low.startswith(("+medal ", "+مدال ")):
            if not arg or len(arg) > 30:
                return "❌ متن مدال باید بین ۱ تا ۳۰ حرف باشه. مثال: +medal 🏆 قهرمان"
            row = conn.execute("SELECT medals FROM players WHERE user_id=?", (target,)).fetchone()
            medals = get_medals(row)
            if arg in medals:
                return "⚠️ این مدال رو از قبل داره."
            medals.append(arg)
            conn.execute("UPDATE players SET medals=? WHERE user_id=?",
                         (json.dumps(medals, ensure_ascii=False), target))
            return f"🏅 مدال «{arg}» داده شد."
        if low.startswith(("-medal ", "-مدال ")):
            row = conn.execute("SELECT medals FROM players WHERE user_id=?", (target,)).fetchone()
            medals = get_medals(row)
            if arg not in medals:
                return "❌ این مدال رو نداره.\nمدال‌هاش: " + (" | ".join(medals) or "—")
            medals.remove(arg)
            conn.execute("UPDATE players SET medals=? WHERE user_id=?",
                         (json.dumps(medals, ensure_ascii=False), target))
            return f"✅ مدال «{arg}» گرفته شد."

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
    if context.user_data:
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
    if not data.startswith(("addtype_", "addcat_", "add_skip_photo")):
        context.user_data.clear()

    # ----- پلیر -----
    if data == "main_menu":
        await show(q, context, main_text(q.from_user), main_menu(admin))
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

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
