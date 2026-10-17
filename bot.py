import os
import io
import json
import random
import sqlite3
import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ================= تنظیمات =================
BOT_TOKEN = "8725147253:AAEV-vZc1bNzS-Gk4HYDGLpQtdQagsvtEdg"
ADMIN_ID = 5591238725
DB_FILE = "grim_valley.db"
MAX_CARS = 4
HOUSE_VALUE = 2500
REPAIR_COST = 400

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
            sell_price INTEGER NOT NULL,
            image_url TEXT
        )""")


# ================= ابزارها =================
def is_admin(uid):
    return uid == ADMIN_ID

def normalize_digits(text):
    text = str(text)
    for i in range(10):
        text = text.replace("۰۱۲۳۴۵۶۷۸۹"[i], str(i))
        text = text.replace("٠١٢٤٥٦٧٨٩"[i], str(i))
    return text

def parse_amount(text):
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

async def safe_delete(message):
    try:
        await message.delete()
    except TelegramError:
        pass

async def replace_message(query, context, text, markup=None, photo=None):
    """پیام قبلی رو حذف می‌کنه و پیام جدید (با عکس در صورت وجود) می‌فرستد."""
    chat_id = query.message.chat_id
    await safe_delete(query.message)
    if photo:
        try:
            await context.bot.send_photo(
                chat_id=chat_id, photo=photo, caption=text, reply_markup=markup
            )
            return
        except TelegramError:
            pass
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)


# ================= منوی پلیر =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    uid = update.effective_user.id
    name = update.effective_user.username or "player"
    with db() as conn:
        ensure_player(conn, uid, name)
        conn.execute("UPDATE players SET username=? WHERE user_id=?", (name, uid))
    await update.message.reply_text(
        f"🎮 به تریدر (Trader) خوش آمدی {name}!\n\nاز منوی زیر استفاده کن:",
        reply_markup=main_menu()
    )

def asset_text(conn, player):
    cars = get_cars(player)
    car_value = 0
    for name in cars:
        row = conn.execute(
            "SELECT buy_price FROM items WHERE name=? AND category='car' ORDER BY id LIMIT 1",
            (name,)
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
    uid = query.from_user.id
    with db() as conn:
        player = conn.execute("SELECT * FROM players WHERE user_id=?", (uid,)).fetchone()
        text = asset_text(conn, player) if player else "❌ اول /start بزن تا ثبت‌نام بشی."
    await replace_message(query, context, text, back_main())

async def show_shop(query, context):
    with db() as conn:
        rows = conn.execute("SELECT DISTINCT category FROM items ORDER BY category").fetchall()
    if not rows:
        await replace_message(query, context, "🛒 فروشگاه فعلاً خالی است.", back_main())
        return
    keyboard = [[InlineKeyboardButton(CATEGORIES.get(r["category"], r["category"]),
                                     callback_data="cat_" + r["category"])] for r in rows]
    keyboard.append([InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")])
    await replace_message(query, context, "🛒 فروشگاه تریدر\n\nیک دسته رو انتخاب کن:",
                          InlineKeyboardMarkup(keyboard))

async def show_category(query, context, category):
    with db() as conn:
        items = conn.execute("SELECT * FROM items WHERE category=? ORDER BY id", (category,)).fetchall()
    if not items:
        await replace_message(query, context, "📦 این دسته فعلاً خالی است.", back_main())
        return
    keyboard = []
    row = []
    for it in items:
        label = f"{it['name']} | {it['buy_price']:,}$"
        row.append(InlineKeyboardButton(label[:48], callback_data="buy_" + str(it["id"])))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 دسته‌بندی‌ها", callback_data="shop")])
    keyboard.append([InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")])
    await replace_message(query, context,
                          f"{CATEGORIES.get(category, category)}\n\nبرای خرید روی آیتم بزن:",
                          InlineKeyboardMarkup(keyboard))

async def buy_item(query, context, item_id):
    uid = query.from_user.id
    with db() as conn:
        item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not item:
            await replace_message(query, context, "❌ آیتم پیدا نشد.", back_main())
            return
        player = conn.execute("SELECT * FROM players WHERE user_id=?", (uid,)).fetchone()
        if not player:
            await replace_message(query, context, "❌ اول /start بزن.", back_main())
            return
        cars = get_cars(player)
        if item["category"] == "car" and len(cars) >= MAX_CARS:
            await replace_message(query, context,
                                  f"❌ سقف ماشین‌هات ({MAX_CARS} عدد) پر شده.\nاول یکی رو بفروش.",
                                  back_main(), photo=item["image_url"])
            return
        if player["cash"] < item["buy_price"]:
            await replace_message(query, context,
                                  f"❌ پولت کمه!\n💰 داری: {player['cash']:,} $\n"
                                  f"💵 نیاز داری: {item['buy_price']:,} $",
                                  back_main(), photo=item["image_url"])
            return
        # تراکنش اتمیک: کم کردن پول و اضافه کردن آیتم در یک عملیات
        cur = conn.execute(
            "UPDATE players SET cash=cash-? WHERE user_id=? AND cash>=?",
            (item["buy_price"], uid, item["buy_price"])
        )
        if cur.rowcount != 1:
            await replace_message(query, context, "❌ موجودی کافی نیست.", back_main())
            return
        if item["category"] == "car":
            cars.append(item["name"])
            conn.execute("UPDATE players SET cars=? WHERE user_id=?", (json.dumps(cars), uid))
    await replace_message(
        query, context,
        f"✅ {item['name']} خریدی!\n💰 {item['buy_price']:,} $ کسر شد.\n"
        f"📦 برای تحویل گرفتن به ادمین سرور پیام بده.",
        back_main(), photo=item["image_url"]
    )

async def show_leaderboard(query, context):
    with db() as conn:
        rows = conn.execute("SELECT username, cash FROM players ORDER BY cash DESC LIMIT 10").fetchall()
    if not rows:
        text = "🏆 هنوز کسی در لیدربورد نیست."
    else:
        text = "🏆 ثروتمندترین‌های گریم ولی:\n\n" + "\n".join(
            f"{i}. {r['username'] or 'پلیر'} — {r['cash']:,} $" for i, r in enumerate(rows, 1)
        )
    await replace_message(query, context, text, back_main())

async def give_daily(query, context):
    uid = query.from_user.id
    today = datetime.now(timezone.utc).date().isoformat()
    with db() as conn:
        player = conn.execute("SELECT * FROM players WHERE user_id=?", (uid,)).fetchone()
        if not player:
            await replace_message(query, context, "❌ اول /start بزن.", back_main())
            return
        if player["last_daily"] == today:
            text = "❌ امروز جایزه‌ات رو گرفتی.\nفردا دوباره بیا!"
        else:
            reward = random.randint(20, 40)
            conn.execute("UPDATE players SET cash=cash+?, last_daily=? WHERE user_id=?",
                         (reward, today, uid))
            text = f"🎁 جایزه روزانه: {reward} $\n✅ به حسابت اضافه شد."
    await replace_message(query, context, text, back_main())


# ================= افزودن آیتم (مکالمه) =================
ADD_PHOTO, ADD_NAME, ADD_BUY, ADD_SELL, ADD_CATEGORY = range(5)

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        if update.callback_query:
            await update.callback_query.answer("❌ فقط ادمین.", show_alert=True)
        return ConversationHandler.END

    context.user_data.pop("add", None)

    if update.callback_query:
        q = update.callback_query
        await q.answer()
        await replace_message(q, context, "📸 عکس آیتم رو بفرست.\n(لغو با /cancel)")
        return ADD_PHOTO

    msg = update.message
    if msg.reply_to_message and msg.reply_to_message.photo:
        context.user_data["add"] = {"image": msg.reply_to_message.photo[-1].file_id}
        await msg.reply_text("✏️ اسم آیتم رو بنویس:")
        return ADD_NAME

    await msg.reply_text("📸 عکس آیتم رو بفرست.\n(لغو با /cancel)")
    return ADD_PHOTO

async def add_photo_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    context.user_data["add"] = {"image": update.message.photo[-1].file_id}
    await update.message.reply_text("✏️ اسم آیتم رو بنویس:\nمثال: Chevy Step Van")
    return ADD_NAME

async def add_name_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data.get("add")
    if not data:
        await update.message.reply_text("❌ دوباره /additem بزن.")
        return ConversationHandler.END
    name = update.message.text.strip()
    if not name or len(name) > 60:
        await update.message.reply_text("❌ یه اسم معتبر (حداکثر ۶۰ حرف) بنویس.")
        return ADD_NAME
    data["name"] = name
    await update.message.reply_text("💵 قیمت خرید رو فقط عدد بنویس (دلار):")
    return ADD_BUY

async def add_buy_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data.get("add")
    if not data:
        return ConversationHandler.END
    value = parse_amount(update.message.text)
    if value is None or value <= 0:
        await update.message.reply_text("❌ یه عدد مثبت بنویس. مثال: 1500")
        return ADD_BUY
    data["buy"] = value
    await update.message.reply_text("♻️ قیمت فروش/اسقاط رو فقط عدد بنویس (دلار):")
    return ADD_SELL

async def add_sell_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data.get("add")
    if not data:
        return ConversationHandler.END
    value = parse_amount(update.message.text)
    if value is None or value < 0:
        await update.message.reply_text("❌ یه عدد معتبر بنویس. مثال: 600")
        return ADD_SELL
    data["sell"] = value
    await update.message.reply_text(
        "🏷️ دسته‌بندی رو بنویس (یکی از این سه تا):\n\n"
        "car  → خودرو\n"
        "resource  → منابع (طلا، الماس...)\n"
        "attachment  → باربند"
    )
    return ADD_CATEGORY

async def add_category_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data.get("add")
    if not data:
        return ConversationHandler.END
    raw = update.message.text.strip().lower()
    mapping = {
        "car": "car", "خودرو": "car", "ماشین": "car",
        "resource": "resource", "منبع": "resource", "منابع": "resource",
        "attachment": "attachment", "باربند": "attachment",
    }
    category = mapping.get(raw)
    if not category:
        await update.message.reply_text("❌ فقط car یا resource یا attachment بنویس.")
        return ADD_CATEGORY

    with db() as conn:
        cur = conn.execute(
            "INSERT INTO items (name, category, buy_price, sell_price, image_url) VALUES (?,?,?,?,?)",
            (data["name"], category, data["buy"], data["sell"], data.get("image"))
        )
        new_id = cur.lastrowid
    context.user_data.pop("add", None)

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 لیست آیتم‌ها", callback_data="admin_list")],
        [InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")],
    ])
    caption = (
        f"✅ آیتم ثبت شد!\n\n"
        f"🆔 کد: {new_id}\n"
        f"📛 اسم: {data['name']}\n"
        f"🏷️ دسته: {CATEGORIES[category]}\n"
        f"💵 خرید: {data['buy']:,} $\n"
        f"♻️ فروش: {data['sell']:,} $"
    )
    if data.get("image"):
        await update.message.reply_photo(photo=data["image"], caption=caption, reply_markup=markup)
    else:
        await update.message.reply_text(caption, reply_markup=markup)
    return ConversationHandler.END


# ================= ویرایش آیتم (مکالمه) =================
EDIT_PRICE, EDIT_NAME, EDIT_PHOTO = range(1, 4)

async def edit_price_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("❌ فقط ادمین.", show_alert=True)
        return ConversationHandler.END
    await q.answer()
    parts = q.data.split("_")          # eb_3  /  es_3
    field = "buy_price" if parts[0] == "eb" else "sell_price"
    context.user_data["edit"] = {"id": int(parts[1]), "field": field}
    label = "قیمت خرید" if field == "buy_price" else "قیمت فروش"
    await replace_message(q, context, f"💵 {label} جدید رو فقط عدد بنویس:\n(لغو با /cancel)")
    return EDIT_PRICE

async def edit_price_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("edit")
    if not state:
        await update.message.reply_text("❌ دوباره از لیست آیتم رو انتخاب کن.")
        return ConversationHandler.END
    value = parse_amount(update.message.text)
    if value is None or value < 0:
        await update.message.reply_text("❌ یه عدد معتبر بنویس.")
        return EDIT_PRICE
    with db() as conn:
        conn.execute(f"UPDATE items SET {state['field']}=? WHERE id=?", (value, state["id"]))
    context.user_data.pop("edit", None)
    await update.message.reply_text(f"✅ قیمت به {value:,} $ تغییر کرد.")
    return ConversationHandler.END

async def edit_name_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("❌ فقط ادمین.", show_alert=True)
        return ConversationHandler.END
    await q.answer()
    context.user_data["edit"] = {"id": int(q.data.split("_")[1]), "field": "name"}
    await replace_message(q, context, "✏️ اسم جدید آیتم رو بنویس:\n(لغو با /cancel)")
    return EDIT_NAME

async def edit_name_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("edit")
    if not state:
        return ConversationHandler.END
    name = update.message.text.strip()
    if not name or len(name) > 60:
        await update.message.reply_text("❌ اسم معتبر بنویس (حداکثر ۶۰ حرف).")
        return EDIT_NAME
    with db() as conn:
        conn.execute("UPDATE items SET name=? WHERE id=?", (name, state["id"]))
    context.user_data.pop("edit", None)
    await update.message.reply_text(f"✅ اسم به «{name}» تغییر کرد.")
    return ConversationHandler.END

async def edit_photo_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("❌ فقط ادمین.", show_alert=True)
        return ConversationHandler.END
    await q.answer()
    context.user_data["edit"] = {"id": int(q.data.split("_")[1]), "field": "image"}
    await replace_message(q, context, "📸 عکس جدید رو بفرست:\n(لغو با /cancel)")
    return EDIT_PHOTO

async def edit_photo_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("edit")
    if not state:
        return ConversationHandler.END
    file_id = update.message.photo[-1].file_id
    with db() as conn:
        conn.execute("UPDATE items SET image_url=? WHERE id=?", (file_id, state["id"]))
    context.user_data.pop("edit", None)
    await update.message.reply_text("✅ عکس جایگزین شد.")
    return ConversationHandler.END

async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("add", None)
    context.user_data.pop("edit", None)
    if update.effective_message:
        await update.effective_message.reply_text("❌ لغو شد.")
    return ConversationHandler.END


# ================= پنل ادمین: لیست / جزئیات / حذف =================
def build_item_list(items):
    keyboard, row = [], []
    for it in items:
        label = f"{it['name']} | {it['buy_price']:,}$"
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
        await update.message.reply_text("📋 فروشگاه خالی است.\nبا /additem آیتم اضافه کن.")
        return
    text, markup = build_item_list(items)
    await update.message.reply_text(text, reply_markup=markup)

async def show_admin_list(query, context):
    with db() as conn:
        items = conn.execute("SELECT * FROM items ORDER BY id").fetchall()
    if not items:
        await replace_message(query, context,
                              "📋 فروشگاه خالی است.\nبرای افزودن دکمه زیر رو بزن.", admin_menu())
        return
    text, markup = build_item_list(items)
    await replace_message(query, context, text, markup)

async def show_item_detail(query, context, item_id):
    with db() as conn:
        it = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not it:
        await replace_message(query, context, "❌ آیتم پیدا نشد.", admin_menu())
        return
    text = (
        f"🆔 کد: {it['id']}\n"
        f"📛 اسم: {it['name']}\n"
        f"🏷️ دسته: {CATEGORIES.get(it['category'], it['category'])}\n"
        f"💵 قیمت خرید: {it['buy_price']:,} $\n"
        f"♻️ قیمت فروش: {it['sell_price']:,} $"
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ قیمت خرید", callback_data="eb_" + str(it["id"])),
         InlineKeyboardButton("✏️ قیمت فروش", callback_data="es_" + str(it["id"]))],
        [InlineKeyboardButton("✏️ اسم", callback_data="en_" + str(it["id"])),
         InlineKeyboardButton("🖼️ عوض کردن عکس", callback_data="ep_" + str(it["id"]))],
        [InlineKeyboardButton("🗑️ حذف آیتم", callback_data="del_" + str(it["id"]))],
        [InlineKeyboardButton("📋 لیست", callback_data="admin_list"),
         InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")],
    ])
    await replace_message(query, context, text, markup, photo=it["image_url"])

async def confirm_delete(query, context, item_id):
    with db() as conn:
        it = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not it:
        await replace_message(query, context, "❌ آیتم پیدا نشد.", admin_menu())
        return
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ بله، حذف کن", callback_data="delyes_" + str(item_id)),
         InlineKeyboardButton("❌ نه", callback_data="item_" + str(item_id))],
    ])
    await replace_message(query, context,
                          f"⚠️ مطمئنی «{it['name']}» حذف بشه؟\nاین کار برگشت‌پذیر نیست.", markup)

async def do_delete(query, context, item_id):
    with db() as conn:
        conn.execute("DELETE FROM items WHERE id=?", (item_id,))
    await replace_message(query, context, "🗑️ آیتم حذف شد.", admin_menu())


# ================= دستورات ادمین با ریپلای =================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ دسترسی غیرمجاز.")
        return
    await update.message.reply_text("🛠️ پنل مدیریت تریدر:", reply_markup=admin_menu())

async def admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    msg = update.message
    if not msg.reply_to_message:
        return
    target = msg.reply_to_message.from_user.id
    target_name = msg.reply_to_message.from_user.username or "player"
    text = msg.text.strip()
    low = text.lower()

    with db() as conn:
        ensure_player(conn, target, target_name)

        # خانه
        if low in ("+house", "+خونه", "+خانه"):
            conn.execute("UPDATE players SET has_house=1 WHERE user_id=?", (target,))
            await msg.reply_text("✅ خانه ثبت شد.")
            return
        if low in ("-house", "-خونه", "-خانه"):
            conn.execute("UPDATE players SET has_house=0 WHERE user_id=?", (target,))
            await msg.reply_text("✅ خانه حذف شد.")
            return

        # تعمیر
        if low in ("repair", "تعمیر", "+repair"):
            player = conn.execute("SELECT cash FROM players WHERE user_id=?", (target,)).fetchone()
            if player["cash"] < REPAIR_COST:
                await msg.reply_text(f"❌ پولش برای تعمیر ({REPAIR_COST} $) کافی نیست.")
                return
            conn.execute("UPDATE players SET cash=cash-? WHERE user_id=?", (REPAIR_COST, target))
            await msg.reply_text(f"🔧 هزینه تعمیر: {REPAIR_COST} $ کم شد.")
            return

        # اضافه کردن ماشین
        if low.startswith("+car ") or low.startswith("+ماشین "):
            name = text.split(" ", 1)[1].strip()
            player = conn.execute("SELECT cars FROM players WHERE user_id=?", (target,)).fetchone()
            row = conn.execute("SELECT * FROM players WHERE user_id=?", (target,)).fetchone()
            cars = get_cars(row)
            if len(cars) >= MAX_CARS:
                await msg.reply_text(f"❌ سقف {MAX_CARS} ماشین پر شده!")
                return
            cars.append(name)
            conn.execute("UPDATE players SET cars=? WHERE user_id=?", (json.dumps(cars), target))
            await msg.reply_text(f"✅ {name} اضافه شد. ({len(cars)}/{MAX_CARS})")
            return

        # کم کردن ماشین (با اسم یا بدون اسم = آخرین ماشین)
        if low.startswith("-car") or low.startswith("-ماشین"):
            parts = text.split(" ", 1)
            name = parts[1].strip() if len(parts) > 1 else ""
            row = conn.execute("SELECT cars FROM players WHERE user_id=?", (target,)).fetchone()
            cars = get_cars(row)
            if not cars:
                await msg.reply_text("❌ این پلیر ماشینی نداره.")
                return
            if name:
                found = next((i for i, c in enumerate(cars) if c.lower() == name.lower()), None)
                if found is None:
                    await msg.reply_text("❌ این ماشین توی لیستش نیست.\nلیستش:\n" +
                                          "\n".join(f"• {c}" for c in cars))
                    return
                removed = cars.pop(found)
            else:
                removed = cars.pop()
            conn.execute("UPDATE players SET cars=? WHERE user_id=?", (json.dumps(cars), target))
            await msg.reply_text(f"✅ {removed} حذف شد. ({len(cars)}/{MAX_CARS} باقی‌مانده)")
            return

        # پول
        if len(text) >= 2 and text[0] in "+-":
            amount = parse_amount(text[1:])
            if amount is None or amount <= 0:
                await msg.reply_text("❌ عدد معتبر بنویس. مثال: +200")
                return
            player = conn.execute("SELECT cash FROM players WHERE user_id=?", (target,)).fetchone()
            if text[0] == "+":
                conn.execute("UPDATE players SET cash=cash+? WHERE user_id=?", (amount, target))
                await msg.reply_text(f"✅ {amount:,} $ اضافه شد.\n💰 موجودی جدید: {player['cash'] + amount:,} $")
            else:
                if player["cash"] < amount:
                    await msg.reply_text(f"❌ اینقدر پول نداره! موجودیش: {player['cash']:,} $")
                    return
                conn.execute("UPDATE players SET cash=cash-? WHERE user_id=?", (amount, target))
                await msg.reply_text(f"✅ {amount:,} $ کم شد.\n💰 موجودی جدید: {player['cash'] - amount:,} $")
            return

async def wealth_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ دسترسی غیرمجاز.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ روی پیام پلیر ریپلای کن و /wealth بزن.")
        return
    target = update.message.reply_to_message.from_user.id
    with db() as conn:
        player = conn.execute("SELECT * FROM players WHERE user_id=?", (target,)).fetchone()
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
    if doc.file_size > 20 * 1024 * 1024:
        await msg.reply_text("❌ فایل-too-large")
        return

    tmp = "restore_tmp.db"
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(custom_path=tmp)
        check = sqlite3.connect(tmp)
        check.execute("SELECT COUNT(*) FROM players").fetchone()
        check.execute("SELECT COUNT(*) FROM items").fetchone()
        ok = check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        check.close()
        if not ok:
            raise ValueError("invalid database")
        os.replace(tmp, DB_FILE)
        await msg.reply_text("✅ بکاپ بازیابی شد. همه پول‌ها، ماشین‌ها و آیتم‌ها برگشتن.")
    except Exception as e:
        logger.error("Restore failed: %s", e)
        if os.path.exists(tmp):
            os.remove(tmp)
        await msg.reply_text("❌ این فایل بکاپ معتبر نیست.")


# ================= دیسپچر دکمه‌ها =================
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    uid = q.from_user.id

    if data == "main_menu":
        await replace_message(q, context, "🎮 تریدر | منوی اصلی", main_menu())

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

    elif data.startswith("buy_"):
        raw = data[4:]
        if raw.isdigit():
            await buy_item(q, context, int(raw))

    elif data == "admin_backup":
        if is_admin(uid):
            await send_backup(q.message.chat_id, context)

    elif data == "admin_list":
        if is_admin(uid):
            await show_admin_list(q, context)

    elif data.startswith("item_"):
        if is_admin(uid) and data[5:].isdigit():
            await show_item_detail(q, context, int(data[5:]))

    elif data.startswith("delyes_"):
        if is_admin(uid) and data[7:].isdigit():
            await do_delete(q, context, int(data[7:]))

    elif data.startswith("del_"):
        if is_admin(uid) and data[4:].isdigit():
            await confirm_delete(q, context, int(data[4:]))


# ================= خطاها =================
async def on_error(update, context):
    logger.error("Update caused error: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ یه خطای داخلی پیش اومد. دوباره /start بزن.")
        except TelegramError:
            pass


# ================= اجرا =================
def main():
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()

    add_conv = ConversationHandler(
        entry_points=[
            CommandHandler("additem", add_start),
            CallbackQueryHandler(add_start, pattern=r"^admin_add$"),
        ],
        states={
            ADD_PHOTO: [MessageHandler(filters.PHOTO, add_photo_step)],
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name_step)],
            ADD_BUY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_buy_step)],
            ADD_SELL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_sell_step)],
            ADD_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_category_step)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        name="add_item_conv",
    )

    edit_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(edit_price_start, pattern=r"^e[bs]_\d+$"),
            CallbackQueryHandler(edit_name_start, pattern=r"^en_\d+$"),
            CallbackQueryHandler(edit_photo_start, pattern=r"^ep_\d+$"),
        ],
        states={
            EDIT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_price_value)],
            EDIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_name_value)],
            EDIT_PHOTO: [MessageHandler(filters.PHOTO, edit_photo_value)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        name="edit_item_conv",
    )

    application = Application.builder().token(BOT_TOKEN).build()

    # ترتیب مهم است: مکالمه‌ها قبل از دیسپچر عمومی
    application.add_handler(add_conv)
    application.add_handler(edit_conv)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("listitems", cmd_list_items))
    application.add_handler(CommandHandler("wealth", wealth_command))
    application.add_handler(CommandHandler("backup", cmd_backup))
    application.add_handler(CommandHandler("restore", restore_backup))
    application.add_handler(MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, admin_reply))
    application.add_handler(CallbackQueryHandler(on_button))

    application.add_error_handler(on_error)
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
