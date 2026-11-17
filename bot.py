"""
Telegram Uploader Bot (aiogram 3.x)
- آپلود فایل (تکی/گروهی) و تحویل با لینک اختصاصی
- جوین اجباری (ربات باید ادمین کانال باشه)
- جوین اختیاری/اسپانسری با شمارش دقیق و حذف خودکار بعد از رسیدن به هدف
- پنل ادمین، آمار، تنظیمات، ارسال همگانی، مدیریت ادمین‌ها

اجرا:
    pip install -r requirements.txt
    export BOT_TOKEN="8911656630:AAEnCjmFGUZQPw0JxFOskdL2gaVho5TI73o"
    export OWNER_ID="8982085745"
    python bot.py
"""
import asyncio
import html
import logging
import os
import secrets
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram.exceptions import TelegramRetryAfter
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
DB_PATH = os.environ.get("DB_PATH", "bot.db")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Tehran"))  # منطقه زمانی آمار روزانه

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
ME = None  # پر میشه در main
DB: aiosqlite.Connection = None

DEFAULTS = {
    "welcome": "سلام 👋\nبرای دریافت فایل، از لینک اختصاصی استفاده کن.",
    "join_text": "📢 برای دریافت فایل، اول تو کانال‌های زیر عضو شو و بعد دکمه «عضو شدم» رو بزن:",
    "caption": "",
    "protect": "0",   # جلوگیری از فوروارد/ذخیره
    "autodel": "0",   # حذف خودکار فایل بعد از N ثانیه (0 = خاموش)
}

SEND = {
    "document": ("send_document", "document"),
    "video": ("send_video", "video"),
    "audio": ("send_audio", "audio"),
    "photo": ("send_photo", "photo"),
    "voice": ("send_voice", "voice"),
    "animation": ("send_animation", "animation"),
    "video_note": ("send_video_note", "video_note"),
}


# ───────────────────────── دیتابیس ─────────────────────────
async def init_db():
    global DB
    DB = await aiosqlite.connect(DB_PATH)
    DB.row_factory = aiosqlite.Row
    await DB.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, ts INTEGER);
        CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS codes(code TEXT PRIMARY KEY, downloads INTEGER DEFAULT 0, ts INTEGER);
        CREATE TABLE IF NOT EXISTS files(n INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, ftype TEXT, file_id TEXT, caption TEXT);
        CREATE TABLE IF NOT EXISTS channels(
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, title TEXT, url TEXT,
            kind TEXT, verified INTEGER, target INTEGER DEFAULT 0, joined INTEGER DEFAULT 0, active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS shown(cid INTEGER, uid INTEGER, PRIMARY KEY(cid, uid));
        CREATE TABLE IF NOT EXISTS joins(cid INTEGER, uid INTEGER, counted INTEGER, PRIMARY KEY(cid, uid));
        CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS daily(day TEXT, metric TEXT, ref TEXT, n INTEGER DEFAULT 0, PRIMARY KEY(day, metric, ref));
        """
    )
    await DB.commit()


async def fetchone(sql, args=()):
    cur = await DB.execute(sql, args)
    return await cur.fetchone()


async def fetchall(sql, args=()):
    cur = await DB.execute(sql, args)
    return await cur.fetchall()


async def execute(sql, args=()):
    cur = await DB.execute(sql, args)
    await DB.commit()
    return cur


def today():
    return datetime.now(TZ).strftime("%Y-%m-%d")


async def bump(metric, ref="", n=1):
    await execute(
        "INSERT INTO daily(day,metric,ref,n) VALUES(?,?,?,?) "
        "ON CONFLICT(day,metric,ref) DO UPDATE SET n=n+excluded.n",
        (today(), metric, ref, n),
    )


async def get_set(k):
    r = await fetchone("SELECT v FROM settings WHERE k=?", (k,))
    return r["v"] if r else DEFAULTS[k]


async def put_set(k, v):
    await execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


async def is_admin(uid):
    if uid == OWNER_ID:
        return True
    return bool(await fetchone("SELECT 1 FROM admins WHERE id=?", (uid,)))


async def notify_admins(text):
    ids = {OWNER_ID} | {r["id"] for r in await fetchall("SELECT id FROM admins")}
    for i in ids:
        try:
            await bot.send_message(i, text)
        except Exception:
            pass


# ───────────────────────── منطق جوین ─────────────────────────
async def record_join(ch, uid, counted):
    cur = await DB.execute("INSERT OR IGNORE INTO joins VALUES(?,?,?)", (ch["id"], uid, counted))
    await DB.commit()
    if cur.rowcount and counted:
        await execute("UPDATE channels SET joined=joined+1 WHERE id=?", (ch["id"],))
        await bump("join", str(ch["id"]))
        row = await fetchone("SELECT * FROM channels WHERE id=?", (ch["id"],))
        if row["target"] and row["joined"] >= row["target"] and row["active"]:
            await execute("UPDATE channels SET active=2 WHERE id=?", (ch["id"],))  # 2 = هدف تکمیل شد
            await notify_admins(
                f"🎯 لینک «{html.escape(row['title'])}» به هدف <b>{row['target']}</b> عضو رسید و خودکار از ربات حذف شد.\n"
                f"آمارش تو پنل می‌مونه."
            )


async def missing_channels(uid):
    """کانال‌هایی که کاربر هنوز عضو نیست رو برمی‌گردونه."""
    missing = []
    for ch in await fetchall("SELECT * FROM channels WHERE active=1 ORDER BY kind, id"):
        if ch["verified"]:
            try:
                m = await bot.get_chat_member(ch["chat_id"], uid)
                ok = m.status in ("member", "administrator", "creator") or (
                    m.status == "restricted" and getattr(m, "is_member", False)
                )
            except Exception as e:
                logging.warning("check failed for %s: %s", ch["title"], e)
                ok = True  # اگه ربات دسترسی نداره کاربر رو گیر نمیندازیم
            if ok:
                was_shown = await fetchone("SELECT 1 FROM shown WHERE cid=? AND uid=?", (ch["id"], uid))
                await record_join(ch, uid, 1 if was_shown else 0)
            else:
                await execute("INSERT OR IGNORE INTO shown VALUES(?,?)", (ch["id"], uid))
                missing.append(ch)
        else:  # لینک بدون وریفای
            if not await fetchone("SELECT 1 FROM joins WHERE cid=? AND uid=?", (ch["id"], uid)):
                missing.append(ch)
    return missing


async def show_join_panel(target: Message, missing, code):
    b = InlineKeyboardBuilder()
    for ch in missing:
        icon = "🔒" if ch["kind"] == "force" else "🎁"
        b.button(text=f"{icon} {ch['title']}", url=ch["url"])
    b.button(text="✅ عضو شدم", callback_data=f"chk:{code or '-'}")
    b.adjust(1)
    await target.answer(await get_set("join_text"), reply_markup=b.as_markup())


# ───────────────────────── تحویل فایل ─────────────────────────
async def deliver(chat_id, code):
    files = await fetchall("SELECT * FROM files WHERE code=? ORDER BY n", (code,))
    if not files:
        await bot.send_message(chat_id, "❌ این لینک معتبر نیست یا فایل حذف شده.")
        return
    protect = (await get_set("protect")) == "1"
    extra = await get_set("caption")
    ids = []
    for f in files:
        method, param = SEND[f["ftype"]]
        kw = {"chat_id": chat_id, param: f["file_id"], "protect_content": protect}
        if f["ftype"] != "video_note":
            cap = "\n\n".join(x for x in [f["caption"] or "", extra] if x)
            if cap:
                kw["caption"] = cap
        try:
            msg = await getattr(bot, method)(**kw)
            ids.append(msg.message_id)
        except Exception as e:
            logging.error("send failed: %s", e)
    await execute("UPDATE codes SET downloads=downloads+1 WHERE code=?", (code,))
    await bump("downloads")
    sec = int(await get_set("autodel") or 0)
    if sec and ids:
        note = await bot.send_message(chat_id, f"⏳ این فایل‌ها بعد از {sec} ثانیه حذف میشن. ذخیره کن!")
        asyncio.create_task(_later_delete(chat_id, ids + [note.message_id], sec))


async def _later_delete(chat_id, ids, sec):
    await asyncio.sleep(sec)
    for i in ids:
        try:
            await bot.delete_message(chat_id, i)
        except Exception:
            pass


# ───────────────────────── روتر کاربر ─────────────────────────
user = Router()


@user.message(CommandStart())
async def start(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    cur = await execute("INSERT OR IGNORE INTO users VALUES(?,?)", (m.from_user.id, int(time.time())))
    if cur.rowcount:
        await bump("new_users")
    code = command.args
    missing = await missing_channels(m.from_user.id)  # هر استارت: لینک‌های جدید هم نشون داده میشن
    if missing:
        return await show_join_panel(m, missing, code)
    if code:
        return await deliver(m.chat.id, code)
    txt = await get_set("welcome")
    if await is_admin(m.from_user.id):
        txt += "\n\n🛠 پنل مدیریت: /panel"
    await m.answer(txt)


@user.callback_query(F.data.startswith("chk:"))
async def check_cb(c: CallbackQuery):
    code = c.data[4:]
    code = None if code == "-" else code
    uid = c.from_user.id
    # لینک‌های بدون وریفای: با زدن «عضو شدم» ثبت میشن
    for ch in await fetchall("SELECT * FROM channels WHERE active=1 AND verified=0"):
        await record_join(ch, uid, 1)
    missing = await missing_channels(uid)
    if missing:
        names = "، ".join(ch["title"] for ch in missing)
        return await c.answer(f"❗ هنوز عضو این‌ها نشدی: {names}", show_alert=True)
    try:
        await c.message.delete()
    except Exception:
        pass
    if code:
        await deliver(c.message.chat.id, code)
    else:
        await bot.send_message(c.message.chat.id, "✅ تایید شد! حالا از لینک فایل استفاده کن.")
    await c.answer()


# ───────────────────────── روتر ادمین ─────────────────────────
class IsAdmin(BaseFilter):
    async def __call__(self, event) -> bool:
        return await is_admin(event.from_user.id)


admin = Router()
admin.message.filter(IsAdmin())
admin.callback_query.filter(IsAdmin())


class S(StatesGroup):
    upload = State()
    add = State()
    target = State()
    broadcast = State()
    text = State()
    add_admin = State()


def menu_kb(uid):
    b = InlineKeyboardBuilder()
    b.button(text="📤 آپلود فایل", callback_data="adm:up")
    b.button(text="🔒 جوین اجباری", callback_data="adm:ch:force")
    b.button(text="🎁 جوین اختیاری (اسپانسر)", callback_data="adm:ch:optional")
    b.button(text="📊 آمار", callback_data="adm:stats")
    b.button(text="⚙️ تنظیمات", callback_data="adm:set")
    b.button(text="📣 ارسال همگانی", callback_data="adm:bc")
    if uid == OWNER_ID:
        b.button(text="👤 مدیریت ادمین‌ها", callback_data="adm:admins")
    b.adjust(1, 2, 2, 1, 1)
    return b.as_markup()


def back_kb(to="adm:home"):
    b = InlineKeyboardBuilder()
    b.button(text="🔙 بازگشت", callback_data=to)
    return b.as_markup()


@admin.message(Command("panel"))
async def panel_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🛠 پنل مدیریت", reply_markup=menu_kb(m.from_user.id))


@admin.callback_query(F.data == "adm:home")
async def home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("🛠 پنل مدیریت", reply_markup=menu_kb(c.from_user.id))


# ── آپلود ──
def extract(m: Message):
    if m.animation:
        return "animation", m.animation.file_id
    if m.document:
        return "document", m.document.file_id
    if m.video:
        return "video", m.video.file_id
    if m.audio:
        return "audio", m.audio.file_id
    if m.voice:
        return "voice", m.voice.file_id
    if m.video_note:
        return "video_note", m.video_note.file_id
    if m.photo:
        return "photo", m.photo[-1].file_id
    return None


@admin.callback_query(F.data == "adm:up")
async def up_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.upload)
    await state.update_data(items=[])
    b = InlineKeyboardBuilder()
    b.button(text="✅ پایان و ساخت لینک", callback_data="adm:upend")
    b.button(text="🔙 لغو", callback_data="adm:home")
    b.adjust(1)
    await c.message.edit_text(
        "📤 فایل(ها) رو بفرست (یکی یا چندتا).\nبعد از اتمام دکمه «پایان» رو بزن.", reply_markup=b.as_markup()
    )


@admin.message(S.upload)
async def up_collect(m: Message, state: FSMContext):
    ex = extract(m)
    if not ex:
        return await m.reply("❌ این نوع پیام پشتیبانی نمیشه. فایل بفرست.")
    items = (await state.get_data())["items"]
    items.append((ex[0], ex[1], m.caption or ""))
    await state.update_data(items=items)
    await m.reply(f"✔️ دریافت شد ({len(items)} فایل). ادامه بده یا «پایان» بزن.")


@admin.callback_query(F.data == "adm:upend", S.upload)
async def up_end(c: CallbackQuery, state: FSMContext):
    items = (await state.get_data()).get("items", [])
    if not items:
        return await c.answer("هنوز فایلی نفرستادی!", show_alert=True)
    code = secrets.token_urlsafe(8)
    await execute("INSERT INTO codes(code, ts) VALUES(?,?)", (code, int(time.time())))
    for t, fid, cap in items:
        await execute("INSERT INTO files(code, ftype, file_id, caption) VALUES(?,?,?,?)", (code, t, fid, cap))
    await state.clear()
    await c.message.edit_text(
        f"✅ ذخیره شد ({len(items)} فایل)\n\n🔗 لینک:\n<code>https://t.me/{ME.username}?start={code}</code>\n\n"
        f"🔑 شناسه: <code>{code}</code>\nحذف: <code>/del {code}</code>",
        reply_markup=back_kb(),
    )


@admin.message(Command("del"))
async def del_code(m: Message, command: CommandObject):
    if not command.args:
        return await m.answer("استفاده: /del شناسه")
    await execute("DELETE FROM files WHERE code=?", (command.args,))
    await execute("DELETE FROM codes WHERE code=?", (command.args,))
    await m.answer("🗑 حذف شد.")


# ── کانال‌ها ──
async def resolve_chat(m: Message):
    fo = m.forward_origin
    if fo is not None and getattr(fo, "type", None) == "channel":
        return fo.chat
    t = (m.text or "").strip()
    if not t:
        return None
    try:
        return await bot.get_chat(int(t) if t.lstrip("-").isdigit() else t)
    except Exception:
        return None


async def bot_is_admin(chat_id):
    try:
        mem = await bot.get_chat_member(chat_id, ME.id)
        return mem.status in ("administrator", "creator")
    except Exception:
        return False


async def link_for(chat):
    if chat.username:
        return f"https://t.me/{chat.username}"
    try:
        return (await bot.create_chat_invite_link(chat.id)).invite_link
    except Exception:
        return chat.invite_link


KIND_FA = {"force": "🔒 اجباری", "optional": "🎁 اختیاری"}
ST_ICON = {0: "🔴", 1: "🟢", 2: "✅"}
ST_FA = {0: "غیرفعال 🔴", 1: "فعال 🟢", 2: "هدف تکمیل شد ✅ (از ربات برداشته شده)"}


@admin.callback_query(F.data.startswith("adm:ch:"))
async def ch_list(c: CallbackQuery, state: FSMContext):
    await state.clear()
    kind = c.data.split(":")[2]
    rows = await fetchall("SELECT * FROM channels WHERE kind=? ORDER BY id", (kind,))
    b = InlineKeyboardBuilder()
    for r in rows:
        st = ST_ICON[r["active"]]
        tgt = f"{r['joined']}/{r['target']}" if r["target"] else f"{r['joined']}"
        b.button(text=f"{st} {r['title']} ({tgt})", callback_data=f"adm:chi:{r['id']}")
    b.button(text="➕ افزودن", callback_data=f"adm:add:{kind}")
    b.button(text="🔙 بازگشت", callback_data="adm:home")
    b.adjust(1)
    await c.message.edit_text(f"{KIND_FA[kind]} — لیست:", reply_markup=b.as_markup())


@admin.callback_query(F.data.startswith("adm:add:"))
async def add_start(c: CallbackQuery, state: FSMContext):
    kind = c.data.split(":")[2]
    await state.set_state(S.add)
    await state.update_data(kind=kind)
    if kind == "force":
        txt = (
            "🔒 <b>جوین اجباری</b>\nربات رو اول تو کانال <b>ادمین</b> کن، بعد یکی از این‌ها رو بفرست:\n"
            "• یه پست از کانال فوروارد کن\n• یا @username یا آیدی عددی کانال"
        )
    else:
        txt = (
            "🎁 <b>جوین اختیاری</b>\nیکی از روش‌ها:\n\n"
            "1️⃣ <b>وریفای‌شده (آمار دقیق):</b> ربات ادمین کانال باشه، پست فوروارد کن یا @username بفرست.\n\n"
            "2️⃣ <b>فقط لینک:</b>\n<code>اسم نمایشی | https://t.me/...</code>\n\n"
            "3️⃣ <b>لینک دعوت + وریفای:</b>\n<code>اسم | لینک | @username_یا_آیدی_عددی</code>\n"
            "(ربات باید ادمین اون کانال باشه)"
        )
    await c.message.edit_text(txt, reply_markup=back_kb(f"adm:ch:{kind}"))


@admin.message(S.add)
async def add_recv(m: Message, state: FSMContext):
    kind = (await state.get_data())["kind"]
    t = m.text or ""
    chat_id, verified = None, 0
    if kind == "optional" and "|" in t:
        parts = [p.strip() for p in t.split("|")]
        if len(parts) < 2 or not parts[1].startswith("http"):
            return await m.reply("❌ فرمت غلطه. مثال: اسم | https://t.me/xxx")
        title, url = parts[0], parts[1]
        if len(parts) > 2 and parts[2]:
            try:
                chat = await bot.get_chat(int(parts[2]) if parts[2].lstrip("-").isdigit() else parts[2])
            except Exception:
                return await m.reply("❌ کانال سوم پیدا نشد.")
            if not await bot_is_admin(chat.id):
                return await m.reply("❌ ربات ادمین اون کانال نیست.")
            chat_id, verified = chat.id, 1
    else:
        chat = await resolve_chat(m)
        if not chat:
            return await m.reply("❌ کانال پیدا نشد. پست فوروارد کن یا @username بفرست.")
        if not await bot_is_admin(chat.id):
            return await m.reply("❌ ربات ادمین این کانال نیست. اول ادمینش کن.")
        title, url = chat.title, await link_for(chat)
        if not url:
            return await m.reply("❌ لینک کانال گرفته نشد (دسترسی «دعوت کاربر» به ربات بده).")
        chat_id, verified = chat.id, 1
    cur = await execute(
        "INSERT INTO channels(chat_id,title,url,kind,verified) VALUES(?,?,?,?,?)",
        (str(chat_id) if chat_id else None, title, url, kind, verified),
    )
    if kind == "optional":
        await state.set_state(S.target)
        await state.update_data(cid=cur.lastrowid)
        warn = "" if verified else "\n⚠️ این لینک وریفای نمیشه؛ شمارش بر اساس دکمه «عضو شدم» هست."
        return await m.reply(
            f"✅ اضافه شد.{warn}\n\nهدف تعداد عضو رو بفرست (بعد از رسیدن، لینک خودکار حذف میشه). <code>0</code> = نامحدود"
        )
    await state.clear()
    await m.reply("✅ کانال اجباری اضافه شد.", reply_markup=back_kb("adm:ch:force"))


@admin.message(S.target)
async def target_recv(m: Message, state: FSMContext):
    if not (m.text or "").strip().isdigit():
        return await m.reply("فقط عدد بفرست.")
    cid = (await state.get_data())["cid"]
    new = int(m.text)
    await execute("UPDATE channels SET target=? WHERE id=?", (new, cid))
    row = await fetchone("SELECT * FROM channels WHERE id=?", (cid,))
    msg = "✅ هدف ثبت شد."
    if row["active"] == 2 and (new == 0 or new > row["joined"]):
        await execute("UPDATE channels SET active=1 WHERE id=?", (cid,))
        msg += "\n🟢 لینک دوباره فعال شد."
    await state.clear()
    await m.reply(msg, reply_markup=back_kb("adm:ch:optional"))


@admin.callback_query(F.data.startswith("adm:chi:"))
async def ch_info(c: CallbackQuery):
    cid = int(c.data.split(":")[2])
    r = await fetchone("SELECT * FROM channels WHERE id=?", (cid,))
    if not r:
        return await c.answer("پیدا نشد")
    txt = (
        f"{KIND_FA[r['kind']]} — <b>{html.escape(r['title'])}</b>\n"
        f"وضعیت: {ST_FA[r['active']]}\n"
        f"وریفای: {'بله ✅' if r['verified'] else 'خیر (فقط دکمه)'}\n"
        f"عضو جذب‌شده: <b>{r['joined']}</b>" + (f" / {r['target']}" if r["target"] else "")
        + (f"\nباقی‌مانده: <b>{max(r['target'] - r['joined'], 0)}</b>" if r["target"] else "")
        + f"\n{r['url']}"
    )
    if r["verified"]:
        shown = (await fetchone("SELECT COUNT(*) n FROM shown WHERE cid=?", (cid,)))["n"]
        if shown:
            txt += f"\n📈 نرخ تبدیل: {r['joined']} از {shown} نفری که دیدن ({round(r['joined'] * 100 / shown)}٪)"
    ret = await fetchone("SELECT v FROM settings WHERE k=?", (f"ret:{cid}",))
    if ret:
        stay, left, unk, ts = (int(x) for x in ret["v"].split("|"))
        base = stay + left
        pct = round(stay * 100 / base) if base else 0
        when = datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M")
        txt += f"\n🔍 ماندگاری ({when}): <b>{pct}٪</b> — مونده {stay} | لفت داده {left}"
    b = InlineKeyboardBuilder()
    b.button(text="🔄 فعال/غیرفعال", callback_data=f"adm:cht:{cid}")
    if r["kind"] == "optional":
        b.button(text="🎯 تغییر هدف", callback_data=f"adm:chg:{cid}")
    if r["verified"]:
        b.button(text="🔍 بررسی ماندگاری اعضا", callback_data=f"adm:ret:{cid}")
    b.button(text="🗑 حذف", callback_data=f"adm:chd:{cid}")
    b.button(text="🔙 بازگشت", callback_data=f"adm:ch:{r['kind']}")
    b.adjust(2, 2, 1)
    await c.message.edit_text(txt, reply_markup=b.as_markup(), disable_web_page_preview=True)


@admin.callback_query(F.data.startswith("adm:cht:"))
async def ch_toggle(c: CallbackQuery):
    cid = int(c.data.split(":")[2])
    await execute("UPDATE channels SET active=CASE WHEN active=1 THEN 0 ELSE 1 END WHERE id=?", (cid,))
    c.data = f"adm:chi:{cid}"
    await ch_info(c)


@admin.callback_query(F.data.startswith("adm:chd:"))
async def ch_delete(c: CallbackQuery):
    cid = int(c.data.split(":")[2])
    await execute("DELETE FROM channels WHERE id=?", (cid,))
    await c.answer("حذف شد 🗑")
    await c.message.edit_text("حذف شد.", reply_markup=back_kb())


@admin.callback_query(F.data.startswith("adm:chg:"))
async def ch_goal(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.target)
    await state.update_data(cid=int(c.data.split(":")[2]))
    await c.message.edit_text("هدف جدید رو بفرست (0 = نامحدود):")


# ── ماندگاری اعضا ──
RET_RUNNING = set()


async def _is_member(chat_id, uid):
    for _ in range(3):
        try:
            m = await bot.get_chat_member(chat_id, uid)
            return m.status in ("member", "administrator", "creator") or (
                m.status == "restricted" and getattr(m, "is_member", False)
            )
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except Exception:
            return None
    return None


async def _ret_job(admin_chat, cid, title, chat_id):
    try:
        uids = [r["uid"] for r in await fetchall("SELECT uid FROM joins WHERE cid=? AND counted=1", (cid,))]
        stay = left = unk = 0
        for u in uids:
            res = await _is_member(chat_id, u)
            if res is None:
                unk += 1
            elif res:
                stay += 1
            else:
                left += 1
            await asyncio.sleep(0.05)
        await put_set(f"ret:{cid}", f"{stay}|{left}|{unk}|{int(time.time())}")
        base = stay + left
        pct = round(stay * 100 / base) if base else 0
        await bot.send_message(
            admin_chat,
            f"🔍 <b>ماندگاری «{html.escape(title)}»</b>\n\n"
            f"👥 بررسی‌شده: {len(uids)}\n✅ هنوز عضو: <b>{stay}</b>\n🚪 لفت داده: <b>{left}</b>\n"
            f"❔ نامشخص: {unk}\n\n📈 ماندگاری: <b>{pct}٪</b>",
            reply_markup=back_kb(f"adm:chi:{cid}"),
        )
    except Exception as e:
        logging.error("retention job failed: %s", e)
        await bot.send_message(admin_chat, "❌ بررسی ماندگاری با خطا مواجه شد.")
    finally:
        RET_RUNNING.discard(cid)


@admin.callback_query(F.data.startswith("adm:ret:"))
async def ret_start(c: CallbackQuery):
    cid = int(c.data.split(":")[2])
    r = await fetchone("SELECT * FROM channels WHERE id=?", (cid,))
    if not r or not r["verified"]:
        return await c.answer("فقط برای لینک‌های وریفای‌شده (ربات ادمین باشه)", show_alert=True)
    if cid in RET_RUNNING:
        return await c.answer("⏳ بررسی در حال انجامه...", show_alert=True)
    total = (await fetchone("SELECT COUNT(*) n FROM joins WHERE cid=? AND counted=1", (cid,)))["n"]
    if not total:
        return await c.answer("هنوز عضوی از طریق ربات ثبت نشده.", show_alert=True)
    if not await bot_is_admin(r["chat_id"]):
        return await c.answer("ربات دیگه ادمین اون کانال نیست!", show_alert=True)
    RET_RUNNING.add(cid)
    asyncio.create_task(_ret_job(c.message.chat.id, cid, r["title"], r["chat_id"]))
    mins = max(1, round(total * 0.05 / 60))
    await c.message.edit_text(
        f"⏳ بررسی {total} عضو شروع شد (حدود {mins} دقیقه).\nبعد از اتمام نتیجه رو برات می‌فرستم.",
        reply_markup=back_kb(f"adm:chi:{cid}"),
    )


# ── آمار ──
@admin.callback_query(F.data == "adm:stats")
async def stats(c: CallbackQuery):
    day = int(time.time()) - 86400
    users = (await fetchone("SELECT COUNT(*) n FROM users"))["n"]
    new24 = (await fetchone("SELECT COUNT(*) n FROM users WHERE ts>?", (day,)))["n"]
    files = (await fetchone("SELECT COUNT(*) n FROM codes"))["n"]
    dl = (await fetchone("SELECT COALESCE(SUM(downloads),0) n FROM codes"))["n"]
    txt = (
        f"📊 <b>آمار</b>\n👥 کاربران: <b>{users}</b> (۲۴ ساعت اخیر: {new24})\n"
        f"📁 لینک‌های فایل: <b>{files}</b>\n📥 تعداد دریافت: <b>{dl}</b>\n\n<b>جذب عضو از طریق ربات:</b>\n"
    )
    rows = await fetchall("SELECT * FROM channels ORDER BY kind, id")
    for r in rows:
        st = ST_ICON[r["active"]]
        tgt = f"/{r['target']}" if r["target"] else ""
        tag = "" if r["verified"] else " (بدون وریفای)"
        txt += f"{st} {KIND_FA[r['kind']]} {html.escape(r['title'])}: <b>{r['joined']}</b>{tgt}{tag}\n"
    if not rows:
        txt += "—"
    b = InlineKeyboardBuilder()
    b.button(text="📅 آمار روزانه (۷ روز اخیر)", callback_data="adm:daily")
    b.button(text="🔙 بازگشت", callback_data="adm:home")
    b.adjust(1)
    await c.message.edit_text(txt, reply_markup=b.as_markup())


@admin.callback_query(F.data == "adm:daily")
async def daily(c: CallbackQuery):
    now = datetime.now(TZ)
    days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    rows = await fetchall("SELECT day, metric, SUM(n) n FROM daily WHERE day>=? GROUP BY day, metric", (days[-1],))
    d = {(r["day"], r["metric"]): r["n"] for r in rows}
    txt = "📅 <b>آمار روزانه</b>\n<pre>تاریخ   کاربر  دریافت  عضو\n"
    for day in days:
        txt += f"{day[5:]}  {d.get((day, 'new_users'), 0):>5}  {d.get((day, 'downloads'), 0):>6}  {d.get((day, 'join'), 0):>4}\n"
    txt += "</pre>\n(کاربر = استارت‌کننده جدید | دریافت = فایل تحویل‌شده | عضو = جذب از لینک‌ها)\n"
    per = await fetchall("SELECT ref, n FROM daily WHERE day=? AND metric='join' ORDER BY n DESC", (days[0],))
    if per:
        txt += "\n<b>جذب امروز به تفکیک لینک:</b>\n"
        for r in per:
            ch = await fetchone("SELECT title FROM channels WHERE id=?", (int(r["ref"]),))
            txt += f"• {html.escape(ch['title']) if ch else r['ref']}: <b>{r['n']}</b>\n"
    await c.message.edit_text(txt, reply_markup=back_kb("adm:stats"))


# ── تنظیمات ──
@admin.callback_query(F.data == "adm:set")
async def settings(c: CallbackQuery, state: FSMContext):
    await state.clear()
    prot = await get_set("protect") == "1"
    sec = await get_set("autodel")
    b = InlineKeyboardBuilder()
    b.button(text=f"🛡 ضد فوروارد/ذخیره: {'روشن ✅' if prot else 'خاموش ❌'}", callback_data="adm:tp")
    b.button(text=f"⏳ حذف خودکار فایل: {sec if sec != '0' else 'خاموش'}", callback_data="adm:sw:autodel")
    b.button(text="💬 متن خوش‌آمد", callback_data="adm:sw:welcome")
    b.button(text="📢 متن پیام جوین", callback_data="adm:sw:join_text")
    b.button(text="🏷 کپشن اضافه زیر فایل‌ها", callback_data="adm:sw:caption")
    b.button(text="🔙 بازگشت", callback_data="adm:home")
    b.adjust(1)
    await c.message.edit_text("⚙️ تنظیمات", reply_markup=b.as_markup())


@admin.callback_query(F.data == "adm:tp")
async def toggle_protect(c: CallbackQuery, state: FSMContext):
    await put_set("protect", "0" if await get_set("protect") == "1" else "1")
    await settings(c, state)


@admin.callback_query(F.data.startswith("adm:sw:"))
async def set_text_start(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":")[2]
    await state.set_state(S.text)
    await state.update_data(key=key)
    hint = "عدد ثانیه (0 = خاموش)" if key == "autodel" else "متن جدید (برای خالی کردن کپشن: -)"
    await c.message.edit_text(f"✏️ {hint} رو بفرست:", reply_markup=back_kb("adm:set"))


@admin.message(S.text)
async def set_text_recv(m: Message, state: FSMContext):
    key = (await state.get_data())["key"]
    val = m.html_text if m.text else ""
    if key == "autodel":
        if not val.strip().isdigit():
            return await m.reply("فقط عدد.")
    elif val.strip() == "-":
        val = ""
    await put_set(key, val)
    await state.clear()
    await m.reply("✅ ذخیره شد.", reply_markup=back_kb("adm:set"))


# ── ارسال همگانی ──
@admin.callback_query(F.data == "adm:bc")
async def bc_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.broadcast)
    await c.message.edit_text("📣 پیامت رو بفرست (متن/عکس/ویدیو...) تا برای همه ارسال بشه.", reply_markup=back_kb())


@admin.message(S.broadcast)
async def bc_send(m: Message, state: FSMContext):
    await state.clear()
    users = await fetchall("SELECT id FROM users")
    status = await m.answer(f"⏳ در حال ارسال به {len(users)} نفر...")
    ok = fail = 0
    for u in users:
        try:
            await m.copy_to(u["id"])
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await status.edit_text(f"✅ ارسال شد: {ok}\n❌ ناموفق: {fail}", reply_markup=back_kb())


# ── مدیریت ادمین‌ها (فقط مالک) ──
@admin.callback_query(F.data == "adm:admins")
async def admins_list(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != OWNER_ID:
        return await c.answer("فقط مالک", show_alert=True)
    await state.clear()
    b = InlineKeyboardBuilder()
    for r in await fetchall("SELECT id FROM admins"):
        b.button(text=f"🗑 {r['id']}", callback_data=f"adm:rma:{r['id']}")
    b.button(text="➕ افزودن ادمین", callback_data="adm:adda")
    b.button(text="🔙 بازگشت", callback_data="adm:home")
    b.adjust(1)
    await c.message.edit_text("👤 ادمین‌ها (برای حذف روی آیدی بزن):", reply_markup=b.as_markup())


@admin.callback_query(F.data == "adm:adda")
async def adda(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != OWNER_ID:
        return await c.answer("فقط مالک", show_alert=True)
    await state.set_state(S.add_admin)
    await c.message.edit_text("آیدی عددی ادمین جدید رو بفرست (یا یه پیامش رو فوروارد کن):", reply_markup=back_kb("adm:admins"))


@admin.message(S.add_admin)
async def adda_recv(m: Message, state: FSMContext):
    uid = None
    if m.forward_origin is not None and getattr(m.forward_origin, "type", None) == "user":
        uid = m.forward_origin.sender_user.id
    elif (m.text or "").strip().isdigit():
        uid = int(m.text)
    if not uid:
        return await m.reply("❌ آیدی عددی بفرست.")
    await execute("INSERT OR IGNORE INTO admins VALUES(?)", (uid,))
    await state.clear()
    await m.reply(f"✅ {uid} ادمین شد.", reply_markup=back_kb("adm:admins"))


@admin.callback_query(F.data.startswith("adm:rma:"))
async def rma(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != OWNER_ID:
        return await c.answer("فقط مالک", show_alert=True)
    await execute("DELETE FROM admins WHERE id=?", (int(c.data.split(":")[2]),))
    await admins_list(c, state)


# ───────────────────────── اجرا ─────────────────────────
async def main():
    global ME
    logging.basicConfig(level=logging.INFO)
    await init_db()
    ME = await bot.get_me()
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin)  # اول ادمین، بعد کاربر
    dp.include_router(user)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
