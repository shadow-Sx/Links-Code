import os
import sys
import telebot
from telebot import types
from pymongo import MongoClient
from datetime import datetime
import logging
from flask import Flask, request
import threading

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
DB_NAME = os.getenv('DB_NAME', 'invite_bot_db')

# Admin ID larini olish va tozalash
admin_ids_str = os.getenv('ADMIN_IDS', '')
ADMIN_IDS = []
if admin_ids_str:
    try:
        ADMIN_IDS = [int(id_str.strip()) for id_str in admin_ids_str.split(',') if id_str.strip()]
    except ValueError:
        logger.error("ADMIN_IDS noto'g'ri formatda! Misol: ADMIN_IDS=123456789,987654321")

# MongoDB ulanish
try:
    if not MONGODB_URI:
        raise ValueError("MONGODB_URI environment variable topilmadi!")
    
    client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
    client.admin.command('ping')
    
    db = client[DB_NAME]
    
    # Collections
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
    logger.warning(f"Indeks yaratishda ogohlantirish: {e}")

# Bot yaratish
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML')

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

# Yordamchi funksiyalar
def is_admin(user_id):
    """Foydalanuvchi admin ekanligini tekshiradi"""
    return user_id in ADMIN_IDS

def get_channel_invite_link(channel_id):
    """Kanal uchun bir martalik havola yaratadi"""
    try:
        chat = bot.get_chat(channel_id)
        
        # Bot kanalda admin ekanligini tekshirish
        bot_member = bot.get_chat_member(channel_id, bot.get_me().id)
        if not bot_member.can_invite_users:
            logger.error(f"Bot {channel_id} kanalda taklif qilish huquqiga ega emas!")
            return None
        
        # Yangi bir martalik taklif havolasi yaratish
        invite_link = bot.create_chat_invite_link(
            chat_id=channel_id,
            member_limit=1,
            name=f"One-time invite {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        logger.info(f"Havola yaratildi: {invite_link.invite_link}")
        return invite_link.invite_link
        
    except Exception as e:
        logger.error(f"Havola yaratishda xatolik ({channel_id}): {e}")
        return None

def verify_and_use_code(code, user_id, username, first_name):
    """Kodni tekshiradi va kanalga bir martalik havola beradi"""
    try:
        # Kodni qidirish
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
        
        # Foydalanuvchi bu kodni oldin ishlatganmi tekshirish
        user_id_str = str(user_id)
        if "used_by" in code_data:
            used_users = code_data["used_by"]
            if any(user["user_id"] == user_id_str for user in used_users):
                return {
                    "success": False, 
                    "message": "❌ <b>Siz bu kodni oldin ishlatgansiz!</b>\n\nHar bir kod faqat bir marta ishlatilishi mumkin.",
                    "invite_link": None
                }
        
        # Kanal ma'lumotlari
        channel_data = channels_collection.find_one({"channel_id": code_data["channel_id"]})
        if not channel_data:
            return {
                "success": False, 
                "message": "❌ <b>Kanal topilmadi!</b>\n\nAdmin bilan bog'laning.",
                "invite_link": None
            }
        
        # Bir martalik havola yaratish
        invite_link = get_channel_invite_link(code_data["channel_id"])
        if not invite_link:
            return {
                "success": False, 
                "message": "❌ <b>Havola yaratishda texnik xatolik!</b>\n\nIltimos, keyinroq qayta urinib ko'ring yoki admin bilan bog'laning.",
                "invite_link": None
            }
        
        # Kodni yangilash
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
        
        # Foydalanuvchi statistikasini yangilash
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
        
        # Muvaffaqiyatli xabar
        success_message = f"""
<b>Topildi 🎉</b>
<blockquote><b>Kanalga qoshilish uchun pastdagi tugmani bosing va kanalga qoshiling</b></blockquote>
"""
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

# Bot komandalari
@bot.message_handler(commands=['start'])
def send_welcome(message):
    """Start komandasi"""
    welcome_text = """
🤖 <b>One-Time Invite Bot</b>

Assalomu alaykum, <b>{}</b>!

Men maxsus kod orqali kanallarga bir martalik taklif havolasini beruvchi botman.

📝 <b>Ishlatish tartibi:</b>
1️⃣ Kodni oling
2️⃣ Botga kodni yuboring
3️⃣ Bir martalik havolani oling
4️⃣ Havola orqali kanalga qo'shiling

💡 Kodni shunchaki xabar sifatida yuboring!
""".format(message.from_user.first_name)
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    btn_help = types.KeyboardButton('📚 Yordam')
    btn_info = types.KeyboardButton('ℹ️ Bot haqida')
    markup.add(btn_help, btn_info)
    
    bot.reply_to(message, welcome_text, reply_markup=markup)

@bot.message_handler(commands=['help'])
@bot.message_handler(func=lambda message: message.text == '📚 Yordam')
def send_help(message):
    """Yordam komandasi"""
    help_text = """
📚 <b>Yordam</b>

<b>Bot qanday ishlaydi?</b>
1. Admin botga kanal va kod qo'shadi
2. Sizga maxsus kod beriladi
3. Kodni botga yuborasiz
4. Bot sizga bir martalik taklif havolasini beradi
5. Havola orqali kanalga qo'shilasiz

<b>Muhim:</b>
• Har bir kod faqat 1 marta ishlatiladi
• Havola muddati cheksiz
• Bir xil kodni qayta ishlata olmaysiz

<b>Muammo bo'lsa:</b>
Admin bilan bog'laning
"""
    bot.reply_to(message, help_text)

@bot.message_handler(func=lambda message: message.text == 'ℹ️ Bot haqida')
def about_bot(message):
    """Bot haqida ma'lumot"""
    about_text = """
ℹ️ <b>Bot haqida</b>

<b>Nomi:</b> One-Time Invite Bot
<b>Versiya:</b> 1.0
<b>Yaratilgan:</b> 2024

<b>Xususiyatlari:</b>
✅ Avtomatik kod generatsiyasi
✅ Bir martalik havolalar
✅ Xavfsiz va ishonchli
✅ Cheksiz kodlar

<b>Texnologiyalar:</b>
🐍 Python + pyTelegramBotAPI
🍃 MongoDB
☁️ Render Cloud
"""
    bot.reply_to(message, about_text)

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    """Admin panel"""
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Bu komanda faqat adminlar uchun!")
        return
    
    admin_text = """
👑 <b>Admin Panel</b>

<b>Komandalar:</b>
/add_channel - Yangi kanal qo'shish
/generate_code - Kod generatsiya qilish
/list_channels - Kanallar ro'yxati
/list_codes - Kodlar ro'yxati
/deactivate_code - Kodni o'chirish
/stats - Statistika
/broadcast - Xabar yuborish

<b>Kanallarni boshqarish:</b>
• Botni kanalga admin qiling
• "Add members" huquqini bering
• Kanal ID sini oling
"""
    bot.reply_to(message, admin_text)

@bot.message_handler(commands=['add_channel'])
def add_channel_command(message):
    """Kanal qo'shish"""
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Ruxsat yo'q!")
        return
    
    bot.reply_to(
        message,
        """📝 <b>Kanal qo'shish</b>

Quyidagi formatda yuboring:
<code>/add_channel_id | kanal_id | kanal_nomi</code>

<b>Misol:</b>
<code>/add_channel_id | -1001234567890 | Asosiy Kanal</code>

<b>Kanal ID sini olish:</b>
1. @getidsbot ga kanaldan xabar forward qiling
2. Bot sizga kanal ID sini beradi
""",
        parse_mode='HTML'
    )

@bot.message_handler(commands=['add_channel_id'])
def add_channel_id(message):
    """Kanal ID qo'shish"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        text = message.text.replace('/add_channel_id ', '')
        parts = text.split('|')
        
        if len(parts) != 2:
            bot.reply_to(message, "❌ Format xato! Misol: /add_channel_id | -1001234567890 | Kanal Nomi")
            return
        
        channel_id = parts[0].strip()
        channel_name = parts[1].strip()
        
        try:
            chat = bot.get_chat(channel_id)
            
            bot_member = bot.get_chat_member(channel_id, bot.get_me().id)
            if bot_member.status != 'administrator':
                bot.reply_to(message, "❌ Bot bu kanalda admin emas! Avval admin qiling.")
                return
            
            if not bot_member.can_invite_users:
                bot.reply_to(message, "❌ Botda 'Add members' huquqi yo'q!")
                return
            
        except Exception as e:
            bot.reply_to(message, f"❌ Kanal topilmadi yoki bot a'zo emas! ID: {channel_id}\nXatolik: {e}")
            return
        
        channels_collection.update_one(
            {"channel_id": channel_id},
            {
                "$set": {
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                    "channel_username": getattr(chat, 'username', None),
                    "added_by": str(message.from_user.id),
                    "added_by_name": message.from_user.first_name,
                    "created_at": datetime.now(),
                    "is_active": True
                }
            },
            upsert=True
        )
        
        bot.reply_to(
            message,
            f"""✅ <b>Kanal muvaffaqiyatli qo'shildi!</b>

📱 <b>Kanal:</b> {channel_name}
🆔 <b>ID:</b> <code>{channel_id}</code>
👤 <b>Username:</b> @{getattr(chat, 'username', 'Mavjud emas')}

Endi bu kanal uchun kod generatsiya qilishingiz mumkin!
/generate_code komandasini bosing""",
            parse_mode='HTML'
        )
        
    except Exception as e:
        logger.error(f"Kanal qo'shishda xatolik: {e}")
        bot.reply_to(message, f"❌ Xatolik: {e}")

@bot.message_handler(commands=['generate_code'])
def generate_code_command(message):
    """Kod generatsiya qilish"""
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Ruxsat yo'q!")
        return
    
    channels = list(channels_collection.find({"is_active": True}))
    
    if not channels:
        bot.reply_to(message, "❌ Hech qanday kanal qo'shilmagan! Avval /add_channel")
        return
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    for channel in channels:
        btn_text = f"📱 {channel['channel_name']}"
        callback_data = f"gen_code_{channel['channel_id']}"
        markup.add(types.InlineKeyboardButton(btn_text, callback_data=callback_data))
    
    bot.reply_to(
        message,
        "<b>📝 Kod generatsiya qilish</b>\n\nQaysi kanal uchun kod yaratmoqchisiz?",
        reply_markup=markup,
        parse_mode='HTML'
    )

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
        code_number = get_next_code_number()
        code = code_number
        
        codes_collection.insert_one({
            "code": code,
            "channel_id": channel_id,
            "channel_name": channel.get("channel_name", "Noma'lum"),
            "is_active": True,
            "used_by": [],
            "used_count": 0,
            "created_by": str(call.from_user.id),
            "created_by_name": call.from_user.first_name,
            "created_at": datetime.now(),
            "last_used_at": None,
            "last_invite_link": None
        })
        
        bot.answer_callback_query(call.id, "✅ Kod yaratildi!")
        
        bot.edit_message_text(
            f"""✅ <b>Kod muvaffaqiyatli yaratildi!</b>

🔑 <b>Kod:</b> <code>{code}</code>
📱 <b>Kanal:</b> {channel.get('channel_name', 'Noma\'lum')}
🆔 <b>Kanal ID:</b> <code>{channel_id}</code>
📅 <b>Yaratilgan vaqt:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

<b>Foydalanish:</b>
Foydalanuvchi <code>{code}</code> kodini botga yuboradi va bir martalik havola oladi.""",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode='HTML'
        )
        
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ Xatolik: {e}")
        logger.error(f"Kod yaratishda xatolik: {e}")

@bot.message_handler(commands=['list_channels'])
def list_channels(message):
    """Kanallar ro'yxati"""
    if not is_admin(message.from_user.id):
        return
    
    channels = list(channels_collection.find({"is_active": True}))
    
    if not channels:
        bot.reply_to(message, "📋 Hech qanday kanal yo'q!")
        return
    
    response = "📋 <b>Aktiv kanallar:</b>\n\n"
    for i, channel in enumerate(channels, 1):
        response += f"{i}. 📱 <b>{channel['channel_name']}</b>\n"
        response += f"   🆔 ID: <code>{channel['channel_id']}</code>\n"
        if 'channel_username' in channel and channel['channel_username']:
            response += f"   🔗 @{channel['channel_username']}\n"
        response += "\n"
    
    bot.reply_to(message, response, parse_mode='HTML')

@bot.message_handler(commands=['list_codes'])
def list_codes_command(message):
    """Kodlar ro'yxati"""
    if not is_admin(message.from_user.id):
        return
    
    codes = list(codes_collection.find({"is_active": True}).sort("created_at", -1).limit(20))
    
    if not codes:
        bot.reply_to(message, "📋 Hech qanday aktiv kod yo'q!")
        return
    
    response = "📋 <b>So'nggi aktiv kodlar:</b>\n\n"
    for i, code_data in enumerate(codes, 1):
        response += f"{i}. 🔑 <code>{code_data['code']}</code>\n"
        response += f"   📱 {code_data.get('channel_name', 'Noma\'lum')}\n"
        response += f"   👥 Ishlatilgan: {code_data['used_count']} marta\n"
        response += f"   📅 Yaratilgan: {code_data['created_at'].strftime('%Y-%m-%d %H:%M')}\n\n"
    
    bot.reply_to(message, response, parse_mode='HTML')

@bot.message_handler(commands=['deactivate_code'])
def deactivate_code(message):
    """Kodni o'chirish"""
    if not is_admin(message.from_user.id):
        return
    
    bot.reply_to(
        message,
        "📝 Kodni o'chirish uchun quyidagi formatda yuboring:\n<code>/deactivate kod_raqami</code>",
        parse_mode='HTML'
    )

@bot.message_handler(commands=['deactivate'])
def deactivate_specific_code(message):
    """Maxsus kodni o'chirish"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        code = message.text.replace('/deactivate ', '').strip()
        
        result = codes_collection.update_one(
            {"code": code},
            {"$set": {"is_active": False, "deactivated_at": datetime.now()}}
        )
        
        if result.modified_count > 0:
            bot.reply_to(message, f"✅ Kod <code>{code}</code> muvaffaqiyatli o'chirildi!", parse_mode='HTML')
        else:
            bot.reply_to(message, f"❌ <code>{code}</code> kodi topilmadi!", parse_mode='HTML')
    
    except Exception as e:
        bot.reply_to(message, f"❌ Xatolik: {e}")

@bot.message_handler(commands=['stats'])
def show_stats(message):
    """Statistika"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        total_channels = channels_collection.count_documents({"is_active": True})
        total_codes = codes_collection.count_documents({})
        active_codes = codes_collection.count_documents({"is_active": True})
        used_codes = codes_collection.count_documents({"used_count": {"$gt": 0}})
        total_users = users_collection.count_documents({})
        
        setting = settings_collection.find_one({"_id": "code_counter"})
        last_code = setting.get('last_code_number', 0) if setting else 0
        
        stats_text = f"""
📊 <b>Bot Statistikasi</b>

📱 <b>Kanallar:</b> {total_channels}
🔑 <b>Jami kodlar:</b> {total_codes}
✅ <b>Aktiv kodlar:</b> {active_codes}
👥 <b>Ishlatilgan kodlar:</b> {used_codes}
👤 <b>Foydalanuvchilar:</b> {total_users}
🔢 <b>Oxirgi kod raqami:</b> {last_code}

⏰ <b>Vaqt:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
        bot.reply_to(message, stats_text, parse_mode='HTML')
        
    except Exception as e:
        bot.reply_to(message, f"❌ Statistikada xatolik: {e}")

@bot.message_handler(commands=['broadcast'])
def broadcast_command(message):
    """Barcha foydalanuvchilarga xabar yuborish"""
    if not is_admin(message.from_user.id):
        return
    
    bot.reply_to(
        message,
        "📢 Barcha foydalanuvchilarga yuboriladigan xabarni quyidagi formatda yozing:\n<code>/send_broadcast XABAR_MATNI</code>",
        parse_mode='HTML'
    )

@bot.message_handler(commands=['send_broadcast'])
def send_broadcast(message):
    """Broadcast xabarini yuborish"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        broadcast_text = message.text.replace('/send_broadcast ', '')
        
        if not broadcast_text:
            bot.reply_to(message, "❌ Xabar matni bo'sh!")
            return
        
        users = users_collection.find({})
        sent_count = 0
        error_count = 0
        
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
        
        bot.reply_to(
            message,
            f"✅ Broadcast yakunlandi!\n\n📤 Yuborildi: {sent_count}\n❌ Xatolik: {error_count}"
        )
        
    except Exception as e:
        bot.reply_to(message, f"❌ Xatolik: {e}")

@bot.message_handler(func=lambda message: True)
def handle_all_messages(message):
    """Barcha xabarlarni qayta ishlash - kodlarni tekshirish"""
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    text = message.text.strip()
    
    # Admin komandalarni tekshirish
    if text.startswith('/'):
        bot.reply_to(message, "❌ Noma'lum komanda! /help orqali yordam oling.")
        return
    
    # Kodni tekshirish
    logger.info(f"Foydalanuvchi {user_id} ({first_name}) kod yubordi: {text}")
    result = verify_and_use_code(text, user_id, username, first_name)
    
    if result["success"] and result["invite_link"]:
        # Tugma yaratish - chiroyli ko'rinishda
        markup = types.InlineKeyboardMarkup()
        btn_join = types.InlineKeyboardButton(
            text="▷ 𝗞𝗮𝗻𝗮𝗹𝗴𝗮 𝗾𝗼'𝘀𝗵𝗶𝗹𝗶𝘀𝗵 ◁",
            url=result["invite_link"]
        )
        markup.add(btn_join)
        
        # Xabar va tugmani yuborish
        bot.reply_to(
            message, 
            result["message"],
            reply_markup=markup,
            parse_mode='HTML'
        )
    else:
        # Xatolik xabari
        bot.reply_to(message, result["message"], parse_mode='HTML')

# Flask routes (Render Web Service uchun)
@app.route('/')
def index():
    """Bosh sahifa"""
    return """
    <html>
        <head>
            <title>One-Time Invite Bot</title>
            <style>
                body {
                    font-family: Arial, sans-serif;
                    text-align: center;
                    padding: 50px;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    color: white;
                }
                .container {
                    background: rgba(255, 255, 255, 0.1);
                    padding: 30px;
                    border-radius: 10px;
                    display: inline-block;
                }
            </style>
        </head>
        <body>
            <div class="container">
                <h1>🤖 One-Time Invite Bot</h1>
                <p>Bot ishlamoqda!</p>
                <p>Status: ✅ Active</p>
            </div>
        </body>
    </html>
    """

@app.route('/health')
def health():
    """Health check endpoint"""
    return {"status": "ok", "timestamp": datetime.now().isoformat()}, 200

# Botni alohida thread'da ishga tushirish
def run_bot():
    """Botni ishga tushirish"""
    logger.info("🤖 Bot ishga tushmoqda...")
    try:
        bot.remove_webhook()
        bot.infinity_polling(timeout=10, long_polling_timeout=5)
    except Exception as e:
        logger.error(f"Bot polling xatolik: {e}")

# Asosiy ishga tushirish
if __name__ == '__main__':
    # Botni alohida thread'da ishga tushirish
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    # Flask app'ni ishga tushirish
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
