"""
GitHub Actions uchun: botni BIR MARTA ishga tushiradi.
- O'yin jadvali/e'lonlar: football_bot.py dagi match_loop (o'zgarmagan).
- Yangiliklar, transferlar, mashg'ulot videolari: shu faylning o'zi (news_loop ishlatilmaydi).
football_bot.py ga tegilmaydi. Tokenlar GitHub Secrets'dan olinadi.
Faqat standart kutubxona + python-telegram-bot kerak.
"""
import asyncio
import html
import json
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from types import SimpleNamespace

from telegram import Bot

import football_bot as fb


# ---------------------------------------------------------------- sozlamalar
MAX_PER_SOURCE = 3        # bir saytdan bir tekshirishda eng ko'pi bilan
MAX_NEWS_PER_RUN = 6      # bir ishga tushishda jami yangilik
MAX_VIDEO_PER_RUN = 3     # bir ishga tushishda jami video
NEWS_MAX_AGE_H = 6        # shundan eski yangilik yuborilmaydi
VIDEO_MAX_AGE_H = 72

# Yangilik manbalari. Ishlamagan havola log'ga yoziladi va o'tkazib yuboriladi.
NEWS_FEEDS = [
    ("BBC", "https://feeds.bbci.co.uk/sport/football/rss.xml"),
    ("Guardian", "https://www.theguardian.com/football/rss"),
    ("Sky Sports", "https://www.skysports.com/rss/12040"),
    ("ESPN", "https://www.espn.com/espn/rss/soccer/news"),
    # Transfer uchun qo'shimcha:
    ("Sky Transfer", "https://www.skysports.com/rss/11095"),
    ("TEAMtalk", "https://www.teamtalk.com/feed"),
    ("Football365", "https://www.football365.com/feed"),
]

# Klublarning rasmiy YouTube kanallari (channel_id). Xato ID bo'lsa log'da ko'rinadi.
YOUTUBE = [
    ("Real Madrid", "UCWV3obpZVGgJ3j9FVhEjF2Q"),
    ("Barcelona", "UC14UlmYlSNiQCBe9Eookf_A"),
    ("Man City", "UCkzCjdRMrW2vXLx8mvPVLdQ"),
    ("Liverpool", "UC9LQwHZoucFT94I2h6JOcjw"),
    ("Man United", "UC6yW44UGJJBvYTlfC7CRg2Q"),
    ("Arsenal", "UCpryVRk_VDudG8SHXgWcG0w"),
    ("Chelsea", "UCU2PacFf99vhb3hNiYDmxww"),
]

# ---------------------------------------------------------------- ro'yxatlar
# Premier Liga katta klublari: har qanday yangilik o'tadi
ENGLISH_CLUBS = [
    "manchester city", "man city", "manchester united", "man united", "man utd",
    "liverpool", "arsenal", "chelsea", "tottenham", "spurs",
    # Ronaldo klubi: har qanday yangilik o'tadi
    "al nassr", "al-nassr", "alnassr", "cristiano ronaldo",
]
# Boshqa top ligalar: faqat transfer/shartnoma/mashg'ulot bo'lsa
OTHER_CLUBS = [
    "real madrid", "barcelona", "barca", "atletico madrid", "atletico",
    "bayern", "borussia dortmund", "dortmund", "leverkusen",
    "juventus", "inter milan", "internazionale", "ac milan", "napoli",
    "paris saint-germain", "psg", "marseille",
    # Saudiya ligasi: faqat transfer/shartnoma/mashg'ulot
    "saudi pro league", "al hilal", "al-hilal", "al ittihad", "al-ittihad", "al ahli", "al-ahli",
]
STARS = [
    "mbappe", "haaland", "salah", "bellingham", "vinicius", "yamal", "lewandowski",
    "kane", "rodri", "saka", "palmer", "foden", "de bruyne", "messi", "ronaldo",
    "raphinha", "pedri", "gavi", "rodrygo", "musiala", "wirtz", "osimhen",
    "lautaro", "dembele", "van dijk", "alisson", "bruno fernandes", "rashford",
    "isak", "gyokeres", "olise", "camavinga", "valverde", "modric",
]
# Har doim rad etiladi
BAD_WORDS = [
    "women", "women's", "womens", "wsl", "feminin", "femenin", "lionesses", "ladies",
    "u23", "u21", "u20", "u19", "u18", "u17", "under-21", "under-19", "under-18",
    "youth", "academy", "nba", "nfl", "nhl", "mlb", "tennis", "cricket", "rugby",
    "formula 1", "f1", "golf", "boxing", "ufc", "wwe", "olympic", "basketball",
    "inter miami", "mls", "darts", "snooker", "cycling", "athletics",
    "girls", "femeni", "feminine", "frauen", "w-league", "nwsl", "futsal", "esports",
    "volleyball", "handball", "baseball", "hockey", "motogp", "nascar", "mma", "wrestling",
    "horse racing", "swimming", "gymnastics", "netball", "badminton", "table tennis",
    "euroleague", "hoops",
]
TRANSFER_WORDS = [
    "transfer", "sign", "signing", "signs", "deal", "contract", "extend", "extension",
    "renew", "bid", "offer", "loan", "agree", "agreed", "medical", "here we go",
    "target", "linked", "rumour", "rumor", "interested", "release clause", "fee",
    "swap", "join", "joins", "unveil", "sold", "sell", "buy", "bought", "talks",
    "negotiat", "exit", "leave", "depart", "future", "official", "announce",
    "talk", "approach", "pursue", "sale",
]
TRAIN_WORDS = [
    "training", "train ", "session", "warm-up", "warm up", "behind the scenes",
    "inside", "preparation", "pre-match", "back in", "drills", "gym",
]


def _rx(words):
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(w.strip()) for w in words) + r")", re.I)


RX_ENGLISH = _rx(ENGLISH_CLUBS)
RX_OTHER = _rx(OTHER_CLUBS)
RX_STARS = _rx(STARS)
RX_BAD = _rx(BAD_WORDS)
RX_TRANSFER = _rx(TRANSFER_WORDS)
RX_TRAIN = _rx(TRAIN_WORDS)


# ---------------------------------------------------------------- yordamchilar
class StopLoop(Exception):
    """Sikl oxiridagi uzun kutishga yetganda siklni to'xtatadi."""


_real_sleep = asyncio.sleep


async def _sleep(seconds, *args, **kwargs):
    if seconds >= 60:
        raise StopLoop
    await _real_sleep(seconds, *args, **kwargs)


asyncio.sleep = _sleep


def _local(tag):
    return tag.split("}")[-1].lower()


def _parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def parse_feed(data):
    root = ET.fromstring(data)
    out = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        it = {"title": "", "link": "", "id": "", "date": None, "desc": ""}
        for ch in el:
            n = _local(ch.tag)
            txt = (ch.text or "").strip()
            if n == "title":
                it["title"] = txt
            elif n == "link":
                href = ch.attrib.get("href")
                if href and not it["link"]:
                    it["link"] = href
                elif txt:
                    it["link"] = txt
            elif n in ("guid", "id"):
                it["id"] = txt
            elif n in ("pubdate", "published", "updated") and not it["date"]:
                it["date"] = _parse_date(txt)
            elif n in ("description", "summary") and not it["desc"]:
                it["desc"] = re.sub(r"<[^>]+>", " ", html.unescape(txt))[:400]
        if it["title"] and it["link"]:
            it["id"] = it["id"] or it["link"]
            out.append(it)
    return out


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; FootballBot/1.0)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


async def fetch_feed(name, url):
    try:
        data = await asyncio.to_thread(_fetch, url)
        return parse_feed(data)
    except Exception as e:
        print(f"[{name}] o'qib bo'lmadi: {e!r}")
        return []


def classify_news(it):
    """Yuboriladigan bo'lsa yorliq (label) qaytaradi, bo'lmasa None."""
    title = it["title"]
    text = f"{title} {it['desc']}"
    if RX_BAD.search(text):
        return None
    transfer = bool(RX_TRANSFER.search(text))
    train = bool(RX_TRAIN.search(text))
    if RX_ENGLISH.search(title):
        pass  # Premier Liga top: hamma yangilik o'tadi
    elif RX_OTHER.search(text) or RX_STARS.search(text):
        if not (transfer or train):
            return None
    else:
        return None
    if transfer:
        return "🔄 Transfer"
    if train:
        return "⚽ Mashg'ulot"
    return "📰 Yangilik"


def fmt(label, title, link, source):
    return f"{label}\n<b>{html.escape(title)}</b>\n\n{html.escape(source)} · <a href=\"{html.escape(link)}\">Batafsil</a>"



# ---------------------------------------------------------------- tarjima (o'zbekcha)
_tr_cache = {}
_mm_dead = False  # MyMemory limiti tugasa, shu ishga tushishda boshqa urinmaymiz


def _gemini(text):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    model = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-2.0-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    prompt = (
        "Translate this football news headline from English to Uzbek (Latin script). "
        "Keep player, club and league names in their usual Latin spelling. Use natural football "
        "terms (transfer, shartnoma, mashg'ulot). Reply with ONLY the translation, nothing else.\n\n" + text
    )
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        d = json.loads(r.read())
    out = d["candidates"][0]["content"]["parts"][0]["text"].strip().strip('"')
    return out or None


def _mymemory(text):
    global _mm_dead
    if _mm_dead:
        return None
    q = {"q": text, "langpair": "en|uz"}
    email = os.environ.get("MYMEMORY_EMAIL", "").strip()
    if email:
        q["de"] = email
    url = "https://api.mymemory.translated.net/get?" + urllib.parse.urlencode(q)
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20) as r:
        d = json.loads(r.read())
    out = (d.get("responseData") or {}).get("translatedText", "") or ""
    status = str(d.get("responseStatus"))
    if status != "200" or "MYMEMORY WARNING" in out.upper() or "QUERY LENGTH LIMIT" in out.upper():
        _mm_dead = True
        print("MyMemory limiti tugadi yoki xato:", status)
        return None
    return html.unescape(out).strip() or None


def _translate_sync(text):
    for fn in (_gemini, _mymemory):
        try:
            out = fn(text)
            if out:
                return out
        except Exception as e:
            print(f"Tarjima xatosi ({fn.__name__}):", repr(e)[:150])
    return None


async def to_uz(text):
    """O'zbekchaga tarjima; bo'lmasa asl matn (inglizcha) qaytadi."""
    if text in _tr_cache:
        return _tr_cache[text]
    out = await asyncio.to_thread(_translate_sync, text) or text
    _tr_cache[text] = out
    return out

# ---------------------------------------------------------------- yangiliklar
async def news_once(bot, cfg, state):
    seen = state.setdefault("seen2", [])
    seen_set = set(seen)
    now = datetime.now(timezone.utc)
    channel = cfg["channel"]
    sent_news = 0
    sent_video = 0

    # --- yangiliklar
    results = await asyncio.gather(*[fetch_feed(n, u) for n, u in NEWS_FEEDS])
    for (name, _), items in zip(NEWS_FEEDS, results):
        per_source = 0
        for it in items:
            if it["id"] in seen_set:
                continue
            if it["date"] and now - it["date"] > timedelta(hours=NEWS_MAX_AGE_H):
                seen.append(it["id"]); seen_set.add(it["id"])
                continue
            label = classify_news(it)
            if label is None:
                seen.append(it["id"]); seen_set.add(it["id"])
                continue
            if per_source >= MAX_PER_SOURCE or sent_news >= MAX_NEWS_PER_RUN:
                continue  # keyingi safar yuboriladi
            try:
                await bot.send_message(chat_id=channel, text=fmt(label, await to_uz(it["title"]), it["link"], name),
                                       parse_mode="HTML")
                per_source += 1
                sent_news += 1
                await asyncio.sleep(1)
            except Exception as e:
                print(f"[{name}] yuborib bo'lmadi: {e!r}")
            seen.append(it["id"]); seen_set.add(it["id"])

    # --- mashg'ulot videolari
    vres = await asyncio.gather(*[
        fetch_feed(n, f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}") for n, cid in YOUTUBE
    ])
    for (club, _), items in zip(YOUTUBE, vres):
        for it in items:
            if it["id"] in seen_set:
                continue
            if it["date"] and now - it["date"] > timedelta(hours=VIDEO_MAX_AGE_H):
                seen.append(it["id"]); seen_set.add(it["id"])
                continue
            if RX_BAD.search(it["title"]) or not RX_TRAIN.search(it["title"]):
                seen.append(it["id"]); seen_set.add(it["id"])
                continue
            if sent_video >= MAX_VIDEO_PER_RUN:
                continue
            try:
                text = f"🎥 Mashg'ulot lavhasi · {html.escape(club)}\n<b>{html.escape(await to_uz(it['title']))}</b>\n\n{html.escape(it['link'])}"
                await bot.send_message(chat_id=channel, text=text, parse_mode="HTML")
                sent_video += 1
                await asyncio.sleep(1)
            except Exception as e:
                print(f"[{club}] video yuborib bo'lmadi: {e!r}")
            seen.append(it["id"]); seen_set.add(it["id"])

    state["seen2"] = seen[-1500:]
    print(f"Yangilik: {sent_news}, video: {sent_video}")


# ---------------------------------------------------------------- asosiy
def build_config():
    cfg = dict(fb.DEFAULTS)
    cfg["bot_token"] = os.environ["BOT_TOKEN"]
    cfg["football_token"] = os.environ["FOOTBALL_TOKEN"]
    cfg["group_link"] = os.environ.get("GROUP_LINK", "")
    cfg["admin_id"] = os.environ.get("ADMIN_ID", "")
    cfg["channel"] = os.environ.get("CHANNEL") or cfg["channel"]
    cfg["setup_done"] = True
    return cfg


async def main():
    cfg = build_config()
    state = fb.load_state()
    async with Bot(cfg["bot_token"]) as bot:
        app = SimpleNamespace(bot=bot)
        results = await asyncio.gather(
            news_once(bot, cfg, state),
            fb.match_loop(app, cfg, state),
            return_exceptions=True,
        )
    for r in results:
        if isinstance(r, Exception) and not isinstance(r, StopLoop):
            print("Xato:", repr(r))
    fb.save_json(fb.STATE_FILE, state)
    print("Tayyor.")


asyncio.run(main())
