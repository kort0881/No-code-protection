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
from groq import Groq

# ============ ЛОГИРОВАНИЕ ============

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("KiberSOS")
logging.getLogger("httpx").setLevel(logging.WARNING)

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
MAX_POSTED_IDS = 500
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=25)
IMAGE_TIMEOUT = aiohttp.ClientTimeout(total=40)

# ============ МОДЕЛИ ============

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
                        logger.info("🔄 New day — reset Groq limits")
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
        except:
            pass
    
    def add_tokens(self, model: str, tokens: int):
        self.data["daily_tokens"][model] = self.data["daily_tokens"].get(model, 0) + tokens
        self.save()
    
    def can_use_model(self, model_key: str) -> bool:
        if model_key not in MODELS:
            return False
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
            logger.info(f"⏳ RPM limit ({model_key}). Waiting {wait:.1f}s")
            await asyncio.sleep(wait)
            self.data["minute_start"][model] = time.time()
            self.data["request_count"][model] = 0
        
        last = self.data["last_request_time"].get(model, 0)
        if now - last < 2:
            await asyncio.sleep(2)
        
        self.data["request_count"][model] = self.data["request_count"].get(model, 0) + 1
        self.data["last_request_time"][model] = time.time()


budget = GroqBudget()

# ============ ФИЛЬТРЫ (ЖЁСТКИЙ АНТИОФФТОП) ============

STOP_WORDS = [
    # === ГАДЖЕТЫ/АУДИО ===
    "headphone", "headphones", "earbuds", "earbud", "airpods", "airpod",
    "earphone", "earphones", "headset review", "audio review",
    "bluetooth speaker", "wireless speaker", "portable speaker",
    "jbl ", "jbl charge", "jbl flip", "bose ", "bose quietcomfort",
    "sony wh-", "sony wf-", "beats ", "beats studio", "sennheiser",
    "noise canceling", "noise cancelling", "noise-canceling",
    "audio quality", "sound quality", "bass quality", "music quality",
    "best headphones", "headphone review", "earbuds review",
    "wireless earbuds", "true wireless", "anc headphones",
    "audio gear", "listening experience", "hi-fi", "hi-res audio",
    
    # === СМАРТФОНЫ/ГАДЖЕТЫ ===
    "phone review", "smartphone review", "tablet review",
    "camera review", "lens review", "best phone", "phone comparison",
    "unboxing", "hands-on review", "first impressions",
    "battery life test", "screen quality", "display review",
    
    # === БИЗНЕС-НОВОСТИ ===
    "quarterly earnings", "quarterly results", "fiscal quarter", "fiscal year",
    "appointed ceo", "new ceo", "steps down as", "resigns as ceo",
    "marketing campaign", "brand ambassador", "product launch event",
    "ipo filing", "stock price", "shares rose", "shares fell", "market cap",
    "investor relations", "shareholder", "dividend",
    
    # === КРИПТО/ФИНАНСЫ ===
    "bitcoin price", "ethereum price", "crypto trading", "nft trading",
    "forex trading", "investment advice", "trading strategy",
    "casino", "gambling", "betting", "poker", "slots",
    "price prediction", "bull run", "bear market",
    
    # === СПАМ ===
    "weight loss", "diet pill", "supplement", "miracle cure",
    "free iphone", "you won", "congratulations you", "claim your prize",
    "work from home", "make money fast", "passive income",
    
    # === РАЗВЛЕЧЕНИЯ ===
    "game review", "movie review", "album review", "book review",
    "netflix series", "streaming service", "spotify playlist",
    "box office", "entertainment news", "celebrity",
    
    # === LIFESTYLE ===
    "travel guide", "hotel review", "restaurant review",
    "fashion", "beauty", "skincare", "makeup",
]

GADGET_PATTERNS = [
    r'\b(headphone|headphones|earphone|earphones|earbud|earbuds|airpod|airpods)\b',
    r'\b(bluetooth|wireless)\s*(headphone|earphone|earbud|speaker|headset)',
    r'\bjbl\b', r'\bbose\b', r'\bsony\s*(wh|wf)-', r'\bbeats\b', r'\bsennheiser\b',
    r'\b(best|top|review|rating).{0,30}(headphone|earphone|audio|speaker)',
    r'\b(noise.cancell?ing|anc)\b',
    r'\baudio\s*(quality|review|gear|test)\b',
]

SECURITY_KEYWORDS = [
    "vulnerability", "vulnerabilities", "vulnerable", "exploit", "exploited", "exploitation",
    "malware", "ransomware", "phishing", "spyware", "adware", "rootkit", "keylogger",
    "trojan", "backdoor", "botnet", "ddos", "dos attack", "worm",
    "zero-day", "0-day", "0day", "zeroday",
    "breach", "breached", "data breach", "security breach",
    "leak", "leaked", "leaks", "data leak",
    "hack", "hacked", "hacker", "hackers", "hacking",
    "attack", "attacked", "attacker", "attackers", "cyber attack", "cyberattack",
    "compromise", "compromised", "intrusion", "unauthorized access",
    "incident", "security incident",
    "patch", "patches", "patched", "patching",
    "security update", "security patch", "emergency patch",
    "cve-", "security flaw", "security bug", "security hole", "security issue",
    "security advisory", "security bulletin", "critical update",
    "security fix", "hotfix",
    "apt", "threat actor", "threat group", "nation-state", "state-sponsored",
    "lazarus", "apt28", "apt29", "apt27", "apt41",
    "fancy bear", "cozy bear", "sandworm", "turla",
    "killnet", "lockbit", "blackcat", "alphv", "clop", "revil", "conti",
    "darkside", "ragnar", "maze", "ryuk", "emotet", "trickbot",
    "rce", "remote code execution",
    "privilege escalation", "lpe", "local privilege",
    "sql injection", "sqli",
    "xss", "cross-site scripting",
    "csrf", "ssrf",
    "authentication bypass", "auth bypass",
    "buffer overflow", "memory corruption", "use-after-free",
    "code injection", "command injection",
    "cybersecurity", "cyber security", "infosec", "information security",
    "security researcher", "security team", "security firm",
    "threat intelligence", "threat hunting",
    "malicious", "suspicious", "infected", "payload",
    "command and control", "c2 server", "c&c",
    "ioc", "indicator of compromise",
    "forensic", "incident response",
]

BANNED_PHRASES = [
    "из доверенных источников", "регулярно обновляйте", "будьте бдительны",
    "используйте антивирус", "надёжный пароль", "надежный пароль",
    "будьте осторожны", "проверяйте ссылки", "сложные пароли",
    "надежные решения", "системы обнаружения", "мониторинг трафика",
    "защитить свои данные", "потенциальных атак", "соблюдайте осторожность",
    "базовые правила", "кибергигиен", "используйте надежн",
    "регулярное резервное", "обучение сотрудников", "повышение осведомленности",
    "комплексный подход", "многоуровневая защита", "своевременно устанавливайте",
    "будьте внимательны", "не открывайте подозрительные", "проявляйте осторожность",
]

STRONG_TECH_INDICATORS = [
    "cve-", "0day", "zero-day", "exploit", "payload", "backdoor", "trojan",
    "ransomware", "apt28", "apt29", "lazarus", "sandworm", "fancy bear",
    "lockbit", "blackcat", "alphv", "conti", "revil", "clop",
    ".exe", ".dll", ".apk", ".ps1", ".bat", ".sh", ".vbs", ".msi",
    "powershell", "mimikatz", "cobalt strike", "metasploit",
    "c2 server", "c&c", "reverse shell", "webshell",
    "sql injection", "sqli", "xss", "csrf", "rce", "lpe", "ssrf",
    "buffer overflow", "heap spray", "use-after-free",
    "smb", "rdp", "ssh", "ldap", "kerberos",
    "lateral movement", "persistence", "exfiltration",
    "ioc", "indicator of compromise", "yara", "sigma rule",
]

TECH_INDICATORS = [
    "cve", "vulnerability", "exploit", "malware", "ransomware", "phishing",
    "backdoor", "trojan", "botnet", "ddos", "apt", "threat actor",
    "zero-day", "patch", "update", "security", "breach", "leak",
    "hack", "attack", "compromise", "infected", "payload",
    "windows", "linux", "android", "ios", "chrome", "firefox",
    "microsoft", "google", "apple", "cisco", "fortinet", "palo alto",
    "уязвимост", "вредонос", "эксплойт", "фишинг", "хакер", "взлом",
    "утечк", "атак", "патч", "брешь", "малвар", "ботнет",
]


def passes_local_filters(title: str, text: str) -> bool:
    """Строгая фильтрация: пропускаем ТОЛЬКО security-контент"""
    content = (title + " " + text).lower()
    title_lower = title.lower()
    
    for stop in STOP_WORDS:
        if stop in content:
            logger.info(f"🚫 STOP [{stop}]: {title[:50]}...")
            return False
    
    for pattern in GADGET_PATTERNS:
        if re.search(pattern, title_lower):
            logger.info(f"🚫 GADGET pattern: {title[:50]}...")
            return False
    
    for pattern in GADGET_PATTERNS:
        if re.search(pattern, content):
            security_context = any(kw in content for kw in [
                "vulnerability", "exploit", "attack", "hack", "breach",
                "malware", "security flaw", "cve-", "patch"
            ])
            if not security_context:
                logger.info(f"🚫 GADGET (no security): {title[:50]}...")
                return False
    
    has_security = any(kw in content for kw in SECURITY_KEYWORDS)
    if not has_security:
        logger.info(f"🚫 No security keywords: {title[:50]}...")
        return False
    
    if len(text) < 50:
        return False
    
    return True


def is_too_generic(text: str) -> bool:
    """Проверка русского поста на банальности"""
    text_lower = text.lower()
    
    banned_count = sum(1 for phrase in BANNED_PHRASES if phrase in text_lower)
    if banned_count >= 2:
        logger.info(f"⚠️ Too many generic phrases: {banned_count}")
        return True
    
    strong_tech = sum(1 for t in STRONG_TECH_INDICATORS if t in text_lower)
    
    has_version = bool(re.search(r'\d+\.\d+\.\d+', text))
    has_cve = bool(re.search(r'CVE-\d{4}-\d+', text, re.I))
    has_port = bool(re.search(r'порт\s*\d+', text_lower))
    has_ip = bool(re.search(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', text))
    has_path = bool(re.search(r'[A-Z]:\\|/etc/|/var/|/tmp/|/usr/', text))
    has_command = bool(re.search(r'(sudo|chmod|chown|netsh|reg add|powershell|cmd|curl|wget)', text_lower))
    has_hash = bool(re.search(r'\b[a-f0-9]{32,64}\b', text_lower))
    has_domain = bool(re.search(r'\[?\.\]?(com|net|org|ru|io)\b', text_lower))
    
    specifics_count = sum([has_version, has_cve, has_port, has_ip, has_path, has_command, has_hash, has_domain])
    
    if specifics_count == 0 and strong_tech < 1:
        logger.info(f"⚠️ No specifics, no strong tech")
        return True
    
    if banned_count >= 1 and specifics_count == 0 and strong_tech < 2:
        logger.info(f"⚠️ Generic + no specifics")
        return True
    
    tech_count = sum(1 for term in TECH_INDICATORS if term in text_lower)
    if tech_count < 2:
        logger.info(f"⚠️ Few tech terms: {tech_count}/2")
        return True
    
    words = re.sub(r'[^\w\s]', '', text).split()
    if len(words) < 25:
        logger.info(f"⚠️ Too short: {len(words)} words")
        return True
    
    logger.info(f"✅ Quality OK: {specifics_count} specifics, {strong_tech} strong")
    return False


# ============ СИСТЕМА ПРОВЕРКИ ДУБЛИКАТОВ ============

def normalize_title(title: str) -> str:
    """Нормализация заголовка для сравнения"""
    title = title.lower()
    # Убираем всё кроме букв и цифр
    title = re.sub(r'[^\w\s]', '', title)
    # Убираем стоп-слова
    stop = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'in', 'on', 'at', 'to', 'for', 'of', 'and', 'or', 'new', 'how'}
    words = [w for w in title.split() if w not in stop and len(w) > 2]
    return ' '.join(words)


def extract_key_entities(text: str) -> set:
    """Извлекаем ключевые сущности из текста"""
    entities = set()
    
    # CVE номера
    cves = re.findall(r'CVE-\d{4}-\d+', text, re.I)
    entities.update(cve.upper() for cve in cves)
    
    # Названия малвари/групп (с большой буквы)
    malware_names = re.findall(r'\b([A-Z][a-z]+(?:Bot|Locker|Ware|Lock|Cat|Bear|Worm)?)\b', text)
    entities.update(m.lower() for m in malware_names if len(m) > 3)
    
    # Известные группы/малварь
    known = [
        'lockbit', 'blackcat', 'alphv', 'clop', 'revil', 'conti', 'darkside',
        'lazarus', 'apt28', 'apt29', 'sandworm', 'fancy bear', 'cozy bear',
        'emotet', 'trickbot', 'ryuk', 'maze', 'ragnar', 'qbot', 'qakbot',
        'cobalt strike', 'mimikatz', 'metasploit'
    ]
    text_lower = text.lower()
    for k in known:
        if k in text_lower:
            entities.add(k)
    
    # Компании/продукты
    companies = ['microsoft', 'google', 'apple', 'cisco', 'fortinet', 'palo alto',
                 'vmware', 'citrix', 'adobe', 'oracle', 'sap', 'salesforce',
                 'chrome', 'firefox', 'edge', 'windows', 'linux', 'android', 'ios']
    for c in companies:
        if c in text_lower:
            entities.add(c)
    
    return entities


def calculate_similarity(title1: str, text1: str, title2: str, text2: str) -> float:
    """Комплексная проверка схожести двух новостей"""
    
    # 1. Схожесть заголовков (нормализованных)
    norm1 = normalize_title(title1)
    norm2 = normalize_title(title2)
    title_sim = SequenceMatcher(None, norm1, norm2).ratio()
    
    # 2. Пересечение ключевых сущностей
    entities1 = extract_key_entities(title1 + " " + text1)
    entities2 = extract_key_entities(title2 + " " + text2)
    
    if entities1 and entities2:
        intersection = entities1 & entities2
        union = entities1 | entities2
        entity_sim = len(intersection) / len(union) if union else 0
        
        # Если совпадают CVE — это точно дубль
        cve1 = {e for e in entities1 if e.startswith('CVE-')}
        cve2 = {e for e in entities2 if e.startswith('CVE-')}
        if cve1 and cve2 and cve1 & cve2:
            logger.info(f"   🔴 Same CVE detected: {cve1 & cve2}")
            return 1.0
    else:
        entity_sim = 0
    
    # 3. Схожесть текста (первые 500 символов)
    text_sim = SequenceMatcher(None, text1[:500].lower(), text2[:500].lower()).ratio()
    
    # Взвешенная оценка
    final_score = (title_sim * 0.5) + (entity_sim * 0.35) + (text_sim * 0.15)
    
    return final_score


class State:
    def __init__(self):
        self.data = {
            "posted_ids": {},
            "recent_titles": [],
            "recent_posts": []  # Новое: храним заголовок + текст
        }
        self._load()
    
    def _load(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    self.data.update(loaded)
                    # Миграция: если нет recent_posts
                    if "recent_posts" not in self.data:
                        self.data["recent_posts"] = []
            except:
                pass
    
    def save(self):
        fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, suffix='.json')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False)
            shutil.move(tmp, STATE_FILE)
        except:
            try:
                os.unlink(tmp)
            except:
                pass
    
    def is_posted(self, uid):
        return uid in self.data["posted_ids"]
    
    def is_duplicate(self, title: str, text: str) -> bool:
        """Проверка на дубликат с учётом контента"""
        
        # 1. Быстрая проверка по заголовкам
        norm_new = normalize_title(title)
        for old_title in self.data["recent_titles"][-30:]:
            norm_old = normalize_title(old_title)
            if SequenceMatcher(None, norm_new, norm_old).ratio() > 0.65:
                logger.info(f"🔄 Title duplicate: {title[:50]}...")
                return True
        
        # 2. Глубокая проверка с контентом
        for old_post in self.data["recent_posts"][-20:]:
            old_title = old_post.get("title", "")
            old_text = old_post.get("text", "")
            
            similarity = calculate_similarity(title, text, old_title, old_text)
            
            if similarity > 0.5:
                logger.info(f"🔄 Content duplicate ({similarity:.2f}): {title[:50]}...")
                return True
        
        return False
    
    def mark_posted(self, uid: str, title: str, text: str = ""):
        # Ограничиваем размер posted_ids
        if len(self.data["posted_ids"]) > MAX_POSTED_IDS:
            self.data["posted_ids"] = dict(sorted(
                self.data["posted_ids"].items(),
                key=lambda x: x[1]
            )[-400:])
        
        self.data["posted_ids"][uid] = int(time.time())
        
        # Сохраняем заголовки
        self.data["recent_titles"].append(title)
        if len(self.data["recent_titles"]) > 50:
            self.data["recent_titles"] = self.data["recent_titles"][-50:]
        
        # Сохраняем заголовок + текст для глубокой проверки
        self.data["recent_posts"].append({
            "title": title,
            "text": text[:1000],  # Первые 1000 символов
            "time": int(time.time())
        })
        if len(self.data["recent_posts"]) > 30:
            self.data["recent_posts"] = self.data["recent_posts"][-30:]
        
        self.save()


state = State()


# ============ GROQ CALLER ============

async def call_groq(prompt: str, model_pref: str = "heavy", max_tokens: int = 1500) -> tuple[str, int]:
    order = ["heavy", "light", "fallback"] if model_pref == "heavy" else ["light", "fallback", "heavy"]
    
    for key in order:
        if not budget.can_use_model(key):
            continue
        cfg = MODELS[key]
        
        try:
            await budget.wait_for_rate_limit(key)
            response = await asyncio.to_thread(
                lambda: groq_client.chat.completions.create(
                    model=cfg.name,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=max_tokens,
                    temperature=0.7
                )
            )
            res = response.choices[0].message.content.strip()
            tokens = response.usage.total_tokens if response.usage else 0
            budget.add_tokens(cfg.name, tokens)
            logger.info(f"✅ Model: {key} ({tokens} tok)")
            return res, tokens
            
        except Exception as e:
            logger.warning(f"⚠️ {key} error: {e}")
            await asyncio.sleep(5)
            continue
    
    return "", 0


# ============ ЗАГРУЗКА ПОЛНОГО ТЕКСТА ============

async def fetch_full_article(url: str, session: aiohttp.ClientSession) -> str:
    """Получаем больше текста со страницы"""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with session.get(url, timeout=HTTP_TIMEOUT, headers=headers) as resp:
            if resp.status != 200:
                return ""
            html_text = await resp.text()
            
            paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', html_text, re.DOTALL | re.IGNORECASE)
            text = ' '.join(paragraphs)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = html.unescape(text)
            text = re.sub(r'\s+', ' ', text).strip()
            
            return text[:4000]
    except Exception as e:
        logger.debug(f"Fetch error: {e}")
        return ""


# ============ ГЕНЕРАЦИЯ ПОСТА ============

async def generate_post(item, session: aiohttp.ClientSession) -> Optional[str]:
    full_text = item.text
    if len(item.text) < 500:
        extra = await fetch_full_article(item.link, session)
        if extra:
            full_text = item.text + " " + extra
            logger.info(f"   📄 +{len(extra)} chars")
    
    prompt = f"""You are an editor for a Russian-language Telegram channel about cybersecurity (30k+ subscribers).

SOURCE (English):
Title: {item.title}
Text: {full_text[:3500]}

TASK: Write a post in RUSSIAN with SPECIFIC technical details.

RULES:
❌ NO generic advice: "update software", "use antivirus", "be careful", "strong passwords"
✅ INCLUDE: CVE numbers, software versions, malware names, file paths, ports, commands, IOCs

📌 If source lacks details - write SHORT post (3-4 sentences) about what happened. NO padding.
📌 If NOT about security - respond: SKIP

FORMAT (Russian):
🔥 [Конкретный заголовок]

[2-4 предложения с техническими деталями]

👇 Что делать:
• [Конкретное действие]

Write in RUSSIAN or SKIP:"""

    text, _ = await call_groq(prompt, "heavy", 1000)
    
    if not text:
        return None
    
    text_clean = text.strip()
    if text_clean.upper() == "SKIP" or (text_clean.upper().startswith("SKIP") and len(text_clean) < 20):
        logger.info("⏩ AI: SKIP")
        return None
    
    if len(text) < 80:
        logger.info(f"⏩ Too short: {len(text)}")
        return None
    
    if is_too_generic(text):
        logger.info("⏩ Too generic")
        return None
    
    return text + f"\n\n🔗 <a href='{item.link}'>Источник</a>"


# ============ ИЗОБРАЖЕНИЯ ============

async def generate_image(title: str, session: aiohttp.ClientSession) -> Optional[str]:
    try:
        styles = ["cyberpunk neon", "matrix code rain", "hacker aesthetic", "dark tech noir"]
        clean_t = re.sub(r'[^a-zA-Z0-9\s]', '', title)[:35]
        prompt = f"hacker silhouette computer, {clean_t}, {random.choice(styles)}, dark background, cinematic"
        encoded = urllib.parse.quote(prompt)
        url = f"https://image.pollinations.ai/prompt/{encoded}?width=1280&height=720&nologo=true&seed={random.randint(0, 99999)}"
        
        async with session.get(url, timeout=IMAGE_TIMEOUT) as resp:
            if resp.status == 200:
                data = await resp.read()
                if len(data) > 5000:
                    path = os.path.join(CACHE_DIR, f"img_{int(time.time())}.jpg")
                    with open(path, "wb") as f:
                        f.write(data)
                    logger.info(f"   🖼 Image: {len(data) // 1024}KB")
                    return path
    except Exception as e:
        logger.debug(f"Image error: {e}")
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


# ============ СБОРЩИКИ ============

async def fetch_rss(source: dict, session: aiohttp.ClientSession) -> list:
    items = []
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; KiberSOSBot/1.0)"}
        async with session.get(source['url'], timeout=HTTP_TIMEOUT, headers=headers) as resp:
            if resp.status != 200:
                logger.warning(f"   {source['name']}: HTTP {resp.status}")
                return []
            text = await resp.text()
        
        feed = feedparser.parse(text)
        count = len(feed.entries)
        passed = 0
        
        for entry in feed.entries[:10]:
            link = entry.get('link')
            if not link:
                continue
            
            uid = hashlib.md5(link.encode()).hexdigest()
            if state.is_posted(uid):
                continue
            
            title = entry.get('title', '')
            summary = clean_text(entry.get("summary", "") or entry.get("description", ""))
            
            content = ""
            if hasattr(entry, 'content') and entry.content:
                content = clean_text(entry.content[0].get('value', ''))
            
            full_text = summary + " " + content
            
            if passes_local_filters(title, full_text):
                items.append(NewsItem("news", title, full_text, link, source['name'], uid))
                passed += 1
        
        logger.info(f"   {source['name']}: {passed}/{count} passed")
        
    except Exception as e:
        logger.warning(f"⚠️ RSS error ({source['name']}): {e}")
    return items


async def fetch_youtube(channel: dict, session: aiohttp.ClientSession) -> list:
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
            if not vid:
                continue
            uid = f"yt_{vid}"
            if state.is_posted(uid):
                continue
            
            try:
                ts = await asyncio.to_thread(
                    lambda v=vid: YouTubeTranscriptApi.list_transcripts(v)
                    .find_transcript(['en', 'ru']).fetch()
                )
                full = " ".join([t['text'] for t in ts])
                if passes_local_filters(entry.title, full):
                    items.append(NewsItem(
                        "video", entry.title, full[:5000],
                        entry.link, f"YT:{channel['name']}", uid
                    ))
            except:
                pass
    except:
        pass
    return items


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    return re.sub(r'\s+', ' ', text).strip()


# ============ MAIN ============

async def main():
    logger.info("🚀 Starting KiberSOS")
    
    async with aiohttp.ClientSession() as session:
        logger.info("📡 Fetching sources...")
        tasks = (
            [fetch_rss(s, session) for s in RSS_SOURCES] +
            [fetch_youtube(c, session) for c in YOUTUBE_CHANNELS]
        )
        results = await asyncio.gather(*tasks)
        all_items = [i for r in results for i in r]
        
        logger.info(f"📦 Total after filters: {len(all_items)}")
        
        if not all_items:
            logger.info("No items passed filters")
            await bot.session.close()
            return
        
        random.shuffle(all_items)
        
        posts_done = 0
        posts_rejected = 0
        duplicates_skipped = 0
        MAX_POSTS_PER_RUN = 1
        MAX_ATTEMPTS = 10
        attempts = 0
        
        for item in all_items:
            if posts_done >= MAX_POSTS_PER_RUN:
                break
            if attempts >= MAX_ATTEMPTS:
                logger.info("Max attempts reached")
                break
            
            if not budget.can_use_model("light"):
                logger.warning("⚠️ Budget exhausted")
                break
            
            attempts += 1
            logger.info(f"🔍 [{attempts}/{MAX_ATTEMPTS}] {item.source}: {item.title[:50]}...")
            
            # Проверка дубликатов (УСИЛЕННАЯ)
            if state.is_duplicate(item.title, item.text):
                state.mark_posted(item.uid, item.title, item.text)
                duplicates_skipped += 1
                continue
            
            post_text = await generate_post(item, session)
            if not post_text:
                state.mark_posted(item.uid, item.title, item.text)
                posts_rejected += 1
                continue
            
            # Публикация
            try:
                if len(post_text) > TEXT_ONLY_THRESHOLD:
                    await bot.send_message(CHANNEL_ID, text=post_text)
                else:
                    img = await generate_image(item.title, session)
                    if img:
                        await bot.send_photo(CHANNEL_ID, photo=FSInputFile(img), caption=post_text)
                        try:
                            os.remove(img)
                        except:
                            pass
                    else:
                        await bot.send_message(CHANNEL_ID, text=post_text)
                
                logger.info("✅ Posted!")
                state.mark_posted(item.uid, item.title, item.text)
                posts_done += 1
                
            except Exception as e:
                logger.error(f"Telegram error: {e}")
        
        logger.info(f"📊 Done: {posts_done} posted, {posts_rejected} rejected, {duplicates_skipped} duplicates")
    
    await bot.session.close()


if __name__ == "__main__":
    RSS_SOURCES = [
        {"name": "BleepingComputer", "url": "https://www.bleepingcomputer.com/feed/"},
        {"name": "TheHackerNews", "url": "https://feeds.feedburner.com/TheHackersNews"},
        {"name": "KrebsOnSecurity", "url": "https://krebsonsecurity.com/feed/"},
        {"name": "DarkReading", "url": "https://www.darkreading.com/rss.xml"},
        {"name": "SecurityWeek", "url": "https://www.securityweek.com/feed/"},
        {"name": "ThreatPost", "url": "https://threatpost.com/feed/"},
        {"name": "NakedSecurity", "url": "https://nakedsecurity.sophos.com/feed/"},
        {"name": "WeLiveSecurity", "url": "https://www.welivesecurity.com/en/rss/feed/"},
        {"name": "GrahamCluley", "url": "https://grahamcluley.com/feed/"},
        {"name": "Schneier", "url": "https://www.schneier.com/feed/"},
    ]
    
    YOUTUBE_CHANNELS = [
        {"name": "JohnHammond", "id": "UCVeW9qkBjo3zosnqUbG7CFw"},
        {"name": "NetworkChuck", "id": "UC9x0AN7BWHpXyPic4IQC74Q"},
        {"name": "LiveOverflow", "id": "UClcE-kVhqyiHCcjYwcpfj9w"},
    ]
    
    asyncio.run(main())
