import os
import sqlite3
import random
import json
import threading
from datetime import datetime
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# --- تنظیمات ---
BOT_TOKEN = "8725147253:AAEV-vZc1bNzS-Gk4HYDGLpQtdQagsvtEdg"  # مثال: "123456789:AAH..."
ADMIN_ID = 5591238725
# ---------------

app = Flask(__name__)

@app.route('/')
def home():
    return "Grim Valley Bot is running! 🎮"

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

def init_db():
    conn = sqlite3.connect('grim_valley.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS players
                 (user_id INTEGER PRIMARY KEY, username TEXT, cash INTEGER DEFAULT 0, 
                  has_house INTEGER DEFAULT 0, cars TEXT DEFAULT '[]', last_daily TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS items
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, category TEXT, 
                  buy_price INTEGER, sell_price INTEGER, image_url TEXT)''')
    conn.commit()
    conn.close()

def is_admin(user_id):
    return user_id == ADMIN_ID

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or "Unknown"
    conn = sqlite3.connect('grim_valley.db')
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO players (user_id, username) VALUES (?, ?)", (user_id, username))
    conn.commit()
    conn.close()
    
    keyboard = [
        [InlineKeyboardButton("💼 دارایی‌های من", callback_data='my_assets')],
        [InlineKeyboardButton("🛒 فروشگاه", callback_data='shop')],
        [InlineKeyboardButton("🏆 لیدربورد", callback_data='leaderboard')],
        [InlineKeyboardButton("🎁 جایزه روزانه", callback_data='daily')]
    ]
    await update.message.reply_text(f"🎮 به بات سرور گریم ولی خوش آمدید {username}!", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if query.data == 'my_assets': await show_assets(query, user_id)
    elif query.data == 'shop': await show_shop(query)
    elif query.data == 'leaderboard': await show_leaderboard(query)
    elif query.data == 'daily': await give_daily_reward(query, user_id)
    elif query.data.startswith('buy_'):
        item_id = int(query.data.split('_')[1])
        await buy_item(query, user_id, item_id)

async def show_assets(query, user_id):
    conn = sqlite3.connect('grim_valley.db')
    c = conn.cursor()
    c.execute("SELECT * FROM players WHERE user_id = ?", (user_id,))
    player = c.fetchone()
    conn.close()
    if not player: return await query.edit_message_text("❌ ثبت‌نام نکرده‌اید. /start بزنید.")
    cars = json.loads(player[4]) if player[4] else []
    has_house = "✅ دارد" if player[3] else "❌ ندارد"
    total_value = player[2] + (2500 if player[3] else 0)
    cars_text = "\n".join([f"   • {car}" for car in cars]) if cars else "   • هیچ ماشینی ندارید"
    await query.edit_message_text(f"💼 دارایی‌های شما:\n\n💰 پول: {player[2]:,} $\n🏠 خانه: {has_house}\n🚗 ماشین‌ها ({len(cars)}/4):\n{cars_text}\n\n💎 کل: {total_value:,} $")

async def show_shop(query):
    conn = sqlite3.connect('grim_valley.db')
    c = conn.cursor()
    c.execute("SELECT DISTINCT category FROM items")
    categories = c.fetchall()
    conn.close()
    if not categories: return await query.edit_message_text("🛒 فروشگاه فعلاً خالی است!")
    keyboard = [[InlineKeyboardButton(f"{cat[0]}", callback_data=f'cat_{cat[0]}')] for cat in categories]
    await query.edit_message_text("🛒 فروشگاه گریم ولی\n\nدسته‌بندی را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_leaderboard(query):
    conn = sqlite3.connect('grim_valley.db')
    c = conn.cursor()
    c.execute("SELECT username, cash FROM players ORDER BY cash DESC LIMIT 10")
    top_players = c.fetchall()
    conn.close()
    text = "🏆 لیدربورد:\n\n" + "\n".join([f"{i}. {p[0]}: {p[1]:,} $" for i, p in enumerate(top_players, 1)]) if top_players else "هنوز کسی ثبت‌نام نکرده!"
    await query.edit_message_text(text)

async def give_daily_reward(query, user_id):
    conn = sqlite3.connect('grim_valley.db')
    c = conn.cursor()
    c.execute("SELECT last_daily FROM players WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    if not result: return await query.edit_message_text("❌ ثبت‌نام نکرده‌اید.")
    today = datetime.now().date().isoformat()
    if result[0] == today: return await query.edit_message_text("❌ امروز جایزه گرفته‌اید!")
    reward = random.randint(20, 40)
    c.execute("UPDATE players SET cash = cash + ?, last_daily = ? WHERE user_id = ?", (reward, today, user_id))
    conn.commit()
    conn.close()
    await query.edit_message_text(f"🎁 تبریک! {reward} $ دریافت کردید!")

async def buy_item(query, user_id, item_id):
    conn = sqlite3.connect('grim_valley.db')
    c = conn.cursor()
    c.execute("SELECT cars FROM players WHERE user_id = ?", (user_id,))
    cars = json.loads(c.fetchone()[0]) if c.fetchone()[0] else []
    if len(cars) >= 4: return await query.edit_message_text("❌ ظرفیت ماشین (۴ عدد) پر است!")
    c.execute("SELECT * FROM items WHERE id = ?", (item_id,))
    item = c.fetchone()
    if not item: return await query.edit_message_text("❌ آیتم یافت نشد!")
    c.execute("SELECT cash FROM players WHERE user_id = ?", (user_id,))
    cash = c.fetchone()[0]
    if cash < item[3]: return await query.edit_message_text(f"❌ موجودی کافی نیست! ({cash:,} $ دارید)")
    c.execute("UPDATE players SET cash = cash - ? WHERE user_id = ?", (item[3], user_id))
    if item[2] == 'car':
        cars.append(item[1])
        c.execute("UPDATE players SET cars = ? WHERE user_id = ?", (json.dumps(cars), user_id))
    conn.commit()
    conn.close()
    await query.edit_message_text(f"✅ {item[1]} خریداری شد!\n💰 {item[3]:,} $ کسر شد.")

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return await update.message.reply_text("❌ دسترسی غیرمجاز!")
    keyboard = [[InlineKeyboardButton("➕ افزودن آیتم", callback_data='admin_add')], [InlineKeyboardButton("📋 لیست آیتم‌ها", callback_data='admin_list')]]
    await update.message.reply_text("🛠️ پنل ادمین:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not update.message.reply_to_message: return
    target_user = update.message.reply_to_message.from_user.id
    text = update.message.text.strip()
    conn = sqlite3.connect('grim_valley.db')
    c = conn.cursor()
    
    if text.startswith('+') and text[1:].isdigit():
        amount = int(text[1:])
        c.execute("UPDATE players SET cash = cash + ? WHERE user_id = ?", (amount, target_user))
        conn.commit()
        c.execute("SELECT cash FROM players WHERE user_id = ?", (target_user,))
        await update.message.reply_text(f"✅ {amount:,} $ اضافه شد. جدید: {c.fetchone()[0]:,} $")
    elif text.startswith('-') and text[1:].isdigit():
        amount = int(text[1:])
        c.execute("UPDATE players SET cash = cash - ? WHERE user_id = ?", (amount, target_user))
        conn.commit()
        c.execute("SELECT cash FROM players WHERE user_id = ?", (target_user,))
        await update.message.reply_text(f"✅ {amount:,} $ کم شد. جدید: {c.fetchone()[0]:,} $")
    elif text == '+house':
        c.execute("UPDATE players SET has_house = 1 WHERE user_id = ?", (target_user,))
        conn.commit()
        await update.message.reply_text("✅ خانه ثبت شد.")
    elif text == '-house':
        c.execute("UPDATE players SET has_house = 0 WHERE user_id = ?", (target_user,))
        conn.commit()
        await update.message.reply_text("✅ خانه حذف شد.")
    elif text.startswith('+car '):
        car_name = text[5:].strip()
        c.execute("SELECT cars FROM players WHERE user_id = ?", (target_user,))
        cars = json.loads(c.fetchone()[0]) if c.fetchone()[0] else []
        if len(cars) >= 4: await update.message.reply_text("❌ ظرفیت پر است!")
        else:
            cars.append(car_name)
            c.execute("UPDATE players SET cars = ? WHERE user_id = ?", (json.dumps(cars), target_user))
            conn.commit()
            await update.message.reply_text(f"✅ {car_name} اضافه شد.")
    elif text.startswith('-car '):
        car_name = text[5:].strip()
        c.execute("SELECT cars FROM players WHERE user_id = ?", (target_user,))
        cars = json.loads(c.fetchone()[0]) if c.fetchone()[0] else []
        if car_name in cars:
            cars.remove(car_name)
            c.execute("UPDATE players SET cars = ? WHERE user_id = ?", (json.dumps(cars), target_user))
            conn.commit()
            await update.message.reply_text(f"✅ {car_name} حذف شد.")
        else:
            await update.message.reply_text("❌ در لیست نیست.")
    conn.close()

async def wealth_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not update.message.reply_to_message: return
    target_user = update.message.reply_to_message.from_user.id
    target_username = update.message.reply_to_message.from_user.username or "Unknown"
    conn = sqlite3.connect('grim_valley.db')
    c = conn.cursor()
    c.execute("SELECT * FROM players WHERE user_id = ?", (target_user,))
    player = c.fetchone()
    conn.close()
    if not player: return await update.message.reply_text("❌ پلیر یافت نشد.")
    cars = json.loads(player[4]) if player[4] else []
    has_house = "✅ دارد" if player[3] else "❌ ندارد"
    cars_text = "\n".join([f"   • {car}" for car in cars]) if cars else "   • هیچ ماشینی ندارد"
    await update.message.reply_text(f"💼 دارایی‌های {target_username}:\n\n💰 پول: {player[2]:,} $\n🏠 خانه: {has_house}\n🚗 ماشین‌ها ({len(cars)}/4):\n{cars_text}")

def main():
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("wealth", wealth_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.REPLY, handle_reply))
    
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
