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
from typing import Literal, Optional
from difflib import SequenceMatcher

import aiohttp
import feedparser
from youtube_transcript_api import YouTubeTranscriptApi
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile
from groq import Groq, RateLimitError, APIError

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
        logger.error(f"Missing: {name}")
        exit(1)
    return val

GROQ_API_KEY = get_env("GROQ_API_KEY")
TELEGRAM_BOT_TOKEN = get_env("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = get_env("CHANNEL_ID")

CACHE_DIR = os.getenv("CACHE_DIR", "cache_sec")
os.makedirs(CACHE_DIR, exist_ok=True)
STATE_FILE = os.path.join(CACHE_DIR, "state_groq_v2.json")

# Настройки контента
TEXT_ONLY_THRESHOLD = 700  # Если текст длиннее, картинку НЕ делаем
MAX_POSTED_IDS = 400
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=25)
IMAGE_TIMEOUT = aiohttp.ClientTimeout(total=40)

# ============ GROQ ЛИМИТЫ (БЮДЖЕТ) ============

@dataclass
class ModelConfig:
    name: str
    rpm: int  # requests per minute
    tpm: int  # tokens per minute
    daily_tokens: int
    priority: int

MODELS = {
    "heavy": ModelConfig("llama-3.3-70b-versatile", rpm=30, tpm=6000, daily_tokens=100000, priority=1),
    "light": ModelConfig("llama3-8b-8192", rpm=30, tpm=30000, daily_tokens=500000, priority=2),
    "fallback": ModelConfig("llama-3.1-8b-instant", rpm=30, tpm=20000, daily_tokens=500000, priority=3),
}

class GroqBudget:
    """Умное отслеживание лимитов Groq"""
    
    def __init__(self):
        self.state_file = os.path.join(CACHE_DIR, "groq_budget.json")
        self.data = self._load()
    
    def _load(self) -> dict:
        default = {
            "daily_tokens": {},
            "last_reset": time.strftime("%Y-%m-%d"),
            "last_request_time": {},
            "request_count": {},
            "minute_start": {},
        }
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    saved = json.load(f)
                    if saved.get("last_reset") != time.strftime("%Y-%m-%d"):
                        logger.info("🔄 Новый день — сброс лимитов Groq")
                        saved["daily_tokens"] = {}
                        saved["last_reset"] = time.strftime("%Y-%m-%d")
                    default.update(saved)
            except: pass
        return default
    
    def save(self):
        try:
            with open(self.state_file, "w") as f: json.dump(self.data, f)
        except: pass
    
    def add_tokens(self, model: str, tokens: int):
        self.data["daily_tokens"][model] = self.data["daily_tokens"].get(model, 0) + tokens
        self.save()
    
    def can_use_model(self, model_key: str) -> bool:
        if model_key not in MODELS: return False
        cfg = MODELS[model_key]
        used = self.data["daily_tokens"].get(cfg.name, 0)
        return (cfg.daily_tokens - used) > (cfg.daily_tokens * 0.05) # 5% резерв
    
    async def wait_for_rate_limit(self, model_key: str):
        cfg = MODELS[model_key]
        model = cfg.name
        now = time.time()
        
        # Сброс минутного окна
        if now - self.data["minute_start"].get(model, 0) > 60:
            self.data["minute_start"][model] = now
            self.data["request_count"][model] = 0
        
        # Проверка RPM
        if self.data["request_count"].get(model, 0) >= cfg.rpm - 2:
            wait = 60 - (now - self.data["minute_start"][model]) + 1
            logger.info(f"⏳ Лимит RPM ({model_key}). Ждем {wait:.1f}с")
            await asyncio.sleep(wait)
            self.data["minute_start"][model] = time.time()
            self.data["request_count"][model] = 0
            
        # Минимальный интервал (анти-спам)
        last = self.data["last_request_time"].get(model, 0)
        if now - last < 2: await asyncio.sleep(2)
        
        self.data["request_count"][model] = self.data["request_count"].get(model, 0) + 1
        self.data["last_request_time"][model] = time.time()

budget = GroqBudget()

# ============ ФИЛЬТРЫ ============

STOP_WORDS = [
    "наушник", "jbl", "bluetooth", "гарнитур",
    "квартальный отчет", "назначен директором", "маркетинг", "конференция",
    "мвсфера", "мсвсфера", "astra linux", "астра линукс", "red os", "ред ос",
    "импортозамещ", "postgresql", "highload", "golang", "криптовалют", "казино"
]

BANNED_PHRASES = [
    "из доверенных источников", "регулярно обновляйте", "будьте бдительны",
    "внимательно читайте", "используйте антивирус", "надёжный пароль",
    "не переходите по ссылкам"
]

def is_too_generic(text: str) -> bool:
    """Если пост состоит из банальностей — выкидываем"""
    text_lower = text.lower()
    count = sum(1 for phrase in BANNED_PHRASES if phrase in text_lower)
    return count >= 2

def passes_local_filters(title: str, text: str) -> bool:
    """Быстрая проверка без AI"""
    content = (title + " " + text).lower()
    if any(w in content for w in STOP_WORDS):
        logger.info(f"🚫 Stop word found: {title}")
        return False
    if len(text) < 100:
        return False
    return True

# ============ GROQ CALLER ============

async def call_groq(prompt: str, model_pref: str = "heavy", max_tokens: int = 1500) -> tuple[str, int]:
    """Умный вызов с переключением моделей при ошибках"""
    order = ["heavy", "light", "fallback"] if model_pref == "heavy" else ["light", "fallback", "heavy"]
    
    for key in order:
        if not budget.can_use_model(key): continue
        cfg = MODELS[key]
        
        try:
            await budget.wait_for_rate_limit(key)
            response = await asyncio.to_thread(
                lambda: groq_client.chat.completions.create(
                    model=cfg.name,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=max_tokens
                )
            )
            res = response.choices[0].message.content.strip()
            tokens = response.usage.total_tokens if response.usage else 0
            budget.add_tokens(cfg.name, tokens)
            return res, tokens
            
        except Exception as e:
            logger.warning(f"⚠️ Error {key}: {e}")
            await asyncio.sleep(5)
            continue
            
    return "", 0

# ============ ЛОГИКА ============

async def check_duplicate(new_title: str, recent: list) -> bool:
    """Сначала проверяем локально (бесплатно), потом AI"""
    if not recent: return False
    
    # 1. Локальная проверка (SequenceMatcher)
    norm_new = re.sub(r'\W', '', new_title.lower())
    for old in recent[-20:]:
        norm_old = re.sub(r'\W', '', old.lower())
        if SequenceMatcher(None, norm_new, norm_old).ratio() > 0.6:
            logger.info(f"🔄 Local duplicate: {new_title}")
            return True
            
    # 2. AI проверка (легкая модель)
    history = "\n".join(f"- {t}" for t in recent[-10:])
    prompt = f"Темы:\n{history}\n\nНовая: '{new_title}'\nДубликат? YES/NO"
    ans, _ = await call_groq(prompt, "light", 10)
    
    return "YES" in ans.upper()

async def generate_post(item) -> Optional[str]:
    prompt = f"""Кибербез-канал. Пиши кратко, с конкретикой.

НОВОСТЬ: {item.title}
{item.text[:2000]}

ПРАВИЛА:
- Без банальностей (пароли, антивирус, "будьте осторожны")
- Только конкретные угрозы и действия
- SKIP если нет пользы для обычного юзера

ФОРМАТ:
🔥 [Заголовок]

[2-3 предложения: суть + механика]

👇 ЧТО СДЕЛАТЬ:
• [Конкретное действие]

Пост или SKIP:"""

    text, _ = await call_groq(prompt, "heavy", 1000)
    
    if not text or "SKIP" in text.upper() or len(text) < 100:
        return None
    if is_too_generic(text):
        logger.info(f"⏩ Too generic: {item.title}")
        return None
        
    return text + f"\n\n🔗 <a href='{item.link}'>Источник</a>"

# ============ ИЗОБРАЖЕНИЯ ============

async def generate_image(title, session):
    try:
        styles = ["cyberpunk neon", "matrix code", "glitch art", "isometric 3d"]
        clean_t = re.sub(r'[^a-zA-Z0-9\s]', '', title)[:40]
        prompt = f"hacker silhouette, {clean_t}, {random.choice(styles)}"
        encoded = urllib.parse.quote(prompt)
        url = f"https://image.pollinations.ai/prompt/{encoded}?width=1280&height=720&nologo=true&seed={random.randint(0,99999)}"
        
        async with session.get(url, timeout=IMAGE_TIMEOUT) as resp:
            if resp.status == 200:
                data = await resp.read()
                if len(data) > 5000:
                    path = os.path.join(CACHE_DIR, f"img_{int(time.time())}.jpg")
                    with open(path, "wb") as f: f.write(data)
                    return path
    except: pass
    return None

# ============ КЛАССЫ И ИНИЦИАЛИЗАЦИЯ ============

@dataclass
class NewsItem:
    type: Literal["news", "video"]
    title: str
    text: str
    link: str
    source: str
    uid: str

bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
groq_client = Groq(api_key=GROQ_API_KEY)

# ============ STATE (ПАМЯТЬ) ============

class State:
    def __init__(self):
        self.data = {"posted_ids": {}, "recent_titles": []}
        self._load()
    
    def _load(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f: self.data.update(json.load(f))
            except: pass
    
    def save(self):
        fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, suffix='.json')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f: json.dump(self.data, f)
            shutil.move(tmp, STATE_FILE)
        except: os.unlink(tmp)
    
    def is_posted(self, uid): return uid in self.data["posted_ids"]
    
    def mark_posted(self, uid, title):
        if len(self.data["posted_ids"]) > MAX_POSTED_IDS:
            self.data["posted_ids"] = dict(sorted(self.data["posted_ids"].items(), key=lambda x: x[1])[-300:])
        self.data["posted_ids"][uid] = int(time.time())
        self.data["recent_titles"].append(title)
        if len(self.data["recent_titles"]) > 40: self.data["recent_titles"] = self.data["recent_titles"][-40:]
        self.save()

state = State()

# ============ СБОРЩИКИ ============

async def fetch_rss(source, session):
    items = []
    try:
        async with session.get(source['url'], timeout=HTTP_TIMEOUT) as resp:
            if resp.status != 200: return []
            text = await resp.text()
        feed = feedparser.parse(text)
        for entry in feed.entries[:5]:
            link = entry.get('link')
            if not link: continue
            uid = hashlib.md5(link.encode()).hexdigest()
            if state.is_posted(uid): continue
            
            title = entry.get('title', '')
            text = clean_text(entry.get("summary", ""))
            
            if passes_local_filters(title, text):
                items.append(NewsItem("news", title, text, link, source['name'], uid))
    except: pass
    return items

async def fetch_youtube(channel, session):
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
                ts = await asyncio.to_thread(lambda: YouTubeTranscriptApi.list_transcripts(vid).find_transcript(['ru', 'en']).fetch())
                full = " ".join([t['text'] for t in ts])
                if passes_local_filters(entry.title, full):
                    items.append(NewsItem("video", entry.title, full[:5000], entry.link, f"YouTube {channel['name']}", uid))
            except: pass
    except: pass
    return items

def clean_text(text):
    if not text: return ""
    return html.unescape(re.sub(r'<[^>]+>', ' ', text)).strip()

# ============ MAIN ============

async def main():
    logger.info("🚀 Starting (1 POST LIMIT MODE)...")
    
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_rss(s, session) for s in RSS_SOURCES] + [fetch_youtube(c, session) for c in YOUTUBE_CHANNELS]
        results = await asyncio.gather(*tasks)
        all_items = [i for r in results for i in r]
        
        logger.info(f"📦 Found {len(all_items)} items")
        random.shuffle(all_items)
        
        posts_done = 0
        MAX_POSTS_PER_RUN = 1  # <--- ВОТ ГЛАВНОЕ ИСПРАВЛЕНИЕ
        
        for item in all_items:
            if posts_done >= MAX_POSTS_PER_RUN:
                break
            
            if not budget.can_use_model("light"):
                logger.warning("⚠️ Daily budget exhausted")
                break
            
            logger.info(f"🔍 Analyzing: {item.title}")
            
            if await check_duplicate(item.title, state.data["recent_titles"]):
                state.mark_posted(item.uid, item.title)
                continue
            
            post_text = await generate_post(item)
            if not post_text:
                state.mark_posted(item.uid, item.title)
                continue
            
            try:
                # Решение: Текст или Картинка?
                if len(post_text) > TEXT_ONLY_THRESHOLD:
                    logger.info("📜 Text only (Long read)")
                    await bot.send_message(CHANNEL_ID, text=post_text)
                else:
                    logger.info("📸 Generating image...")
                    img = await generate_image(item.title, session)
                    if img:
                        await bot.send_photo(CHANNEL_ID, photo=FSInputFile(img), caption=post_text)
                        os.remove(img)
                    else:
                        await bot.send_message(CHANNEL_ID, text=post_text)
                
                logger.info("✅ Posted successfully!")
                state.mark_posted(item.uid, item.title)
                posts_done += 1
                
            except Exception as e:
                logger.error(f"Telegram Error: {e}")

    await bot.session.close()

if __name__ == "__main__":
    # Настройки источников
    RSS_SOURCES = [
        {"name": "Kaspersky", "url": "https://www.kaspersky.ru/blog/feed/"},
        {"name": "Kod.ru", "url": "https://kod.ru/rss/"},
        {"name": "BleepingComputer", "url": "https://www.bleepingcomputer.com/feed/"},
        {"name": "Habr Security", "url": "https://habr.com/ru/rss/hub/infosecurity/all/?fl=ru"},
        {"name": "SecurityLab", "url": "https://www.securitylab.ru/rss/news/"},
    ]
    YOUTUBE_CHANNELS = [
        {"name": "Overbafer1", "id": "UC-lHJ97lqoOGgsLFuQ8Y8_g"},
        {"name": "NetworkChuck", "id": "UC9x0AN7BWHpXyPic4IQC74Q"},
    ]
    
    asyncio.run(main())
