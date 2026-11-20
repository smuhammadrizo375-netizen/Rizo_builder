"""
RIZO BUILDER - bot kodlarini sotuvchi bot
Kutubxonalar: requirements.txt
Ishga tushirish: python rizo_builder.py
"""
import asyncio
import logging
import os

import psycopg2
import psycopg2.extras
from aiohttp import web

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, LabeledPrice, Message,
                           PreCheckoutQuery)

# ============ SOZLAMALAR (hosting Environment Variables'dan olinadi) ============
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMINS = [int(x) for x in os.getenv("ADMINS", "").split(",") if x.strip()]
DATABASE_URL = os.getenv("DATABASE_URL", "")   # Neon connection string
PORT = int(os.getenv("PORT", "10000"))
# =================================================================

logging.basicConfig(level=logging.INFO)
user_r, admin_r = Router(), Router()
admin_r.message.filter(F.from_user.id.in_(ADMINS))
admin_r.callback_query.filter(F.from_user.id.in_(ADMINS))

# ---------------------- DB ----------------------
DEFAULT_TEXTS = {
    "stats": "📊 Stats",
    "topup": "💰 Hisob to'ldirish",
    "create": "🤖 Bot yaratish",
    "simple": "🤖 Oddiy botlar",
    "vip": "💎 VIP botlar",
    "profile": "👤 Kabinet",
    "top": "🏆 Top reyting",
    "guide": "📖 Qo'llanma",
}
DEFAULT_SETTINGS = {
    "stars_rate": "200",  # 1 star = necha so'm
    "pay_gift": "🎁 Gift yuborish uchun: @username ga gift yuboring va skrinshot tashlang.",
    "pay_card": "💳 Karta: 0000 0000 0000 0000\nIsm: ISM FAMILIYA",
    "guide_text": "📖 Qo'llanma tez orada qo'shiladi.",
}


_con = None
_cache = {}


def _conn():
    """Bitta ulanishni qayta-qayta ishlatadi (tez ishlashi uchun)."""
    global _con
    if _con is None or _con.closed:
        _con = psycopg2.connect(DATABASE_URL, keepalives=1, keepalives_idle=30,
                                keepalives_interval=10, keepalives_count=5)
        _con.autocommit = True
    return _con


def q(sql, args=(), one=False, commit=False):
    global _con
    sql = sql.replace("?", "%s")
    is_insert = sql.lstrip().upper().startswith("INSERT") and "ON CONFLICT" not in sql.upper()
    if commit and is_insert:
        sql += " RETURNING id"
    for attempt in (1, 2):
        try:
            with _conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, args)
                if commit:
                    return cur.fetchone()["id"] if is_insert else None
                return cur.fetchone() if one else cur.fetchall()
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            _con = None          # ulanish uzilgan bo'lsa, qayta ulanib yana urinadi
            if attempt == 2:
                raise


def init_db():
    q("""CREATE TABLE IF NOT EXISTS rb_users(
        id BIGINT PRIMARY KEY, name TEXT, balance BIGINT DEFAULT 0,
        deposited BIGINT DEFAULT 0, spent BIGINT DEFAULT 0)""", commit=True)
    q("""CREATE TABLE IF NOT EXISTS rb_products(
        id SERIAL PRIMARY KEY, title TEXT, descr TEXT,
        price BIGINT, file_id TEXT, guide TEXT, active INTEGER DEFAULT 1)""", commit=True)
    q("ALTER TABLE rb_products ADD COLUMN IF NOT EXISTS vip INTEGER DEFAULT 0", commit=True)
    q("""CREATE TABLE IF NOT EXISTS rb_payments(
        id SERIAL PRIMARY KEY, user_id BIGINT, method TEXT,
        amount BIGINT, status TEXT DEFAULT 'pending')""", commit=True)
    q("""CREATE TABLE IF NOT EXISTS rb_purchases(
        id SERIAL PRIMARY KEY, user_id BIGINT, product_id INTEGER, price BIGINT)""", commit=True)
    q("CREATE TABLE IF NOT EXISTS rb_settings(key TEXT PRIMARY KEY, value TEXT)", commit=True)
    for k, v in DEFAULT_TEXTS.items():
        q("INSERT INTO rb_settings VALUES(?,?) ON CONFLICT (key) DO NOTHING", ("btn_" + k, v), commit=True)
    for k, v in DEFAULT_SETTINGS.items():
        q("INSERT INTO rb_settings VALUES(?,?) ON CONFLICT (key) DO NOTHING", (k, v), commit=True)


def get(key):
    if key not in _cache:
        r = q("SELECT value FROM rb_settings WHERE key=?", (key,), one=True)
        _cache[key] = r["value"] if r else ""
    return _cache[key]


def put(key, val):
    q("INSERT INTO rb_settings VALUES(?,?) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (key, val), commit=True)
    _cache[key] = val


def user(uid):
    return q("SELECT * FROM rb_users WHERE id=?", (uid,), one=True)


def add_balance(uid, amount):
    q("UPDATE rb_users SET balance=balance+?, deposited=deposited+? WHERE id=?",
      (amount, max(amount, 0), uid), commit=True)


def fmt(n):
    return f"{n:,}".replace(",", " ")


def B(text, data, style=None):
    """style: 'primary' (ko'k), 'success' (yashil), 'danger' (qizil). Yangi aiogram kerak."""
    kw = {"style": style} if style else {}
    return InlineKeyboardButton(text=text, callback_data=data, **kw)


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


BACK = B("⬅️ Orqaga", "menu")

# ---------------------- STATES ----------------------


class TopUp(StatesGroup):
    stars = State()
    amount = State()
    proof = State()


class AddBot(StatesGroup):
    title = State()
    descr = State()
    price = State()
    file = State()
    guide = State()


class AdminSt(StatesGroup):
    bal_uid = State()
    bal_amount = State()
    price = State()
    rename = State()
    setting = State()
    broadcast = State()


# ---------------------- USER ----------------------
def main_menu():
    t = lambda k: get("btn_" + k)
    return kb(
        [B(t("stats"), "stats", "primary"), B(t("profile"), "profile", "primary")],
        [B(t("topup"), "topup", "success")],
        [B(t("create"), "create", "success")],
        [B(t("top"), "top"), B(t("guide"), "guide")],
    )


@user_r.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    if not user(m.from_user.id):
        q("INSERT INTO rb_users(id,name) VALUES(?,?)", (m.from_user.id, m.from_user.full_name), commit=True)
    await m.answer("🤖 <b>Rizo Builder</b> ga xush kelibsiz!\nTayyor bot kodlarini shu yerda sotib oling.",
                   reply_markup=main_menu())


@user_r.callback_query(F.data == "menu")
async def menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("🤖 <b>Rizo Builder</b> — asosiy menyu", reply_markup=main_menu())


@user_r.callback_query(F.data == "stats")
async def stats(c: CallbackQuery):
    u = q("SELECT COUNT(*) n FROM rb_users", one=True)["n"]
    p = q("SELECT COUNT(*) n FROM rb_products WHERE active=1", one=True)["n"]
    s = q("SELECT COUNT(*) n FROM rb_purchases", one=True)["n"]
    await c.message.edit_text(
        f"📊 <b>Statistika</b>\n\n👥 Foydalanuvchilar: {u}\n🤖 Botlar: {p}\n✅ Sotilgan: {s}",
        reply_markup=kb([BACK]))


@user_r.callback_query(F.data == "profile")
async def profile(c: CallbackQuery):
    u = user(c.from_user.id)
    n = q("SELECT COUNT(*) n FROM rb_purchases WHERE user_id=?", (c.from_user.id,), one=True)["n"]
    await c.message.edit_text(
        f"👤 <b>Kabinet</b>\n\n🆔 ID: <code>{u['id']}</code>\n💰 Balans: {fmt(u['balance'])} so'm\n"
        f"🛒 Xaridlar: {n}", reply_markup=kb([BACK]))


@user_r.callback_query(F.data == "top")
async def top(c: CallbackQuery):
    rows = q("SELECT name, deposited FROM rb_users ORDER BY deposited DESC LIMIT 10")
    medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
    text = "🏆 <b>Top reyting</b>\n\n" + "\n".join(
        f"{medals[i]} {r['name']} — {fmt(r['deposited'])} so'm" for i, r in enumerate(rows)) \
        if rows else "Hali ma'lumot yo'q."
    await c.message.edit_text(text, reply_markup=kb([BACK]))


@user_r.callback_query(F.data == "guide")
async def guide(c: CallbackQuery):
    await c.message.edit_text(get("guide_text"), reply_markup=kb([BACK]))


# ---- hisob to'ldirish ----
@user_r.callback_query(F.data == "topup")
async def topup(c: CallbackQuery):
    await c.message.edit_text(
        "💰 <b>Hisobni to'ldirish usuli:</b>",
        reply_markup=kb([B("⭐ Stars", "tp:stars", "success")],
                        [B("🎁 Gift", "tp:gift", "primary")],
                        [B("💵 Pul (karta)", "tp:money", "primary")], [BACK]))


@user_r.callback_query(F.data == "tp:stars")
async def tp_stars(c: CallbackQuery, state: FSMContext):
    await state.set_state(TopUp.stars)
    await c.message.edit_text(f"⭐ Necha stars to'lamoqchisiz? (raqam yuboring)\n1 ⭐ = {get('stars_rate')} so'm",
                              reply_markup=kb([BACK]))


@user_r.message(TopUp.stars)
async def stars_amount(m: Message, state: FSMContext):
    if not (m.text or "").isdigit() or int(m.text) < 1:
        return await m.answer("❗ Faqat raqam yuboring.")
    n = int(m.text)
    await state.clear()
    await m.bot.send_invoice(
        m.chat.id, title="Hisob to'ldirish", description=f"{n} ⭐ = {fmt(n * int(get('stars_rate')))} so'm",
        payload="topup", provider_token="", currency="XTR",
        prices=[LabeledPrice(label="Stars", amount=n)])


@user_r.pre_checkout_query()
async def pre_checkout(p: PreCheckoutQuery):
    await p.answer(ok=True)


@user_r.message(F.successful_payment)
async def paid(m: Message):
    stars = m.successful_payment.total_amount
    som = stars * int(get("stars_rate"))
    add_balance(m.from_user.id, som)
    await m.answer(f"✅ To'lov qabul qilindi!\n+{fmt(som)} so'm hisobingizga tushdi.", reply_markup=main_menu())


@user_r.callback_query(F.data.in_({"tp:gift", "tp:money"}))
async def tp_manual(c: CallbackQuery, state: FSMContext):
    method = c.data.split(":")[1]
    await state.set_state(TopUp.amount)
    await state.update_data(method=method)
    info = get("pay_gift" if method == "gift" else "pay_card")
    await c.message.edit_text(f"{info}\n\n💬 To'lagan summangizni so'mda yozing (masalan: 20000):",
                              reply_markup=kb([BACK]))


@user_r.message(TopUp.amount)
async def manual_amount(m: Message, state: FSMContext):
    if not (m.text or "").isdigit():
        return await m.answer("❗ Faqat raqam yuboring.")
    await state.update_data(amount=int(m.text))
    await state.set_state(TopUp.proof)
    await m.answer("📸 Endi to'lov chekini (skrinshot) yuboring.")


@user_r.message(TopUp.proof)
async def manual_proof(m: Message, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    pid = q("INSERT INTO rb_payments(user_id,method,amount) VALUES(?,?,?)",
            (m.from_user.id, d["method"], d["amount"]), commit=True)
    for a in ADMINS:
        try:
            await m.bot.copy_message(a, m.chat.id, m.message_id)
            await m.bot.send_message(
                a, f"🧾 <b>To'lov #{pid}</b>\n👤 {m.from_user.full_name} (<code>{m.from_user.id}</code>)\n"
                   f"Usul: {d['method']}\nSumma: {fmt(d['amount'])} so'm",
                reply_markup=kb([B("✅ Tasdiqlash", f"pay:ok:{pid}", "success"),
                                 B("❌ Rad etish", f"pay:no:{pid}", "danger")]))
        except Exception:
            pass
    await m.answer("⏳ So'rovingiz adminga yuborildi. Tasdiqlangach balansingiz to'ldiriladi.",
                   reply_markup=main_menu())


# ---- bot sotib olish ----
async def show_products(c: CallbackQuery, vip: int):
    rows = q("SELECT * FROM rb_products WHERE active=1 AND vip=? ORDER BY price", (vip,))
    back = B("⬅️ Orqaga", "create")
    if not rows:
        return await c.message.edit_text("Hozircha botlar yo'q.", reply_markup=kb([back]))
    btns = [[B(f"{r['title']} — {fmt(r['price'])} so'm", f"p:{r['id']}", "danger" if vip else "success")] for r in rows]
    title = "💎 <b>VIP botlar:</b>" if vip else "🤖 <b>Oddiy botlar:</b>"
    await c.message.edit_text(title, reply_markup=kb(*btns, [back]))


@user_r.callback_query(F.data == "create")
async def create(c: CallbackQuery):
    t = lambda k: get("btn_" + k)
    await c.message.edit_text(
        "🤖 <b>Bot yaratish</b>\nBo'limni tanlang:",
        reply_markup=kb([B(t("simple"), "cr:0", "success"), B(t("vip"), "cr:1", "danger")], [BACK]))


@user_r.callback_query(F.data.in_({"cr:0", "cr:1"}))
async def create_list(c: CallbackQuery):
    await show_products(c, int(c.data.split(":")[1]))


@user_r.callback_query(F.data.startswith("p:"))
async def product(c: CallbackQuery):
    r = q("SELECT * FROM rb_products WHERE id=?", (int(c.data[2:]),), one=True)
    await c.message.edit_text(
        f"🤖 <b>{r['title']}</b>\n\n{r['descr']}\n\n💵 Narxi: <b>{fmt(r['price'])} so'm</b>",
        reply_markup=kb([B("🛒 Sotib olish", f"buy:{r['id']}", "success")], [B("⬅️ Orqaga", f"cr:{1 if r['vip'] else 0}")]))


@user_r.callback_query(F.data.startswith("buy:"))
async def buy(c: CallbackQuery):
    r = q("SELECT * FROM rb_products WHERE id=?", (int(c.data[4:]),), one=True)
    u = user(c.from_user.id)
    if u["balance"] < r["price"]:
        return await c.answer(f"❌ Balans yetmaydi! Kerak: {fmt(r['price'])}, sizda: {fmt(u['balance'])}",
                              show_alert=True)
    q("UPDATE rb_users SET balance=balance-?, spent=spent+? WHERE id=?", (r["price"], r["price"], u["id"]), commit=True)
    q("INSERT INTO rb_purchases(user_id,product_id,price) VALUES(?,?,?)", (u["id"], r["id"], r["price"]), commit=True)
    await c.message.answer_document(r["file_id"], caption=f"✅ <b>{r['title']}</b> sotib olindi!")
    if r["guide"]:
        await c.message.answer(f"📖 <b>Qo'llanma:</b>\n\n{r['guide']}")
    await c.message.answer("Asosiy menyu:", reply_markup=main_menu())


# ---------------------- ADMIN ----------------------
def admin_menu():
    return kb(
        [B("➕ Bot qo'shish", "a:add", "success"), B("💎 VIP bot qo'shish", "a:addvip", "danger")],
        [B("🗂 Botlar / narx", "a:list", "primary")],
        [B("💵 Balans to'ldirish", "a:bal", "success"), B("🧾 Kutilayotgan to'lovlar", "a:pays", "primary")],
        [B("🏆 Top reyting", "top"), B("📊 Statistika", "stats")],
        [B("✏️ Tugma nomlari", "a:ren", "primary"), B("⚙️ Sozlamalar", "a:set", "primary")],
        [B("📢 Xabar yuborish", "a:bc", "danger")],
    )


ABACK = B("⬅️ Admin", "a:menu")


@admin_r.message(Command("admin"))
async def admin_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🛠 <b>Admin panel</b>", reply_markup=admin_menu())


@admin_r.callback_query(F.data == "a:menu")
async def admin_back(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("🛠 <b>Admin panel</b>", reply_markup=admin_menu())


# -- bot qo'shish --
@admin_r.callback_query(F.data == "a:add")
async def a_add(c: CallbackQuery, state: FSMContext):
    await state.set_state(AddBot.title)
    await state.update_data(vip=0)
    await c.message.answer("Bot nomini yozing (tugmada chiqadi, masalan: ⭐ Stars Referal Bot):")


@admin_r.callback_query(F.data == "a:addvip")
async def a_addvip(c: CallbackQuery, state: FSMContext):
    await state.set_state(AddBot.title)
    await state.update_data(vip=1)
    await c.message.answer("💎 VIP bot nomini yozing:")


@admin_r.message(AddBot.title)
async def ab_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text)
    await state.set_state(AddBot.descr)
    await m.answer("Tavsifini yozing:")


@admin_r.message(AddBot.descr)
async def ab_descr(m: Message, state: FSMContext):
    await state.update_data(descr=m.text)
    await state.set_state(AddBot.price)
    await m.answer("Narxini yozing (so'm, faqat raqam):")


@admin_r.message(AddBot.price)
async def ab_price(m: Message, state: FSMContext):
    if not (m.text or "").isdigit():
        return await m.answer("❗ Raqam yuboring.")
    await state.update_data(price=int(m.text))
    await state.set_state(AddBot.file)
    await m.answer("Bot kodini fayl (.py / .zip) qilib yuboring:")


@admin_r.message(AddBot.file, F.document)
async def ab_file(m: Message, state: FSMContext):
    await state.update_data(file_id=m.document.file_id)
    await state.set_state(AddBot.guide)
    await m.answer("Qo'llanma matnini yozing (yo'q bo'lsa — <code>-</code> yuboring):")


@admin_r.message(AddBot.guide)
async def ab_guide(m: Message, state: FSMContext):
    d = await state.get_data()
    guide_txt = "" if m.text.strip() == "-" else m.text
    vip = d.get("vip", 0)
    pid = q("INSERT INTO rb_products(title,descr,price,file_id,guide,vip) VALUES(?,?,?,?,?,?)",
            (d["title"], d["descr"], d["price"], d["file_id"], guide_txt, vip), commit=True)
    await state.clear()
    await m.answer(f"✅ {'💎 VIP bot' if vip else 'Bot'} #{pid} sotuvga qo'yildi.", reply_markup=admin_menu())


# -- botlar ro'yxati / narx / o'chirish --
@admin_r.callback_query(F.data == "a:list")
async def a_list(c: CallbackQuery):
    rows = q("SELECT * FROM rb_products")
    btns = [[B(f"{'💎' if r['vip'] else ('✅' if r['active'] else '⛔')} {r['title']} — {fmt(r['price'])}", f"a:p:{r['id']}")] for r in rows]
    await c.message.edit_text("🗂 Botlar:", reply_markup=kb(*btns, [ABACK]))


@admin_r.callback_query(F.data.startswith("a:p:"))
async def a_prod(c: CallbackQuery):
    pid = int(c.data.split(":")[2])
    r = q("SELECT * FROM rb_products WHERE id=?", (pid,), one=True)
    await c.message.edit_text(
        f"<b>{r['title']}</b>\nNarx: {fmt(r['price'])} so'm\nHolat: {'sotuvda' if r['active'] else 'yashirin'}\nTur: {'💎 VIP' if r['vip'] else 'Oddiy'}",
        reply_markup=kb([B("💵 Narxni o'zgartirish", f"a:pr:{pid}", "primary")],
                        [B("👁 Yoqish/O'chirish", f"a:tg:{pid}", "primary")],
                        [B("💎 VIP ↔ Oddiy", f"a:vp:{pid}", "danger")],
                        [B("🗑 Butunlay o'chirish", f"a:dl:{pid}", "danger")], [B("⬅️ Orqaga", "a:list")]))


@admin_r.callback_query(F.data.startswith("a:pr:"))
async def a_price(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminSt.price)
    await state.update_data(pid=int(c.data.split(":")[2]))
    await c.message.answer("Yangi narxni yozing:")


@admin_r.message(AdminSt.price)
async def a_price_set(m: Message, state: FSMContext):
    if not (m.text or "").isdigit():
        return await m.answer("❗ Raqam yuboring.")
    d = await state.get_data()
    q("UPDATE rb_products SET price=? WHERE id=?", (int(m.text), d["pid"]), commit=True)
    await state.clear()
    await m.answer("✅ Narx yangilandi.", reply_markup=admin_menu())


@admin_r.callback_query(F.data.startswith("a:tg:"))
async def a_toggle(c: CallbackQuery):
    q("UPDATE rb_products SET active=1-active WHERE id=?", (int(c.data.split(":")[2]),), commit=True)
    await a_list(c)


@admin_r.callback_query(F.data.startswith("a:vp:"))
async def a_vip_toggle(c: CallbackQuery):
    q("UPDATE rb_products SET vip=1-vip WHERE id=?", (int(c.data.split(":")[2]),), commit=True)
    await a_prod(c)


@admin_r.callback_query(F.data.startswith("a:dl:"))
async def a_del(c: CallbackQuery):
    q("DELETE FROM rb_products WHERE id=?", (int(c.data.split(":")[2]),), commit=True)
    await a_list(c)


# -- balans to'ldirish --
@admin_r.callback_query(F.data == "a:bal")
async def a_bal(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminSt.bal_uid)
    await c.message.answer("Foydalanuvchi ID sini yozing:")


@admin_r.message(AdminSt.bal_uid)
async def a_bal_uid(m: Message, state: FSMContext):
    if not (m.text or "").isdigit() or not user(int(m.text)):
        return await m.answer("❗ Bunday foydalanuvchi topilmadi.")
    await state.update_data(uid=int(m.text))
    await state.set_state(AdminSt.bal_amount)
    await m.answer("Summani yozing (ayirish uchun minus: -5000):")


@admin_r.message(AdminSt.bal_amount)
async def a_bal_amount(m: Message, state: FSMContext):
    try:
        n = int(m.text)
    except Exception:
        return await m.answer("❗ Raqam yuboring.")
    d = await state.get_data()
    add_balance(d["uid"], n)
    await state.clear()
    await m.answer(f"✅ {d['uid']} balansiga {fmt(n)} so'm qo'shildi.", reply_markup=admin_menu())
    try:
        await m.bot.send_message(d["uid"], f"💰 Hisobingiz {fmt(n)} so'mga o'zgardi.")
    except Exception:
        pass


# -- to'lovlar --
@admin_r.callback_query(F.data == "a:pays")
async def a_pays(c: CallbackQuery):
    rows = q("SELECT * FROM rb_payments WHERE status='pending'")
    if not rows:
        return await c.message.edit_text("Kutilayotgan to'lov yo'q.", reply_markup=kb([ABACK]))
    for r in rows:
        await c.message.answer(
            f"🧾 #{r['id']} | {r['user_id']} | {r['method']} | {fmt(r['amount'])} so'm",
            reply_markup=kb([B("✅ Tasdiqlash", f"pay:ok:{r['id']}", "success"),
                             B("❌ Rad etish", f"pay:no:{r['id']}", "danger")]))


@admin_r.callback_query(F.data.startswith("pay:"))
async def pay_decide(c: CallbackQuery):
    _, act, pid = c.data.split(":")
    r = q("SELECT * FROM rb_payments WHERE id=?", (int(pid),), one=True)
    if not r or r["status"] != "pending":
        return await c.answer("Allaqachon ko'rib chiqilgan.", show_alert=True)
    if act == "ok":
        add_balance(r["user_id"], r["amount"])
        q("UPDATE rb_payments SET status='ok' WHERE id=?", (r["id"],), commit=True)
        msg, note = "✅ Tasdiqlandi", f"✅ To'lovingiz tasdiqlandi! +{fmt(r['amount'])} so'm."
    else:
        q("UPDATE rb_payments SET status='no' WHERE id=?", (r["id"],), commit=True)
        msg, note = "❌ Rad etildi", "❌ To'lovingiz rad etildi."
    await c.message.edit_text(f"{c.message.text}\n\n{msg}")
    try:
        await c.bot.send_message(r["user_id"], note)
    except Exception:
        pass


# -- tugma nomlarini o'zgartirish --
@admin_r.callback_query(F.data == "a:ren")
async def a_ren(c: CallbackQuery):
    btns = [[B(get("btn_" + k), f"a:rn:{k}")] for k in DEFAULT_TEXTS]
    await c.message.edit_text("Qaysi tugma nomini o'zgartiramiz?", reply_markup=kb(*btns, [ABACK]))


@admin_r.callback_query(F.data.startswith("a:rn:"))
async def a_rn(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminSt.rename)
    await state.update_data(key="btn_" + c.data.split(":")[2])
    await c.message.answer("Yangi nomni yozing (emoji bilan):")


@admin_r.message(AdminSt.rename)
async def a_rn_set(m: Message, state: FSMContext):
    d = await state.get_data()
    put(d["key"], m.text)
    await state.clear()
    await m.answer("✅ Tugma nomi yangilandi.", reply_markup=admin_menu())


# -- sozlamalar --
SETTING_NAMES = {"stars_rate": "⭐ Stars kursi", "pay_gift": "🎁 Gift rekvizit",
                 "pay_card": "💳 Karta rekvizit", "guide_text": "📖 Qo'llanma matni"}


@admin_r.callback_query(F.data == "a:set")
async def a_set(c: CallbackQuery):
    btns = [[B(v, f"a:st:{k}")] for k, v in SETTING_NAMES.items()]
    await c.message.edit_text("⚙️ Sozlamalar:", reply_markup=kb(*btns, [ABACK]))


@admin_r.callback_query(F.data.startswith("a:st:"))
async def a_st(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":")[2]
    await state.set_state(AdminSt.setting)
    await state.update_data(key=key)
    await c.message.answer(f"Hozirgi qiymat:\n{get(key)}\n\nYangisini yozing:")


@admin_r.message(AdminSt.setting)
async def a_st_set(m: Message, state: FSMContext):
    d = await state.get_data()
    put(d["key"], m.text)
    await state.clear()
    await m.answer("✅ Saqlandi.", reply_markup=admin_menu())


# -- xabar yuborish --
@admin_r.callback_query(F.data == "a:bc")
async def a_bc(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminSt.broadcast)
    await c.message.answer("Hammaga yuboriladigan xabarni yozing:")


@admin_r.message(AdminSt.broadcast)
async def a_bc_send(m: Message, state: FSMContext):
    await state.clear()
    ok = 0
    for r in q("SELECT id FROM rb_users"):
        try:
            await m.copy_to(r["id"])
            ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await m.answer(f"✅ {ok} ta foydalanuvchiga yuborildi.", reply_markup=admin_menu())


# ---------------------- RUN ----------------------
async def health(request):
    return web.Response(text="Rizo Builder ishlayapti ✅")


async def main():
    init_db()
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()   # UptimeRobot shu manzilga so'rov yuboradi
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_routers(admin_r, user_r)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
