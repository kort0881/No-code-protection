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

# ============ ФИЛЬТРЫ ============

# Стоп-слова для английских источников
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
    "устранять уязвимости", "злоумышленниками для атак", "соблюдайте осторожность",
    "базовые правила", "кибергигиен", "не открывайте подозрительные",
    "используйте надежн", "регулярное резервное", "обучение сотрудников",
    "повышение осведомленности", "комплексный подход", "многоуровневая защита"
]

# СИЛЬНЫЕ технические индикаторы (конкретика)
STRONG_TECH_INDICATORS = [
    "cve-", "0day", "zero-day", "exploit", "payload", "backdoor", "trojan",
    "ransomware", "apt28", "apt29", "lazarus", "sandworm", "fancy bear",
    "cozy bear", "killnet", "lockbit", "blackcat", "alphv", "conti",
    ".exe", ".dll", ".apk", ".ps1", ".bat", ".sh", ".vbs",
    "powershell", "mimikatz", "cobalt strike", "metasploit", "nmap",
    "c2 server", "c&c", "command and control", "reverse shell",
    "sql injection", "xss", "csrf", "rce", "lpe", "privilege escalation",
    "buffer overflow", "heap spray", "use-after-free", "race condition",
    "порт 445", "порт 3389", "порт 22", "порт 80", "порт 443",
    "smb", "rdp", "ssh", "ftp", "telnet", "vnc",
    "lateral movement", "persistence", "exfiltration", "c2 beacon"
]

# Обычные технические термины (для подсчёта)
TECH_INDICATORS = [
    "cve-", "0day", "exploit", "payload", "shell", "sudo", "root",
    "dns", "ssh", "rdp", "smb", "api", "token", "hash", "aes", "rsa", "tls", "ssl",
    "android", "ios", "windows", "linux", "macos",
    "chrome", "firefox", "safari", "edge", "telegram", "whatsapp",
    "apt", "phishing", "malware", "ransomware", "backdoor", "trojan",
    "вредонос", "эксплойт", "уязвимост", "фишинг", "хакер", "взлом",
    "утечк", "брешь", "патч", "обновлени"
]


def is_too_generic(text: str) -> bool:
    """Улучшенная проверка сгенерированного РУССКОГО поста на банальности"""
    text_lower = text.lower()
    
    # 1. Проверка банальных фраз (порог 2+)
    banned_count = sum(1 for phrase in BANNED_PHRASES if phrase in text_lower)
    if banned_count >= 2:
        logger.info(f"⚠️ Too many generic phrases: {banned_count}")
        return True
    
    # 2. Проверка на СИЛЬНЫЕ технические индикаторы
    strong_tech = sum(1 for t in STRONG_TECH_INDICATORS if t in text_lower)
    
    # 3. Проверка на конкретные версии/номера/CVE
    has_version = bool(re.search(r'\d+\.\d+\.\d+', text))  # Версия типа 1.2.3
    has_cve = bool(re.search(r'CVE-\d{4}-\d+', text, re.I))  # CVE-2024-12345
    has_port = bool(re.search(r'порт\s*\d+', text_lower))  # порт 445
    has_ip = bool(re.search(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', text))  # IP адрес
    has_path = bool(re.search(r'[A-Z]:\\|/etc/|/var/|/tmp/', text))  # Путь к файлу
    has_command = bool(re.search(r'(sudo|chmod|chown|netsh|reg add|powershell|cmd)', text_lower))  # Команды
    has_hash = bool(re.search(r'[a-f0-9]{32,64}', text_lower))  # MD5/SHA хеш
    
    specifics_count = sum([has_version, has_cve, has_port, has_ip, has_path, has_command, has_hash])
    
    # Если нет ни одного конкретного индикатора
    if specifics_count == 0 and strong_tech < 2:
        logger.info(f"⚠️ No specific details (versions/CVE/ports/paths): strong_tech={strong_tech}")
        return True
    
    # Если есть 1 банальная фраза, требуем больше конкретики
    if banned_count == 1 and specifics_count == 0 and strong_tech < 3:
        logger.info(f"⚠️ Has generic phrase but lacks specifics")
        return True
    
    # 4. Общие технические термины (мягкая проверка)
    tech_count = sum(1 for term in TECH_INDICATORS if term in text_lower)
    if tech_count < 2:
        logger.info(f"⚠️ Not enough technical terms: {tech_count}/2")
        return True
    
    # 5. Проверка на избыток советов-списков
    lines = [l for l in text.split('\n') if l.strip()]
    advice_lines = [l for l in lines if l.strip().startswith(('•', '-', '✓', '—', '–'))]
    if len(advice_lines) >= 4 and len(lines) > 0:
        if len(advice_lines) / len(lines) > 0.5:
            # Проверяем, есть ли в советах конкретика
            advice_text = ' '.join(advice_lines).lower()
            advice_has_specifics = (
                bool(re.search(r'\d+\.\d+', advice_text)) or
                bool(re.search(r'cve-', advice_text)) or
                bool(re.search(r'порт\s*\d+', advice_text)) or
                any(t in advice_text for t in STRONG_TECH_INDICATORS[:20])
            )
            if not advice_has_specifics:
                logger.info(f"⚠️ Too many generic tips without specifics: {len(advice_lines)} lines")
                return True
    
    # 6. Проверка минимальной длины полезного контента
    # Убираем эмодзи, пробелы, ссылки
    clean_text = re.sub(r'[^\w\s]', '', text)
    clean_text = re.sub(r'\s+', ' ', clean_text).strip()
    words = clean_text.split()
    if len(words) < 30:
        logger.info(f"⚠️ Post too short: {len(words)} words")
        return True
    
    logger.info(f"✅ Post passed quality check: {specifics_count} specifics, {strong_tech} strong terms, {banned_count} banned phrases")
    return False


def passes_local_filters(title: str, text: str) -> bool:
    """Фильтрация АНГЛИЙСКОГО исходника"""
    content = (title + " " + text).lower()
    
    # Проверка стоп-слов
    if any(w in content for w in STOP_WORDS):
        logger.info(f"🚫 Stop word found: {title[:50]}...")
        return False
    
    # Минимальная длина
    if len(text) < 100:
        return False
    
    # Требуем наличие security-ключевых слов в английском тексте
    security_keywords = [
        "vulnerability", "exploit", "malware", "ransomware", "phishing",
        "hacker", "breach", "attack", "threat", "zero-day", "patch",
        "security", "cybersecurity", "cyber attack", "data breach",
        "cve-", "backdoor", "trojan", "apt", "intrusion", "compromise"
    ]
    if not any(kw in content for kw in security_keywords):
        logger.info(f"🚫 No security keywords: {title[:50]}...")
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
            logger.info(f"🔄 Local duplicate: {new_title[:50]}...")
            return True
            
    history = "\n".join(f"- {t}" for t in recent[-10:])
    prompt = f"Темы:\n{history}\n\nНовая: '{new_title}'\nДубликат? YES/NO"
    ans, _ = await call_groq(prompt, "light", 10)
    
    return "YES" in ans.upper()


async def generate_post(item) -> Optional[str]:
    prompt = f"""Ты — редактор русскоязычного Telegram-канала про кибербезопасность (30к+ подписчиков).

ИСХОДНАЯ НОВОСТЬ (English):
Заголовок: {item.title}
Текст: {item.text[:2500]}

ТВОЯ ЗАДАЧА:
1. Прочитай английский текст и переведи суть на РУССКИЙ язык
2. Напиши пост на РУССКОМ с конкретными техническими деталями

СТРОГИЕ ПРАВИЛА:
❌ ЗАПРЕЩЕНО писать банальности:
   - "регулярно обновляйте ПО" (без указания конкретной версии)
   - "используйте антивирус", "будьте осторожны"
   - "надежные пароли", "двухфакторная аутентификация"
   - "системы обнаружения", "мониторинг трафика"
   - "защитить свои данные", "соблюдайте кибергигиену"
   - "обучение сотрудников", "повышение осведомленности"

✅ ОБЯЗАТЕЛЬНО включи КОНКРЕТИКУ:
   - CVE-номера уязвимостей (CVE-2024-XXXXX)
   - Точные версии софта (Chrome 131.0.6778.264, Windows 11 23H2)
   - Номера портов (порт 445, порт 3389)
   - Пути к файлам (C:\\Windows\\Temp\\evil.dll, /etc/passwd)
   - Команды для проверки/защиты (netsh, powershell, grep)
   - Названия малвари/групп (LockBit, APT29, Cobalt Strike)
   - IP-адреса или домены (если есть в источнике)
   - Хеши файлов (MD5/SHA256, если есть)

📌 Если в источнике НЕТ технических деталей — пиши SKIP

ФОРМАТ ПОСТА (на РУССКОМ):
🔥 [Конкретный заголовок с версией/CVE/названием угрозы]

[2-3 предложения: ЧТО произошло + технические детали атаки]

👇 ЧТО СДЕЛАТЬ:
• [Конкретное действие с версией/командой/путём]
• [Ещё одно конкретное действие]

ПРИМЕРЫ ХОРОШЕГО ПОСТА:
✅ "Обновите Chrome до 131.0.6778.108 — исправлена CVE-2024-12692 (RCE через V8)"
✅ "Заблокируйте порт 445: netsh advfirewall firewall add rule name='Block SMB' dir=in action=block protocol=TCP localport=445"
✅ "Проверьте наличие C:\\Windows\\Temp\\svchost.exe (не путать с системным)"
✅ "APT29 использует Cobalt Strike с C2 на домене evil.example[.]com"

ПРИМЕРЫ ПЛОХОГО (НЕ ПИСАТЬ ТАК):
❌ "Используйте надежные решения для мониторинга сети"
❌ "Регулярно обновляйте программное обеспечение"  
❌ "Будьте бдительны при переходе по ссылкам"
❌ "Проводите обучение сотрудников по кибербезопасности"

Напиши пост на РУССКОМ языке или SKIP:"""

    text, _ = await call_groq(prompt, "heavy", 1200)
    
    if not text or "SKIP" in text.upper() or len(text) < 120:
        logger.info("⏩ AI returned SKIP or too short")
        return None
    
    # Проверка на банальности
    if is_too_generic(text):
        logger.info(f"⏩ Post rejected: too generic")
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
        
        logger.info(f"📦 Found {len(all_items)} items after local filters")
        random.shuffle(all_items)
        
        posts_done = 0
        posts_rejected = 0
        MAX_POSTS_PER_RUN = 1
        
        for item in all_items:
            if posts_done >= MAX_POSTS_PER_RUN:
                break
            
            if not budget.can_use_model("light"):
                logger.warning("⚠️ Daily budget exhausted")
                break
            
            logger.info(f"🔍 Analyzing: {item.title[:60]}...")
            
            if await check_duplicate(item.title, state.data["recent_titles"]):
                state.mark_posted(item.uid, item.title)
                continue
            
            post_text = await generate_post(item)
            if not post_text:
                state.mark_posted(item.uid, item.title)
                posts_rejected += 1
                logger.info(f"⏩ Rejected ({posts_rejected} total)")
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

        logger.info(f"📊 Summary: {posts_done} posted, {posts_rejected} rejected as generic")

    await bot.session.close()


if __name__ == "__main__":
    # ============ ЗАПАДНЫЕ ИСТОЧНИКИ (ENGLISH) ============
    RSS_SOURCES = [
        {"name": "BleepingComputer", "url": "https://www.bleepingcomputer.com/feed/"},
        {"name": "The Hacker News", "url": "https://feeds.feedburner.com/TheHackersNews"},
        {"name": "Krebs on Security", "url": "https://krebsonsecurity.com/feed/"},
        {"name": "Dark Reading", "url": "https://www.darkreading.com/rss_simple.asp"},
        {"name": "Threatpost", "url": "https://threatpost.com/feed/"},
        {"name": "Ars Technica Security", "url": "https://arstechnica.com/tag/security/feed/"},
        {"name": "SecurityWeek", "url": "https://www.securityweek.com/feed/"},
    ]
    
    YOUTUBE_CHANNELS = [
        {"name": "John Hammond", "id": "UCVeW9qkBjo3zosnqUbG7CFw"},
        {"name": "NetworkChuck", "id": "UC9x0AN7BWHpXyPic4IQC74Q"},
        {"name": "LiveOverflow", "id": "UClcE-kVhqyiHCcjYwcpfj9w"},
        {"name": "IppSec", "id": "UCa6eh7gCkpPo5XXUDfygQQA"},
        {"name": "STÖK", "id": "UCQN2DsjnYH60SFBIA6IkNwg"},
    ]
    
    asyncio.run(main())
