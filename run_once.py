"""
GitHub Actions uchun: football_bot.py ni BIR MARTA aylantirib ishga tushiradi.
Yangiliklarni va o'yinlarni bir marta tekshiradi, keyin chiqib ketadi.
Keyingi ishga tushirishni GitHub (cron) har 30 daqiqada qiladi.

football_bot.py ga tegilmaydi. Tokenlar GitHub Secrets'dan olinadi.
"""
import asyncio
import os
from types import SimpleNamespace

from telegram import Bot

import football_bot as fb


class StopLoop(Exception):
    """Sikl oxiridagi uzun kutishga yetganda siklni to'xtatadi."""


_real_sleep = asyncio.sleep


async def _sleep(seconds, *args, **kwargs):
    # 60 soniyadan uzun kutish = siklning keyingi aylanishi. Kerak emas, chiqamiz.
    if seconds >= 60:
        raise StopLoop
    await _real_sleep(seconds, *args, **kwargs)


asyncio.sleep = _sleep


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
            fb.news_loop(app, cfg, state),
            fb.match_loop(app, cfg, state),
            return_exceptions=True,
        )
    for r in results:
        if isinstance(r, Exception) and not isinstance(r, StopLoop):
            print("Xato:", repr(r))
    fb.save_json(fb.STATE_FILE, state)
    print("Tayyor.")


asyncio.run(main())
