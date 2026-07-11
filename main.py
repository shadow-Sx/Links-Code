import os
import sys
import telebot
from telebot import types
from pymongo import MongoClient
from datetime import datetime, timedelta
import logging
from flask import Flask
import threading
import traceback
import time
import random
import string

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Flask
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

# Havola muddati (daqiqa)
LINK_EXPIRE_MINUTES = 10

# ============ FUNKSIYALAR ============

def generate_random_code(length=8):
    """Random kod yaratish - raqam va harflar"""
    chars = string.ascii_uppercase + string.digits
    return ''.join(random.choice(chars) for _ in range(length))

def get_next_code_number():
    """Ketma-ket raqamli kod"""
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

def create_code_for_channel(channel_id, channel_name, code_type="number"):
    """Kanal uchun kod yaratish"""
    try:
        if code_type == "number":
            code = get_next_code_number()
        else:
            # Random kod - unique bo'lishini tekshirish
            while True:
                code = generate_random_code()
                if not codes_collection.find_one({"code": code}):
                    break
        
        codes_collection.insert_one({
            "code": code,
            "channel_id": channel_id,
            "channel_name": channel_name,
            "code_type": code_type,
            "is_active": True,
            "used_by": [],
            "used_count": 0,
            "created_by": "auto",
            "created_at": datetime.now(),
            "last_used_at": None,
            "last_invite_link": None
        })
        logger.info(f"✅ Kod yaratildi: {code} ({code_type}) -> {channel_name}")
        return code
    except Exception as e:
        logger.error(f"Kod yaratishda xatolik: {e}")
        return None

def get_channel_invite_link(channel_id):
    """Kanal uchun 1 kishilik va vaqtli havola yaratadi"""
    try:
        bot_member = bot.get_chat_member(channel_id, bot.get_me().id)
        if bot_member.status != 'administrator' or not bot_member.can_invite_users:
            return None, None
        
        expire_time = datetime.now() + timedelta(minutes=LINK_EXPIRE_MINUTES)
        
        invite = bot.create_chat_invite_link(
            chat_id=channel_id,
            member_limit=1,
            expire_date=expire_time
        )
        
        logger.info(f"✅ Havola yaratildi: {invite.invite_link[:30]}...")
        return invite.invite_link, expire_time
    except Exception as e:
        logger.error(f"Havola yaratishda xatolik: {e}")
        return None, None

def verify_and_use_code(code, user_id, username, first_name):
    """Kodni tekshirish va havola berish (qayta ishlatish mumkin)"""
    try:
        code_data = codes_collection.find_one({"code": code.upper(), "is_active": True})
        
        if not code_data:
            return {"success": False, "message": "❌ Noto'g'ri yoki aktiv bo'lmagan kod!", "invite_link": None}
        
        channel_data = channels_collection.find_one({"channel_id": code_data["channel_id"]})
        if not channel_data:
            return {"success": False, "message": "❌ Kanal topilmadi!", "invite_link": None}
        
        invite_link, expire_time = get_channel_invite_link(code_data["channel_id"])
        if not invite_link:
            return {"success": False, "message": "❌ Havola yaratishda xatolik!", "invite_link": None}
        
        # Kod statistikasini yangilash (qayta ishlatishga ruxsat)
        user_id_str = str(user_id)
        
        codes_collection.update_one(
            {"_id": code_data["_id"]},
            {
                "$push": {"used_by": {
                    "user_id": user_id_str,
                    "username": username,
                    "first_name": first_name,
                    "used_at": datetime.now(),
                    "expire_time": expire_time
                }},
                "$inc": {"used_count": 1},
                "$set": {
                    "last_used_at": datetime.now(),
                    "last_invite_link": invite_link,
                    "last_expire_time": expire_time
                }
            }
        )
        
        users_collection.update_one(
            {"user_id": user_id_str},
            {
                "$set": {"username": username, "first_name": first_name, "last_used_at": datetime.now()},
                "$push": {"used_codes": {
                    "code": code,
                    "channel_name": channel_data.get("channel_name"),
                    "used_at": datetime.now(),
                    "expire_time": expire_time
                }},
                "$inc": {"total_uses": 1}
            },
            upsert=True
        )
        
        expire_str = expire_time.strftime('%H:%M') if expire_time else "10 daqiqa"
        
        success_message = f"""
<b>Topildi 🎉</b>
<blockquote><b>Kanalga qo'shiling aksholda 10-daqiqadan so'ng havola mudati tugaydi⛓‍💥</b></blockquote>
"""
        return {
            "success": True,
            "message": success_message,
            "invite_link": invite_link
        }
    except Exception as e:
        logger.error(f"Kod tekshirishda xatolik: {e}")
        return {"success": False, "message": "❌ Xatolik!", "invite_link": None}

# ============ ADMIN MENYU ============

def admin_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        types.KeyboardButton('➕ Kanal qo\'shish'),
        types.KeyboardButton('📋 Kanallar')
    )
    markup.add(
        types.KeyboardButton('📊 Statistika'),
        types.KeyboardButton('📢 Xabar yuborish')
    )
    return markup

# ============ BOT KOMANDALARI ============

@bot.message_handler(commands=['start'])
def start(message):
    # Deep linking: /start KOD
    args = message.text.split()
    
    if len(args) > 1:
        # Agar kod bilan kelgan bo'lsa
        code = args[1].upper()
        result = verify_and_use_code(code, message.from_user.id, message.from_user.username, message.from_user.first_name)
        
        if result["success"] and result["invite_link"]:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("▷ 𝗞𝗮𝗻𝗮𝗹𝗴𝗮 𝗾𝗼'𝘀𝗵𝗶𝗹𝗶𝘀𝗵 ◁", url=result["invite_link"]))
            bot.reply_to(message, result["message"], reply_markup=markup)
        else:
            bot.reply_to(message, result["message"])
        return
    
    # Oddiy start
    if is_admin(message.from_user.id):
        bot.reply_to(message, "👑 <b>Admin panel</b>\n\nKerakli amalni tanlang:", reply_markup=admin_menu())
    else:
        bot.reply_to(message, "🔑 <b>Kodni kiriting:</b>", reply_markup=types.ReplyKeyboardRemove())

@bot.message_handler(commands=['help'])
def help_cmd(message):
    if is_admin(message.from_user.id):
        bot.reply_to(message, "👑 Admin panel:\n\n➕ Kanal qo'shish\n📋 Kanallar - ro'yxat va o'chirish\n📊 Statistika\n📢 Xabar yuborish - tugma bilan")
    else:
        bot_msg = f"🤖 <b>Bot haqida</b>\n\n"
        bot_msg += f"📝 Kodni oling va botga yuboring\n"
        bot_msg += f"🔗 Yoki havola orqali: https://t.me/{bot.get_me().username}?start=KOD\n\n"
        bot_msg += f"⏰ Havola 1 kishilik va {LINK_EXPIRE_MINUTES} daqiqa amal qiladi"
        bot.reply_to(message, bot_msg)

# ============ 1. KANAL QO'SHISH ============

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == '➕ Kanal qo\'shish')
def add_channel_start(message):
    user_states[message.from_user.id] = "waiting_channel_id"
    bot.reply_to(
        message,
        """📝 <b>Kanal qo'shish</b>

Kanal ID sini yuboring:
Misol: <code>-1001234567890</code>

📌 Kanal ID sini @getidsbot orqali oling.

❌ Bekor qilish: /cancel""",
        reply_markup=types.ReplyKeyboardRemove()
    )

@bot.message_handler(func=lambda m: user_states.get(m.from_user.id) == "waiting_channel_id" and is_admin(m.from_user.id))
def process_channel_id(message):
    channel_id = message.text.strip()
    
    try:
        chat = bot.get_chat(channel_id)
        bot_member = bot.get_chat_member(channel_id, bot.get_me().id)
        
        if bot_member.status != 'administrator':
            bot.reply_to(message, "❌ Bot bu kanalda admin emas!\n\nBotni kanalga admin qiling va 'Add members' huquqini bering.", reply_markup=admin_menu())
            del user_states[message.from_user.id]
            return
        
        if not bot_member.can_invite_users:
            bot.reply_to(message, "❌ Botda 'Add members' huquqi yo'q!", reply_markup=admin_menu())
            del user_states[message.from_user.id]
            return
        
        channel_name = chat.title or "Noma'lum kanal"
        channel_username = getattr(chat, 'username', None)
        
        # Kanalni saqlash
        channels_collection.update_one(
            {"channel_id": channel_id},
            {"$set": {
                "channel_id": channel_id,
                "channel_name": channel_name,
                "channel_username": channel_username,
                "added_by": str(message.from_user.id),
                "added_by_name": message.from_user.first_name,
                "created_at": datetime.now(),
                "is_active": True
            }},
            upsert=True
        )
        
        # Kod yaratish turini so'rash
        user_states[message.from_user.id] = {
            "state": "waiting_code_type",
            "channel_id": channel_id,
            "channel_name": channel_name
        }
        
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("🔢 Raqam", callback_data=f"codetype_number_{channel_id}"),
            types.InlineKeyboardButton("🎲 Random", callback_data=f"codetype_random_{channel_id}")
        )
        
        bot.reply_to(
            message,
            f"✅ <b>Kanal topildi:</b> {channel_name}\n\n<b>Kod yaratish turini tanlang:</b>",
            reply_markup=markup
        )
        
    except Exception as e:
        bot.reply_to(message, f"❌ Xatolik: {e}\n\nKanal ID sini tekshiring!", reply_markup=admin_menu())
        del user_states[message.from_user.id]

# Kod turini tanlash
@bot.callback_query_handler(func=lambda call: call.data.startswith('codetype_'))
def choose_code_type(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Ruxsat yo'q!")
        return
    
    parts = call.data.split('_')
    code_type = parts[1]  # number yoki random
    channel_id = parts[2]
    
    channel_name = "Kanal"
    if call.message.chat.id in user_states:
        channel_name = user_states[call.message.chat.id].get("channel_name", "Kanal")
        del user_states[call.message.chat.id]
    
    code = create_code_for_channel(channel_id, channel_name, code_type)
    
    if code:
        type_name = "Raqam" if code_type == "number" else "Random"
        bot_username = bot.get_me().username
        deep_link = f"https://t.me/{bot_username}?start={code}"
        
        bot.answer_callback_query(call.id, "✅ Kod yaratildi!")
        
        success_text = f"""
✅ <b>Kod yaratildi!</b>

📱 <b>Kanal:</b> {channel_name}
🔑 <b>Kod:</b> <code>{code}</code>
📋 <b>Tur:</b> {type_name}

🔗 <b>Havola:</b> {deep_link}

📝 <b>Foydalanish:</b>
• Kodni botga yuboring
• Yoki havola orqali: {deep_link}

⏰ Havola: 1 kishilik, {LINK_EXPIRE_MINUTES} daqiqa
"""
        bot.edit_message_text(
            success_text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode='HTML',
            disable_web_page_preview=True
        )
    else:
        bot.answer_callback_query(call.id, "❌ Kod yaratishda xatolik!")
        bot.edit_message_text(
            "❌ Kod yaratishda xatolik!",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id
        )

# ============ 2. KANALLAR RO'YXATI VA O'CHIRISH ============

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == '📋 Kanallar')
def list_channels_btn(message):
    channels = list(channels_collection.find({"is_active": True}))
    
    if not channels:
        bot.reply_to(message, "📋 Kanallar yo'q!")
        return
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    for ch in channels:
        channel_name = ch.get('channel_name', 'Nomsiz')
        code_count = codes_collection.count_documents({"channel_id": ch['channel_id'], "is_active": True})
        btn_text = f"📱 {channel_name} | 🔑 {code_count} kod"
        markup.add(types.InlineKeyboardButton(btn_text, callback_data=f"channel_{ch['channel_id']}"))
    
    bot.reply_to(message, "<b>📋 Kanallar ro'yxati:</b>\n\nKanalni tanlang:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('channel_'))
def channel_detail(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Ruxsat yo'q!")
        return
    
    channel_id = call.data.replace('channel_', '')
    channel = channels_collection.find_one({"channel_id": channel_id})
    
    if not channel:
        bot.answer_callback_query(call.id, "❌ Kanal topilmadi!")
        return
    
    code_count = codes_collection.count_documents({"channel_id": channel_id, "is_active": True})
    used_count = codes_collection.count_documents({"channel_id": channel_id, "used_count": {"$gt": 0}})
    
    text = f"""
📱 <b>{channel.get('channel_name', 'Nomsiz')}</b>

🆔 <b>ID:</b> <code>{channel_id}</code>
{"🔗 <b>Username:</b> @" + channel.get('channel_username') if channel.get('channel_username') else ""}
🔑 <b>Kodlar:</b> {code_count} ta
👥 <b>Ishlatilgan:</b> {used_count} ta
📅 <b>Qo'shilgan:</b> {channel.get('created_at').strftime('%Y-%m-%d %H:%M') if channel.get('created_at') else 'Noma\'lum'}
"""
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔑 Kod yaratish", callback_data=f"codetype_number_{channel_id}"))
    markup.add(types.InlineKeyboardButton("🗑 O'chirish", callback_data=f"delete_ch_{channel_id}"))
    markup.add(types.InlineKeyboardButton("⬅️ Orqaga", callback_data="back_to_channels"))
    
    bot.edit_message_text(
        text,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        parse_mode='HTML',
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data == "back_to_channels")
def back_to_channels(call):
    if not is_admin(call.from_user.id):
        return
    
    channels = list(channels_collection.find({"is_active": True}))
    
    if not channels:
        bot.edit_message_text("📋 Kanallar yo'q!", chat_id=call.message.chat.id, message_id=call.message.message_id)
        return
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    for ch in channels:
        code_count = codes_collection.count_documents({"channel_id": ch['channel_id'], "is_active": True})
        btn_text = f"📱 {ch.get('channel_name', 'Nomsiz')} | 🔑 {code_count} kod"
        markup.add(types.InlineKeyboardButton(btn_text, callback_data=f"channel_{ch['channel_id']}"))
    
    bot.edit_message_text(
        "<b>📋 Kanallar ro'yxati:</b>\n\nKanalni tanlang:",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        parse_mode='HTML',
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('delete_ch_'))
def delete_channel(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Ruxsat yo'q!")
        return
    
    channel_id = call.data.replace('delete_ch_', '')
    channel = channels_collection.find_one({"channel_id": channel_id})
    
    if not channel:
        bot.answer_callback_query(call.id, "❌ Kanal topilmadi!")
        return
    
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("✅ Ha, o'chirish", callback_data=f"confirm_delete_{channel_id}"),
        types.InlineKeyboardButton("❌ Yo'q", callback_data=f"channel_{channel_id}")
    )
    
    bot.edit_message_text(
        f"⚠️ <b>{channel.get('channel_name')}</b> kanalini o'chirmoqchimisiz?\n\nBarcha kodlar ham o'chiriladi!",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        parse_mode='HTML',
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('confirm_delete_'))
def confirm_delete_channel(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Ruxsat yo'q!")
        return
    
    channel_id = call.data.replace('confirm_delete_', '')
    
    channels_collection.update_one(
        {"channel_id": channel_id},
        {"$set": {"is_active": False, "deleted_at": datetime.now()}}
    )
    
    codes_collection.update_many(
        {"channel_id": channel_id},
        {"$set": {"is_active": False, "deleted_at": datetime.now()}}
    )
    
    bot.answer_callback_query(call.id, "✅ Kanal o'chirildi!")
    
    channels = list(channels_collection.find({"is_active": True}))
    
    if not channels:
        bot.edit_message_text("📋 Kanallar yo'q!", chat_id=call.message.chat.id, message_id=call.message.message_id)
        return
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    for ch in channels:
        code_count = codes_collection.count_documents({"channel_id": ch['channel_id'], "is_active": True})
        btn_text = f"📱 {ch.get('channel_name', 'Nomsiz')} | 🔑 {code_count} kod"
        markup.add(types.InlineKeyboardButton(btn_text, callback_data=f"channel_{ch['channel_id']}"))
    
    bot.edit_message_text(
        "<b>📋 Kanallar ro'yxati:</b>\n\nKanalni tanlang:",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        parse_mode='HTML',
        reply_markup=markup
    )

# ============ 3. STATISTIKA ============

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == '📊 Statistika')
def stats_btn(message):
    total_channels = channels_collection.count_documents({"is_active": True})
    total_codes = codes_collection.count_documents({})
    active_codes = codes_collection.count_documents({"is_active": True})
    used_codes = codes_collection.count_documents({"used_count": {"$gt": 0}})
    
    all_users = users_collection.distinct("user_id")
    total_users = len(all_users)
    
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_users = users_collection.count_documents({"last_used_at": {"$gte": today}})
    
    setting = settings_collection.find_one({"_id": "code_counter"})
    last_code = setting.get('last_code_number', 0) if setting else 0
    
    text = f"""
📊 <b>Bot Statistikasi</b>

📱 <b>Kanallar:</b> {total_channels} ta
🔑 <b>Jami kodlar:</b> {total_codes} ta
✅ <b>Aktiv kodlar:</b> {active_codes} ta
👥 <b>Ishlatilgan:</b> {used_codes} ta

👤 <b>Jami foydalanuvchilar:</b> {total_users} ta
🆕 <b>Bugun:</b> {today_users} ta

🔢 <b>Oxirgi raqam:</b> {last_code}
⏰ <b>Havola muddati:</b> {LINK_EXPIRE_MINUTES} daqiqa

⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}
"""
    bot.reply_to(message, text)

# ============ 4. XABAR YUBORISH ============

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == '📢 Xabar yuborish')
def broadcast_btn(message):
    user_states[message.from_user.id] = {"state": "waiting_broadcast_text"}
    bot.reply_to(
        message,
        """📢 <b>Xabar yuborish</b>

1️⃣ Avval xabar matnini yuboring
2️⃣ Keyin tugma qo'shish uchun:
<code>Tugma matni - havola</code>

Misol:
<code>Kanalga o'tish - https://t.me/kanal</code>

❌ Bekor qilish: /cancel""",
        reply_markup=types.ReplyKeyboardRemove()
    )

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and isinstance(user_states.get(m.from_user.id), dict) and user_states[m.from_user.id].get("state") == "waiting_broadcast_text")
def process_broadcast_text(message):
    user_states[message.from_user.id] = {
        "state": "waiting_broadcast_buttons",
        "text": message.text.strip()
    }
    bot.reply_to(
        message,
        """📝 Xabar matni saqlandi!

Endi tugmalarni qo'shing:
<code>Tugma matni - havola</code>

Yoki tugmasiz yuborish uchun <b>/send</b>

❌ Bekor qilish: /cancel"""
    )

@bot.message_handler(commands=['send'])
def send_broadcast_no_buttons(message):
    if not is_admin(message.from_user.id) or message.from_user.id not in user_states:
        return
    
    state_data = user_states[message.from_user.id]
    if not isinstance(state_data, dict) or state_data.get("state") != "waiting_broadcast_buttons":
        return
    
    text = state_data.get("text", "")
    del user_states[message.from_user.id]
    
    send_broadcast_to_users(message, text, None)

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and isinstance(user_states.get(m.from_user.id), dict) and user_states[m.from_user.id].get("state") == "waiting_broadcast_buttons")
def process_broadcast_buttons(message):
    text = message.text.strip()
    
    buttons = []
    lines = text.split('\n')
    
    for line in lines:
        if ' - ' in line:
            parts = line.split(' - ', 1)
            if len(parts) == 2:
                btn_text = parts[0].strip()
                btn_url = parts[1].strip()
                if btn_text and btn_url:
                    buttons.append({"text": btn_text, "url": btn_url})
    
    state_data = user_states[message.from_user.id]
    broadcast_text = state_data.get("text", "")
    del user_states[message.from_user.id]
    
    send_broadcast_to_users(message, broadcast_text, buttons)

def send_broadcast_to_users(message, text, buttons):
    users = users_collection.find({})
    sent = 0
    err = 0
    
    status_msg = bot.reply_to(message, "📤 Xabar yuborilmoqda...", reply_markup=admin_menu())
    
    for user in users:
        try:
            if buttons:
                markup = types.InlineKeyboardMarkup()
                for btn in buttons:
                    markup.add(types.InlineKeyboardButton(btn['text'], url=btn['url']))
                bot.send_message(user['user_id'], text, reply_markup=markup)
            else:
                bot.send_message(user['user_id'], text)
            sent += 1
        except:
            err += 1
    
    bot.edit_message_text(
        f"✅ <b>Xabar yuborildi!</b>\n\n📤 Yuborildi: {sent}\n❌ Xatolik: {err}",
        chat_id=status_msg.chat.id,
        message_id=status_msg.message_id,
        parse_mode='HTML'
    )

# ============ CANCEL ============

@bot.message_handler(commands=['cancel'])
def cancel(message):
    if message.from_user.id in user_states:
        del user_states[message.from_user.id]
        if is_admin(message.from_user.id):
            bot.reply_to(message, "❌ Bekor qilindi!", reply_markup=admin_menu())
        else:
            bot.reply_to(message, "❌ Bekor qilindi!", reply_markup=types.ReplyKeyboardRemove())

# ============ ASOSIY XABAR HANDLER ============

@bot.message_handler(func=lambda m: True)
def handle_message(message):
    user_id = message.from_user.id
    
    if user_id in user_states:
        return
    
    text = message.text.strip().upper()
    result = verify_and_use_code(text, user_id, message.from_user.username, message.from_user.first_name)
    
    if result["success"] and result["invite_link"]:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("▷ 𝗞𝗮𝗻𝗮𝗹𝗴𝗮 𝗾𝗼'𝘀𝗵𝗶𝗹𝗶𝘀𝗵 ◁", url=result["invite_link"]))
        bot.reply_to(message, result["message"], reply_markup=markup)
    else:
        bot.reply_to(message, result["message"])

# ============ FLASK ============

@app.route('/')
def index():
    return f"<h1>🤖 Bot ishlamoqda!</h1><p>Status: {'✅ Active' if bot_started else '⏳ Starting...'}</p><p>Havola muddati: {LINK_EXPIRE_MINUTES} daqiqa</p>"

@app.route('/health')
def health():
    return {"status": "healthy" if bot_started else "starting", "link_expire_minutes": LINK_EXPIRE_MINUTES}

# ============ BOTNI ISHGA TUSHIRISH ============

def init_bot():
    global bot_started
    try:
        logger.info("🤖 Bot ishga tushmoqda...")
        bot.remove_webhook()
        time.sleep(0.5)
        bot_info = bot.get_me()
        logger.info(f"✅ Bot: @{bot_info.username}")
        logger.info(f"⏰ Havola muddati: {LINK_EXPIRE_MINUTES} daqiqa")
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
