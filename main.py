import os
import sys
import telebot
from telebot import types
from pymongo import MongoClient
from datetime import datetime
import logging
from flask import Flask
import threading
import traceback
import time

# Logging sozlamalari
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Flask app
app = Flask(__name__)

# Environment variables
BOT_TOKEN = os.getenv('BOT_TOKEN')
MONGODB_URI = os.getenv('MONGODB_URI')
DB_NAME = os.getenv('DB_NAME', 'UzbLinks')

# Admin ID
admin_ids_str = os.getenv('ADMIN_IDS', '')
ADMIN_IDS = []
if admin_ids_str:
    try:
        ADMIN_IDS = [int(id_str.strip()) for id_str in admin_ids_str.split(',') if id_str.strip()]
    except ValueError:
        logger.error("ADMIN_IDS noto'g'ri formatda!")

logger.info(f"🔧 Konfiguratsiya: DB={DB_NAME}, Adminlar={len(ADMIN_IDS)} ta")

# MongoDB
try:
    client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000, connectTimeoutMS=10000, socketTimeoutMS=10000)
    client.admin.command('ping')
    db = client[DB_NAME]
    codes_collection = db['invite_codes']
    channels_collection = db['channels']
    users_collection = db['users']
    settings_collection = db['settings']
    logger.info("✅ MongoDB ulandi!")
except Exception as e:
    logger.error(f"❌ MongoDB xatolik: {e}")
    sys.exit(1)

# Indekslar
try:
    codes_collection.create_index("code", unique=True)
    codes_collection.create_index("channel_id")
    channels_collection.create_index("channel_id", unique=True)
    users_collection.create_index("user_id", unique=True)
except:
    pass

# Bot
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML')
bot_started = False
user_states = {}

# Funksiyalar
def get_next_code_number():
    try:
        setting = settings_collection.find_one_and_update(
            {"_id": "code_counter"},
            {"$inc": {"last_code_number": 1}},
            upsert=True,
            return_document=True
        )
        return str(setting.get('last_code_number', 1)) if setting else "1"
    except:
        return str(int(datetime.now().timestamp()))

def is_admin(user_id):
    return user_id in ADMIN_IDS

def get_channel_invite_link(channel_id):
    try:
        bot_member = bot.get_chat_member(channel_id, bot.get_me().id)
        if bot_member.status != 'administrator' or not bot_member.can_invite_users:
            return None
        invite = bot.create_chat_invite_link(chat_id=channel_id, member_limit=1)
        return invite.invite_link
    except:
        return None

def verify_and_use_code(code, user_id, username, first_name):
    try:
        code_data = codes_collection.find_one({"code": code, "is_active": True})
        
        if not code_data:
            return {"success": False, "message": "❌ Noto'g'ri yoki aktiv bo'lmagan kod!", "invite_link": None}
        
        user_id_str = str(user_id)
        if "used_by" in code_data:
            if any(u["user_id"] == user_id_str for u in code_data["used_by"]):
                return {"success": False, "message": "❌ Siz bu kodni oldin ishlatgansiz!", "invite_link": None}
        
        channel_data = channels_collection.find_one({"channel_id": code_data["channel_id"]})
        if not channel_data:
            return {"success": False, "message": "❌ Kanal topilmadi!", "invite_link": None}
        
        invite_link = get_channel_invite_link(code_data["channel_id"])
        if not invite_link:
            return {"success": False, "message": "❌ Havola yaratishda xatolik!", "invite_link": None}
        
        codes_collection.update_one(
            {"_id": code_data["_id"]},
            {
                "$push": {"used_by": {"user_id": user_id_str, "username": username, "first_name": first_name, "used_at": datetime.now()}},
                "$inc": {"used_count": 1},
                "$set": {"last_used_at": datetime.now(), "last_invite_link": invite_link}
            }
        )
        
        users_collection.update_one(
            {"user_id": user_id_str},
            {
                "$set": {"username": username, "first_name": first_name, "last_used_at": datetime.now()},
                "$push": {"used_codes": {"code": code, "channel_name": channel_data.get("channel_name"), "used_at": datetime.now()}},
                "$inc": {"total_uses": 1}
            },
            upsert=True
        )
        
        return {
            "success": True,
            "message": f"✅ Kod qabul qilindi!\n\n📱 Kanal: {channel_data.get('channel_name')}\n\n🔽 Pastdagi tugma orqali kanalga qo'shiling:",
            "invite_link": invite_link
        }
    except Exception as e:
        logger.error(f"Kod tekshirishda xatolik: {e}")
        return {"success": False, "message": "❌ Xatolik!", "invite_link": None}

# Admin menyusi
def admin_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        types.KeyboardButton('➕ Kanal qo\'shish'),
        types.KeyboardButton('🔑 Kod yaratish'),
        types.KeyboardButton('📋 Kanallar'),
        types.KeyboardButton('📝 Kodlar'),
        types.KeyboardButton('📊 Statistika'),
        types.KeyboardButton('📢 Xabar yuborish')
    )
    return markup

# /start
@bot.message_handler(commands=['start'])
def start(message):
    if is_admin(message.from_user.id):
        bot.reply_to(message, "👑 Admin panel\n\nKerakli amalni tanlang:", reply_markup=admin_menu())
    else:
        bot.reply_to(message, "🔑 <b>Kodni kiriting:</b>", reply_markup=types.ReplyKeyboardRemove())

# /help
@bot.message_handler(commands=['help'])
def help_cmd(message):
    if is_admin(message.from_user.id):
        bot.reply_to(message, "Admin komandalari:\n➕ Kanal qo'shish - Kanal ID orqali qo'shish\n🔑 Kod yaratish - Yangi kod\n📋 Kanallar - Ro'yxat\n📝 Kodlar - Kodlar ro'yxati\n📊 Statistika\n📢 Xabar yuborish")
    else:
        bot.reply_to(message, "📝 Kodni oling va botga yuboring. Bot sizga bir martalik havola beradi.")

# Admin tugmalari
@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == '➕ Kanal qo\'shish')
def add_channel_start(message):
    user_states[message.from_user.id] = "waiting_channel_id"
    bot.reply_to(message, "📝 Kanal ID sini yuboring:\n\nMisol: -1001234567890\n\n❌ Bekor qilish: /cancel", reply_markup=types.ReplyKeyboardRemove())

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == '🔑 Kod yaratish')
def generate_code_btn(message):
    channels = list(channels_collection.find({"is_active": True}))
    if not channels:
        bot.reply_to(message, "❌ Kanal yo'q! Avval kanal qo'shing.")
        return
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    for ch in channels:
        markup.add(types.InlineKeyboardButton(f"📱 {ch['channel_name']}", callback_data=f"gen_{ch['channel_id']}"))
    
    bot.reply_to(message, "Qaysi kanal uchun kod yaratamiz?", reply_markup=markup)

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == '📋 Kanallar')
def list_channels_btn(message):
    channels = list(channels_collection.find({"is_active": True}))
    if not channels:
        bot.reply_to(message, "Kanallar yo'q!")
        return
    text = "📋 <b>Kanallar:</b>\n\n"
    for i, ch in enumerate(channels, 1):
        text += f"{i}. {ch['channel_name']}\n   🆔 <code>{ch['channel_id']}</code>\n\n"
    bot.reply_to(message, text)

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == '📝 Kodlar')
def list_codes_btn(message):
    codes = list(codes_collection.find({"is_active": True}).sort("created_at", -1).limit(20))
    if not codes:
        bot.reply_to(message, "Kodlar yo'q!")
        return
    text = "📝 <b>Kodlar:</b>\n\n"
    for i, cd in enumerate(codes, 1):
        status = "✅" if cd['used_count'] > 0 else "🆕"
        text += f"{i}. {status} <code>{cd['code']}</code> - {cd.get('channel_name', 'N/A')}\n"
    bot.reply_to(message, text)

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == '📊 Statistika')
def stats_btn(message):
    total_ch = channels_collection.count_documents({"is_active": True})
    total_co = codes_collection.count_documents({})
    active_co = codes_collection.count_documents({"is_active": True})
    used_co = codes_collection.count_documents({"used_count": {"$gt": 0}})
    total_us = users_collection.count_documents({})
    
    text = f"""
📊 <b>Statistika</b>

📱 Kanallar: {total_ch}
🔑 Kodlar: {total_co}
✅ Aktiv: {active_co}
👥 Ishlatilgan: {used_co}
👤 Foydalanuvchilar: {total_us}
"""
    bot.reply_to(message, text)

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == '📢 Xabar yuborish')
def broadcast_btn(message):
    user_states[message.from_user.id] = "waiting_broadcast"
    bot.reply_to(message, "📢 Yuboriladigan xabarni yozing:\n\n❌ Bekor qilish: /cancel", reply_markup=types.ReplyKeyboardRemove())

# /cancel
@bot.message_handler(commands=['cancel'])
def cancel(message):
    if message.from_user.id in user_states:
        del user_states[message.from_user.id]
        if is_admin(message.from_user.id):
            bot.reply_to(message, "❌ Bekor qilindi!", reply_markup=admin_menu())
        else:
            bot.reply_to(message, "❌ Bekor qilindi!", reply_markup=types.ReplyKeyboardRemove())

# State: Kanal ID kiritish
@bot.message_handler(func=lambda m: user_states.get(m.from_user.id) == "waiting_channel_id" and is_admin(m.from_user.id))
def process_channel_id(message):
    channel_id = message.text.strip()
    
    try:
        chat = bot.get_chat(channel_id)
        bot_member = bot.get_chat_member(channel_id, bot.get_me().id)
        
        if bot_member.status != 'administrator':
            bot.reply_to(message, "❌ Bot admin emas!", reply_markup=admin_menu())
            del user_states[message.from_user.id]
            return
        
        if not bot_member.can_invite_users:
            bot.reply_to(message, "❌ 'Add members' huquqi yo'q!", reply_markup=admin_menu())
            del user_states[message.from_user.id]
            return
        
        channel_name = chat.title or f"Kanal"
        
        channels_collection.update_one(
            {"channel_id": channel_id},
            {"$set": {
                "channel_id": channel_id,
                "channel_name": channel_name,
                "channel_username": getattr(chat, 'username', None),
                "added_by": str(message.from_user.id),
                "created_at": datetime.now(),
                "is_active": True
            }},
            upsert=True
        )
        
        del user_states[message.from_user.id]
        bot.reply_to(message, f"✅ Kanal qo'shildi: {channel_name}\n\nEndi 🔑 Kod yaratish tugmasini bosing!", reply_markup=admin_menu())
        
    except Exception as e:
        bot.reply_to(message, f"❌ Xatolik: {e}\n\nKanal ID sini tekshiring!", reply_markup=admin_menu())
        del user_states[message.from_user.id]

# State: Broadcast
@bot.message_handler(func=lambda m: user_states.get(m.from_user.id) == "waiting_broadcast" and is_admin(m.from_user.id))
def process_broadcast(message):
    broadcast_text = message.text.strip()
    del user_states[message.from_user.id]
    
    users = users_collection.find({})
    sent = 0
    err = 0
    
    msg = bot.reply_to(message, "📤 Yuborilmoqda...", reply_markup=admin_menu())
    
    for user in users:
        try:
            bot.send_message(user['user_id'], f"📢 {broadcast_text}")
            sent += 1
        except:
            err += 1
    
    bot.edit_message_text(f"✅ Yuborildi: {sent}\n❌ Xatolik: {err}", chat_id=msg.chat.id, message_id=msg.message_id)

# Kod yaratish callback
@bot.callback_query_handler(func=lambda call: call.data.startswith('gen_'))
def generate_code_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Ruxsat yo'q!")
        return
    
    channel_id = call.data.replace('gen_', '')
    channel = channels_collection.find_one({"channel_id": channel_id})
    
    if not channel:
        bot.answer_callback_query(call.id, "❌ Kanal topilmadi!")
        return
    
    try:
        code = get_next_code_number()
        codes_collection.insert_one({
            "code": code,
            "channel_id": channel_id,
            "channel_name": channel.get("channel_name"),
            "is_active": True,
            "used_by": [],
            "used_count": 0,
            "created_by": str(call.from_user.id),
            "created_at": datetime.now()
        })
        
        bot.answer_callback_query(call.id, "✅ Kod yaratildi!")
        bot.edit_message_text(
            f"✅ <b>Kod yaratildi!</b>\n\n🔑 Kod: <code>{code}</code>\n📱 Kanal: {channel.get('channel_name')}\n\n📝 Bu kodni foydalanuvchiga bering.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode='HTML'
        )
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ Xatolik: {e}")

# Asosiy xabar handler (kodlarni tekshirish)
@bot.message_handler(func=lambda m: True)
def handle_message(message):
    user_id = message.from_user.id
    
    # State ichida bo'lsa qaytish
    if user_id in user_states:
        return
    
    text = message.text.strip()
    result = verify_and_use_code(text, user_id, message.from_user.username, message.from_user.first_name)
    
    if result["success"] and result["invite_link"]:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("▷ 𝗞𝗮𝗻𝗮𝗹𝗴𝗮 𝗾𝗼'𝘀𝗵𝗶𝗹𝗶𝘀𝗵 ◁", url=result["invite_link"]))
        bot.reply_to(message, result["message"], reply_markup=markup)
    else:
        bot.reply_to(message, result["message"])

# Flask routes
@app.route('/')
def index():
    return f"<h1>🤖 Bot ishlamoqda!</h1><p>Status: {'✅ Active' if bot_started else '⏳ Starting...'}</p>"

@app.route('/health')
def health():
    return {"status": "healthy" if bot_started else "starting"}

# Botni ishga tushirish
def init_bot():
    global bot_started
    try:
        logger.info("🤖 Bot ishga tushmoqda...")
        bot.remove_webhook()
        time.sleep(0.5)
        bot_info = bot.get_me()
        logger.info(f"✅ Bot: @{bot_info.username}")
        bot_started = True
        bot.infinity_polling(timeout=10, long_polling_timeout=5)
    except Exception as e:
        logger.error(f"❌ Xatolik: {e}")
        bot_started = False

bot_thread = threading.Thread(target=init_bot, daemon=True)
bot_thread.start()

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
