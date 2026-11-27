"""
Futbol kanal boti (hammasi bitta faylda):
  1) Bir nechta saytdan futbol yangiliklari -> kanalga (aniq, katta rasm bilan)
  2) O'yin bor kuni ertalab: bugungi o'yinlar jadvali (rasm + vaqtlar). O'yin yo'q bo'lsa, yozmaydi
  3) O'yindan ~30 daqiqa oldin: jamoalar, vaqt, oxirgi 5 o'yin (rasm) + kanal va jonli efir havolasi
  4) O'yin boshlanganda: "boshlandi, jonli efir guruhda" + havolalar
  5) Botga yozilgan xabarlarga javob (admin ID berilsa, xabarlar adminga ham yuboriladi)

Tokenlar kod ichida emas, birinchi ishga tushishda so'raladi va config.json ga saqlanadi.
"""
import asyncio
import html
import io
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from PIL import Image, ImageDraw, ImageFont
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

CONFIG_FILE = "config.json"
STATE_FILE = "state.json"
OLD_SEEN_FILE = "seen.json"  # eski news.py ro'yxati (bo'lsa, ishlatiladi)
TZ = timezone(timedelta(hours=5))  # Toshkent vaqti
API = "https://api.football-data.org/v4"
CONFIG_VERSION = 2
MIN_IMG_WIDTH = 600  # bundan kichik rasmlar olinmaydi (xira chiqadi)
BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

DEFAULTS = {
    "version": CONFIG_VERSION,
    "bot_token": "",
    "football_token": "",
    "channel": "@abdurakhimo_official",
    "group_link": "",
    "admin_id": "",
    "setup_done": False,
    # football-data.org bepul rejadagi ligalar: PL, PD, SA, BL1, FL1, CL ...
    "competitions": "PL,PD,SA,BL1,FL1,CL",
    "news_every_min": 30,
    "match_check_min": 10,
    "pre_match_min": 30,  # o'yindan necha daqiqa oldin e'lon
    "morning_hour": 8,  # ertalabki jadval posti (Toshkent soati)
    "post_started": True,  # "O'yin boshlandi" postini yuborish
    "translate": False,  # True qilsangiz, o'zbekchaga tarjima qiladi
    "feeds": [
        "https://feeds.bbci.co.uk/sport/football/rss.xml",
        "https://www.theguardian.com/football/rss",
        "https://www.skysports.com/rss/12040",
        "https://www.espn.com/espn/rss/soccer/news",
    ],
}

UZ_MONTHS = [
    "yanvar", "fevral", "mart", "aprel", "may", "iyun",
    "iyul", "avgust", "sentabr", "oktabr", "noyabr", "dekabr",
]
UZ_DAYS = ["dushanba", "seshanba", "chorshanba", "payshanba", "juma", "shanba", "yakshanba"]


# ---------------------------------------------------------------- sozlamalar
def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_config():
    cfg = dict(DEFAULTS)
    file_version = CONFIG_VERSION
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        file_version = saved.get("version", 1)
        cfg.update(saved)

    changed = False

    # eski config.json ni yangilash (yangi vaqtlar)
    if file_version < CONFIG_VERSION:
        cfg["pre_match_min"] = DEFAULTS["pre_match_min"]
        cfg["match_check_min"] = DEFAULTS["match_check_min"]
        cfg["version"] = CONFIG_VERSION
        changed = True

    required = [
        ("bot_token", "Bot tokeni (BotFather bergan): "),
        ("football_token", "football-data.org API tokeni: "),
        ("group_link", "Jonli efir guruhi havolasi (masalan https://t.me/...): "),
    ]
    for key, question in required:
        if not str(cfg.get(key, "")).strip():
            cfg[key] = input(question).strip()
            changed = True

    if not cfg.get("setup_done"):
        cfg["admin_id"] = input(
            "Sizning Telegram ID raqamingiz (ixtiyoriy, bilmasangiz Enter bosing): "
        ).strip()
        cfg["setup_done"] = True
        changed = True

    if changed:
        save_json(CONFIG_FILE, cfg)
        print("Sozlamalar config.json ga saqlandi.")
    return cfg


def load_state():
    state = {"news": [], "announced": [], "started": [], "morning": ""}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state.update(json.load(f))
    elif os.path.exists(OLD_SEEN_FILE):
        try:
            with open(OLD_SEEN_FILE, "r", encoding="utf-8") as f:
                state["news"] = list(json.load(f))
        except Exception:
            pass
    return state


# ------------------------------------------------------------------ yordamchi
def clean(text):
    """HTML teglarni va &quot; &#39; kabi kodlarni oddiy belgilarga aylantiradi."""
    text = re.sub(r"<[^>]+>", "", text or "")
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def pick_emoji(title):
    t = title.lower()
    if any(w in t for w in ["goal", "score", "hat-trick"]):
        return "⚽🔥"
    if any(w in t for w in ["win", "beat", "victory", "champion", "title", "cup"]):
        return "🏆🔥"
    if any(w in t for w in ["transfer", "sign", "deal", "join", "loan", "bid"]):
        return "🔁✍️"
    if any(w in t for w in ["injur", "ruled out", "out for"]):
        return "🤕❌"
    if any(w in t for w in ["sack", "fired", "manager", "coach"]):
        return "🧢"
    return "⚽"


def translate_sync(text, enabled):
    if not enabled or not text:
        return text
    try:
        from deep_translator import GoogleTranslator

        return clean(GoogleTranslator(source="en", target="uz").translate(text))
    except Exception as e:
        print("Tarjima xatosi:", e)
        return text


def footer_channel(cfg):
    return f"📢 {cfg['channel']}"


def footer_full(cfg):
    return f"📢 {cfg['channel']}\n📺 Jonli efir: {cfg['group_link']}"


async def send(bot, cfg, caption, photo=None):
    if photo is not None:
        try:
            await bot.send_photo(
                chat_id=cfg["channel"], photo=photo, caption=caption[:1024]
            )
            return
        except Exception as e:
            print("Rasm yuborilmadi, matn yuboriladi:", e)
    await bot.send_message(chat_id=cfg["channel"], text=caption[:4096])


# ------------------------------------------------------------------ rasm (aniq)
def fetch_og_image_url(link):
    """Maqola sahifasidagi katta (og:image) rasm manzili."""
    try:
        r = requests.get(link, headers=BROWSER, timeout=12)
        pats = [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        ]
        for p in pats:
            m = re.search(p, r.text, re.I)
            if m:
                return html.unescape(m.group(1))
    except Exception as e:
        print("og:image olinmadi:", e)
    return None


def rss_image_urls(entry):
    """RSS ichidagi rasmlar, kattasidan kichigiga."""
    found = []
    for key in ("media_content", "media_thumbnail"):
        for it in entry.get(key, []) or []:
            url = it.get("url")
            if not url:
                continue
            try:
                width = int(it.get("width") or 0)
            except ValueError:
                width = 0
            if "ichef.bbci.co.uk" in url:
                url = url.replace("/240/", "/976/")
                width = max(width, 976)
            found.append((width, url))
    found.sort(key=lambda x: x[0], reverse=True)
    return [u for _, u in found]


def download_image(url):
    r = requests.get(url, headers=BROWSER, timeout=20)
    r.raise_for_status()
    data = r.content
    img = Image.open(io.BytesIO(data))
    w, h = img.size
    if w < MIN_IMG_WIDTH:
        return None  # kichik rasm, xira chiqadi
    if img.format not in ("JPEG", "PNG") or len(data) > 9_000_000:
        img = img.convert("RGB")
        if w > 1600:
            img = img.resize((1600, int(h * 1600 / w)))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=92)
        data = out.getvalue()
    buf = io.BytesIO(data)
    buf.name = "photo.jpg"
    return buf


def best_image(entry):
    candidates = []
    if entry.get("link"):
        og = fetch_og_image_url(entry["link"])
        if og:
            candidates.append(og)
    candidates += rss_image_urls(entry)
    for url in candidates:
        try:
            buf = download_image(url)
            if buf:
                return buf
        except Exception as e:
            print("Rasm yuklanmadi:", e)
    return None


# ------------------------------------------------------------------ yangiliklar
async def post_news(bot, cfg, entry):
    title = clean(entry.get("title"))
    summary = clean(entry.get("summary"))
    emoji = pick_emoji(title)
    title_t = await asyncio.to_thread(translate_sync, title, cfg["translate"])
    summary_t = await asyncio.to_thread(translate_sync, summary, cfg["translate"])

    footer = "\n\n" + footer_channel(cfg)
    body = f"{emoji} {title_t}\n\n{summary_t}".strip()
    caption = body[: 1000 - len(footer)] + footer
    photo = await asyncio.to_thread(best_image, entry)
    await send(bot, cfg, caption, photo)


async def news_loop(app, cfg, state):
    bot = app.bot
    while True:
        try:
            for url in cfg["feeds"]:
                feed = await asyncio.to_thread(feedparser.parse, url)
                new = [
                    e
                    for e in feed.entries
                    if e.get("link") and e.link not in state["news"]
                ]
                to_post = new[:1]  # har saytdan bir tekshirishda 1 ta
                for e in new:
                    state["news"].append(e.link)
                for e in reversed(to_post):
                    try:
                        await post_news(bot, cfg, e)
                        await asyncio.sleep(3)
                    except Exception as err:
                        print("Yangilik xatosi:", err)
            state["news"] = state["news"][-1500:]
            save_json(STATE_FILE, state)
            print(datetime.now(TZ).strftime("%H:%M"), "Yangiliklar tekshirildi")
        except Exception as err:
            print("Yangiliklar siklida xato:", err)
        await asyncio.sleep(cfg["news_every_min"] * 60)


# ------------------------------------------------------------------ o'yinlar
def fd_get(cfg, path, params=None):
    headers = {"X-Auth-Token": cfg["football_token"]}
    r = None
    for attempt in range(2):
        r = requests.get(API + path, headers=headers, params=params, timeout=30)
        if r.status_code == 429 and attempt == 0:
            time.sleep(65)  # limit: daqiqasiga 10 ta so'rov
            continue
        break
    if r.status_code != 200:
        raise RuntimeError(f"football-data.org {r.status_code}: {r.text[:200]}")
    return r.json()


def parse_utc(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def team_name(t):
    return t.get("shortName") or t.get("name") or "?"


def team_form(cfg, team_id):
    """Jamoaning oxirgi 5 ta tugagan o'yini (eskisidan yangisiga)."""
    data = fd_get(cfg, f"/teams/{team_id}/matches", {"status": "FINISHED"})
    matches = sorted(data.get("matches", []), key=lambda m: m["utcDate"])
    out = []
    for m in matches:
        s = m["score"]["fullTime"]
        if s["home"] is None or s["away"] is None:
            continue
        is_home = m["homeTeam"]["id"] == team_id
        gf, ga = (s["home"], s["away"]) if is_home else (s["away"], s["home"])
        res = "G" if gf > ga else ("D" if gf == ga else "M")
        opp = m["awayTeam"] if is_home else m["homeTeam"]
        out.append({"res": res, "score": f"{gf}-{ga}", "opp": team_name(opp)})
    return out[-5:]


def font(size, bold=False):
    names = ["arialbd.ttf", "arial.ttf"] if bold else ["arial.ttf"]
    paths = [f"C:/Windows/Fonts/{n}" for n in names] + names + ["DejaVuSans.ttf"]
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


COLORS = {"G": (46, 160, 67), "D": (140, 140, 140), "M": (205, 60, 60)}


def to_buf(img, name):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    buf.name = name
    return buf


def make_form_image(home, away, hform, aform, comp, kickoff):
    W, H = 1000, 600
    img = Image.new("RGB", (W, H), (18, 28, 45))
    d = ImageDraw.Draw(img)
    f_title, f_team = font(40, True), font(34, True)
    f_row, f_small = font(26), font(26)

    def center(text, cx, y, f, fill=(255, 255, 255)):
        w = d.textlength(text, font=f)
        d.text((cx - w / 2, y), text, font=f, fill=fill)

    center(f"{home[:14]}  vs  {away[:14]}", W / 2, 25, f_title)
    center(f"{comp} - {kickoff} (Toshkent)", W / 2, 80, f_small, (170, 180, 200))

    for cx, name, form in ((250, home, hform), (750, away, aform)):
        center(name[:20], cx, 140, f_team, (255, 214, 10))
        center("Oxirgi 5 o'yin", cx, 185, f_small, (170, 180, 200))
        for i, g in enumerate(form):
            y = 240 + i * 66
            d.ellipse((cx - 200, y, cx - 154, y + 46), fill=COLORS[g["res"]])
            center(g["res"], cx - 177, y + 6, f_row)
            d.text(
                (cx - 135, y + 8),
                f"{g['score']}  {g['opp'][:15]}",
                font=f_row,
                fill=(255, 255, 255),
            )
    return to_buf(img, "form.png")


def make_schedule_image(rows, date_text):
    """rows: [(vaqt, uy, mehmon, liga), ...]"""
    W = 1000
    row_h = 64
    H = 190 + len(rows) * row_h + 20
    img = Image.new("RGB", (W, H), (18, 28, 45))
    d = ImageDraw.Draw(img)
    f_title, f_sub = font(46, True), font(28)
    f_time, f_team, f_comp = font(34, True), font(32), font(24)

    def center(text, cx, y, f, fill=(255, 255, 255)):
        w = d.textlength(text, font=f)
        d.text((cx - w / 2, y), text, font=f, fill=fill)

    center("Bugungi o'yinlar", W / 2, 30, f_title, (255, 214, 10))
    center(date_text, W / 2, 95, f_sub, (170, 180, 200))
    d.line((60, 150, W - 60, 150), fill=(60, 75, 100), width=2)
    for i, (t, h, a, c) in enumerate(rows):
        y = 175 + i * row_h
        d.text((60, y), t, font=f_time, fill=(255, 214, 10))
        d.text((190, y + 2), f"{h[:17]} - {a[:17]}", font=f_team, fill=(255, 255, 255))
        w = d.textlength(c, font=f_comp)
        d.text((W - 60 - w, y + 8), c, font=f_comp, fill=(150, 165, 190))
    return to_buf(img, "schedule.png")


def uz_date(dt):
    return f"{dt.day}-{UZ_MONTHS[dt.month - 1]}, {UZ_DAYS[dt.weekday()]}"


async def post_morning(bot, cfg, matches, local_now):
    rows = []
    lines = []
    for m in matches:
        ko = parse_utc(m["utcDate"]).astimezone(TZ).strftime("%H:%M")
        h, a = team_name(m["homeTeam"]), team_name(m["awayTeam"])
        comp = m.get("competition", {})
        code = comp.get("code") or comp.get("name", "")[:10]
        rows.append((ko, h, a, code))
        lines.append(f"🕒 {ko} — {h} vs {a} ({comp.get('name', '')})")

    date_text = uz_date(local_now)
    photo = None
    try:
        photo = make_schedule_image(rows[:12], date_text)
    except Exception as e:
        print("Jadval rasmi yaratilmadi:", e)

    text = (
        f"📅 Bugungi o'yinlar — {date_text}\n\n"
        + "\n".join(lines)
        + "\n\n"
        + footer_full(cfg)
    )
    await send(bot, cfg, text, photo)


async def announce_match(bot, cfg, m, mins):
    home, away = m["homeTeam"], m["awayTeam"]
    hname, aname = team_name(home), team_name(away)
    comp = m.get("competition", {}).get("name", "")
    ko = parse_utc(m["utcDate"]).astimezone(TZ).strftime("%H:%M")

    hform = await asyncio.to_thread(team_form, cfg, home["id"])
    await asyncio.sleep(7)  # bepul limit: daqiqasiga 10 ta so'rov
    aform = await asyncio.to_thread(team_form, cfg, away["id"])

    photo = None
    if hform or aform:
        try:
            photo = make_form_image(hname, aname, hform, aform, comp, ko)
        except Exception as e:
            print("Rasm yaratilmadi:", e)

    lines = [
        f"⏰ {max(1, round(mins))} daqiqadan keyin o'yin boshlanadi!",
        "",
        f"🏟 {hname} — {aname}",
        f"🏆 {comp}",
        f"🕒 Boshlanishi: {ko} (Toshkent vaqti)",
    ]
    if hform or aform:
        lines += [
            "",
            "📊 Oxirgi 5 o'yin (G - g'alaba, D - durang, M - mag'lubiyat):",
            f"🏠 {hname}: {' '.join(g['res'] for g in hform) or '-'}",
            f"✈️ {aname}: {' '.join(g['res'] for g in aform) or '-'}",
        ]
    lines += ["", footer_full(cfg)]
    await send(bot, cfg, "\n".join(lines), photo)


async def post_started(bot, cfg, m):
    hname, aname = team_name(m["homeTeam"]), team_name(m["awayTeam"])
    text = (
        f"🔴 O'YIN BOSHLANDI!\n\n"
        f"⚽ {hname} — {aname}\n"
        f"📺 Jonli efir shu guruhda!\n\n" + footer_full(cfg)
    )
    await bot.send_message(chat_id=cfg["channel"], text=text)


async def match_loop(app, cfg, state):
    bot = app.bot
    while True:
        try:
            now = datetime.now(timezone.utc)
            local_now = datetime.now(TZ)
            today = local_now.date()
            params = {
                "competitions": cfg["competitions"],
                "dateFrom": (today - timedelta(days=1)).isoformat(),
                "dateTo": (today + timedelta(days=1)).isoformat(),
            }
            data = await asyncio.to_thread(fd_get, cfg, "/matches", params)
            matches = [
                m
                for m in data.get("matches", [])
                if m.get("status") not in ("POSTPONED", "CANCELLED", "SUSPENDED")
            ]

            # 1) ertalabki jadval (o'yin bo'lmasa, yozmaydi)
            if (
                local_now.hour >= cfg["morning_hour"]
                and state.get("morning") != today.isoformat()
            ):
                todays = sorted(
                    [
                        m
                        for m in matches
                        if parse_utc(m["utcDate"]).astimezone(TZ).date() == today
                        and parse_utc(m["utcDate"]) > now
                    ],
                    key=lambda m: m["utcDate"],
                )
                if todays:
                    try:
                        await post_morning(bot, cfg, todays, local_now)
                    except Exception as err:
                        print("Ertalabki jadval xatosi:", err)
                state["morning"] = today.isoformat()
                save_json(STATE_FILE, state)

            # 2) har bir o'yin uchun e'lon va "boshlandi"
            for m in matches:
                mid = str(m["id"])
                mins = (parse_utc(m["utcDate"]) - now).total_seconds() / 60

                if 0 < mins <= cfg["pre_match_min"] and mid not in state["announced"]:
                    try:
                        await announce_match(bot, cfg, m, mins)
                    except Exception as err:
                        print("E'lon xatosi:", err)
                    state["announced"].append(mid)
                    save_json(STATE_FILE, state)

                if -30 < mins <= 0 and mid not in state["started"]:
                    if cfg.get("post_started", True):
                        try:
                            await post_started(bot, cfg, m)
                        except Exception as err:
                            print("Boshlandi posti xatosi:", err)
                    state["started"].append(mid)
                    state["announced"].append(mid)
                    save_json(STATE_FILE, state)

            state["announced"] = state["announced"][-500:]
            state["started"] = state["started"][-500:]
            print(datetime.now(TZ).strftime("%H:%M"), "O'yinlar tekshirildi")
        except Exception as err:
            print("O'yinlar siklida xato:", err)
        await asyncio.sleep(cfg["match_check_min"] * 60)


# ------------------------------------------------------------------ bot buyruqlari
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Salom! Men futbol botman ⚽")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Sizning Telegram ID: {update.effective_user.id}")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = context.application.bot_data["cfg"]
    msg = update.message
    if not msg or not msg.text:
        return
    admin_id = str(cfg.get("admin_id", "")).strip()
    if admin_id and str(msg.from_user.id) == admin_id:
        await msg.reply_text("Salom, admin! Bot ishlayapti ✅")
        return
    await msg.reply_text("Xabaringiz qabul qilindi. Admin tez orada javob beradi 🙌")
    if admin_id:
        try:
            await context.bot.forward_message(
                chat_id=int(admin_id),
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
            )
        except Exception as e:
            print("Adminga yuborilmadi:", e)


# ------------------------------------------------------------------ ishga tushirish
def main():
    cfg = load_config()
    state = load_state()

    async def post_init(app):
        app.bot_data["cfg"] = cfg
        app.bot_data["tasks"] = [
            asyncio.create_task(news_loop(app, cfg, state)),
            asyncio.create_task(match_loop(app, cfg, state)),
        ]

    app = ApplicationBuilder().token(cfg["bot_token"]).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, on_message)
    )
    print("Bot ishga tushdi. To'xtatish: Ctrl+C")
    app.run_polling()


if __name__ == "__main__":
    main()
