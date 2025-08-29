# -*- coding: utf-8 -*-
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.dispatcher import FSMContext
from aiogram.utils import executor

# =============== تحميل الإعدادات ===============
from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "YOUR_USERNAME")
ADMIN_IDS = [int(i) for i in os.getenv("ADMIN_IDS", "").split(",") if i.strip().isdigit()]

FORCE_CHANNELS = [os.getenv("FORCE_CH1", "@SMSFARS_1"), os.getenv("FORCE_CH2", "@SMSFARS_2")]

CH_ATTEMPTS   = int(os.getenv("CH_ATTEMPTS",   "-1002627555519"))
CH_LOGIN      = int(os.getenv("CH_LOGIN",      "-10026017331"))
CH_SUPPORT_IN = int(os.getenv("CH_SUPPORT_IN", "-1002555952121"))

PUBLIC_ACTIVATIONS = os.getenv("PUBLIC_ACTIVATIONS", "@SMSFARS_2")
PUBLIC_OFFICIAL    = os.getenv("PUBLIC_OFFICIAL", "@SMSFARS_1")

FIVESIM_API_KEY     = os.getenv("FIVESIM_API_KEY", "")
SMSACTIVATE_API_KEY = os.getenv("SMSACTIVATE_API_KEY", "")

CURRENCY = os.getenv("CURRENCY", "₽")
BRAND = "𓆪•|ــــــ( 𝗖𝗥𝗔𝗭𝗬◉▿◉𝗦𝙈𝗦)ــــــ|•𓆩"

if not BOT_TOKEN:
    raise SystemExit("⚠️ ضع BOT_TOKEN في .env")

# =============== قاعدة البيانات ===============
DB = "crazy_sms.db"

def init_db():
    with closing(sqlite3.connect(DB)) as con, con:
        con.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            email TEXT,
            balance REAL DEFAULT 0,
            created_at TEXT,
            last_ip TEXT,
            last_seen TEXT
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS sessions(
            user_id INTEGER PRIMARY KEY,
            logged_in INTEGER DEFAULT 0
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            provider TEXT,
            country TEXT,
            service TEXT,
            phone TEXT,
            price REAL,
            status TEXT,
            created_at TEXT
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS prices(
            country TEXT PRIMARY KEY,
            price REAL
        )""")
        # أسعار افتراضية
        defaults = {"sa":30,"eg":25,"ye":20,"tr":18}
        for c,p in defaults.items():
            con.execute("INSERT OR IGNORE INTO prices(country,price) VALUES(?,?)", (c,p))
        # مزودون مفعّلون افتراضياً
        for k,v in [("provider_5sim_enabled","1"),("provider_sms_enabled","1")]:
            con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",(k,v))

def get_setting(key, default=None):
    with closing(sqlite3.connect(DB)) as con:
        cur = con.execute("SELECT value FROM settings WHERE key=?",(key,))
        r = cur.fetchone()
        return r[0] if r else default

def set_setting(key, value):
    with closing(sqlite3.connect(DB)) as con, con:
        con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",(key,str(value)))

def get_price(country_code, default=25):
    with closing(sqlite3.connect(DB)) as con:
        cur = con.execute("SELECT price FROM prices WHERE country=?",(country_code,))
        r = cur.fetchone()
        return float(r[0]) if r else default

def set_price(country_code, price):
    with closing(sqlite3.connect(DB)) as con, con:
        con.execute("INSERT OR REPLACE INTO prices(country,price) VALUES(?,?)",(country_code, float(price)))

def db_get_user(user_id):
    with closing(sqlite3.connect(DB)) as con:
        cur = con.execute("SELECT user_id,email,balance,created_at,last_ip,last_seen FROM users WHERE user_id=?",(user_id,))
        return cur.fetchone()

def db_upsert_user(user_id, email=None, ip=None):
    now = datetime.utcnow().isoformat()
    row = db_get_user(user_id)
    with closing(sqlite3.connect(DB)) as con, con:
        if row is None:
            con.execute("INSERT INTO users(user_id,email,balance,created_at,last_ip,last_seen) VALUES(?,?,?,?,?,?)",
                        (user_id, email, 0, now, ip, now))
            con.execute("INSERT OR REPLACE INTO sessions(user_id,logged_in) VALUES(?,?)", (user_id, 1))
        else:
            if email:
                con.execute("UPDATE users SET email=? WHERE user_id=?", (email, user_id))
            if ip:
                con.execute("UPDATE users SET last_ip=?, last_seen=? WHERE user_id=?", (ip, now, user_id))
            con.execute("INSERT OR REPLACE INTO sessions(user_id,logged_in) VALUES(?,?)", (user_id, 1))

def db_is_logged_in(user_id)->bool:
    with closing(sqlite3.connect(DB)) as con:
        cur = con.execute("SELECT logged_in FROM sessions WHERE user_id=?", (user_id,))
        r = cur.fetchone()
        return bool(r and r[0])

def db_get_balance(user_id):
    with closing(sqlite3.connect(DB)) as con:
        cur = con.execute("SELECT COALESCE(balance,0) FROM users WHERE user_id=?", (user_id,))
        r = cur.fetchone()
        return float(r[0]) if r else 0.0

# =============== بوت ===============
bot = Bot(token=BOT_TOKEN, parse_mode=types.ParseMode.HTML)
dp  = Dispatcher(bot, storage=MemoryStorage())

# دول + خدمات
COUNTRIES = [("🇸🇦 السعودية","sa"),("🇪🇬 مصر","eg"),("🇾🇪 اليمن","ye"),("🇹🇷 تركيا","tr")]
SERVICES  = [("WhatsApp","whatsapp"),("Telegram","telegram")]

# FSM
class Auth(StatesGroup):
    ask_email = State()
class AdminSetPrice(StatesGroup):
    choose_country = State()
    enter_price = State()

# =============== أدوات ===============
async def ensure_force_sub(user_id: int) -> bool:
    for ch in FORCE_CHANNELS:
        try:
            st = await bot.get_chat_member(ch, user_id)
            if st.status in ("left","kicked"):
                return False
        except Exception:
            return False
    return True

def main_menu(balance: float):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("⚡ طلب رقم", "👤 لوحة الحساب")
    kb.add("🧾 شروط الاستخدام", "🆘 الدعم والمساعدة")
    return kb

def back_home_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("🔙 رجوع", "🏠 الصفحة الرئيسية")
    return kb

def welcome_text(ip: str):
    return (f"<b>{BRAND}</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"👋 أهلاً وسهلاً بك عزيزي\n"
            f"🛰️ IP دخولك: <code>{ip or 'N/A'}</code>\n\n"
            f"🚀 <b>{BRAND}</b> يمكنك:\n"
            f"• تفعيل حساباتك في جميع المنصات بسهولة\n"
            f"• استخدام أرقام وهمية شغّالة 100%\n"
            f"• الحصول على أرقام فورية وسريعة\n"
            f"• دعم فني متواصل 24/7\n\n"
            f"🏆 مميزاتنا:\n"
            f"✅ أسعار منافسة\n"
            f"✅ أرقام مضمونة\n"
            f"✅ إشعارات لحظية عند الشراء\n"
            f"✅ لوحة تحكم سهلة وبسيطة\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🛠 الإدارة والدعم الفني: <a href='https://t.me/{ADMIN_USERNAME}'>اضغط هنا</a>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💎 CRAZY SMS — حيث تبدأ رحلتك نحو التفعيل السريع!")

TERMS_TEXT = (
"📜 <b>شروط الاستخدام</b>\n\n"
"• هذه الخدمة مخصّصة للتفعيل والاختبار والخصوصية وفق القانون المحلي وسياسات المنصات.\n"
"• يُحظر أي استخدام مخالف للقوانين أو لسياسات الجهات المالكة للخدمات.\n"
"• الأرقام مؤقتة للتفعيل وقد تتغير توافرًا وسعرًا.\n"
"• باستخدامك للبوت فأنت توافق على هذه الشروط."
)

def support_text():
    return (f"🧑‍💻 للتواصل مع الدعم الفني والإدارة:\n"
            f"@{ADMIN_USERNAME}\n"
            f"🔗 <a href='https://t.me/{ADMIN_USERNAME}'>اضغط هنا للتواصل المباشر</a>\n\n"
            f"إن كان الخاص مغلقًا، أرسل رسالتك هنا وسيحوّلها البوت لقناة الدعم.")

# =============== أوامر عامة ===============
@dp.message_handler(commands=["start"])
async def start(m: types.Message, state: FSMContext):
    await state.finish()
    ok = await ensure_force_sub(m.from_user.id)
    if not ok:
        btn = types.InlineKeyboardMarkup().add(
            types.InlineKeyboardButton("✅ تحقّقت من الاشتراك، اضغط هنا", callback_data="recheck")
        )
        chs = "\n".join([f"• {c}" for c in FORCE_CHANNELS])
        await m.answer("🔔 للمتابعة، الرجاء الاشتراك في القنوات التالية:\n"+chs+"\n\nثم اضغط الزر للتحقّق.", reply_markup=btn)
        return
    ip = m.from_user.language_code or "N/A"
    db_upsert_user(m.from_user.id, ip=ip)
    await m.answer(welcome_text(ip), reply_markup=main_menu(db_get_balance(m.from_user.id)))

@dp.callback_query_handler(lambda c:c.data=="recheck")
async def recheck(c: types.CallbackQuery):
    if await ensure_force_sub(c.from_user.id):
        await c.message.delete()
        await c.message.answer("✅ تم التحقّق من الاشتراك. أهلاً بك!", reply_markup=main_menu(db_get_balance(c.from_user.id)))
    else:
        await c.answer("لا يزال الاشتراك غير مكتمل.", show_alert=True)

@dp.message_handler(lambda m: m.text == "🧾 شروط الاستخدام")
async def terms(m: types.Message):
    await m.answer(TERMS_TEXT, reply_markup=back_home_menu())

@dp.message_handler(lambda m: m.text == "🆘 الدعم والمساعدة")
async def support(m: types.Message):
    await m.answer(support_text(), reply_markup=back_home_menu())

@dp.message_handler(lambda m: m.text == "🏠 الصفحة الرئيسية")
async def home(m: types.Message):
    await m.answer("🏠 عدت إلى الصفحة الرئيسية.", reply_markup=main_menu(db_get_balance(m.from_user.id)))

# =============== حساب المستخدم ===============
def account_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("💡 إنشاء حساب", "✅ تسجيل الدخول")
    kb.add("📊 الإحصائيات", "🏠 الصفحة الرئيسية")
    return kb

@dp.message_handler(lambda m: m.text == "👤 لوحة الحساب")
async def account(m: types.Message):
    u = db_get_user(m.from_user.id)
    bal = db_get_balance(m.from_user.id)
    logged = "✅ مسجل" if db_is_logged_in(m.from_user.id) else "❌ غير مسجل"
    email = u[1] if u else None
    await m.answer(f"👤 <b>حسابك</b>\n• البريد: <code>{email or 'غير مضاف'}</code>\n• الحالة: {logged}\n• الرصيد: {bal:.3f} {CURRENCY}", reply_markup=account_menu())

class Auth(StatesGroup):
    ask_email = State()

@dp.message_handler(lambda m: m.text == "💡 إنشاء حساب")
async def ask_email(m: types.Message, state:FSMContext):
    await Auth.ask_email.set()
    await m.answer("📧 أرسل بريدك الإلكتروني (لن تحتاج كلمة مرور).", reply_markup=back_home_menu())

@dp.message_handler(state=Auth.ask_email, content_types=types.ContentTypes.TEXT)
async def save_email(m: types.Message, state:FSMContext):
    email = m.text.strip()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        await m.answer("❌ بريد غير صالح. حاول مجددًا.")
        return
    db_upsert_user(m.from_user.id, email=email)
    await state.finish()
    # إشعار للإدارة
    try:
        await bot.send_message(CH_LOGIN, f"🔔 <b>تسجيل جديد/دخول</b>\n• المستخدم: <a href='tg://user?id={m.from_user.id}'>{m.from_user.full_name}</a>\n• الآيدي: <code>{m.from_user.id}</code>\n• البريد: <code>{email}</code>")
    except: pass
    await m.answer("✅ تم إنشاء حسابك وتسجيل دخولك.", reply_markup=main_menu(db_get_balance(m.from_user.id)))

@dp.message_handler(lambda m: m.text == "✅ تسجيل الدخول")
async def login(m: types.Message):
    u = db_get_user(m.from_user.id)
    if u and u[1]:
        db_upsert_user(m.from_user.id)
        try:
            await bot.send_message(CH_LOGIN, f"🔓 <b>تسجيل دخول</b> | {m.from_user.id} | البريد: <code>{u[1]}</code>")
        except: pass
        await m.answer(f"✅ تم تسجيل دخولك.\nبريدك: <code>{u[1]}</code>", reply_markup=main_menu(db_get_balance(m.from_user.id)))
    else:
        await m.answer("ℹ️ يجب أن يكون لديك بريد مسجّل مسبقًا. استخدم «إنشاء حساب».", reply_markup=account_menu())

# =============== الطلبات (أرقام) ===============
class OrderFlow(StatesGroup):
    choose_country = State()
    choose_service = State()
    waiting_code = State()

@dp.message_handler(lambda m: m.text == "⚡ طلب رقم")
async def order_entry(m: types.Message, state:FSMContext):
    kb = types.InlineKeyboardMarkup(row_width=2)
    for name, code in COUNTRIES:
        price = get_price(code, 25)
        kb.insert(types.InlineKeyboardButton(f"{name} — {price} {CURRENCY}", callback_data=f"c_{code}"))
    await m.answer(f"🌍 اختر الدولة.\n<b>{BRAND}</b>", reply_markup=kb)
    await OrderFlow.choose_country.set()

@dp.callback_query_handler(lambda c:c.data.startswith("c_"), state=OrderFlow.choose_country)
async def picked_country(c: types.CallbackQuery, state:FSMContext):
    country = c.data.split("_",1)[1]
    await state.update_data(country=country)
    kb = types.InlineKeyboardMarkup(row_width=2)
    for label, sid in SERVICES:
        kb.insert(types.InlineKeyboardButton(label, callback_data=f"s_{sid}"))
    await c.message.edit_text("اختر الخدمة:", reply_markup=kb)
    await OrderFlow.choose_service.set()

@dp.callback_query_handler(lambda c:c.data.startswith("s_"), state=OrderFlow.choose_service)
async def picked_service(c: types.CallbackQuery, state:FSMContext):
    service = c.data.split("_",1)[1]
    data = await state.get_data()
    country = data.get("country")

    # مكان الدمج الحقيقي مع 5SIM/sms-activate (لاحقًا)
    provider = "5sim" if get_setting("provider_5sim_enabled","1")=="1" else "sms-activate"
    price = get_price(country, 25)
    phone = "+99900012345"  # Placeholder رقم افتراضي

    with closing(sqlite3.connect(DB)) as con, con:
        con.execute("INSERT INTO orders(user_id,provider,country,service,phone,price,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (c.from_user.id, provider, country, service, phone, price, "WAIT_CODE", datetime.utcnow().isoformat()))

    # إشعار محاولات الشراء (واضح)
    try:
        await bot.send_message(CH_ATTEMPTS,
            f"🟠 <b>محاولة شراء</b>\n"
            f"• المستخدم: <a href='tg://user?id={c.from_user.id}'>{c.from_user.full_name}</a>\n"
            f"• الدولة: <code>{country}</code>\n"
            f"• الخدمة: <code>{service}</code>\n"
            f"• السعر: {price} {CURRENCY}\n"
            f"• الرقم: <code>{phone}</code>")
    except: pass

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔁 تغيير الرقم", callback_data="chg_num"))
    kb.add(types.InlineKeyboardButton("❌ إلغاء الرقم", callback_data="cancel_num"))
    await c.message.edit_text(
        f"📲 تم طلب رقمك بنجاح من <b>{BRAND}</b>\n"
        f"• الدولة: <code>{country}</code>\n"
        f"• الخدمة: <code>{service}</code>\n"
        f"• السعر: <b>{price} {CURRENCY}</b>\n"
        f"• الرقم: <code>{phone}</code>\n\n"
        f"عند وصول الكود سيظهر هنا مباشرة.",
        reply_markup=kb
    )
    await OrderFlow.waiting_code.set()

@dp.callback_query_handler(lambda c:c.data=="chg_num", state=OrderFlow.waiting_code)
async def change_number(c: types.CallbackQuery, state:FSMContext):
    data = await state.get_data()
    country = data.get("country")
    new_phone = "+99900054321"
    price = get_price(country, 25)
    await c.message.edit_text(
        f"🔁 تم تغيير الرقم بنجاح (نفس الدولة).\n"
        f"• الدولة: <code>{country}</code>\n"
        f"• الرقم الجديد: <code>{new_phone}</code>\n"
        f"• السعر: {price} {CURRENCY}\n\n"
        f"بانتظار كود التفعيل…"
    )

@dp.callback_query_handler(lambda c:c.data=="cancel_num", state=OrderFlow.waiting_code)
async def cancel_number(c: types.CallbackQuery, state:FSMContext):
    await state.finish()
    # أزرار ما بعد الإلغاء
    price_label = get_price("ye", 20)  # افتراضي للعرض
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(f"🛒 الشراء مجددًا بسعر ({price_label} {CURRENCY})", callback_data="relist_countries"))
    kb.add(types.InlineKeyboardButton("🌍 رجوع للدول", callback_data="relist_countries"))
    kb.add(types.InlineKeyboardButton("🏠 الصفحة الرئيسية", callback_data="go_home"))
    await c.message.edit_text("✅ تم إلغاء الرقم بنجاح.", reply_markup=kb)

@dp.callback_query_handler(lambda c:c.data=="relist_countries")
async def relist_countries(c: types.CallbackQuery):
    kb = types.InlineKeyboardMarkup(row_width=2)
    for name, code in COUNTRIES:
        price = get_price(code, 25)
        kb.insert(types.InlineKeyboardButton(f"{name} — {price} {CURRENCY}", callback_data=f"c_{code}"))
    await c.message.edit_text("🌍 اختر الدولة:", reply_markup=kb)

@dp.callback_query_handler(lambda c:c.data=="go_home")
async def go_home_cb(c: types.CallbackQuery):
    await c.message.edit_text("🏠 عدت إلى الصفحة الرئيسية.")
    await c.message.answer(".", reply_markup=main_menu(db_get_balance(c.from_user.id)))

# =============== لوحة تحكم الأدمن ===============
def is_admin(user_id:int)->bool:
    return user_id in ADMIN_IDS

@dp.message_handler(commands=["admin"])
async def admin_panel(m: types.Message, state:FSMContext):
    if not is_admin(m.from_user.id):
        return
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("💰 تعديل الأسعار", callback_data="ad_prices"),
        types.InlineKeyboardButton("🔌 المزوّدون", callback_data="ad_providers"),
    )
    kb.add(
        types.InlineKeyboardButton("📢 القنوات", callback_data="ad_channels"),
        types.InlineKeyboardButton("📈 إحصائيات", callback_data="ad_stats"),
    )
    await m.answer("🛠 <b>لوحة تحكم الإدارة</b>", reply_markup=kb)

@dp.callback_query_handler(lambda c:c.data=="ad_prices")
async def ad_prices(c: types.CallbackQuery, state:FSMContext):
    if not is_admin(c.from_user.id): return
    kb = types.InlineKeyboardMarkup(row_width=2)
    for name, code in COUNTRIES:
        kb.insert(types.InlineKeyboardButton(f"{name} ({get_price(code)} {CURRENCY})", callback_data=f"adp_{code}"))
    kb.add(types.InlineKeyboardButton("↩️ رجوع", callback_data="ad_back"))
    await c.message.edit_text("💰 اختر دولة لتعديل السعر:", reply_markup=kb)

@dp.callback_query_handler(lambda c:c.data.startswith("adp_"))
async def ad_price_pick(c: types.CallbackQuery, state:FSMContext):
    if not is_admin(c.from_user.id): return
    code = c.data.split("_",1)[1]
    await state.update_data(ad_country=code)
    await AdminSetPrice.enter_price.set()
    await c.message.edit_text(f"اكتب السعر الجديد لدولة <code>{code}</code> (أرقام فقط).")

@dp.message_handler(state=AdminSetPrice.enter_price)
async def ad_price_set(m: types.Message, state:FSMContext):
    if not is_admin(m.from_user.id):
        await state.finish(); return
    try:
        price = float(m.text.strip())
        data = await state.get_data()
        code = data.get("ad_country")
        set_price(code, price)
        await state.finish()
        await m.answer(f"✅ تم تحديث سعر <code>{code}</code> إلى {price} {CURRENCY}.")
        await admin_panel(m, state)
    except:
        await m.answer("❌ أدخل رقمًا صالحًا.")

@dp.callback_query_handler(lambda c:c.data=="ad_providers")
async def ad_providers(c: types.CallbackQuery, state:FSMContext):
    if not is_admin(c.from_user.id): return
    p5 = "مفعل ✅" if get_setting("provider_5sim_enabled","1")=="1" else "معطل ❌"
    ps = "مفعل ✅" if get_setting("provider_sms_enabled","1")=="1" else "معطل ❌"
    ap5 = "موجود 🔐" if FIVESIM_API_KEY else "غير مُضاف ⚠️"
    aps = "موجود 🔐" if SMSACTIVATE_API_KEY else "غير مُضاف ⚠️"
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton(f"5SIM: {p5}", callback_data="tog_5sim"),
        types.InlineKeyboardButton(f"SMS-Activate: {ps}", callback_data="tog_sms"),
    )
    kb.add(types.InlineKeyboardButton("↩️ رجوع", callback_data="ad_back"))
    txt = (f"🔌 <b>المزوّدون</b>\n"
           f"• 5SIM: {p5} | مفاتيح: {ap5}\n"
           f"• SMS-Activate: {ps} | مفاتيح: {aps}\n\n"
           f"✳️ نصيحة أمان: أضف مفاتيح الـ API كمتغيرات بيئة فقط.")
    await c.message.edit_text(txt, reply_markup=kb)

@dp.callback_query_handler(lambda c:c.data=="tog_5sim")
async def tog_5sim(c: types.CallbackQuery):
    if not is_admin(c.from_user.id): return
    cur = get_setting("provider_5sim_enabled","1")
    set_setting("provider_5sim_enabled", "0" if cur=="1" else "1")
    await ad_providers(c, None)

@dp.callback_query_handler(lambda c:c.data=="tog_sms")
async def tog_sms(c: types.CallbackQuery):
    if not is_admin(c.from_user.id): return
    cur = get_setting("provider_sms_enabled","1")
    set_setting("provider_sms_enabled", "0" if cur=="1" else "1")
    await ad_providers(c, None)

@dp.callback_query_handler(lambda c:c.data=="ad_channels")
async def ad_channels(c: types.CallbackQuery, state:FSMContext):
    if not is_admin(c.from_user.id): return
    txt = (f"📢 <b>القنوات</b>\n"
           f"• اشتراك إجباري: {FORCE_CHANNELS}\n"
           f"• محاولات الشراء: <code>{CH_ATTEMPTS}</code>\n"
           f"• تسجيل الدخول: <code>{CH_LOGIN}</code>\n"
           f"• دعم العملاء (تجميع الرسائل): <code>{CH_SUPPORT_IN}</code>\n"
           f"• عامة - التفعيلات: {PUBLIC_ACTIVATIONS}\n"
           f"• عامة - الرسمية: {PUBLIC_OFFICIAL}\n")
    kb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("↩️ رجوع", callback_data="ad_back"))
    await c.message.edit_text(txt, reply_markup=kb)

@dp.callback_query_handler(lambda c:c.data=="ad_stats")
async def ad_stats(c: types.CallbackQuery, state:FSMContext):
    if not is_admin(c.from_user.id): return
    with closing(sqlite3.connect(DB)) as con:
        users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        orders= con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    kb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("↩️ رجوع", callback_data="ad_back"))
    await c.message.edit_text(f"📈 <b>إحصائيات</b>\n• المستخدمون: {users}\n• الطلبات: {orders}", reply_markup=kb)

@dp.callback_query_handler(lambda c:c.data=="ad_back")
async def ad_back(c: types.CallbackQuery):
    if not is_admin(c.from_user.id): return
    await c.message.delete()
    await bot.send_message(c.from_user.id, "⬅️ عودة للوحة التحكم.")
    # إعادة عرض اللوحة
    class Dummy: pass
    D=Dummy(); D.from_user=c.from_user
    await admin_panel(D, None)

# =============== تحويل رسائل العملاء إلى قناة الدعم ===============
@dp.message_handler(content_types=types.ContentTypes.TEXT)
async def route_to_support(m: types.Message):
    # تجاهل أزرارنا المعروفة
    known = {"⚡ طلب رقم","👤 لوحة الحساب","🧾 شروط الاستخدام","🆘 الدعم والمساعدة",
             "🏠 الصفحة الرئيسية","🔙 رجوع","💡 إنشاء حساب","✅ تسجيل الدخول","📊 الإحصائيات"}
    if m.text in known:
        return
    try:
        await bot.send_message(
            CH_SUPPORT_IN,
            f"📩 <b>رسالة عميل</b>\n"
            f"• من: <a href='tg://user?id={m.from_user.id}'>{m.from_user.full_name}</a> (<code>{m.from_user.id}</code>)\n"
            f"• النص:\n{m.text}"
        )
        await m.answer("✅ تم تحويل رسالتك للدعم. سنعاود التواصل معك قريبًا.", reply_markup=main_menu(db_get_balance(m.from_user.id)))
    except:
        await m.answer("⚠️ تعذر تحويل رسالتك الآن. حاول لاحقًا.")

# =============== تشغيل ===============
if __name__ == "__main__":
    init_db()
    executor.start_polling(dp, skip_updates=True)