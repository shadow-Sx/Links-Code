import os
import sys
import telebot
from telebot import types
from pymongo import MongoClient
from datetime import datetime
import logging
from flask import Flask, request
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

# Flask app yaratish (Render Web Service uchun)
app = Flask(__name__)

# Environment variables (Render'dan oladi)
BOT_TOKEN = os.getenv('BOT_TOKEN')
MONGODB_URI = os.getenv('MONGODB_URI')
DB_NAME = os.getenv('DB_NAME', 'UzbLinks')

# Admin ID larini olish va tozalash
admin_ids_str = os.getenv('ADMIN_IDS', '')
ADMIN_IDS = []
if admin_ids_str:
    try:
        ADMIN_IDS = [int(id_str.strip()) for id_str in admin_ids_str.split(',') if id_str.strip()]
    except ValueError:
        logger.error("ADMIN_IDS noto'g'ri formatda! Misol: ADMIN_IDS=123456789,987654321")

logger.info(f"🔧 Konfiguratsiya yuklandi: DB={DB_NAME}, Adminlar={len(ADMIN_IDS)} ta")

# MongoDB ulanish
try:
    if not MONGODB_URI:
        raise ValueError("MONGODB_URI environment variable topilmadi!")
    
    logger.info("🍃 MongoDB ga ulanishga harakat qilinmoqda...")
    
    client = MongoClient(
        MONGODB_URI,
        serverSelectionTimeoutMS=10000,
        connectTimeoutMS=10000,
        socketTimeoutMS=10000
    )
    
    client.admin.command('ping')
    
    db = client[DB_NAME]
    
    codes_collection = db['invite_codes']
    channels_collection = db['channels']
    users_collection = db['users']
    settings_collection = db['settings']
    
    logger.info("✅ MongoDB ga muvaffaqiyatli ulandi!")
    
except Exception as e:
    logger.error(f"❌ MongoDB ulanishda xatolik: {e}")
    sys.exit(1)

# MongoDB indekslar yaratish
try:
    codes_collection.create_index("code", unique=True)
    codes_collection.create_index("channel_id")
    channels_collection.create_index("channel_id", unique=True)
    users_collection.create_index("user_id", unique=True)
    logger.info("✅ MongoDB indekslar yaratildi!")
except Exception as e:
    logger.warning(f"⚠️ Indeks yaratishda ogohlantirish: {e}")

# Bot yaratish
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML')
logger.info("🤖 Bot obyekti yaratildi")

# Bot ishga tushganligini ko'rsatuvchi flag
bot_started = False

# Foydalanuvchi holatini saqlash (kanal qo'shish jarayoni uchun)
user_states = {}

# Oxirgi kod raqamini olish
def get_next_code_number():
    """Eng oxirgi kod raqamini qaytaradi va 1 ga oshiradi"""
    try:
        setting = settings_collection.find_one_and_update(
            {"_id": "code_counter"},
            {"$inc": {"last_code_number": 1}},
            upsert=True,
            return_document=True
        )
        
        if setting:
            code_number = setting.get('last_code_number', 1)
        else:
            settings_collection.insert_one({"_id": "code_counter", "last_code_number": 1})
            code_number = 1
        
        return str(code_number)
    except Exception as e:
        logger.error(f"Kod raqami olishda xatolik: {e}")
        return str(int(datetime.now().timestamp()))

def is_admin(user_id):
    """Foydalanuvchi admin ekanligini tekshiradi"""
    return user_id in ADMIN_IDS

def get_channel_invite_link(channel_id):
    """Kanal uchun bir martalik havola yaratadi"""
    try:
        bot_member = bot.get_chat_member(channel_id, bot.get_me().id)
        
        if bot_member.status != 'administrator':
            logger.error(f"Bot {channel_id} kanalda admin emas!")
            return None
        
        if not bot_member.can_invite_users:
            logger.error(f"Bot {channel_id} kanalda taklif qilish huquqiga ega emas!")
            return None
        
        invite_link = bot.create_chat_invite_link(
            chat_id=channel_id,
            member_limit=1,
            name=f"One-time invite"
        )
        
        logger.info(f"✅ Havola yaratildi: {invite_link.invite_link[:30]}...")
        return invite_link.invite_link
        
    except Exception as e:
        logger.error(f"Havola yaratishda xatolik ({channel_id}): {e}")
        return None

def verify_and_use_code(code, user_id, username, first_name):
    """Kodni tekshiradi va kanalga bir martalik havola beradi"""
    try:
        code_data = codes_collection.find_one({
            "code": code,
            "is_active": True
        })
        
        if not code_data:
            return {
                "success": False, 
                "message": "❌ <b>Noto'g'ri yoki aktiv bo'lmagan kod!</b>\n\nIltimos, to'g'ri kodni kiriting.",
                "invite_link": None
            }
        
        user_id_str = str(user_id)
        if "used_by" in code_data:
            used_users = code_data["used_by"]
            if any(user["user_id"] == user_id_str for user in used_users):
                return {
                    "success": False, 
                    "message": "❌ <b>Siz bu kodni oldin ishlatgansiz!</b>\n\nHar bir kod faqat bir marta ishlatilishi mumkin.",
                    "invite_link": None
                }
        
        channel_data = channels_collection.find_one({"channel_id": code_data["channel_id"]})
        if not channel_data:
            return {
                "success": False, 
                "message": "❌ <b>Kanal topilmadi!</b>\n\nAdmin bilan bog'laning.",
                "invite_link": None
            }
        
        invite_link = get_channel_invite_link(code_data["channel_id"])
        if not invite_link:
            return {
                "success": False, 
                "message": "❌ <b>Havola yaratishda texnik xatolik!</b>\n\nIltimos, keyinroq qayta urinib ko'ring.",
                "invite_link": None
            }
        
        codes_collection.update_one(
            {"_id": code_data["_id"]},
            {
                "$push": {
                    "used_by": {
                        "user_id": user_id_str,
                        "username": username or "No username",
                        "first_name": first_name or "No name",
                        "used_at": datetime.now()
                    }
                },
                "$inc": {"used_count": 1},
                "$set": {
                    "last_used_at": datetime.now(),
                    "last_invite_link": invite_link
                }
            }
        )
        
        users_collection.update_one(
            {"user_id": user_id_str},
            {
                "$set": {
                    "username": username,
                    "first_name": first_name,
                    "last_used_at": datetime.now()
                },
                "$push": {
                    "used_codes": {
                        "code": code,
                        "channel_name": channel_data.get("channel_name", "Noma'lum"),
                        "used_at": datetime.now()
                    }
                },
                "$inc": {"total_uses": 1}
            },
            upsert=True
        )
        
        success_message = f"""
✅ <b>Kod qabul qilindi!</b>

📱 <b>Kanal:</b> {channel_data.get('channel_name', 'Noma\'lum kanal')}

🔽 Kanalga qo'shilish uchun pastdagi tugmani bosing:
"""
        logger.info(f"✅ Kod {code} ishlatildi - Foydalanuvchi: {user_id}")
        return {
            "success": True, 
            "message": success_message,
            "invite_link": invite_link
        }
        
    except Exception as e:
        logger.error(f"Kod tekshirishda xatolik: {e}")
        return {
            "success": False, 
            "message": "❌ <b>Xatolik yuz berdi!</b>\n\nIltimos, qaytadan urinib ko'ring.",
            "invite_link": None
        }

# ============ ADMIN MENYU ============
def admin_menu():
    """Admin menyusi tugmalari"""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    btn_add_channel = types.KeyboardButton('➕ Kanal qo\'shish')
    btn_generate_code = types.KeyboardButton('🔑 Kod yaratish')
    btn_list_channels = types.KeyboardButton('📋 Kanallar')
    btn_list_codes = types.KeyboardButton('📝 Kodlar')
    btn_stats = types.KeyboardButton('📊 Statistika')
    btn_broadcast = types.KeyboardButton('📢 Xabar yuborish')
    btn_back = types.KeyboardButton('⬅️ Orqaga')
    
    markup.add(btn_add_channel, btn_generate_code)
    markup.add(btn_list_channels, btn_list_codes)
    markup.add(btn_stats, btn_broadcast)
    markup.add(btn_back)
    
    return markup

# ============ ODDIY FOYDALANUVCHI MENYU ============
def user_menu():
    """Oddiy foydalanuvchi menyusi"""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    btn_help = types.KeyboardButton('📚 Yordam')
    btn_info = types.KeyboardButton('ℹ️ Bot haqida')
    
    markup.add(btn_help, btn_info)
    
    return markup

# ============ BOT KOMANDALARI ============
@bot.message_handler(commands=['start'])
def send_welcome(message):
    """Start komandasi"""
    user_id = message.from_user.id
    
    if is_admin(user_id):
        # Admin uchun
        welcome_text = f"""
👑 <b>Admin panelga xush kelibsiz, {message.from_user.first_name}!</b>

Quyidagi tugmalardan foydalaning:
• ➕ Kanal qo'shish
• 🔑 Kod yaratish
• 📋 Kanallar ro'yxati
• 📝 Kodlar ro'yxati
• 📊 Statistika
"""
        bot.reply_to(message, welcome_text, reply_markup=admin_menu())
    else:
        # Oddiy foydalanuvchi uchun
        welcome_text = """
🤖 <b>Kod orqali kanalga qo'shilish boti</b>

🔑 <b>Kodni kiriting:</b>
"""
        bot.reply_to(message, welcome_text, reply_markup=user_menu())

@bot.message_handler(commands=['help'])
@bot.message_handler(func=lambda message: message.text == '📚 Yordam')
def send_help(message):
    """Yordam"""
    help_text = """
📚 <b>Yordam</b>

<b>Bot qanday ishlaydi?</b>
1. Sizga maxsus kod beriladi
2. Kodni botga yuborasiz
3. Bot sizga bir martalik havola beradi
4. Tugma orqali kanalga qo'shilasiz

<b>Muhim:</b>
• Har bir kod faqat 1 marta ishlatiladi
• Havola muddati cheksiz
• Kodni qayta ishlata olmaysiz
"""
    bot.reply_to(message, help_text)

@bot.message_handler(func=lambda message: message.text == 'ℹ️ Bot haqida')
def about_bot(message):
    """Bot haqida"""
    about_text = """
ℹ️ <b>Bot haqida</b>

<b>Nomi:</b> UzbLinks Invite Bot
<b>Versiya:</b> 1.0

<b>Xususiyatlari:</b>
✅ Bir martalik havolalar
✅ Avtomatik kod generatsiyasi
✅ Xavfsiz va ishonchli
"""
    bot.reply_to(message, about_text)

# ============ ADMIN TUGMALARI ============
@bot.message_handler(func=lambda message: message.text == '➕ Kanal qo\'shish' and is_admin(message.from_user.id))
def add_channel_start(message):
    """Kanal qo'shish jarayoni boshlanishi"""
    user_states[message.from_user.id] = "waiting_channel_id"
    
    msg = bot.reply_to(
        message,
        """📝 <b>Kanal qo'shish</b>

Kanal ID sini yuboring:
<code>-1001234567890</code>

<b>Kanal ID sini olish:</b>
• @getidsbot ga kanaldan xabar forward qiling
• Bot sizga ID ni beradi

❌ Bekor qilish uchun /cancel""",
        parse_mode='HTML'
    )

@bot.message_handler(func=lambda message: message.text == '🔑 Kod yaratish' and is_admin(message.from_user.id))
def generate_code_button(message):
    """Kod yaratish tugmasi"""
    channels = list(channels_collection.find({"is_active": True}))
    
    if not channels:
        bot.reply_to(message, "❌ Hech qanday kanal qo'shilmagan! Avval kanal qo'shing.")
        return
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    for channel in channels:
        btn_text = f"📱 {channel['channel_name']}"
        callback_data = f"gen_code_{channel['channel_id']}"
        markup.add(types.InlineKeyboardButton(btn_text, callback_data=callback_data))
    
    bot.reply_to(
        message,
        "<b>Qaysi kanal uchun kod yaratamiz?</b>",
        reply_markup=markup,
        parse_mode='HTML'
    )

@bot.message_handler(func=lambda message: message.text == '📋 Kanallar' and is_admin(message.from_user.id))
def list_channels_button(message):
    """Kanallar ro'yxati tugmasi"""
    channels = list(channels_collection.find({"is_active": True}))
    
    if not channels:
        bot.reply_to(message, "📋 Hech qanday kanal qo'shilmagan!")
        return
    
    response = "📋 <b>Aktiv kanallar:</b>\n\n"
    for i, ch in enumerate(channels, 1):
        response += f"{i}. 📱 <b>{ch['channel_name']}</b>\n"
        response += f"   🆔 <code>{ch['channel_id']}</code>\n"
        if ch.get('channel_username'):
            response += f"   🔗 @{ch['channel_username']}\n"
        response += f"   📅 {ch['created_at'].strftime('%Y-%m-%d')}\n\n"
    
    bot.reply_to(message, response, parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text == '📝 Kodlar' and is_admin(message.from_user.id))
def list_codes_button(message):
    """Kodlar ro'yxati tugmasi"""
    codes = list(codes_collection.find({"is_active": True}).sort("created_at", -1).limit(30))
    
    if not codes:
        bot.reply_to(message, "📋 Hech qanday aktiv kod yo'q!")
        return
    
    response = "📝 <b>Aktiv kodlar:</b>\n\n"
    for i, cd in enumerate(codes, 1):
        status = "✅ Ishlatilgan" if cd['used_count'] > 0 else "🆕 Yangi"
        response += f"{i}. 🔑 <code>{cd['code']}</code> | {cd.get('channel_name', 'N/A')} | {status}\n"
    
    bot.reply_to(message, response, parse_mode='HTML')

@bot.message_handler(func=lambda message: message.text == '📊 Statistika' and is_admin(message.from_user.id))
def stats_button(message):
    """Statistika tugmasi"""
    try:
        total_channels = channels_collection.count_documents({"is_active": True})
        total_codes = codes_collection.count_documents({})
        active_codes = codes_collection.count_documents({"is_active": True})
        used_codes = codes_collection.count_documents({"used_count": {"$gt": 0}})
        total_users = users_collection.count_documents({})
        
        setting = settings_collection.find_one({"_id": "code_counter"})
        last_code = setting.get('last_code_number', 0) if setting else 0
        
        stats_text = f"""
📊 <b>Statistika</b>

📱 Kanallar: <b>{total_channels}</b>
🔑 Jami kodlar: <b>{total_codes}</b>
✅ Aktiv kodlar: <b>{active_codes}</b>
👥 Ishlatilgan: <b>{used_codes}</b>
👤 Foydalanuvchilar: <b>{total_users}</b>
🔢 Oxirgi kod: <b>{last_code}</b>

⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}
"""
        bot.reply_to(message, stats_text, parse_mode='HTML')
    except Exception as e:
        bot.reply_to(message, f"❌ Xatolik: {e}")

@bot.message_handler(func=lambda message: message.text == '📢 Xabar yuborish' and is_admin(message.from_user.id))
def broadcast_button(message):
    """Broadcast tugmasi"""
    user_states[message.from_user.id] = "waiting_broadcast"
    bot.reply_to(
        message,
        "📢 <b>Barcha foydalanuvchilarga yuboriladigan xabarni yozing:</b>\n\n❌ Bekor qilish uchun /cancel",
        parse_mode='HTML'
    )

@bot.message_handler(func=lambda message: message.text == '⬅️ Orqaga' and is_admin(message.from_user.id))
def back_to_start(message):
    """Orqaga qaytish"""
    send_welcome(message)

@bot.message_handler(commands=['cancel'])
def cancel_action(message):
    """Jarayonni bekor qilish"""
    if message.from_user.id in user_states:
        del user_states[message.from_user.id]
        bot.reply_to(message, "❌ Jarayon bekor qilindi!", reply_markup=admin_menu() if is_admin(message.from_user.id) else user_menu())
    else:
        bot.reply_to(message, "❌ Bekor qilinadigan jarayon yo'q!")

# ============ STATE HANDLER - Kanal qo'shish ============
@bot.message_handler(func=lambda message: user_states.get(message.from_user.id) == "waiting_channel_id")
def process_channel_id(message):
    """Kanal ID sini qabul qilish"""
    if not is_admin(message.from_user.id):
        return
    
    channel_id = message.text.strip()
    
    try:
        # Kanalni tekshirish
        chat = bot.get_chat(channel_id)
        bot_member = bot.get_chat_member(channel_id, bot.get_me().id)
        
        if bot_member.status != 'administrator':
            bot.reply_to(message, "❌ Bot bu kanalda admin emas! Avval botni kanalga admin qiling va 'Add members' huquqini bering.")
            del user_states[message.from_user.id]
            return
        
        if not bot_member.can_invite_users:
            bot.reply_to(message, "❌ Botda 'Add members' huquqi yo'q!")
            del user_states[message.from_user.id]
            return
        
        # Kanal nomini olish
        channel_name = chat.title or f"Kanal {channel_id}"
        
        # Kanalni saqlash
        channels_collection.update_one(
            {"channel_id": channel_id},
            {
                "$set": {
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                    "channel_username": getattr(chat, 'username', None),
                    "added_by": str(message.from_user.id),
                    "created_at": datetime.now(),
                    "is_active": True
                }
            },
            upsert=True
        )
        
        del user_states[message.from_user.id]
        
        bot.reply_to(
            message,
            f"""✅ <b>Kanal muvaffaqiyatli qo'shildi!</b>

📱 <b>Nomi:</b> {channel_name}
🆔 <b>ID:</b> <code>{channel_id}</code>
👤 <b>Username:</b> @{getattr(chat, 'username', 'Mavjud emas')}

Endi 🔑 Kod yaratish tugmasini bosing!""",
            parse_mode='HTML',
            reply_markup=admin_menu()
        )
        
    except Exception as e:
        bot.reply_to(
            message,
            f"❌ <b>Xatolik!</b>\n\n{str(e)}\n\nKanal ID sini tekshiring va bot kanalga a'zo ekanligini tekshiring.",
            parse_mode='HTML'
        )
        del user_states[message.from_user.id]

# ============ STATE HANDLER - Broadcast ============
@bot.message_handler(func=lambda message: user_states.get(message.from_user.id) == "waiting_broadcast")
def process_broadcast(message):
    """Broadcast xabarini yuborish"""
    if not is_admin(message.from_user.id):
        return
    
    broadcast_text = message.text.strip()
    del user_states[message.from_user.id]
    
    users = users_collection.find({})
    sent_count = 0
    error_count = 0
    
    processing_msg = bot.reply_to(message, "📤 Xabar yuborilmoqda...")
    
    for user in users:
        try:
            bot.send_message(
                user['user_id'],
                f"📢 <b>E'lon</b>\n\n{broadcast_text}",
                parse_mode='HTML'
            )
            sent_count += 1
        except:
            error_count += 1
    
    bot.edit_message_text(
        f"✅ <b>Xabar yuborildi!</b>\n\n📤 Yuborildi: {sent_count}\n❌ Xatolik: {error_count}",
        chat_id=processing_msg.chat.id,
        message_id=processing_msg.message_id,
        parse_mode='HTML'
    )

# ============ KOD YARATISH CALLBACK ============
@bot.callback_query_handler(func=lambda call: call.data.startswith('gen_code_'))
def generate_code_for_channel(call):
    """Tanlangan kanal uchun kod yaratish"""
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Ruxsat yo'q!")
        return
    
    channel_id = call.data.replace('gen_code_', '')
    channel = channels_collection.find_one({"channel_id": channel_id})
    
    if not channel:
        bot.answer_callback_query(call.id, "❌ Kanal topilmadi!")
        return
    
    try:
        code = get_next_code_number()
        
        codes_collection.insert_one({
            "code": code,
            "channel_id": channel_id,
            "channel_name": channel.get("channel_name", "Noma'lum"),
            "is_active": True,
            "used_by": [],
            "used_count": 0,
            "created_by": str(call.from_user.id),
            "created_at": datetime.now(),
            "last_used_at": None,
            "last_invite_link": None
        })
        
        bot.answer_callback_query(call.id, "✅ Kod yaratildi!")
        
        bot.edit_message_text(
            f"""✅ <b>Kod muvaffaqiyatli yaratildi!</b>

🔑 <b>Kod:</b> <code>{code}</code>
📱 <b>Kanal:</b> {channel.get('channel_name')}

📝 Foydalanuvchiga ushbu kodni bering:
<code>{code}</code>""",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode='HTML'
        )
        
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ Xatolik: {e}")

# ============ ASOSIY XABAR HANDLER ============
@bot.message_handler(func=lambda message: True)
def handle_all_messages(message):
    """Barcha xabarlarni qayta ishlash - asosan kodlarni tekshirish"""
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    text = message.text.strip()
    
    # Agar foydalanuvchi biror holatda bo'lsa (waiting state)
    if user_id in user_states:
        return
    
    # Kodni tekshirish
    result = verify_and_use_code(text, user_id, username, first_name)
    
    if result["success"] and result["invite_link"]:
        markup = types.InlineKeyboardMarkup()
        btn_join = types.InlineKeyboardButton(
            text="▷ 𝗞𝗮𝗻𝗮𝗹𝗴𝗮 𝗾𝗼'𝘀𝗵𝗶𝗹𝗶𝘀𝗵 ◁",
            url=result["invite_link"]
        )
        markup.add(btn_join)
        
        bot.reply_to(message, result["message"], reply_markup=markup, parse_mode='HTML')
    else:
        # Agar kod noto'g'ri bo'lsa va admin bo'lmasa
        if not is_admin(user_id):
            bot.reply_to(
                message,
                result["message"] + "\n\n💡 <b>Yangi kodni kiriting:</b>",
                parse_mode='HTML'
            )
        else:
            bot.reply_to(message, result["message"], parse_mode='HTML')

# ============ FLASK ROUTES ============
@app.route('/')
def index():
    """Bosh sahifa"""
    global bot_started
    return f"""
    <html>
        <head>
            <title>UzbLinks Bot</title>
            <meta charset="UTF-8">
            <style>
                body {{
                    font-family: Arial;
                    text-align: center;
                    padding: 50px;
                    background: linear-gradient(135deg, #667eea, #764ba2);
                    color: white;
                }}
                .container {{
                    background: rgba(255,255,255,0.1);
                    padding: 30px;
                    border-radius: 10px;
                    display: inline-block;
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>🤖 UzbLinks Bot</h1>
                <p>Status: {"✅ Active" if bot_started else "⏳ Starting..."}</p>
                <p>DB: {DB_NAME}</p>
                <p>Adminlar: {len(ADMIN_IDS)} ta</p>
            </div>
        </body>
    </html>
    """

@app.route('/health')
def health():
    """Health check"""
    global bot_started
    return {
        "status": "healthy" if bot_started else "starting",
        "service": "UzbLinks Bot",
        "timestamp": datetime.now().isoformat()
    }, 200

# ============ BOTNI ISHGA TUSHIRISH ============
def init_bot():
    """Botni ishga tushirish"""
    global bot_started
    logger.info("=" * 50)
    logger.info("🤖 Bot ishga tushirilmoqda...")
    logger.info("=" * 50)
    
    try:
        logger.info("🔄 Webhook o'chirilmoqda...")
        bot.remove_webhook()
        time.sleep(0.5)
        
        logger.info("📡 Bot ma'lumotlari olinmoqda...")
        bot_info = bot.get_me()
        logger.info(f"✅ Bot topildi: @{bot_info.username} - {bot_info.first_name}")
        
        bot_started = True
        logger.info("🎉 Bot muvaffaqiyatli ishga tushdi!")
        logger.info("🔄 Polling boshlanmoqda...")
        
        bot.infinity_polling(timeout=10, long_polling_timeout=5)
        
    except Exception as e:
        logger.error(f"❌ Bot ishga tushishda xatolik: {e}")
        logger.error(traceback.format_exc())
        bot_started = False

# Botni avtomatik ishga tushirish
logger.info("🚀 Botni ishga tushirish boshlandi...")
bot_thread = threading.Thread(target=init_bot, name="BotThread", daemon=True)
bot_thread.start()
logger.info(f"✅ Bot thread yaratildi: {bot_thread.name}")

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    logger.info(f"🌐 Web server {port} portda ishga tushmoqda...")
    app.run(host='0.0.0.0', port=port, debug=False)
