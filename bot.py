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
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ================= تنظیمات =================
BOT_TOKEN = "8725147253:AAEV-vZc1bNzS-Gk4HYDGLpQtdQagsvtEdg"
ADMIN_ID = 5591238725
ADMIN_ID_2 = 6270844351
DB_FILE = "grim_valley.db"
MAX_CARS = 3
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
        conn.commit()
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
            sell_price INTEGER NOT NULL
        )""")


# ================= ابزارها =================
def is_admin(uid):
    return uid == ADMIN_ID or uid == ADMIN_ID_2

def normalize_digits(text):
    text = str(text)
    text = text.replace("۰", "0").replace("۱", "1").replace("۲", "2").replace("۳", "3").replace("۴", "4")
    text = text.replace("۵", "5").replace("۶", "6").replace("۷", "7").replace("۸", "8").replace("۹", "9")
    text = text.replace("٠", "0").replace("١", "1").replace("٢", "2").replace("٣", "3").replace("٤", "4")
    text = text.replace("٥", "5").replace("٦", "6").replace("٧", "7").replace("٨", "8").replace("٩", "9")
    return text

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

async def replace_message(query, context, text, markup=None):
    chat_id = query.message.chat_id
    await safe_delete(query.message)
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)


# ================= منوی پلیر =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    uid = update.effective_user.id
name = update.effective_user.username or update.effective_user.first_name or "پلیر"
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
username = query.from_user.username or query.from_user.first_name or "ناشناس"    
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
                                  back_main())
            return
        if player["cash"] < item["buy_price"]:
            await replace_message(query, context,
                                  f"❌ پولت کمه!\n💰 داری: {player['cash']:,} $\n"
                                  f"💵 نیاز داری: {item['buy_price']:,} $",
                                  back_main())
            return
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
    
    # ارسال اعلان به ادمین
    admin_msg = (
        f"🛒 **سفارش جدید!**\n\n"
        f"👤 پلیر: @{username}\n"
        f"🆔 آیدی: `{uid}`\n"
        f"📦 آیتم: {item['name']}\n"
        f"️ دسته: {CATEGORIES.get(item['category'], item['category'])}\n"
        f" قیمت: {item['buy_price']:,} $\n\n"
        f"⏰ زمان: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    
        # ارسال به ادمین اول
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=admin_msg,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Failed to send admin notification: {e}")
    
    # ارسال به ادمین دوم
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID_2,
            text=admin_msg,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Failed to send admin notification to second admin: {e}")
    
    await replace_message(
        query, context,
        f"✅ {item['name']} خریدی!\n💰 {item['buy_price']:,} $ کسر شد.\n"
        f"📦 برای تحویل گرفتن به ادمین سرور پیام بده.",
        back_main()
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


# ================= افزودن آیتم (ساده) =================
async def additem_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ فقط ادمین.")
        return
    
    context.user_data.clear()
    context.user_data["step"] = 1
    await update.message.reply_text("✏️ مرحله ۱ از ۴:\nاسم آیتم رو بنویس:\nمثال: Chevy Step Van\n\n(لغو با /cancel)")

async def handle_text_for_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    
    step = context.user_data.get("step")
    if not step:
        return
    
    text = update.message.text.strip()
    
    try:
        if step == 1:
            if not text or len(text) > 60:
                await update.message.reply_text("❌ یه اسم معتبر (حداکثر ۶۰ حرف) بنویس.")
                return
            context.user_data["item_name"] = text
            context.user_data["step"] = 2
            await update.message.reply_text("✏️ مرحله ۲ از ۴:\nقیمت خرید رو فقط عدد بنویس:\nمثال: 1500")
        
        elif step == 2:
            value = parse_amount(text)
            if value is None or value <= 0:
                await update.message.reply_text("❌ یه عدد مثبت بنویس. مثال: 1500")
                return
            context.user_data["item_buy"] = value
            context.user_data["step"] = 3
            await update.message.reply_text("✏️ مرحله ۳ از ۴:\nقیمت فروش رو فقط عدد بنویس:\nمثال: 600")
        
        elif step == 3:
            value = parse_amount(text)
            if value is None or value < 0:
                await update.message.reply_text("❌ یه عدد معتبر بنویس. مثال: 600")
                return
            context.user_data["item_sell"] = value
            context.user_data["step"] = 4
            await update.message.reply_text(
                "✏️ مرحله ۴ از ۴:\nدسته‌بندی رو بنویس:\n\n"
                "car → خودرو\n"
                "resource → منابع\n"
                "attachment → باربند"
            )
        
        elif step == 4:
            raw = text.lower()
            mapping = {
                "car": "car", "خودرو": "car", "ماشین": "car",
                "resource": "resource", "منبع": "resource", "منابع": "resource",
                "attachment": "attachment", "باربند": "attachment",
            }
            category = mapping.get(raw)
            if not category:
                await update.message.reply_text("❌ فقط car یا resource یا attachment بنویس.")
                return
            
            name = context.user_data.get("item_name")
            buy = context.user_data.get("item_buy")
            sell = context.user_data.get("item_sell")
            
            if not name or not buy or not sell:
                await update.message.reply_text("❌ اطلاعات ناقصه. دوباره /additem بزن.")
                return
            
            with db() as conn:
                cur = conn.execute(
                    "INSERT INTO items (name, category, buy_price, sell_price) VALUES (?,?,?,?)",
                    (name, category, buy, sell)
                )
                new_id = cur.lastrowid
            
            context.user_data.clear()
            
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 لیست آیتم‌ها", callback_data="admin_list")],
                [InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")],
            ])
            caption = (
                f"✅ آیتم ثبت شد!\n\n"
                f"🆔 کد: {new_id}\n"
                f"📛 اسم: {name}\n"
                f"🏷️ دسته: {CATEGORIES[category]}\n"
                f"💵 خرید: {buy:,} $\n"
                f"♻️ فروش: {sell:,} $"
            )
            await update.message.reply_text(caption, reply_markup=markup)
    
    except Exception as e:
        logger.error(f"Error in handle_text_for_add: {e}", exc_info=True)
        await update.message.reply_text(f"⚠️ خطا: {str(e)}")


# ================= ویرایش آیتم =================
async def handle_text_for_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    
    edit_step = context.user_data.get("edit_step")
    if not edit_step:
        return
    
    text = update.message.text.strip()
    
    try:
        if edit_step == "edit_price":
            value = parse_amount(text)
            if value is None or value < 0:
                await update.message.reply_text("❌ یه عدد معتبر بنویس.")
                return
            field = context.user_data.get("edit_field")
            item_id = context.user_data.get("edit_id")
            with db() as conn:
                conn.execute(f"UPDATE items SET {field}=? WHERE id=?", (value, item_id))
            context.user_data.clear()
            label = "قیمت خرید" if field == "buy_price" else "قیمت فروش"
            await update.message.reply_text(f"✅ {label} به {value:,} $ تغییر کرد.")
        
        elif edit_step == "edit_name":
            if not text or len(text) > 60:
                await update.message.reply_text("❌ اسم معتبر بنویس (حداکثر ۶۰ حرف).")
                return
            item_id = context.user_data.get("edit_id")
            with db() as conn:
                conn.execute("UPDATE items SET name=? WHERE id=?", (text, item_id))
            context.user_data.clear()
            await update.message.reply_text(f"✅ اسم به «{text}» تغییر کرد.")
    
    except Exception as e:
        logger.error(f"Error in handle_text_for_edit: {e}", exc_info=True)
        await update.message.reply_text(f"⚠️ خطا: {str(e)}")


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
        await replace_message(query, context, "📋 فروشگاه خالی است.\nبرای افزودن دکمه زیر رو بزن.", admin_menu())
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
        [InlineKeyboardButton("✏️ اسم", callback_data="en_" + str(it["id"]))],
        [InlineKeyboardButton("🗑️ حذف آیتم", callback_data="del_" + str(it["id"]))],
        [InlineKeyboardButton("📋 لیست", callback_data="admin_list"),
         InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")],
    ])
    await replace_message(query, context, text, markup)

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
    if not msg or not msg.reply_to_message or not msg.text:
        return
    target = msg.reply_to_message.from_user.id
    target_name = msg.reply_to_message.from_user.username or "player"
    text = msg.text.strip()
    if not text:
        return
    low = text.lower()

    with db() as conn:
        ensure_player(conn, target, target_name)

        if low in ("+house", "+خونه", "+خانه"):
            conn.execute("UPDATE players SET has_house=1 WHERE user_id=?", (target,))
            await msg.reply_text("✅ خانه ثبت شد.")
            return
        if low in ("-house", "-خونه", "-خانه"):
            conn.execute("UPDATE players SET has_house=0 WHERE user_id=?", (target,))
            await msg.reply_text("✅ خانه حذف شد.")
            return

        if low in ("repair", "تعمیر", "+repair"):
            player = conn.execute("SELECT cash FROM players WHERE user_id=?", (target,)).fetchone()
            if player["cash"] < REPAIR_COST:
                await msg.reply_text(f"❌ پولش برای تعمیر ({REPAIR_COST} $) کافی نیست.")
                return
            conn.execute("UPDATE players SET cash=cash-? WHERE user_id=?", (REPAIR_COST, target))
            await msg.reply_text(f"🔧 هزینه تعمیر: {REPAIR_COST} $ کم شد.")
            return

        if low.startswith("+car ") or low.startswith("+ماشین "):
            name = text.split(" ", 1)[1].strip()
            row = conn.execute("SELECT cars FROM players WHERE user_id=?", (target,)).fetchone()
            cars = get_cars(row)
            if len(cars) >= MAX_CARS:
                await msg.reply_text(f"❌ سقف {MAX_CARS} ماشین پر شده!")
                return
            cars.append(name)
            conn.execute("UPDATE players SET cars=? WHERE user_id=?", (json.dumps(cars), target))
            await msg.reply_text(f"✅ {name} اضافه شد. ({len(cars)}/{MAX_CARS})")
            return

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
        await msg.reply_text("❌ فایل خیلی حجیمه.")
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


# ================= لغو =================
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ لغو شد.")


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
    elif data == "admin_add":
        if is_admin(uid):
            context.user_data.clear()
            context.user_data["step"] = 1
            await replace_message(q, context, "✏️ مرحله ۱ از ۴:\nاسم آیتم رو بنویس:\nمثال: Chevy Step Van\n\n(لغو با /cancel)")
    elif data.startswith("item_"):
        if is_admin(uid) and data[5:].isdigit():
            await show_item_detail(q, context, int(data[5:]))
    elif data.startswith("delyes_"):
        if is_admin(uid) and data[7:].isdigit():
            await do_delete(q, context, int(data[7:]))
    elif data.startswith("del_"):
        if is_admin(uid) and data[4:].isdigit():
            await confirm_delete(q, context, int(data[4:]))
    elif data.startswith("eb_"):
        if is_admin(uid):
            parts = data.split("_")
            context.user_data.clear()
            context.user_data["edit_step"] = "edit_price"
            context.user_data["edit_field"] = "buy_price"
            context.user_data["edit_id"] = int(parts[1])
            await replace_message(q, context, "💵 قیمت خرید جدید رو فقط عدد بنویس:\n(لغو با /cancel)")
    elif data.startswith("es_"):
        if is_admin(uid):
            parts = data.split("_")
            context.user_data.clear()
            context.user_data["edit_step"] = "edit_price"
            context.user_data["edit_field"] = "sell_price"
            context.user_data["edit_id"] = int(parts[1])
            await replace_message(q, context, "💵 قیمت فروش جدید رو فقط عدد بنویس:\n(لغو با /cancel)")
    elif data.startswith("en_"):
        if is_admin(uid):
            parts = data.split("_")
            context.user_data.clear()
            context.user_data["edit_step"] = "edit_name"
            context.user_data["edit_id"] = int(parts[1])
            await replace_message(q, context, "✏️ اسم جدید آیتم رو بنویس:\n(لغو با /cancel)")


# ================= خطاها =================
async def on_error(update, context):
    logger.error("Update caused error: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            error_msg = str(context.error)[:300]
            await update.effective_message.reply_text(f"⚠️ خطا:\n{error_msg}")
        except TelegramError:
            pass


# ================= اجرا =================
def main():
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("additem", additem_start))
    application.add_handler(CommandHandler("listitems", cmd_list_items))
    application.add_handler(CommandHandler("wealth", wealth_command))
    application.add_handler(CommandHandler("backup", cmd_backup))
    application.add_handler(CommandHandler("restore", restore_backup))
    application.add_handler(CommandHandler("cancel", cancel))
    
    application.add_handler(MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, admin_reply))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_for_add))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_for_edit))
    
    application.add_handler(CallbackQueryHandler(on_button))
    application.add_error_handler(on_error)
    
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
