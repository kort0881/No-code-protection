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
from datetime import datetime
from dataclasses import dataclass
from typing import Literal

import aiohttp
import feedparser
from youtube_transcript_api import YouTubeTranscriptApi
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile
from openai import OpenAI

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

OPENAI_API_KEY = get_env("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = get_env("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = get_env("CHANNEL_ID")

CACHE_DIR = os.getenv("CACHE_DIR", "cache_sec")
os.makedirs(CACHE_DIR, exist_ok=True)
STATE_FILE = os.path.join(CACHE_DIR, "state_smart_v3.json")

# Лимиты
TELEGRAM_CAPTION_LIMIT = 1024
TELEGRAM_MESSAGE_LIMIT = 4096
TEXT_ONLY_THRESHOLD = 850
MAX_POSTED_IDS = 400
POSTED_IDS_TRIM_TO = 300

# Таймауты
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=25)
IMAGE_TIMEOUT = aiohttp.ClientTimeout(total=40)

# ⛔️ ЧЕРНЫЙ СПИСОК (СТОП-СЛОВА)
# Если эти слова есть в заголовке - новость летит в мусорку СРАЗУ.
# Пиши сюда всё, что тебя достало.
STOP_WORDS = [
    "наушник", "jbl", "bluetooth", "блютуз", 
    "гарнитур", "headphone", "earbud", 
    "квартальный отчет", "назначен директором", "маркетинг"
]

# ============ ИСТОЧНИКИ ============

RSS_SOURCES = [
    {"name": "Kaspersky Daily", "url": "https://www.kaspersky.ru/blog/feed/", "type": "rss"},
    {"name": "Kod.ru", "url": "https://kod.ru/rss/", "type": "rss"},
    {"name": "BleepingComputer", "url": "https://www.bleepingcomputer.com/feed/", "type": "rss"},
    {"name": "3DNews Soft", "url": "https://3dnews.ru/software/rss/", "type": "rss"},
    {"name": "Habr Security", "url": "https://habr.com/ru/rss/hub/infosecurity/all/?fl=ru", "type": "rss"},
    {"name": "SecurityLab", "url": "https://www.securitylab.ru/rss/news/", "type": "rss"},
]

YOUTUBE_CHANNELS = [
    {"name": "Overbafer1", "id": "UC-lHJ97lqoOGgsLFuQ8Y8_g"},
    {"name": "NetworkChuck", "id": "UC9x0AN7BWHpXyPic4IQC74Q"},
    {"name": "The Hated One", "id": "UCjr2bPAyPV7t35mVihRBCzw"},
    {"name": "NN", "id": "UCfJkM0E6qT8j6w6q5x5x_9A"},
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
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# ============ STATE MANAGEMENT ============

class State:
    def __init__(self):
        self.data = {"posted_ids": {}, "recent_titles": []}
        self._load()
    
    def _load(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    self.data.update(json.load(f))
                    if "recent_titles" not in self.data: self.data["recent_titles"] = []
                
                # Логируем память, чтобы убедиться, что она работает
                count = len(self.data["recent_titles"])
                last_3 = self.data["recent_titles"][-3:] if count > 0 else []
                logger.info(f"💾 Memory loaded. Remember {count} past topics. Last 3: {last_3}")
                
            except Exception as e:
                logger.error(f"State load error: {e}")
    
    def save(self):
        fd, tmp_path = tempfile.mkstemp(dir=CACHE_DIR, suffix='.json')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            shutil.move(tmp_path, STATE_FILE)
        except Exception as e:
            logger.error(f"State save error: {e}")
            if os.path.exists(tmp_path): os.unlink(tmp_path)
    
    def is_posted(self, uid):
        return uid in self.data["posted_ids"]
    
    def mark_posted(self, uid, title):
        if len(self.data["posted_ids"]) > MAX_POSTED_IDS:
            sorted_ids = sorted(self.data["posted_ids"].items(), key=lambda x: x[1])
            self.data["posted_ids"] = dict(sorted_ids[-POSTED_IDS_TRIM_TO:])
        
        self.data["posted_ids"][uid] = int(time.time())
        
        self.data["recent_titles"].append(title)
        if len(self.data["recent_titles"]) > 40: # Помним последние 40 тем
            self.data["recent_titles"] = self.data["recent_titles"][-40:]
        
        self.save()

    def get_recent_titles_str(self):
        return "\n".join(f"- {t}" for t in self.data["recent_titles"])

state = State()

# ============ TEXT UTILS ============

def clean_text(text):
    if not text: return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return html.unescape(text).strip()

def split_text(text, max_len=4090):
    if len(text) <= max_len: return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_idx = text.rfind('\n', 0, max_len)
        if split_idx == -1: split_idx = text.rfind(' ', 0, max_len)
        if split_idx == -1: split_idx = max_len
        chunks.append(text[:split_idx])
        text = text[split_idx:].strip()
    return chunks

# ============ DUPLICATE CHECK (PARANOID MODE) ============

async def check_duplicate_topic(new_title):
    history = state.get_recent_titles_str()
    if not history: return False

    # Промпт "Параноик"
    prompt = f"""У меня есть список уже опубликованных новостей:
{history}

Новая новость: "{new_title}"

Задача: Проверь, не писали ли мы об этом ранее.
Если тема хоть немного совпадает (например, "взлом наушников" и "уязвимость Bluetooth") - ответь YES.
Если это абсолютно новая тема - ответь NO.
Будь строгим, лучше перебдеть. YES или NO?"""

    try:
        resp = await asyncio.to_thread(lambda: openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1, # Почти ноль, чтобы не фантазировал
            max_tokens=5
        ))
        is_dup = "YES" in resp.choices[0].message.content.strip().upper()
        if is_dup: logger.info(f"🚫 Duplicate detected (GPT): {new_title}")
        return is_dup
    except: return False

# ============ IMAGES ============

def generate_prompt(title):
    styles = [
        "dark cyberpunk city atmosphere, neon rain, cinematic lighting, 8k",
        "abstract data flow visualization, matrix style, green and black code",
        "minimalist glitch art, distorted reality, tech noir aesthetic",
        "isometric server room, stylized 3d render, soft blue lighting",
        "detailed blueprint schematic, white lines on dark blue background"
    ]
    obj = ["digital anomaly", "broken smartphone screen", "anonymous hacker", "red warning hologram"]
    
    clean_t = re.sub(r'[^a-zA-Z0-9\s]', '', title)[:40]
    return f"{random.choice(obj)}, {clean_t}, {random.choice(styles)}"

async def generate_image(title, session):
    try:
        prompt = generate_prompt(title)
        encoded = urllib.parse.quote(prompt)
        seed = random.randint(0, 99999)
        url = f"https://image.pollinations.ai/prompt/{encoded}?width=1280&height=720&nologo=true&seed={seed}"
        
        async with session.get(url, timeout=IMAGE_TIMEOUT) as resp:
            if resp.status == 200:
                data = await resp.read()
                if len(data) > 1000:
                    path = os.path.join(CACHE_DIR, f"img_{int(time.time())}.jpg")
                    with open(path, "wb") as f: f.write(data)
                    return path
    except Exception as e:
        logger.warning(f"Image gen failed: {e}")
    return None

# ============ FETCHERS ============

async def fetch_rss_feed(source, session):
    items = []
    try:
        async with session.get(source['url'], timeout=HTTP_TIMEOUT) as resp:
            if resp.status != 200: return []
            text = await resp.text()
            
        feed = feedparser.parse(text)
        for entry in feed.entries[:3]:
            link = entry.get('link')
            if not link: continue
            
            uid = hashlib.md5(link.encode()).hexdigest()
            if state.is_posted(uid): continue
            
            items.append(NewsItem(
                type="news", title=entry.get('title', ''), 
                text=clean_text(entry.get("summary", "")),
                link=link, source=source['name'], uid=uid
            ))
    except Exception as e:
        logger.warning(f"RSS error {source['name']}: {e}")
    return items

async def fetch_single_youtube(channel, session):
    items = []
    try:
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel['id']}"
        async with session.get(url, timeout=HTTP_TIMEOUT) as resp:
            if resp.status != 200: return []
            text = await resp.text()
            
        feed = feedparser.parse(text)
        for entry in feed.entries[:2]:
            vid = entry.get('yt_videoid')
            uid = f"yt_{vid}"
            if state.is_posted(uid): continue
            
            try:
                transcript = await asyncio.to_thread(lambda: 
                    YouTubeTranscriptApi.list_transcripts(vid).find_transcript(['ru', 'en']).fetch()
                )
                full_text = " ".join([t['text'] for t in transcript])
                items.append(NewsItem(
                    type="video", title=entry.title, text=full_text[:5000],
                    link=entry.link, source=f"YouTube {channel['name']}", uid=uid
                ))
            except: pass
    except Exception as e:
        logger.warning(f"YT error {channel['name']}: {e}")
    return items

# ============ GPT & POST ============

async def generate_post_content(item):
    if item.type == 'video':
        prompt = "Ты автор канала. Сделай краткий разбор видео (Squeeze). Без воды. Структура: Заголовок, Проблема, Решение."
    else:
        # ПРОМПТ С ТРЕБОВАНИЕМ ДЕТАЛЕЙ
        prompt = """Ты ведущий аналитик кибербезопасности. Напиши пост для канала.

ТРЕБОВАНИЯ:
1. Если новость короткая — ДОПИШИ технические подробности от себя. Пост должен быть полным.
2. Никаких "будьте бдительны". Только факты и инструкции.
3. Если тема — "очередной отчет" или "назначение директора" — верни SKIP.

СТРУКТУРА:
🔥 [Заголовок]
[Суть проблемы + Технические детали (как это работает)]
👇 ИНСТРУКЦИЯ ПО ЗАЩИТЕ:
• [Совет 1]
• [Совет 2]"""

    try:
        resp = await asyncio.to_thread(lambda: openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"Title: {item.title}\nText: {item.text}"}
            ],
            max_tokens=2000
        ))
        text = resp.choices[0].message.content.strip()
        if "SKIP" in text or len(text) < 50: return None
        return text + f"\n\n🔗 <a href='{item.link}'>Источник</a>"
    except Exception as e:
        logger.error(f"GPT error: {e}")
        return None

# ============ MAIN LOOP ============

async def main():
    logger.info("🚀 Starting scan...")
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_rss_feed(s, session) for s in RSS_SOURCES] + \
                [fetch_single_youtube(c, session) for c in YOUTUBE_CHANNELS]
        
        results = await asyncio.gather(*tasks)
        all_items = [item for sublist in results for item in sublist]
        
        logger.info(f"📦 Found {len(all_items)} raw items")
        random.shuffle(all_items)
        
        for item in all_items:
            # 1. СТОП-СЛОВА (МОМЕНТАЛЬНЫЙ БАН)
            low_title = item.title.lower()
            if any(bad in low_title for bad in STOP_WORDS):
                logger.info(f"🚫 BANNED WORD detected: {item.title}")
                state.mark_posted(item.uid, item.title)
                continue

            logger.info(f"🔍 Analyzing: {item.title}")
            
            # 2. GPT ПРОВЕРКА НА ПОВТОРЫ
            if await check_duplicate_topic(item.title):
                state.mark_posted(item.uid, item.title)
                continue
            
            post_text = await generate_post_content(item)
            if not post_text:
                state.mark_posted(item.uid, item.title)
                continue
            
            # Постинг
            try:
                if len(post_text) > TEXT_ONLY_THRESHOLD:
                    logger.info("📜 Long read -> Text only")
                    await bot.send_message(CHANNEL_ID, text=post_text, disable_web_page_preview=False)
                else:
                    logger.info("📸 Short read -> Image + Text")
                    img = await generate_image(item.title, session)
                    if img:
                        await bot.send_photo(CHANNEL_ID, photo=FSInputFile(img), caption=post_text)
                        os.remove(img)
                    else:
                        await bot.send_message(CHANNEL_ID, text=post_text)
                
                logger.info("✅ Success!")
                state.mark_posted(item.uid, item.title)
                break # 1 пост за раз
            except Exception as e:
                logger.error(f"Telegram error: {e}")

    await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
