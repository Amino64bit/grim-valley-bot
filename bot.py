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
BOT_TOKEN = "8725147253:AAGyHLZQOED6YFCij-cV-_XrY9fgdFgczXk"
ADMIN_ID = 5591238725
ADMIN_ID_2 = 6270844351
DB_FILE = "grim_valley.db"
MAX_CARS = 3
HOUSE_VALUE = 2500
REPAIR_COST = 400

# ضد اسپم: بیشتر از RATE_MAX عمل در RATE_WINDOW ثانیه => RATE_BLOCK ثانیه بلاک
RATE_WINDOW = 10
RATE_MAX = 8
RATE_BLOCK = 30

CATEGORIES = {
    "car": "🚗 خودرو",
    "resource": "💎 منابع",
    "attachment": "📦 باربند",
}
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
        # مالک هر منو (برای اینکه فقط کسی که منو رو باز کرده بتونه ازش استفاده کنه)
        conn.execute("""CREATE TABLE IF NOT EXISTS panels (
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            owner_id INTEGER NOT NULL,
            created INTEGER NOT NULL,
            PRIMARY KEY (chat_id, message_id)
        )""")
        # مهاجرت: اضافه کردن ستون عکس به دیتابیس‌های قدیمی
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
        if "photo_id" not in cols:
            conn.execute("ALTER TABLE items ADD COLUMN photo_id TEXT")
        if "is_real" not in cols:
            conn.execute("ALTER TABLE items ADD COLUMN is_real INTEGER NOT NULL DEFAULT 0")
        if "real_price" not in cols:
            conn.execute("ALTER TABLE items ADD COLUMN real_price TEXT")
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


def get_cars(player):
    try:
        data = json.loads(player["cars"] or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return []


def ensure_player(conn, uid, username="player"):
    conn.execute("INSERT OR IGNORE INTO players (user_id, username) VALUES (?,?)", (uid, username))


def user_label(user):
    return user.username or user.first_name or "پلیر"


def price_label(it):
    if it["is_real"]:
        return f"💶 {it['real_price']}"
    return f"{it['buy_price']:,}$"


# ---------- کیبوردها ----------
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💼 دارایی‌های من", callback_data="my_assets")],
        [InlineKeyboardButton("🛒 فروشگاه", callback_data="shop")],
        [InlineKeyboardButton("🏆 لیدربورد", callback_data="leaderboard")],
        [InlineKeyboardButton("🎁 جایزه روزانه", callback_data="daily")],
    ])


def back_main():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")]])


def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ افزودن آیتم", callback_data="admin_add")],
        [InlineKeyboardButton("📋 لیست و ویرایش آیتم‌ها", callback_data="admin_list")],
        [InlineKeyboardButton("💾 گرفتن بکاپ", callback_data="admin_backup")],
        [InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")],
    ])


def cancel_markup():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="cancel_flow")]])


def category_markup():
    kb = [[InlineKeyboardButton(label, callback_data="addcat_" + key)] for key, label in CATEGORIES.items()]
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


# ================= منوی پلیر =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    uid = update.effective_user.id
    name = user_label(update.effective_user)
    with db() as conn:
        ensure_player(conn, uid, name)
        conn.execute("UPDATE players SET username=? WHERE user_id=?", (name, uid))
    await reply_panel(update, f"🎮 به تریدر (Trader) خوش آمدی {name}!\n\nاز منوی زیر استفاده کن:", main_menu())


def asset_text(conn, player):
    cars = get_cars(player)
    car_value = 0
    for name in cars:
        row = conn.execute(
            "SELECT buy_price FROM items WHERE name=? AND category='car' AND is_real=0 ORDER BY id LIMIT 1", (name,)
        ).fetchone()
        if row:
            car_value += row["buy_price"]
    house_value = HOUSE_VALUE if player["has_house"] else 0
    car_lines = "\n".join(f"   • {c}" for c in cars) if cars else "   • هیچ ماشینی نداری"
    total = player["cash"] + car_value + house_value
    return (
        f"💼 دارایی‌های {player['username'] or 'پلیر'}:\n\n"
        f"💰 پول نقد: {player['cash']:,} $\n"
        f"🏠 خانه: {'✅ دارد' if player['has_house'] else '❌ ندارد'}\n"
        f"🚗 ماشین‌ها ({len(cars)}/{MAX_CARS}):\n{car_lines}\n\n"
        f"🚙 ارزش ماشین‌ها: {car_value:,} $\n"
        f"🏠 ارزش خانه: {house_value:,} $\n"
        f"💎 کل دارایی: {total:,} $"
    )


async def show_assets(query, context):
    with db() as conn:
        player = conn.execute("SELECT * FROM players WHERE user_id=?", (query.from_user.id,)).fetchone()
        text = asset_text(conn, player) if player else "❌ اول /start بزن تا ثبت‌نام بشی."
    await show(query, context, text, back_main())


async def show_shop(query, context):
    with db() as conn:
        rows = conn.execute("SELECT DISTINCT category FROM items ORDER BY category").fetchall()
    if not rows:
        await show(query, context, "🛒 فروشگاه فعلاً خالی است.", back_main())
        return
    keyboard = [[InlineKeyboardButton(CATEGORIES.get(r["category"], r["category"]),
                                     callback_data="cat_" + r["category"])] for r in rows]
    keyboard.append([InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")])
    await show(query, context, "🛒 فروشگاه تریدر\n\nیک دسته رو انتخاب کن:", InlineKeyboardMarkup(keyboard))


async def show_category(query, context, category):
    with db() as conn:
        items = conn.execute("SELECT * FROM items WHERE category=? ORDER BY id", (category,)).fetchall()
    if not items:
        await show(query, context, "📦 این دسته فعلاً خالی است.", back_main())
        return
    keyboard, row = [], []
    for it in items:
        icon = "🖼 " if it["photo_id"] else ""
        label = f"{icon}{it['name']} | {price_label(it)}"
        row.append(InlineKeyboardButton(label[:48], callback_data="pick_" + str(it["id"])))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 دسته‌بندی‌ها", callback_data="shop")])
    keyboard.append([InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")])
    await show(query, context, f"{CATEGORIES.get(category, category)}\n\nیک آیتم رو انتخاب کن:",
               InlineKeyboardMarkup(keyboard))


async def show_item(query, context, item_id):
    with db() as conn:
        it = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not it:
        await show(query, context, "❌ آیتم پیدا نشد.", back_main())
        return
    text = (
        f"📛 {it['name']}\n"
        f"🏷️ دسته: {CATEGORIES.get(it['category'], it['category'])}\n"
        f"💵 قیمت: {price_label(it)}"
    )
    if it["is_real"]:
        text += "\n\n💶 این آیتم با پول واقعیه و به دارایی بازی‌ات ربطی نداره. ادمین باهات هماهنگ می‌کنه."
        buy_btn = InlineKeyboardButton("💶 درخواست خرید", callback_data="rq_" + str(it["id"]))
    else:
        buy_btn = InlineKeyboardButton(f"🛒 خرید ({it['buy_price']:,}$)", callback_data="buy_" + str(it["id"]))
    markup = InlineKeyboardMarkup([
        [buy_btn],
        [InlineKeyboardButton("🔙 برگشت", callback_data="cat_" + it["category"]),
         InlineKeyboardButton("🏠 منوی اصلی", callback_data="main_menu")],
    ])
    await show(query, context, text, markup, photo=it["photo_id"])


def try_buy(uid, item_id):
    """خرید اتمیک. خروجی: (وضعیت, آیتم, موجودی)"""
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not item or item["is_real"]:
            return "no_item", None, 0
        player = conn.execute("SELECT * FROM players WHERE user_id=?", (uid,)).fetchone()
        if not player:
            return "no_player", item, 0
        cars = get_cars(player)
        if item["category"] == "car" and len(cars) >= MAX_CARS:
            return "car_full", item, player["cash"]
        if player["cash"] < item["buy_price"]:
            return "poor", item, player["cash"]
        conn.execute("UPDATE players SET cash=cash-? WHERE user_id=?", (item["buy_price"], uid))
        if item["category"] == "car":
            cars.append(item["name"])
            conn.execute("UPDATE players SET cars=? WHERE user_id=?", (json.dumps(cars), uid))
        return "ok", item, player["cash"] - item["buy_price"]


async def notify_admins(context, user, item):
    real = bool(item["is_real"])
    uname = f"@{escape(user.username)}" if user.username else "ندارد"
    if real:
        head = "💶 <b>درخواست خرید پولی!</b>"
        price = f"💶 قیمت: {escape(item['real_price'] or '')}"
        note = "\n\n📞 باید با این پلیر هماهنگ کنی و پیگیری کنی. (چیزی از دارایی بازی‌اش کم نشده)"
    else:
        head = "🛒 <b>سفارش جدید!</b>"
        price = f"💰 قیمت: {item['buy_price']:,} $"
        note = ""
    msg = (
        f"{head}\n\n"
        f"👤 نام: <a href=\"tg://user?id={user.id}\">{escape(user.full_name)}</a>\n"
        f"🔗 یوزرنیم: {uname}\n"
        f"🆔 آیدی: <code>{user.id}</code>\n"
        f"📦 آیتم: {escape(item['name'])}\n"
        f"🏷️ دسته: {CATEGORIES.get(item['category'], item['category'])}\n"
        f"{price}\n\n"
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
REAL_COOLDOWN = 120  # ثانیه؛ جلوگیری از درخواست تکراری برای یک آیتم


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
               f"💶 می‌خوای «{it['name']}» رو به قیمت {it['real_price']} بخری؟\n\n"
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
               f"✅ درخواست خرید «{it['name']}» برای ادمین ارسال شد.\n"
               f"📩 ادمین به‌زودی باهات تماس می‌گیره.", back_main())


async def buy_item(query, context, item_id):
    user = query.from_user
    status, item, cash = try_buy(user.id, item_id)

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
                   f"❌ پولت کمه!\n💰 داری: {cash:,} $\n💵 نیاز داری: {item['buy_price']:,} $", back_main())
        return

    await notify_admins(context, user, item)

    await show(query, context,
               f"✅ {item['name']} خریدی!\n💰 {item['buy_price']:,} $ کسر شد.\n"
               f"📦 برای تحویل گرفتن به ادمین سرور پیام بده.", back_main())


async def show_leaderboard(query, context):
    with db() as conn:
        rows = conn.execute("SELECT username, cash FROM players ORDER BY cash DESC LIMIT 10").fetchall()
    if not rows:
        text = "🏆 هنوز کسی در لیدربورد نیست."
    else:
        text = "🏆 ثروتمندترین‌های گریم ولی:\n\n" + "\n".join(
            f"{i}. {r['username'] or 'پلیر'} — {r['cash']:,} $" for i, r in enumerate(rows, 1)
        )
    await show(query, context, text, back_main())


async def give_daily(query, context):
    uid = query.from_user.id
    today = datetime.now(timezone.utc).date().isoformat()
    with db() as conn:
        player = conn.execute("SELECT * FROM players WHERE user_id=?", (uid,)).fetchone()
        if not player:
            text = "❌ اول /start بزن."
        elif player["last_daily"] == today:
            text = "❌ امروز جایزه‌ات رو گرفتی.\nفردا دوباره بیا!"
        else:
            reward = random.randint(20, 40)
            conn.execute("UPDATE players SET cash=cash+?, last_daily=? WHERE user_id=?", (reward, today, uid))
            text = f"🎁 جایزه روزانه: {reward} $\n✅ به حسابت اضافه شد."
    await show(query, context, text, back_main())


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
    text = (f"✅ آیتم ثبت شد!\n\n🆔 کد: {new_id}\n📛 اسم: {name}\n"
            f"🏷️ دسته: {CATEGORIES[category]}\n"
            f"💵 قیمت: {('💶 ' + real_price + ' (پولی)') if real_price else format(buy, ',') + ' $'}\n🖼 عکس: {'دارد' if photo_id else 'ندارد'}")
    await send_panel(context, chat_id, owner_id, text, markup, photo=photo_id)


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
                    await reply_panel(update, "❌ قیمت رو با واحد بنویس (حداکثر ۳۰ حرف). مثال: 20 یورو", cancel_markup())
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


def item_back_markup(item_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 صفحه آیتم", callback_data="item_" + str(item_id)),
         InlineKeyboardButton("📋 لیست", callback_data="admin_list")],
    ])


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
    """تنها هندلر متن: اول فلو افزودن/ویرایش، بعد دستورات ریپلای ادمین."""
    if not update.message or not update.message.text or not is_admin(update.effective_user.id):
        return
    if context.user_data.get("step") or context.user_data.get("edit_step"):
        await handle_admin_flow(update, context)
    elif update.message.reply_to_message:
        await admin_reply(update, context)


# ================= پنل ادمین: لیست / جزئیات / حذف =================
def build_item_list(items):
    keyboard, row = [], []
    for it in items:
        label = f"{it['name']} | {price_label(it)}"
        row.append(InlineKeyboardButton(label[:45], callback_data="item_" + str(it["id"])))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("➕ افزودن آیتم", callback_data="admin_add")])
    keyboard.append([InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")])
    return "📋 لیست آیتم‌ها — برای ویرایش روی یک آیتم بزن:", InlineKeyboardMarkup(keyboard)


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


async def show_admin_list(query, context):
    with db() as conn:
        items = conn.execute("SELECT * FROM items ORDER BY id").fetchall()
    if not items:
        await show(query, context, "📋 فروشگاه خالی است.\nبرای افزودن دکمه زیر رو بزن.", admin_menu())
        return
    text, markup = build_item_list(items)
    await show(query, context, text, markup)


async def show_item_detail(query, context, item_id):
    with db() as conn:
        it = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not it:
        await show(query, context, "❌ آیتم پیدا نشد.", admin_menu())
        return
    text = (
        f"🆔 کد: {it['id']}\n"
        f"📛 اسم: {it['name']}\n"
        f"🏷️ دسته: {CATEGORIES.get(it['category'], it['category'])}\n"
        f"📌 نوع: {'پولی (پول واقعی)' if it['is_real'] else 'بازی'}\n"
        f"💵 قیمت: {price_label(it)}\n"
        f"🖼 عکس: {'دارد' if it['photo_id'] else 'ندارد'}"
    )
    i = str(it["id"])
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ قیمت", callback_data="eb_" + i),
         InlineKeyboardButton("✏️ اسم", callback_data="en_" + i)],
        [InlineKeyboardButton("🖼 تغییر عکس", callback_data="ep_" + i)],
        [InlineKeyboardButton("🗑️ حذف آیتم", callback_data="del_" + i)],
        [InlineKeyboardButton("📋 لیست", callback_data="admin_list"),
         InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")],
    ])
    await show(query, context, text, markup, photo=it["photo_id"])


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


# ================= دستورات ادمین با ریپلای =================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ دسترسی غیرمجاز.")
        return
    await reply_panel(update, "🛠️ پنل مدیریت تریدر:", admin_menu())


def apply_admin_command(target, target_name, text):
    """اجرای دستور ریپلای. متن جواب رو برمی‌گردونه (یا None اگه دستور نبود)."""
    low = text.lower()
    if not (text[0] in "+-" or low in ("repair", "تعمیر")):
        return None

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
            name = text.split(" ", 1)[1].strip()
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
            parts = text.split(" ", 1)
            name = parts[1].strip() if len(parts) > 1 else ""
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
    result = apply_admin_command(replied.from_user.id, user_label(replied.from_user), text)
    if result:
        await msg.reply_text(result)


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
        init_db()  # اگه بکاپ قدیمی بود، ستون‌های جدید اضافه میشه
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

    if data == "main_menu":
        await show(q, context, "🎮 تریدر | منوی اصلی", main_menu())
    elif data == "my_assets":
        await show_assets(q, context)
    elif data == "shop":
        await show_shop(q, context)
    elif data == "leaderboard":
        await show_leaderboard(q, context)
    elif data == "daily":
        await give_daily(q, context)
    elif data.startswith("cat_"):
        await show_category(q, context, data[4:])
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
    elif data == "cancel_flow":
        context.user_data.clear()
        await show(q, context, "❌ عملیات لغو شد.\n\n🛠️ پنل مدیریت تریدر:", admin_menu())
    elif data == "admin_backup":
        await send_backup(msg.chat_id, context)
    elif data == "admin_list":
        await show_admin_list(q, context)
    elif data == "admin_add":
        context.user_data.clear()
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
        if context.user_data.get("step") == "category" and cat in CATEGORIES:
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
        context.user_data.clear()
        context.user_data.update(edit_step="edit_price", edit_id=num)
        await show(q, context, "💵 قیمت جدید رو فقط عدد بنویس:", cancel_markup())
    elif data.startswith("en_") and num is not None:
        context.user_data.clear()
        context.user_data.update(edit_step="edit_name", edit_id=num)
        await show(q, context, "✏️ اسم جدید آیتم رو بنویس:", cancel_markup())
    elif data.startswith("ep_") and num is not None:
        context.user_data.clear()
        context.user_data.update(edit_step="edit_photo", edit_id=num)
        await show(q, context, "🖼 عکس جدید آیتم رو بفرست:", cancel_markup())


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
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("additem", additem_start))
    app.add_handler(CommandHandler("listitems", cmd_list_items))
    app.add_handler(CommandHandler("wealth", wealth_command))
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
