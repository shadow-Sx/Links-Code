import os
import telebot
from telebot import types
from pymongo import MongoClient
from datetime import datetime
import logging
from dotenv import load_dotenv

# Logging sozlamalari
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Environment variables yuklash
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
MONGODB_URI = os.getenv('MONGODB_URI')

# MongoDB ulanish
try:
    client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
    db = client['invite_bot_db']
    codes_collection = db['invite_codes']
    channels_collection = db['channels']
    users_collection = db['users']
    # Test ulanish
    client.admin.command('ping')
    logger.info("MongoDB ga ulanish muvaffaqiyatli!")
except Exception as e:
    logger.error(f"MongoDB ulanishda xatolik: {e}")
    raise

# Bot yaratish
bot = telebot.TeleBot(BOT_TOKEN)

# Admin ID sini saqlash
ADMIN_IDS = [123456789]  # Admin Telegram ID lari (o'zingizni ID ni qo'ying)

# MongoDB indekslar
codes_collection.create_index("code", unique=True)
codes_collection.create_index("channel_id")
channels_collection.create_index("channel_id", unique=True)

# Yordamchi funksiyalar
def is_admin(user_id):
    """Foydalanuvchi admin ekanligini tekshiradi"""
    return user_id in ADMIN_IDS

def get_channel_invite_link(channel_id):
    """Kanal uchun bir martalik havola yaratadi"""
    try:
        chat = bot.get_chat(channel_id)
        # Yangi taklif havolasi yaratish
        invite_link = bot.create_chat_invite_link(
            chat_id=channel_id,
            member_limit=1,  # Bir martalik foydalanish
            expire_date=None  # Muddatsiz
        )
        return invite_link.invite_link
    except Exception as e:
        logger.error(f"Havola yaratishda xatolik: {e}")
        return None

def verify_and_use_code(code, user_id):
    """Kodni tekshiradi va kanalga havola beradi"""
    try:
        # Kodni topish
        code_data = codes_collection.find_one({"code": code, "is_active": True})
        
        if not code_data:
            return {"success": False, "message": "❌ Noto'g'ri yoki aktiv bo'lmagan kod!"}
        
        # Foydalanuvchi bu kodni oldin ishlatganmi
        if "used_by" in code_data and user_id in code_data["used_by"]:
            return {"success": False, "message": "❌ Siz bu kodni oldin ishlatgansiz!"}
        
        # Kanal ma'lumotlari
        channel_data = channels_collection.find_one({"channel_id": code_data["channel_id"]})
        if not channel_data:
            return {"success": False, "message": "❌ Kanal topilmadi!"}
        
        # Bir martalik havola yaratish
        invite_link = get_channel_invite_link(code_data["channel_id"])
        if not invite_link:
            return {"success": False, "message": "❌ Havola yaratishda xatolik! Admin bilan bog'laning."}
        
        # Kodni yangilash
        codes_collection.update_one(
            {"_id": code_data["_id"]},
            {
                "$push": {"used_by": user_id},
                "$inc": {"used_count": 1},
                "$set": {"last_used_at": datetime.now()}
            }
        )
        
        # Foydalanuvchi statistikasi
        users_collection.update_one(
            {"user_id": user_id},
            {
                "$set": {"last_used_at": datetime.now()},
                "$push": {"used_codes": code},
                "$inc": {"total_uses": 1}
            },
            upsert=True
        )
        
        return {
            "success": True, 
            "message": f"✅ Kodingiz qabul qilindi!\n\n📱 Kanal: {channel_data['channel_name']}\n🔗 Bir martalik havola: {invite_link}\n\n⚠️ Bu havola faqat 1 marta ishlatilishi mumkin!"
        }
        
    except Exception as e:
        logger.error(f"Kodni tekshirishda xatolik: {e}")
        return {"success": False, "message": "❌ Xatolik yuz berdi! Qaytadan urinib ko'ring."}

# Bot komandalari
@bot.message_handler(commands=['start'])
def send_welcome(message):
    """Start komandasi"""
    welcome_text = """
👋 Salom! Men kanallarga bir martalik havola beruvchi botman.

📝 Ishlatish:
1. Kodni oling
2. Botga kodni yuboring
3. Bot sizga bir martalik kanal havolasini beradi

Kodni kiriting: 
"""
    bot.reply_to(message, welcome_text)

@bot.message_handler(commands=['help'])
def send_help(message):
    """Yordam komandasi"""
    help_text = """
🤖 Bot haqida ma'lumot:

Bu bot sizga maxsus kod orqali kanallarga bir martalik taklif havolalarini beradi.

📝 Foydalanish:
• Botga xabar sifatida kodni yuboring
• Bot sizga bir martalik havolani taqdim etadi

⚠️ Har bir kod faqat bir marta ishlatilishi mumkin!

Admin komandalari:
/add_channel - Yangi kanal qo'shish
/add_code - Yangi kod yaratish
/list_codes - Barcha kodlarni ko'rish
/stats - Statistika
"""
    bot.reply_to(message, help_text)

@bot.message_handler(commands=['add_channel'])
def add_channel(message):
    """Admin - Yangi kanal qo'shish"""
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Kechirasiz, bu komanda faqat adminlar uchun!")
        return
    
    bot.reply_to(message, "📝 Kanal qo'shish uchun quyidagi formatda yuboring:\n\n`/new_channel <kanal_id> <kanal_nomi>`\n\nMisol: `/new_channel -1001234567890 @KanalNomi`", parse_mode='Markdown')

@bot.message_handler(commands=['new_channel'])
def new_channel(message):
    """Yangi kanal qo'shish jarayoni"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        parts = message.text.split()
        channel_id = parts[1]
        channel_name = ' '.join(parts[2:])
        
        channels_collection.update_one(
            {"channel_id": channel_id},
            {
                "$set": {
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                    "added_by": message.from_user.id,
                    "created_at": datetime.now()
                }
            },
            upsert=True
        )
        
        bot.reply_to(message, f"✅ Kanal muvaffaqiyatli qo'shildi!\nID: {channel_id}\nNomi: {channel_name}")
    except Exception as e:
        bot.reply_to(message, f"❌ Xatolik: {e}\n\nFormat: `/new_channel <kanal_id> <kanal_nomi>`", parse_mode='Markdown')

@bot.message_handler(commands=['add_code'])
def add_code(message):
    """Admin - Yangi kod qo'shish"""
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Kechirasiz, bu komanda faqat adminlar uchun!")
        return
    
    bot.reply_to(message, "📝 Kod qo'shish uchun quyidagi formatda yuboring:\n\n`/new_code <kod> <kanal_id>`\n\nMisol: `/new_code 354 -1001234567890`", parse_mode='Markdown')

@bot.message_handler(commands=['new_code'])
def new_code(message):
    """Yangi kod yaratish jarayoni"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        parts = message.text.split()
        code = parts[1]
        channel_id = parts[2]
        
        # Kanal mavjudligini tekshirish
        channel = channels_collection.find_one({"channel_id": channel_id})
        if not channel:
            bot.reply_to(message, "❌ Bunday kanal topilmadi! Avval kanalni qo'shing.")
            return
        
        # Kod yaratish
        codes_collection.insert_one({
            "code": code,
            "channel_id": channel_id,
            "is_active": True,
            "used_by": [],
            "used_count": 0,
            "created_by": message.from_user.id,
            "created_at": datetime.now(),
            "last_used_at": None
        })
        
        bot.reply_to(message, f"✅ Kod muvaffaqiyatli qo'shildi!\nKod: {code}\nKanal: {channel['channel_name']}")
    except Exception as e:
        if "duplicate" in str(e):
            bot.reply_to(message, "❌ Bu kod allaqachon mavjud!")
        else:
            bot.reply_to(message, f"❌ Xatolik: {e}\n\nFormat: `/new_code <kod> <kanal_id>`", parse_mode='Markdown')

@bot.message_handler(commands=['list_codes'])
def list_codes(message):
    """Admin - Barcha kodlarni ro'yxati"""
    if not is_admin(message.from_user.id):
        return
    
    codes = codes_collection.find({"is_active": True})
    response = "📋 Aktiv kodlar:\n\n"
    
    for code in codes:
        channel = channels_collection.find_one({"channel_id": code["channel_id"]})
        channel_name = channel["channel_name"] if channel else "Noma'lum kanal"
        response += f"🔑 Kod: `{code['code']}` - 📱 {channel_name} (Ishlatilgan: {code['used_count']})\n"
    
    if response == "📋 Aktiv kodlar:\n\n":
        response += "Hech qanday aktiv kod yo'q!"
    
    bot.reply_to(message, response, parse_mode='Markdown')

@bot.message_handler(commands=['stats'])
def show_stats(message):
    """Admin - Statistika"""
    if not is_admin(message.from_user.id):
        return
    
    total_codes = codes_collection.count_documents({})
    active_codes = codes_collection.count_documents({"is_active": True})
    total_users = users_collection.count_documents({})
    
    stats_text = f"""
📊 Bot statistikasi:

📝 Jami kodlar: {total_codes}
✅ Aktiv kodlar: {active_codes}
👥 Foydalanuvchilar: {total_users}
"""
    bot.reply_to(message, stats_text)

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    """Kodlarni qabul qilish"""
    code = message.text.strip()
    user_id = message.from_user.id
    
    # Kod faqat raqamlar yoki harflar
    result = verify_and_use_code(code, user_id)
    bot.reply_to(message, result["message"])

# Polling boshlash
if __name__ == '__main__':
    logger.info("Bot ishga tushmoqda...")
    try:
        bot.polling(none_stop=True, interval=0)
    except Exception as e:
        logger.error(f"Bot polling xatolik: {e}")
