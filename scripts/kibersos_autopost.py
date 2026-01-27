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
from dataclasses import dataclass, field
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

TEXT_ONLY_THRESHOLD = 700
MAX_POSTED_IDS = 400
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=25)
IMAGE_TIMEOUT = aiohttp.ClientTimeout(total=40)

# ============ GROQ ЛИМИТЫ ============

@dataclass
class ModelConfig:
    name: str
    rpm: int  # requests per minute
    tpm: int  # tokens per minute
    daily_tokens: int
    priority: int  # меньше = выше приоритет

MODELS = {
    "heavy": ModelConfig("llama-3.3-70b-versatile", rpm=30, tpm=6000, daily_tokens=100000, priority=1),
    "light": ModelConfig("llama3-8b-8192", rpm=30, tpm=30000, daily_tokens=500000, priority=2),
    "fallback": ModelConfig("llama-3.1-8b-instant", rpm=30, tpm=20000, daily_tokens=500000, priority=3),
}

# ============ RATE LIMITER & TOKEN TRACKER ============

class GroqBudget:
    """Отслеживание лимитов Groq с автосбросом"""
    
    def __init__(self):
        self.state_file = os.path.join(CACHE_DIR, "groq_budget.json")
        self.data = self._load()
    
    def _load(self) -> dict:
        default = {
            "daily_tokens": {},  # model -> tokens used today
            "last_reset": time.strftime("%Y-%m-%d"),
            "last_request_time": {},  # model -> timestamp
            "request_count": {},  # model -> count in current minute
            "minute_start": {},  # model -> minute start timestamp
        }
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    saved = json.load(f)
                    # Сброс при новом дне
                    if saved.get("last_reset") != time.strftime("%Y-%m-%d"):
                        logger.info("🔄 New day — resetting token budget")
                        saved["daily_tokens"] = {}
                        saved["last_reset"] = time.strftime("%Y-%m-%d")
                    default.update(saved)
            except:
                pass
        return default
    
    def save(self):
        try:
            with open(self.state_file, "w") as f:
                json.dump(self.data, f)
        except Exception as e:
            logger.warning(f"Budget save error: {e}")
    
    def get_daily_usage(self, model: str) -> int:
        return self.data["daily_tokens"].get(model, 0)
    
    def add_tokens(self, model: str, tokens: int):
        self.data["daily_tokens"][model] = self.get_daily_usage(model) + tokens
        self.save()
    
    def can_use_model(self, model_key: str) -> bool:
        """Проверяем, не исчерпан ли дневной лимит"""
        if model_key not in MODELS:
            return False
        cfg = MODELS[model_key]
        used = self.get_daily_usage(cfg.name)
        remaining = cfg.daily_tokens - used
        
        # Оставляем 10% резерв
        if remaining < cfg.daily_tokens * 0.1:
            logger.warning(f"⚠️ {model_key} almost exhausted: {remaining} tokens left")
            return False
        return True
    
    async def wait_for_rate_limit(self, model_key: str):
        """Ждём, если превышен RPM"""
        cfg = MODELS[model_key]
        model = cfg.name
        now = time.time()
        
        # Сброс счётчика каждую минуту
        minute_start = self.data["minute_start"].get(model, 0)
        if now - minute_start > 60:
            self.data["minute_start"][model] = now
            self.data["request_count"][model] = 0
        
        count = self.data["request_count"].get(model, 0)
        
        if count >= cfg.rpm - 2:  # Оставляем запас в 2 запроса
            wait_time = 60 - (now - minute_start) + 1
            logger.info(f"⏳ Rate limit {model_key}: waiting {wait_time:.1f}s")
            await asyncio.sleep(wait_time)
            self.data["minute_start"][model] = time.time()
            self.data["request_count"][model] = 0
        
        # Минимальный интервал между запросами (2 секунды)
        last_req = self.data["last_request_time"].get(model, 0)
        if now - last_req < 2:
            await asyncio.sleep(2 - (now - last_req))
        
        self.data["request_count"][model] = self.data["request_count"].get(model, 0) + 1
        self.data["last_request_time"][model] = time.time()

budget = GroqBudget()

# ============ ФИЛЬТРЫ (БЕЗ AI) ============

STOP_WORDS = [
    "наушник", "jbl", "bluetooth", "гарнитур",
    "квартальный отчет", "назначен директором", "маркетинг", "конференция",
    "мвсфера", "мсвсфера", "astra linux", "астра линукс", "red os", "ред ос",
    "импортозамещ", "postgresql", "highload", "golang", "криптовалют", "казино"
]

BANNED_PHRASES = [
    "из доверенных источников", "из официальных источников",
    "регулярно обновляйте", "будьте бдительны", "будьте осторожны",
    "внимательно читайте", "проверяйте разрешения", "используйте антивирус",
    "надёжный пароль", "надежный пароль", "сложный пароль",
    "не переходите по ссылкам", "не открывайте подозрительные",
    "двухфакторную аутентификацию", "двухфакторная аутентификация"
]

def local_similarity(title1: str, title2: str) -> float:
    """Локальная проверка похожести без AI"""
    # Нормализация
    t1 = re.sub(r'[^\w\s]', '', title1.lower())
    t2 = re.sub(r'[^\w\s]', '', title2.lower())
    
    # SequenceMatcher для общей похожести
    ratio = SequenceMatcher(None, t1, t2).ratio()
    
    # Проверка ключевых слов
    words1 = set(t1.split())
    words2 = set(t2.split())
    
    # Убираем стоп-слова
    stop = {'в', 'на', 'и', 'для', 'с', 'по', 'из', 'к', 'от', 'the', 'a', 'an', 'in', 'on', 'for'}
    words1 -= stop
    words2 -= stop
    
    if not words1 or not words2:
        return ratio
    
    # Jaccard similarity для слов
    intersection = len(words1 & words2)
    union = len(words1 | words2)
    jaccard = intersection / union if union else 0
    
    return max(ratio, jaccard)

def is_local_duplicate(title: str, recent_titles: list, threshold: float = 0.6) -> bool:
    """Локальная проверка дубликатов — экономит вызовы AI"""
    for recent in recent_titles[-30:]:
        if local_similarity(title, recent) > threshold:
            logger.info(f"🔄 Local duplicate: '{title}' ~ '{recent}'")
            return True
    return False

def is_too_generic(text: str) -> bool:
    """Проверка на банальности"""
    text_lower = text.lower()
    count = sum(1 for phrase in BANNED_PHRASES if phrase in text_lower)
    return count >= 2

def passes_local_filters(title: str, text: str) -> bool:
    """Все локальные проверки в одном месте"""
    low_title = title.lower()
    low_text = (text or "").lower()
    
    # Стоп-слова
    for word in STOP_WORDS:
        if word in low_title or word in low_text:
            logger.info(f"🚫 Stop word '{word}': {title}")
            return False
    
    # Слишком короткий контент
    if len(text) < 100:
        logger.info(f"🚫 Too short: {title}")
        return False
    
    return True

# ============ GROQ API С ОПТИМИЗАЦИЕЙ ============

async def call_groq(
    prompt: str, 
    model_preference: str = "heavy",
    max_tokens: int = 1500
) -> tuple[str, int]:
    """
    Вызов Groq с автоматическим fallback и отслеживанием токенов.
    Возвращает (ответ, использовано_токенов)
    """
    
    # Определяем порядок моделей для fallback
    model_order = ["heavy", "light", "fallback"]
    if model_preference == "light":
        model_order = ["light", "fallback", "heavy"]
    
    last_error = None
    
    for model_key in model_order:
        if not budget.can_use_model(model_key):
            continue
        
        cfg = MODELS[model_key]
        
        try:
            await budget.wait_for_rate_limit(model_key)
            
            response = await asyncio.to_thread(
                lambda: groq_client.chat.completions.create(
                    model=cfg.name,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=max_tokens
                )
            )
            
            result = response.choices[0].message.content.strip()
            tokens_used = response.usage.total_tokens if response.usage else max_tokens
            
            budget.add_tokens(cfg.name, tokens_used)
            logger.debug(f"✓ {model_key}: {tokens_used} tokens")
            
            return result, tokens_used
            
        except RateLimitError as e:
            logger.warning(f"⚠️ Rate limit {model_key}: {e}")
            last_error = e
            # Принудительное ожидание
            await asyncio.sleep(30)
            continue
            
        except APIError as e:
            logger.warning(f"⚠️ API error {model_key}: {e}")
            last_error = e
            continue
            
        except Exception as e:
            logger.error(f"❌ Unexpected error {model_key}: {e}")
            last_error = e
            continue
    
    logger.error(f"❌ All models failed: {last_error}")
    return "", 0

# ============ ОПТИМИЗИРОВАННАЯ ПРОВЕРКА ДУБЛИКАТОВ ============

async def check_duplicate(new_title: str, recent_titles: list) -> bool:
    """
    Двухэтапная проверка: сначала локально, потом AI (если нужно)
    """
    if not recent_titles:
        return False
    
    # Этап 1: Локальная проверка (бесплатно!)
    if is_local_duplicate(new_title, recent_titles):
        return True
    
    # Этап 2: AI проверка только для неочевидных случаев
    # Берём только 10 последних для экономии токенов
    history = "\n".join(f"- {t}" for t in recent_titles[-10:])
    
    # Короткий промпт = меньше токенов
    prompt = f"""Темы: 
{history}

Новая: "{new_title}"

Дубликат? YES/NO"""

    answer, tokens = await call_groq(prompt, model_preference="light", max_tokens=10)
    
    if not answer:
        # При ошибке API — пропускаем (лучше опубликовать, чем потерять)
        return False
    
    return "YES" in answer.upper()

# ============ ГЕНЕРАЦИЯ ПОСТА (ОПТИМИЗИРОВАННЫЙ ПРОМПТ) ============

async def generate_post(item) -> Optional[str]:
    """Генерация с укороченным промптом для экономии токенов"""
    
    # Сокращаем входной текст
    text_preview = item.text[:2000]  # Было 3000
    
    prompt = f"""Кибербез-канал. Пиши кратко, с конкретикой.

НОВОСТЬ: {item.title}
{text_preview}

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

    text, tokens = await call_groq(prompt, model_preference="heavy", max_tokens=800)
    
    logger.info(f"📝 Generated: {tokens} tokens")
    
    if not text or "SKIP" in text.upper() or len(text) < 100:
        return None
    
    if is_too_generic(text):
        logger.info(f"⏩ Too generic: {item.title}")
        return None
    
    return text + f"\n\n🔗 <a href='{item.link}'>Источник</a>"

# ============ RSS SOURCES ============

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
groq_client = Groq(api_key=GROQ_API_KEY)

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
                logger.info(f"💾 Memory: {len(self.data.get('recent_titles', []))} topics")
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
        return self.data.get("recent_titles", [])

state = State()

# ============ UTILS ============

def clean_text(text):
    if not text: 
        return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return html.unescape(text).strip()

# ============ FETCHERS ============

async def fetch_rss(source, session):
    items = []
    try:
        async with session.get(source['url'], timeout=HTTP_TIMEOUT) as resp:
            if resp.status != 200: 
                return []
            text = await resp.text()
        feed = feedparser.parse(text)
        for entry in feed.entries[:5]:
            link = entry.get('link')
            if not link: 
                continue
            uid = hashlib.md5(link.encode()).hexdigest()
            if state.is_posted(uid): 
                continue
            
            title = entry.get('title', '')
            text = clean_text(entry.get("summary", ""))
            
            # Локальная фильтрация ДО добавления
            if not passes_local_filters(title, text):
                continue
                
            items.append(NewsItem(
                type="news", title=title,
                text=text, link=link, 
                source=source['name'], uid=uid
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
                # Явно передаём vid в лямбду
                transcript = await asyncio.to_thread(
                    lambda v=vid: YouTubeTranscriptApi.list_transcripts(v)
                        .find_transcript(['ru', 'en']).fetch()
                )
                full_text = " ".join([t['text'] for t in transcript])
                
                if not passes_local_filters(entry.title, full_text):
                    continue
                    
                items.append(NewsItem(
                    type="video", title=entry.title, 
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

# ============ IMAGES ============

async def generate_image(title, session):
    try:
        styles = ["cyberpunk neon", "matrix code", "glitch art"]
        clean_t = re.sub(r'[^a-zA-Z0-9\s]', '', title)[:40]
        prompt = f"hacker silhouette, {clean_t}, {random.choice(styles)}"
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

# ============ MAIN ============

async def main():
    logger.info("🚀 Starting (Groq Optimized v2)...")
    
    # Показываем текущий бюджет
    for key, cfg in MODELS.items():
        used = budget.get_daily_usage(cfg.name)
        remaining = cfg.daily_tokens - used
        pct = (remaining / cfg.daily_tokens) * 100
        logger.info(f"💰 {key}: {remaining:,} tokens left ({pct:.1f}%)")
    
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_rss(s, session) for s in RSS_SOURCES]
        tasks += [fetch_youtube(c, session) for c in YOUTUBE_CHANNELS]
        results = await asyncio.gather(*tasks)
        all_items = [item for sublist in results for item in sublist]
        
        logger.info(f"📦 Found {len(all_items)} items (after local filters)")
        random.shuffle(all_items)
        
        posts_today = 0
        max_posts = 3  # Лимит постов за запуск
        
        for item in all_items:
            if posts_today >= max_posts:
                logger.info(f"📊 Reached post limit ({max_posts})")
                break
            
            # Проверяем бюджет перед обработкой
            if not budget.can_use_model("light"):
                logger.warning("⚠️ Token budget exhausted!")
                break
            
            logger.info(f"🔍 Checking: {item.title}")
            
            # Двухэтапная проверка дубликатов
            if await check_duplicate(item.title, state.get_recent_titles()):
                state.mark_posted(item.uid, item.title)
                continue
            
            # Генерация поста
            post_text = await generate_post(item)
            if not post_text:
                state.mark_posted(item.uid, item.title)
                continue
            
            try:
                if len(post_text) > TEXT_ONLY_THRESHOLD:
                    await bot.send_message(CHANNEL_ID, text=post_text)
                else:
                    img = await generate_image(item.title, session)
                    if img:
                        await bot.send_photo(
                            CHANNEL_ID, 
                            photo=FSInputFile(img), 
                            caption=post_text
                        )
                        os.remove(img)
                    else:
                        await bot.send_message(CHANNEL_ID, text=post_text)
                
                logger.info("✅ Posted!")
                state.mark_posted(item.uid, item.title)
                posts_today += 1
                
                # Пауза между постами
                await asyncio.sleep(5)
                
            except Exception as e:
                logger.error(f"Telegram error: {e}")
    
    # Финальная статистика
    logger.info("📊 Session stats:")
    for key, cfg in MODELS.items():
        used = budget.get_daily_usage(cfg.name)
        logger.info(f"   {key}: {used:,} tokens used today")
    
    await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
