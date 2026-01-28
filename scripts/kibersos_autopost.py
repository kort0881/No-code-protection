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

TEXT_ONLY_THRESHOLD = 700
MAX_POSTED_IDS = 400
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=25)
IMAGE_TIMEOUT = aiohttp.ClientTimeout(total=40)

# ============ ОБНОВЛЕННЫЕ МОДЕЛИ ============

@dataclass
class ModelConfig:
    name: str
    rpm: int
    tpm: int
    daily_tokens: int
    priority: int

MODELS = {
    "heavy": ModelConfig("llama-3.3-70b-versatile", rpm=30, tpm=6000, daily_tokens=100000, priority=1),
    "light": ModelConfig("llama-3.1-8b-instant", rpm=30, tpm=20000, daily_tokens=500000, priority=2),
    "fallback": ModelConfig("mixtral-8x7b-32768", rpm=30, tpm=5000, daily_tokens=100000, priority=3),
}

class GroqBudget:
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
        return (cfg.daily_tokens - used) > (cfg.daily_tokens * 0.05)
    
    async def wait_for_rate_limit(self, model_key: str):
        cfg = MODELS[model_key]
        model = cfg.name
        now = time.time()
        
        if now - self.data["minute_start"].get(model, 0) > 60:
            self.data["minute_start"][model] = now
            self.data["request_count"][model] = 0
        
        if self.data["request_count"].get(model, 0) >= cfg.rpm - 2:
            wait = 60 - (now - self.data["minute_start"][model]) + 1
            logger.info(f"⏳ Лимит RPM ({model_key}). Ждем {wait:.1f}с")
            await asyncio.sleep(wait)
            self.data["minute_start"][model] = time.time()
            self.data["request_count"][model] = 0
            
        last = self.data["last_request_time"].get(model, 0)
        if now - last < 2: await asyncio.sleep(2)
        
        self.data["request_count"][model] = self.data["request_count"].get(model, 0) + 1
        self.data["last_request_time"][model] = time.time()

budget = GroqBudget()

# ============ ФИЛЬТРЫ (ОБНОВЛЕНЫ ДЛЯ АНГЛИЙСКОГО) ============

# Теперь стоп-слова на английском (источники английские)
STOP_WORDS = [
    "headphone", "jbl", "bluetooth headset", "earbuds",
    "quarterly earnings", "appointed ceo", "marketing campaign", "conference announcement",
    "cryptocurrency", "casino", "gambling", "nft trading", "bitcoin price"
]

# Банальные фразы на РУССКОМ (для проверки сгенерированного поста)
BANNED_PHRASES = [
    "из доверенных источников", "регулярно обновляйте", "будьте бдительны",
    "внимательно читайте", "используйте антивирус", "надёжный пароль",
    "не переходите по ссылкам", "будьте осторожны", "проверяйте ссылки",
    "установите обновления", "используйте двухфакторную", "сложные пароли",
    "надежные решения", "системы обнаружения", "мониторинг трафика",
    "обеспечения безопасности", "защитить свои данные", "потенциальных атак",
    "устранять уязвимости", "злоумышленниками для атак"
]

# Технические термины (на английском и русском — для проверки обоих)
TECH_INDICATORS = [
    "cve-", "0day", "exploit", "payload", "shell", "sudo", "root",
    "port ", "ip", "dns", "ssh", "rdp", "smb", "http", "api",
    "token", "hash", "salt", "aes", "rsa", "tls", "ssl",
    "android", "ios", "windows", "linux", "macos",
    "chrome", "firefox", "safari", "edge",
    "apt", "lazarus", "fancy bear", "sandworm", "apt28", "apt29",
    ".exe", ".dll", ".apk", ".sh", ".bat", ".js",
    "phishing", "malware", "ransomware", "backdoor", "trojan",
    # Русские варианты
    "порт ", "вредонос", "эксплойт", "уязвимост", "фишинг"
]

def is_too_generic(text: str) -> bool:
    """Проверка сгенерированного РУССКОГО поста на банальности"""
    text_lower = text.lower()
    
    banned_count = sum(1 for phrase in BANNED_PHRASES if phrase in text_lower)
    if banned_count >= 1:
        logger.info(f"⚠️ Generic phrase detected: {banned_count} matches")
        return True
    
    tech_count = sum(1 for term in TECH_INDICATORS if term in text_lower)
    if tech_count < 2:
        logger.info(f"⚠️ Not enough technical details: {tech_count}/2")
        return True
    
    lines = text.split('\n')
    advice_lines = [l for l in lines if l.strip().startswith('•') or l.strip().startswith('-')]
    if len(advice_lines) > 0 and len(advice_lines) / max(len(lines), 1) > 0.4:
        logger.info(f"⚠️ Too many generic tips: {len(advice_lines)} lines")
        return True
    
    return False

def passes_local_filters(title: str, text: str) -> bool:
    """Фильтрация АНГЛИЙСКОГО исходника"""
    content = (title + " " + text).lower()
    if any(w in content for w in STOP_WORDS):
        logger.info(f"🚫 Stop word found: {title}")
        return False
    if len(text) < 100:
        return False
    
    # Требуем наличие security-ключевых слов в английском тексте
    security_keywords = [
        "vulnerability", "exploit", "malware", "ransomware", "phishing",
        "hacker", "breach", "attack", "threat", "zero-day", "patch",
        "security", "cybersecurity", "cyber attack", "data breach"
    ]
    if not any(kw in content for kw in security_keywords):
        logger.info(f"🚫 No security keywords: {title}")
        return False
    
    return True

# ============ GROQ CALLER ============

async def call_groq(prompt: str, model_pref: str = "heavy", max_tokens: int = 1500) -> tuple[str, int]:
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
            logger.info(f"✅ Model used: {cfg.name} ({tokens} tokens)")
            return res, tokens
            
        except Exception as e:
            logger.warning(f"⚠️ Error {key} ({cfg.name}): {e}")
            await asyncio.sleep(5)
            continue
            
    return "", 0

# ============ ЛОГИКА ============

async def check_duplicate(new_title: str, recent: list) -> bool:
    if not recent: return False
    
    norm_new = re.sub(r'\W', '', new_title.lower())
    for old in recent[-20:]:
        norm_old = re.sub(r'\W', '', old.lower())
        if SequenceMatcher(None, norm_new, norm_old).ratio() > 0.6:
            logger.info(f"🔄 Local duplicate: {new_title}")
            return True
            
    history = "\n".join(f"- {t}" for t in recent[-10:])
    prompt = f"Темы:\n{history}\n\nНовая: '{new_title}'\nДубликат? YES/NO"
    ans, _ = await call_groq(prompt, "light", 10)
    
    return "YES" in ans.upper()

async def generate_post(item) -> Optional[str]:
    # ПРОМПТ С ПЕРЕВОДОМ (как в первом скрипте)
    prompt = f"""Ты — редактор русскоязычного Telegram-канала про кибербезопасность (30к+ подписчиков).

ИСХОДНАЯ НОВОСТЬ (English):
Заголовок: {item.title}
Текст: {item.text[:2500]}

ТВОЯ ЗАДАЧА:
1. Прочитай английский текст и переведи суть на РУССКИЙ язык
2. Напиши пост на РУССКОМ с конкретными рекомендациями

СТРОГИЕ ПРАВИЛА:
❌ НЕ ПИШИ банальности:
   - "обновляйте ПО", "антивирус", "будьте осторожны"
   - "надежные пароли", "системы обнаружения"
   - "защитить свои данные", "мониторинг трафика"

✅ ОБЯЗАТЕЛЬНО укажи:
   - Конкретные уязвимости (CVE-номера, версии софта)
   - Технические детали атаки (порты, протоколы, команды)
   - Специфические действия для защиты (не "обновитесь", а "обновитесь до Chrome 131.0.6778.264")

📌 Если нет технических деталей или польза неясна — пиши SKIP

ФОРМАТ (на РУССКОМ):
🔥 [Заголовок с конкретикой]

[2-3 предложения: ЧТО произошло + КАК работает атака]

👇 ЧТО СДЕЛАТЬ:
• [Конкретное действие с версиями/командами/настройками]
• [Еще одно конкретное действие]

ПРИМЕРЫ ХОРОШЕГО:
✅ "Обновите Chrome до версии 131.0.6778.108 (CVE-2024-12345)"
✅ "Закройте порт 445 (SMB) командой: netsh advfirewall firewall add rule..."
✅ "Проверьте наличие файла evil.dll в C:\\Windows\\Temp"

ПРИМЕРЫ ПЛОХОГО:
❌ "Используйте надежные решения для мониторинга"
❌ "Регулярно обновляйте программное обеспечение"
❌ "Будьте бдительны при переходе по ссылкам"

Пост на РУССКОМ или SKIP:"""

    text, _ = await call_groq(prompt, "heavy", 1200)
    
    if not text or "SKIP" in text.upper() or len(text) < 120:
        logger.info("⏩ AI returned SKIP or too short")
        return None
    
    if is_too_generic(text):
        logger.info(f"⏩ Post is too generic after generation")
        return None
        
    return text + f"\n\n🔗 <a href='{item.link}'>Источник</a>"

# ============ ИЗОБРАЖЕНИЯ ============

async def generate_image(title, session):
    try:
        styles = ["cyberpunk neon red", "matrix green code", "hacker terminal glitch", "dark web aesthetic"]
        clean_t = re.sub(r'[^a-zA-Z0-9\s]', '', title)[:40]
        prompt = f"hacker silhouette keyboard, {clean_t}, {random.choice(styles)}, dark background"
        encoded = urllib.parse.quote(prompt)
        url = f"https://image.pollinations.ai/prompt/{encoded}?width=1280&height=720&nologo=true&seed={random.randint(0,99999)}"
        
        async with session.get(url, timeout=IMAGE_TIMEOUT) as resp:
            if resp.status == 200:
                data = await resp.read()
                if len(data) > 5000:
                    path = os.path.join(CACHE_DIR, f"img_{int(time.time())}.jpg")
                    with open(path, "wb") as f: f.write(data)
                    logger.info(f"   🖼 Image saved: {len(data)} bytes")
                    return path
    except Exception as e:
        logger.warning(f"   ⚠️ Image generation failed: {e}")
    return None

# ============ КЛАССЫ ============

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

# ============ STATE ============

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
            text = clean_text(entry.get("summary", "") or entry.get("description", ""))
            
            if passes_local_filters(title, text):
                items.append(NewsItem("news", title, text, link, source['name'], uid))
    except Exception as e:
        logger.warning(f"⚠️ RSS fetch error ({source['name']}): {e}")
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
                ts = await asyncio.to_thread(lambda: YouTubeTranscriptApi.list_transcripts(vid).find_transcript(['en', 'ru']).fetch())
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
    logger.info("🚀 Starting (Western Sources → Russian Posts)")
    
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_rss(s, session) for s in RSS_SOURCES] + [fetch_youtube(c, session) for c in YOUTUBE_CHANNELS]
        results = await asyncio.gather(*tasks)
        all_items = [i for r in results for i in r]
        
        logger.info(f"📦 Found {len(all_items)} items")
        random.shuffle(all_items)
        
        posts_done = 0
        MAX_POSTS_PER_RUN = 1
        
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
    # ============ ЗАПАДНЫЕ ИСТОЧНИКИ (ENGLISH) ============
    RSS_SOURCES = [
        # Топовые англоязычные источники по кибербезопасности
        {"name": "BleepingComputer", "url": "https://www.bleepingcomputer.com/feed/"},
        {"name": "The Hacker News", "url": "https://feeds.feedburner.com/TheHackersNews"},
        {"name": "Krebs on Security", "url": "https://krebsonsecurity.com/feed/"},
        {"name": "Dark Reading", "url": "https://www.darkreading.com/rss_simple.asp"},
        {"name": "Threatpost", "url": "https://threatpost.com/feed/"},
        {"name": "Ars Technica Security", "url": "https://arstechnica.com/tag/security/feed/"},
        {"name": "SecurityWeek", "url": "https://www.securityweek.com/feed/"},
    ]
    
    YOUTUBE_CHANNELS = [
        # Топовые англоязычные каналы про хакинг/безопасность
        {"name": "John Hammond", "id": "UCVeW9qkBjo3zosnqUbG7CFw"},
        {"name": "NetworkChuck", "id": "UC9x0AN7BWHpXyPic4IQC74Q"},
        {"name": "LiveOverflow", "id": "UClcE-kVhqyiHCcjYwcpfj9w"},
        {"name": "IppSec", "id": "UCa6eh7gCkpPo5XXUDfygQQA"},
        {"name": "STÖK", "id": "UCQN2DsjnYH60SFBIA6IkNwg"},
    ]
    
    asyncio.run(main())

