import os
import json
import asyncio
import random
import re
import time
import hashlib
import html
import urllib.parse
import tempfile
import shutil
import logging
from dataclasses import dataclass
from typing import Literal

import aiohttp
import feedparser
from youtube_transcript_api import YouTubeTranscriptApi
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile
from google import genai

# ============ ЛОГИРОВАНИЕ ============

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("KiberSOS")

# ============ КОНФИГУРАЦИЯ ============

def get_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        logger.error(f"Missing env variable: {name}")
        exit(1)
    return val

GEMINI_API_KEY = get_env("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = get_env("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = get_env("CHANNEL_ID")

CACHE_DIR = os.getenv("CACHE_DIR", "cache_sec")
os.makedirs(CACHE_DIR, exist_ok=True)
STATE_FILE = os.path.join(CACHE_DIR, "state_gemini_v2.json")

# Лимиты
TEXT_ONLY_THRESHOLD = 850
MAX_POSTED_IDS = 400

# Таймауты
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=25)
IMAGE_TIMEOUT = aiohttp.ClientTimeout(total=40)

# Стоп-слова
STOP_WORDS = [
    "наушник", "jbl", "bluetooth", "гарнитур",
    "квартальный отчет", "назначен директором", "маркетинг", "конференция",
    "мвсфера", "мсвсфера", "astra linux", "астра линукс", "red os", "ред ос",
    "роса хром", "импортозамещ", "реестр по", "гостех",
    "обновил логотип", "презентовал новую версию",
    "postgresql", "highload", "go,", "golang"
]

# ============ ИСТОЧНИКИ ============

RSS_SOURCES = [
    {"name": "Kaspersky Daily", "url": "https://www.kaspersky.ru/blog/feed/"},
    {"name": "Kod.ru", "url": "https://kod.ru/rss/"},
    {"name": "BleepingComputer", "url": "https://www.bleepingcomputer.com/feed/"},
    {"name": "Habr Security", "url": "https://habr.com/ru/rss/hub/infosecurity/all/?fl=ru"},
    {"name": "SecurityLab", "url": "https://www.securitylab.ru/rss/news/"},
]

YOUTUBE_CHANNELS = [
    {"name": "Overbafer1", "id": "UC-lHJ97lqoOGgsLFuQ8Y8_g"},
    {"name": "NetworkChuck", "id": "UC9x0AN7BWHpXyPic4IQC74Q"},
    {"name": "The Hated One", "id": "UCjr2bPAyPV7t35mVihRBCzw"},
]

@dataclass
class NewsItem:
    type: Literal["news", "video"]
    title: str
    text: str
    link: str
    source: str
    uid: str

# ============ INIT ============

bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

# Новый клиент Gemini
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# ============ STATE ============

class State:
    def __init__(self):
        self.data = {"posted_ids": {}, "recent_titles": []}
        self._load()
    
    def _load(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    self.data.update(json.load(f))
                    if "recent_titles" not in self.data:
                        self.data["recent_titles"] = []
                logger.info(f"💾 Memory: {len(self.data['recent_titles'])} topics")
            except:
                pass
    
    def save(self):
        fd, tmp_path = tempfile.mkstemp(dir=CACHE_DIR, suffix='.json')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            shutil.move(tmp_path, STATE_FILE)
        except Exception as e:
            logger.error(f"Save error: {e}")
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
    
    def is_posted(self, uid):
        return uid in self.data["posted_ids"]
    
    def mark_posted(self, uid, title):
        if len(self.data["posted_ids"]) > MAX_POSTED_IDS:
            sorted_ids = sorted(self.data["posted_ids"].items(), key=lambda x: x[1])
            self.data["posted_ids"] = dict(sorted_ids[-300:])
        
        self.data["posted_ids"][uid] = int(time.time())
        self.data["recent_titles"].append(title)
        if len(self.data["recent_titles"]) > 40:
            self.data["recent_titles"] = self.data["recent_titles"][-40:]
        self.save()

    def get_recent_titles(self):
        return self.data["recent_titles"]

state = State()

# ============ UTILS ============

def clean_text(text):
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return html.unescape(text).strip()

# ============ GEMINI FUNCTIONS ============

async def call_gemini(prompt: str) -> str:
    """Вызов Gemini API с новой библиотекой"""
    try:
        response = await asyncio.to_thread(
            lambda: gemini_client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt
            )
        )
        return response.text.strip()
    except Exception as e:
        logger.error(f"Gemini API error: {e}")
        return ""

async def check_duplicate_gemini(new_title):
    """Проверка дублей через Gemini"""
    recent = state.get_recent_titles()
    if not recent:
        return False
    
    history = "\n".join(f"- {t}" for t in recent[-20:])
    
    prompt = f"""Список последних тем канала:
{history}

Новая тема: "{new_title}"

Это дубликат или очень похожая тема? Ответь ТОЛЬКО одним словом: YES или NO"""

    answer = await call_gemini(prompt)
    is_dup = "YES" in answer.upper()
    if is_dup:
        logger.info(f"🚫 Duplicate: {new_title}")
    return is_dup

async def generate_post_gemini(item):
    """Генерация поста через Gemini"""
    
    prompt = f"""Ты редактор канала про кибербезопасность для обычных людей.

ПРАВИЛА:
1. Если новость про: госсофт (Астра, МСВСфера), корпоративные отчеты, назначения директоров, конференции, криптовалюту, казино — ответь SKIP
2. Если новость про: взломы телефонов, мошенников, утечки данных, VPN, безопасность Android/iPhone — напиши пост

СТИЛЬ: Как друг рассказывает другу. Без официоза.

СТРУКТУРА:
🔥 [Цепляющий заголовок]

[Суть проблемы простым языком]

👇 ЧТО ДЕЛАТЬ:
• [Совет 1]
• [Совет 2]

---
Заголовок новости: {item.title}
Текст: {item.text[:3000]}
---

Напиши пост или ответь SKIP:"""

    text = await call_gemini(prompt)
    
    if not text or "SKIP" in text.upper() or len(text) < 50:
        logger.info(f"⏩ Skipped: {item.title}")
        return None
    
    return text + f"\n\n🔗 <a href='{item.link}'>Источник</a>"

# ============ IMAGES ============

async def generate_image(title, session):
    try:
        styles = [
            "dark cyberpunk, neon rain, cinematic",
            "matrix style, green code on black",
            "glitch art, tech noir aesthetic",
            "isometric 3d render, soft blue lighting"
        ]
        objects = ["digital anomaly", "hacker silhouette", "broken screen", "warning hologram"]
        
        clean_t = re.sub(r'[^a-zA-Z0-9\s]', '', title)[:40]
        prompt = f"{random.choice(objects)}, {clean_t}, {random.choice(styles)}"
        
        encoded = urllib.parse.quote(prompt)
        seed = random.randint(0, 99999)
        url = f"https://image.pollinations.ai/prompt/{encoded}?width=1280&height=720&nologo=true&seed={seed}"
        
        async with session.get(url, timeout=IMAGE_TIMEOUT) as resp:
            if resp.status == 200:
                data = await resp.read()
                if len(data) > 1000:
                    path = os.path.join(CACHE_DIR, f"img_{int(time.time())}.jpg")
                    with open(path, "wb") as f:
                        f.write(data)
                    return path
    except Exception as e:
        logger.warning(f"Image error: {e}")
    return None

# ============ FETCHERS ============

async def fetch_rss(source, session):
    items = []
    try:
        async with session.get(source['url'], timeout=HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                return []
            text = await resp.text()
        
        feed = feedparser.parse(text)
        for entry in feed.entries[:3]:
            link = entry.get('link')
            if not link:
                continue
            
            uid = hashlib.md5(link.encode()).hexdigest()
            if state.is_posted(uid):
                continue
            
            items.append(NewsItem(
                type="news",
                title=entry.get('title', ''),
                text=clean_text(entry.get("summary", "")),
                link=link,
                source=source['name'],
                uid=uid
            ))
    except Exception as e:
        logger.warning(f"RSS error {source['name']}: {e}")
    return items

async def fetch_youtube(channel, session):
    items = []
    try:
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel['id']}"
        async with session.get(url, timeout=HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                return []
            text = await resp.text()
        
        feed = feedparser.parse(text)
        for entry in feed.entries[:2]:
            vid = entry.get('yt_videoid')
            uid = f"yt_{vid}"
            if state.is_posted(uid):
                continue
            
            try:
                transcript = await asyncio.to_thread(
                    lambda: YouTubeTranscriptApi.list_transcripts(vid)
                    .find_transcript(['ru', 'en']).fetch()
                )
                full_text = " ".join([t['text'] for t in transcript])
                items.append(NewsItem(
                    type="video",
                    title=entry.title,
                    text=full_text[:5000],
                    link=entry.link,
                    source=f"YouTube {channel['name']}",
                    uid=uid
                ))
            except:
                pass
    except Exception as e:
        logger.warning(f"YT error {channel['name']}: {e}")
    return items

# ============ MAIN ============

async def main():
    logger.info("🚀 Starting (Gemini 2.0 FREE mode)...")
    
    async with aiohttp.ClientSession() as session:
        # Сбор данных
        tasks = [fetch_rss(s, session) for s in RSS_SOURCES]
        tasks += [fetch_youtube(c, session) for c in YOUTUBE_CHANNELS]
        
        results = await asyncio.gather(*tasks)
        all_items = [item for sublist in results for item in sublist]
        
        logger.info(f"📦 Found {len(all_items)} items")
        random.shuffle(all_items)
        
        for item in all_items:
            # 1. Стоп-слова
            low_title = item.title.lower()
            if any(bad in low_title for bad in STOP_WORDS):
                logger.info(f"🚫 Banned word: {item.title}")
                state.mark_posted(item.uid, item.title)
                continue
            
            logger.info(f"🔍 Checking: {item.title}")
            
            # 2. Проверка дублей
            if await check_duplicate_gemini(item.title):
                state.mark_posted(item.uid, item.title)
                continue
            
            # 3. Генерация поста
            post_text = await generate_post_gemini(item)
            if not post_text:
                state.mark_posted(item.uid, item.title)
                continue
            
            # 4. Отправка
            try:
                if len(post_text) > TEXT_ONLY_THRESHOLD:
                    logger.info("📜 Text only")
                    await bot.send_message(CHANNEL_ID, text=post_text)
                else:
                    logger.info("📸 With image")
                    img = await generate_image(item.title, session)
                    if img:
                        await bot.send_photo(CHANNEL_ID, photo=FSInputFile(img), caption=post_text)
                        os.remove(img)
                    else:
                        await bot.send_message(CHANNEL_ID, text=post_text)
                
                logger.info("✅ Posted!")
                state.mark_posted(item.uid, item.title)
                break
                
            except Exception as e:
                logger.error(f"Telegram error: {e}")
    
    await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
