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

TEXT_ONLY_THRESHOLD = 3000          # было 700
MAX_POSTED_IDS = 500
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=25)
IMAGE_TIMEOUT = aiohttp.ClientTimeout(total=40)

RECENT_POSTS_CHECK = 10
RECENT_SIMILARITY_THRESHOLD = 0.40
MIN_TOPIC_DIVERSITY = 3

# ============ МОДЕЛИ GROQ ============
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
        remaining = cfg.daily_tokens - used
        if remaining < cfg.daily_tokens * 0.1:
            logger.warning(f"⚠️ Low daily tokens for {model_key}: {remaining} left")
        return remaining > (cfg.daily_tokens * 0.05)
    
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

# ============ РАСШИРЕННЫЙ СПИСОК БАНАЛЬНОСТЕЙ ============
BANNED_PHRASES = [
    # Базовые
    "из доверенных источников", "регулярно обновляйте", "будьте бдительны",
    "используйте антивирус", "надёжный пароль", "надежный пароль",
    "будьте осторожны", "проверяйте ссылки", "сложные пароли",
    "защитить свои данные", "потенциальных атак", "соблюдайте осторожность",
    "базовые правила", "кибергигиен", "используйте надежн",
    "регулярное резервное", "обучение сотрудников", "повышение осведомленности",
    "комплексный подход", "многоуровневая защита", "своевременно устанавливайте",
    "будьте внимательны", "не открывайте подозрительные", "проявляйте осторожность",
    # Антивирус
    "обновить сигнатуры", "обновите сигнатуры", "обновление сигнатур",
    "антивирусное ПО", "антивирус", "антивируса", "антивирусные",
    "включить детекцию", "включите детекцию", "включить обнаружение",
    "сканировать систему", "провести сканирование", "полное сканирование",
    # Общие
    "рекомендуется обновить", "следует обновить", "необходимо обновить",
    "выпустила исправление", "выпустила патч", "выпустила обновление",
    "установите последнее обновление", "обновитесь до последней версии",
    "своевременно устанавливайте обновления", "не откладывайте обновления",
    "принять меры", "предпринять шаги", "обеспечить безопасность",
    # Резервное копирование
    "делайте резервные копии", "создавайте бэкапы", "резервное копирование",
    # MFA/2FA
    "включите двухфакторную аутентификацию", "включите 2fa", "используйте двухфакторную",
    # Сеть
    "используйте vpn", "подключайтесь через vpn", "настройте firewall",
    # Пароли
    "меняйте пароли", "используйте уникальные пароли", "парольный менеджер",
    # Дополнительные (новые)
    "регулярно обновляйте программное обеспечение", "используйте лицензионное ПО",
    "не скачивайте файлы из непроверенных источников", "проверяйте цифровые подписи",
    "настройте брандмауэр", "ограничьте права пользователей", "применяйте принцип наименьших привилегий",
    "сегментируйте сеть", "используйте списки контроля доступа",
]

BANNED_ADVICE_PATTERNS = [
    r'обнови(те|ть)?\s+(сигнатуры|антивирус|защитник|базы)',
    r'включи(те|ть)?\s+(детекцию|обнаружение|защиту|функцию)',
    r'установи(те|ть)?\s+(последнее|новое|свежее)\s+(обновление|патч|исправление)',
    r'обнови(те|ться)?\s+до\s+последней\s+версии',
    r'сканируй(те|ть)?\s+(систему|устройство)',
    r'проведи(те|ть)?\s+(сканирование|проверку|аудит)',
    r'используй(те|ть)?\s+(антивирус|защитник|защитное\s+ПО)',
    r'будь(те)?\s+(осторожны|бдительны|внимательны)',
    r'проявляй(те)?\s+(осторожность|бдительность)',
    r'проверяй(те|ть)?\s+(ссылки|вложения|письма)',
    r'не\s+(открывай(те)?|кликай(те)?)\s+подозрительные',
    r'создавай(те|ть)?\s+(резервные\s+копии|бэкапы)',
    r'делай(те)?\s+резервные\s+копии',
    r'включи(те|ть)?\s+(2fa|mfa|двухфакторную)',
    r'используй(те|ть)?\s+(vpn|файрвол|межсетевой)',
    r'регулярно\s+(обновляй(те)?|меняй(те)?|проверяй(те)?)',
    r'своевременно\s+(устанавливай(те)?|обновляй(те)?)',
    r'приня(ть|тие)\s+(меры|мер|действия|шаги)',
    r'обеспеч(ите|ить)\s+безопасность',
    r'повыс(ите|ить)\s+(уровень\s+)?безопасности',
    r'усил(ите|ить)\s+(защиту|безопасность)',
    r'след(ует|ить)\s+(за\s+)?обновлениями',
    r'монитор(ьте|инг)\s+(трафик|события|логи|активность)',
    r'обуч(ите|айте)\s+сотрудников',
    r'повы(сьте|шайте)\s+осведомленность',
    r'соблюдай(те)?\s+(правила|меры)',
    r'комплексн(ый|ого|ая)\s+(подход|меры|защита)',
    r'многоуровнев(ая|ую|ой)\s+защита',
    r'кибергигиен(а|ы|е|у)',
]

SPECIFIC_INDICATORS = [
    r'CVE-\d{4}-\d+',
    r'\d+\.\d+\.\d+[\.\d+]*',
    r'порт[ы]?\s*\d+',
    r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}',
    r'[a-f0-9]{32,64}',
    r'0x[a-f0-9]+',
    r'\b[A-Z]:\\',
    r'/etc/|/var/|/tmp/|/usr/|/opt/',
    r'\.[exe|dll|apk|ps1|bat|sh|vbs|msi|doc|docx|pdf|zip|rar]{3,4}\b',
    r'\b(powershell|cmd|bash|python|curl|wget|netsh|reg\s+add|chmod|chown|sudo|netstat|tasklist|sc\s+query)\b',
    r'\b(smb|rdp|ssh|ldap|kerberos|http|https|ftp|smtp|dns|vpn|ipsec|ssl|tls)\b',
    r'\b(sql\s*injection|sqli|xss|csrf|ssrf|rce|lpe|rop|heap\s+spray|uaf|use.after.free)\b',
    r'\b(mimikatz|cobalt\s*strike|metasploit|burp|nmap|wireshark|volatility|yara|sigma)\b',
    r'\b(ioc|indicator\s+of\s+compromise|ttp|ttps|mitre\s+att&ck|cvss|epss)\b',
    r'https?://[^\s]+',
]

STRONG_TECH_INDICATORS = [
    "cve-", "0day", "exploit", "payload", "backdoor", "trojan",
    "ransomware", "apt28", "lazarus", "lockbit", "blackcat",
    ".exe", ".dll", ".ps1", "powershell", "mimikatz", "cobalt strike",
    "reverse shell", "sql injection", "sqli", "rce", "lpe",
    "lateral movement", "persistence", "yara", "sigma rule",
]

TECH_INDICATORS = [
    "cve", "vulnerability", "exploit", "malware", "ransomware",
    "backdoor", "trojan", "botnet", "apt", "zero-day", "patch",
    "breach", "leak", "hack", "attack", "compromise",
    "windows", "linux", "android", "microsoft", "google",
    "уязвимост", "вредонос", "эксплойт", "фишинг", "хакер", "атак",
]

STOP_WORDS = [
    "headphone", "earbuds", "airpods", "bluetooth speaker", "jbl",
    "bose", "sony wh-", "beats", "sennheiser", "noise canceling",
    "audio quality", "phone review", "camera review", "unboxing",
    "quarterly earnings", "appointed ceo", "stock price", "bitcoin price",
    "casino", "gambling", "weight loss", "free iphone", "work from home",
]

SECURITY_KEYWORDS = [
    "vulnerability", "exploit", "malware", "ransomware", "phishing",
    "breach", "leak", "hack", "attack", "patch", "cve", "0day",
    "apt", "threat actor", "lockbit", "blackcat", "alert", "ioc",
]

# ============ ФИЛЬТРЫ ============
def passes_local_filters(title: str, text: str) -> bool:
    content = (title + " " + text).lower()
    title_lower = title.lower()
    for stop in STOP_WORDS:
        if stop in content:
            return False
    has_security = any(kw in content for kw in SECURITY_KEYWORDS)
    if not has_security:
        return False
    if len(text) < 50:
        return False
    return True

def count_specific_indicators(text: str) -> int:
    count = 0
    text_lower = text.lower()
    for pattern in SPECIFIC_INDICATORS:
        count += len(re.findall(pattern, text_lower, re.IGNORECASE))
    for ind in STRONG_TECH_INDICATORS:
        if ind in text_lower:
            count += 2
    return count

def has_banned_advice(text: str) -> tuple[bool, list]:
    text_lower = text.lower()
    found = []
    for phrase in BANNED_PHRASES:
        if phrase in text_lower:
            found.append(phrase)
    for pattern in BANNED_ADVICE_PATTERNS:
        matches = re.findall(pattern, text_lower)
        found.extend(matches)
    return len(found) > 0, list(set(found))

def extract_advice_section(text: str) -> str:
    patterns = [
        r'👇\s*Что делать[:：]?(.*?)(?:\n\n|$)',
        r'👇\s*Рекомендации[:：]?(.*?)(?:\n\n|$)',
        r'🔧\s*Что делать[:：]?(.*?)(?:\n\n|$)',
        r'✅\s*Рекомендации[:：]?(.*?)(?:\n\n|$)',
        r'📌\s*Меры[:：]?(.*?)(?:\n\n|$)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip()
    lines = text.split('\n')
    advice = []
    for line in reversed(lines):
        if '•' in line or '-' in line or '*' in line:
            advice.insert(0, line)
        elif advice:
            break
    return '\n'.join(advice)

def is_too_generic(text: str) -> bool:
    text_lower = text.lower()
    banned_count = sum(1 for phrase in BANNED_PHRASES if phrase in text_lower)
    has_banned_patterns, _ = has_banned_advice(text)
    specific_count = count_specific_indicators(text)
    strong_tech = sum(1 for t in STRONG_TECH_INDICATORS if t in text_lower)
    advice = extract_advice_section(text)
    advice_banal, _ = has_banned_advice(advice) if advice else (False, [])
    if banned_count >= 2:
        return True
    if advice and advice_banal and count_specific_indicators(advice) < 2:
        return True
    if specific_count == 0 and strong_tech < 1:
        return True
    tech_count = sum(1 for term in TECH_INDICATORS if term in text_lower)
    if tech_count < 2:
        return True
    words = re.sub(r'[^\w\s]', '', text).split()
    if len(words) < 25:
        return True
    if banned_count >= 1 and specific_count < 2 and strong_tech < 2:
        return True
    return False

def clean_banal_advice(text: str) -> str:
    lines = text.split('\n')
    cleaned = []
    for line in lines:
        low = line.lower()
        if any(phrase in low for phrase in BANNED_PHRASES):
            continue
        if any(re.search(p, low) for p in BANNED_ADVICE_PATTERNS):
            continue
        cleaned.append(line)
    return '\n'.join(cleaned)

def post_quality_score(text: str) -> float:
    """0..1, 1 - максимально технический пост"""
    score = 0.0
    cves = re.findall(r'CVE-\d{4}-\d+', text, re.I)
    score += min(len(cves) * 0.2, 0.6)
    ips = re.findall(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', text)
    score += min(len(ips) * 0.15, 0.5)
    hashes = re.findall(r'[a-fA-F0-9]{32,64}', text)
    score += min(len(hashes) * 0.2, 0.6)
    commands = re.findall(r'(?i)(powershell|cmd|reg add|sc config|netsh|wmic|gpupdate)', text)
    score += min(len(commands) * 0.1, 0.4)
    paths = re.findall(r'[A-Z]:\\[^\s]+|/etc/[^\s]+|/var/[^\s]+', text)
    score += min(len(paths) * 0.1, 0.3)
    ports = re.findall(r'порт[а-я]*\s*\d+', text, re.I)
    score += min(len(ports) * 0.1, 0.3)
    if len(text) > 2000:
        score += 0.2
    elif len(text) > 1000:
        score += 0.1
    return min(score, 1.0)

def smart_trim(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    for sep in ['. ', '! ', '? ', '\n\n', '\n', ' ']:
        pos = text.rfind(sep, 0, max_len)
        if pos != -1:
            end = pos + (len(sep) if sep != ' ' else 0)
            return text[:end].strip()
    return text[:max_len].rsplit(' ', 1)[0].strip()

# ============ СОСТОЯНИЕ И ДУБЛИКАТЫ ============
def normalize_title(title: str) -> str:
    title = title.lower()
    title = re.sub(r'[^\w\s]', '', title)
    stop = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'in', 'on', 'at', 'to', 'for', 'of', 'and', 'or', 'new', 'how'}
    words = [w for w in title.split() if w not in stop and len(w) > 2]
    return ' '.join(words)

def extract_key_entities(text: str) -> set:
    entities = set()
    cves = re.findall(r'CVE-\d{4}-\d+', text, re.I)
    entities.update(cve.upper() for cve in cves)
    malware = re.findall(r'\b([A-Z][a-z]+(?:Bot|Locker|Ware|Lock|Cat|Bear|Worm)?)\b', text)
    entities.update(m.lower() for m in malware if len(m) > 3)
    known = ['lockbit', 'blackcat', 'lazarus', 'apt28', 'sandworm', 'emotet', 'trickbot', 'cobalt strike']
    text_lower = text.lower()
    for k in known:
        if k in text_lower:
            entities.add(k)
    companies = ['microsoft', 'google', 'apple', 'cisco', 'fortinet', 'vmware', 'windows', 'linux']
    for c in companies:
        if c in text_lower:
            entities.add(c)
    return entities

def detect_topic(title: str, text: str) -> str:
    content = (title + " " + text).lower()
    if any(x in content for x in ['ransomware', 'lockbit', 'blackcat']):
        return 'ransomware'
    if any(x in content for x in ['apt', 'lazarus', 'apt28', 'sandworm']):
        return 'apt'
    if re.search(r'cve-\d{4}-\d+', content):
        return 'vulnerability'
    if any(x in content for x in ['phishing', 'social engineering']):
        return 'phishing'
    if any(x in content for x in ['ddos', 'botnet']):
        return 'ddos'
    if any(x in content for x in ['breach', 'leak', 'exposed']):
        return 'breach'
    if any(x in content for x in ['patch', 'update']):
        return 'patch'
    return 'general'

def calculate_similarity(title1, text1, title2, text2) -> float:
    norm1 = normalize_title(title1)
    norm2 = normalize_title(title2)
    title_sim = SequenceMatcher(None, norm1, norm2).ratio()
    entities1 = extract_key_entities(title1 + " " + text1)
    entities2 = extract_key_entities(title2 + " " + text2)
    if entities1 and entities2:
        inter = len(entities1 & entities2)
        union = len(entities1 | entities2)
        entity_sim = inter / union if union else 0
        cve1 = {e for e in entities1 if e.startswith('CVE-')}
        cve2 = {e for e in entities2 if e.startswith('CVE-')}
        if cve1 and cve2 and cve1 & cve2:
            return 1.0
    else:
        entity_sim = 0
    text_sim = SequenceMatcher(None, text1[:500].lower(), text2[:500].lower()).ratio()
    return title_sim * 0.5 + entity_sim * 0.35 + text_sim * 0.15

class State:
    def __init__(self):
        self.data = {
            "posted_ids": {},
            "recent_titles": [],
            "recent_posts": [],
            "recent_topics": []
        }
        self._load()
    
    def _load(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    self.data.update(loaded)
                    if "recent_posts" not in self.data:
                        self.data["recent_posts"] = []
                    if "recent_topics" not in self.data:
                        self.data["recent_topics"] = []
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
        norm_new = normalize_title(title)
        # по заголовкам
        for old_title in self.data["recent_titles"][-30:]:
            if SequenceMatcher(None, norm_new, normalize_title(old_title)).ratio() > 0.65:
                return True
        # по CVE
        cve_new = set(re.findall(r'CVE-\d{4}-\d+', text, re.I))
        for old in self.data["recent_posts"][-20:]:
            cve_old = set(re.findall(r'CVE-\d{4}-\d+', old.get("text",""), re.I))
            if cve_new & cve_old:
                return True
        # по контенту
        for old in self.data["recent_posts"][-20:]:
            if calculate_similarity(title, text, old.get("title",""), old.get("text","")) > 0.5:
                return True
        return False
    
    def is_too_similar_to_recent(self, title: str, text: str) -> bool:
        recent = self.data["recent_posts"][-RECENT_POSTS_CHECK:]
        if len(recent) < 2:
            return False
        new_entities = extract_key_entities(title + " " + text)
        new_topic = detect_topic(title, text)
        for post in recent:
            old_title = post.get("title","")
            old_text = post.get("text","")
            old_topic = post.get("topic","general")
            if SequenceMatcher(None, normalize_title(title), normalize_title(old_title)).ratio() > RECENT_SIMILARITY_THRESHOLD:
                return True
            if new_topic == old_topic and new_topic != 'general':
                if len(new_entities & extract_key_entities(old_title + " " + old_text)) >= 2:
                    return True
            if calculate_similarity(title, text, old_title, old_text) > RECENT_SIMILARITY_THRESHOLD:
                return True
        return False
    
    def get_recent_topics_stats(self) -> dict:
        stats = {}
        for topic in self.data["recent_topics"][-10:]:
            stats[topic] = stats.get(topic, 0) + 1
        return stats
    
    def needs_diversity(self) -> str:
        if len(self.data["recent_topics"]) < 5:
            return ""
        last5 = self.data["recent_topics"][-5:]
        stats = {}
        for t in last5:
            stats[t] = stats.get(t, 0) + 1
        for topic, cnt in stats.items():
            if cnt >= 3:
                return topic
        return ""
    
    def mark_posted(self, uid: str, title: str, text: str = "", topic: str = "general"):
        if len(self.data["posted_ids"]) > MAX_POSTED_IDS:
            self.data["posted_ids"] = dict(sorted(self.data["posted_ids"].items(), key=lambda x: x[1])[-400:])
        self.data["posted_ids"][uid] = int(time.time())
        self.data["recent_titles"].append(title)
        if len(self.data["recent_titles"]) > 50:
            self.data["recent_titles"] = self.data["recent_titles"][-50:]
        self.data["recent_posts"].append({"title": title, "text": text[:1000], "topic": topic, "time": int(time.time())})
        if len(self.data["recent_posts"]) > 30:
            self.data["recent_posts"] = self.data["recent_posts"][-30:]
        self.data["recent_topics"].append(topic)
        if len(self.data["recent_topics"]) > 15:
            self.data["recent_topics"] = self.data["recent_topics"][-15:]
        self.save()

state = State()

# ============ GROQ С ПОВТОРАМИ ============
groq_client = Groq(api_key=GROQ_API_KEY)

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

async def call_groq_with_retry(prompt: str, model_pref: str, max_tokens: int, retries: int = 2) -> tuple[str, int]:
    for attempt in range(retries):
        res, tokens = await call_groq(prompt, model_pref, max_tokens)
        if res:
            return res, tokens
        wait = 2 ** attempt * 5
        logger.warning(f"Retry {attempt+1}/{retries} in {wait}s")
        await asyncio.sleep(wait)
    return "", 0

# ============ ЗАГРУЗКА ПОЛНОГО ТЕКСТА ============
async def fetch_full_article(url: str, session: aiohttp.ClientSession) -> str:
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
    except:
        return ""

# ============ ГЕНЕРАЦИЯ ПОСТА (НОВЫЙ ПРОМПТ) ============
async def generate_post(item, session: aiohttp.ClientSession) -> Optional[str]:
    full_text = item.text
    if len(item.text) < 500:
        extra = await fetch_full_article(item.link, session)
        if extra:
            full_text = item.text + " " + extra
            logger.info(f"   📄 +{len(extra)} chars")

    prompt = f"""You are a senior editor for a Russian-language Telegram cybersecurity channel (30k+ subscribers).
Your readers are SOC analysts, pentesters, sysadmins, DevSecOps engineers. They demand DEPTH, not platitudes.

SOURCE (English):
Title: {item.title}
Text: {full_text[:3500]}

TASK: Write a detailed post in RUSSIAN. Target length: 2500–3800 characters (Telegram limit is ~4096).

════════════════════════════════════
❌ INSTANTLY REJECTED — do not write:
════════════════════════════════════
• «Обновите антивирус» / «Используйте антивирус» — бесполезно
• «Установите последнее обновление» — без версии и CVE это мусор
• «Будьте бдительны» / «Проявляйте осторожность» — пустышка
• «Включите 2FA» / «Используйте VPN» / «Делайте бэкапы» — банально
• «Повышайте осведомлённость сотрудников» — вы не корпоративный тренинг
• Любой совет без конкретного инструмента, команды, пути, порта или хэша

════════════════════════════════════
✅ ФОРМАТ ПОСТА (строго соблюдать):
════════════════════════════════════

🔥 [Заголовок: название CVE или малвари + суть угрозы, макс. 80 символов]

[БЛОК 1 — ЧТО ПРОИЗОШЛО: 3–5 предложений]
Конкретика: кто атакует, какой вектор, какие системы затронуты, масштаб.
Примеры хорошего стиля:
  «Группа Lazarus эксплуатирует CVE-2024-21338 (CVSS 8.8) в драйвере appid.sys Windows — позволяет повысить привилегии до SYSTEM через гонку условий в функции NtQuerySystemInformation.»
  «LockBit 3.0 распространяется через брутфорс RDP (порт 3389) и фишинговые .lnk-файлы, маскированные под PDF. После закрепления — выгружает EDR через Process Hollowing в explorer.exe.»

[БЛОК 2 — КАК РАБОТАЕТ АТАКА: 3–5 предложений]
Технический разбор механизма. Цепочка: начальный вектор → закрепление → действия.
Упоминайте: конкретные техники (MITRE ATT&CK если уместно), инструменты (Cobalt Strike, Mimikatz и т.д.), порты, пути реестра, процессы, Event ID.
Примеры:
  «Эксплойт записывает шеллкод по адресу 0x7ffe0300, обходя KASLR через утечку адреса ядра из EPROCESS.»
  «После запуска PowerShell-загрузчик скачивает второй стейдж с C2 185.220.101.42:8443, сохраняет в %APPDATA%\\Microsoft\\Windows\\svchost32.exe и прописывается в HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run.»

[БЛОК 3 — IOC / ТЕХНИЧЕСКИЕ ДЕТАЛИ: список]
Если есть — обязательно включить:
  • Хэши файлов (MD5/SHA256)
  • IP/домены C2
  • Пути файлов или ключи реестра
  • User-Agent или сетевые сигнатуры

[БЛОК 4 — ЧТО ДЕЛАТЬ: минимум 4 пункта с техническими деталями]
Каждый пункт — конкретное действие с инструментом/командой/путём.
НЕ «обновите», а КАК обновить и ЧТО именно.
НЕ «проверьте логи», а какой Event ID, в каком журнале, какой фильтр.

Примеры ХОРОШИХ пунктов:
  • Заблокируйте исходящие соединения с рабочих станций на порт 8443/TCP через GPO → Computer Configuration → Windows Settings → Security Settings → Windows Firewall → Outbound Rules → New Rule → Port → 8443
  • Проверьте Event ID 4688 (создание процесса) в Security.evtx на наличие цепочки: wscript.exe → powershell.exe с параметром -enc или -nop — признак загрузчика
  • Добавьте в SIEM/EDR правило: процесс с именем svchost32.exe (обратите внимание — не svchost.exe) = немедленный алерт
  • Если используете Microsoft Defender: Get-MpThreat | Where-Object {{$_.ThreatName -like '*Lazarus*'}} — проверка активных детектов
  • Для Splunk: index=wineventlog EventCode=4624 LogonType=10 | where src_ip!="10.0.0.0/8" — детект нелегитимного RDP
  • Временное смягчение до патча: reg add "HKLM\\SYSTEM\\CurrentControlSet\\Services\\AppID" /v Start /t REG_DWORD /d 4 /f — отключение уязвимого сервиса
  • Для Linux: grep -r 'svchost32\|185\.220\.101' /var/log/syslog /var/log/auth.log — поиск IOC в логах
  • Используйте YARA-правило: strings: $a = "NtQuerySystemInformation" $b = {{48 8B 05 ?? ?? ?? ??}} condition: all of them — для поиска на хостах

════════════════════════════════════
ДОПОЛНИТЕЛЬНЫЕ ПРАВИЛА:
════════════════════════════════════
• Если есть CVE — всегда указывай CVSS score и affected versions
• Если нет IOC — пиши «IOC на момент публикации не раскрыты, следим за обновлениями»
• Пиши уверенно, без воды. Каждое предложение несёт ценность.
• Не пиши «важно», «следует отметить», «стоит обратить внимание» — это вода
• Telegram не поддерживает Markdown-таблицы — используй только текст и символы

Если источника недостаточно для технического поста — ответь только словом SKIP.

Пиши на русском:"""

    model_choice = "heavy" if len(full_text) > 1500 else "light"
    max_tokens = 3200  # для получения 3500-4000 символов
    text, _ = await call_groq_with_retry(prompt, model_choice, max_tokens, retries=2)
    
    if not text:
        return None
    
    text_clean = text.strip()
    if text_clean.upper() == "SKIP" or text_clean.upper().startswith("SKIP"):
        logger.info("⏩ AI: SKIP")
        return None
    
    if len(text) < 150:
        logger.info(f"⏩ Too short: {len(text)}")
        return None
    
    # Удаление банальностей
    if is_too_generic(text):
        cleaned = clean_banal_advice(text)
        if len(cleaned) < len(text) * 0.7 or is_too_generic(cleaned):
            logger.info("⏩ Rejected: generic")
            return None
        text = cleaned
        logger.info("🧹 Cleaned from banalities")
    
    # Оценка качества
    quality = post_quality_score(text)
    if quality < 0.4:
        logger.info(f"⏩ Low quality score: {quality:.2f}")
        return None
    logger.info(f"📊 Quality score: {quality:.2f}")
    
    # Добавление ссылки и обрезка до лимита Telegram
    source_suffix = f"\n\n🔗 <a href='{item.link}'>Источник</a>"
    max_len = 4096 - len(source_suffix)
    if len(text) > max_len:
        text = smart_trim(text, max_len)
        logger.info(f"✂️ Trimmed to {len(text)} chars")
    
    return text + source_suffix

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
                    logger.info(f"   🖼 Image: {len(data)//1024}KB")
                    return path
    except:
        pass
    return None

# ============ КЛАССЫ ДАННЫХ ============
@dataclass
class NewsItem:
    type: Literal["news", "video"]
    title: str
    text: str
    link: str
    source: str
    uid: str

bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

# ============ СБОР RSS И YOUTUBE ============
async def fetch_rss(source: dict, session: aiohttp.ClientSession) -> list:
    items = []
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; KiberSOSBot/1.0)"}
        async with session.get(source['url'], timeout=HTTP_TIMEOUT, headers=headers) as resp:
            if resp.status != 200:
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
                    items.append(NewsItem("video", entry.title, full[:5000], entry.link, f"YT:{channel['name']}", uid))
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

# ============ ОСНОВНОЙ ЦИКЛ ============
async def main():
    logger.info("🚀 Starting KiberSOS v3.0 (Enhanced, long posts, no generic advice)")
    
    async with aiohttp.ClientSession() as session:
        logger.info("📡 Fetching sources...")
        tasks = [fetch_rss(s, session) for s in RSS_SOURCES] + [fetch_youtube(c, session) for c in YOUTUBE_CHANNELS]
        results = await asyncio.gather(*tasks)
        all_items = [i for r in results for i in r]
        logger.info(f"📦 Total after filters: {len(all_items)}")
        
        if not all_items:
            logger.info("No items passed filters")
            await bot.session.close()
            return
        
        # Разнообразие тем
        dominant = state.needs_diversity()
        if dominant:
            other = [i for i in all_items if detect_topic(i.title, i.text) != dominant]
            same = [i for i in all_items if detect_topic(i.title, i.text) == dominant]
            all_items = other + same
            logger.info(f"⚖️ Reordered: {len(other)} other topics first")
        else:
            random.shuffle(all_items)
        
        posts_done = 0
        max_posts = 1
        attempts = 0
        max_attempts = 15
        
        for item in all_items:
            if posts_done >= max_posts or attempts >= max_attempts:
                break
            if not budget.can_use_model("light"):
                logger.warning("⚠️ Budget exhausted")
                break
            attempts += 1
            logger.info(f"🔍 [{attempts}/{max_attempts}] {item.source}: {item.title[:50]}...")
            
            if state.is_duplicate(item.title, item.text):
                state.mark_posted(item.uid, item.title, item.text, detect_topic(item.title, item.text))
                continue
            if state.is_too_similar_to_recent(item.title, item.text):
                state.mark_posted(item.uid, item.title, item.text, detect_topic(item.title, item.text))
                continue
            
            post_text = await generate_post(item, session)
            if not post_text:
                state.mark_posted(item.uid, item.title, item.text, detect_topic(item.title, item.text))
                continue
            
            try:
                if len(post_text) > TEXT_ONLY_THRESHOLD:
                    await bot.send_message(CHANNEL_ID, text=post_text)
                else:
                    img = await generate_image(item.title, session)
                    if img:
                        await bot.send_photo(CHANNEL_ID, photo=FSInputFile(img), caption=post_text)
                        try: os.remove(img)
                        except: pass
                    else:
                        await bot.send_message(CHANNEL_ID, text=post_text)
                logger.info("✅ Posted!")
                state.mark_posted(item.uid, item.title, item.text, detect_topic(item.title, item.text))
                posts_done += 1
            except Exception as e:
                logger.error(f"Telegram error: {e}")
        
        stats = state.get_recent_topics_stats()
        if stats:
            logger.info(f"📈 Recent topics: {stats}")
    
    await bot.session.close()

# ============ ИСТОЧНИКИ ============
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
    {"name": "CyberScoop", "url": "https://www.cyberscoop.com/feed/"},
    {"name": "HackRead", "url": "https://www.hackread.com/feed/"},
    {"name": "InfoSecurity Magazine", "url": "https://www.infosecurity-magazine.com/rss/news/"},
    {"name": "ZDNet Security", "url": "https://www.zdnet.com/topic/security/rss.xml"},
    {"name": "Malwarebytes Labs", "url": "https://blog.malwarebytes.com/feed/"},
    {"name": "RecordedFuture", "url": "https://www.recordedfuture.com/feed"},
    {"name": "Kaspersky", "url": "https://www.kaspersky.com/blog/feed/"},
    {"name": "Cisco Talos", "url": "https://blog.talosintelligence.com/feeds/posts/default"},
    {"name": "Unit42", "url": "https://unit42.paloaltonetworks.com/feed/"},
    {"name": "CERT-EU", "url": "https://cert.europa.eu/blog/atom.xml"},
]

YOUTUBE_CHANNELS = [
    {"name": "JohnHammond", "id": "UCVeW9qkBjo3zosnqUbG7CFw"},
    {"name": "NetworkChuck", "id": "UC9x0AN7BWHpXyPic4IQC74Q"},
    {"name": "LiveOverflow", "id": "UClcE-kVhqyiHCcjYwcpfj9w"},
]

if __name__ == "__main__":
    asyncio.run(main())
