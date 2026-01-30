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

# ============ УСИЛЕННАЯ ЗАЩИТА ОТ БАНАЛЬНОСТЕЙ ============

# === РАСШИРЕННЫЙ СПИСОК БАНАЛЬНОСТЕЙ ===
BANNED_PHRASES = [
    # Базовые банальности
    "из доверенных источников", "регулярно обновляйте", "будьте бдительны",
    "используйте антивирус", "надёжный пароль", "надежный пароль",
    "будьте осторожны", "проверяйте ссылки", "сложные пароли",
    "надежные решения", "системы обнаружения", "мониторинг трафика",
    "защитить свои данные", "потенциальных атак", "соблюдайте осторожность",
    "базовые правила", "кибергигиен", "используйте надежн",
    "регулярное резервное", "обучение сотрудников", "повышение осведомленности",
    "комплексный подход", "многоуровневая защита", "своевременно устанавливайте",
    "будьте внимательны", "не открывайте подозрительные", "проявляйте осторожность",
    
    # Антивирусные банальности
    "обновить сигнатуры", "обновите сигнатуры", "обновление сигнатур",
    "антивирусное ПО", "антивирус", "антивируса", "антивирусные",
    "включить детекцию", "включите детекцию", "включить обнаружение",
    "сканировать систему", "провести сканирование", "полное сканирование",
    
    # Общие рекомендации без ценности
    "рекомендуется обновить", "следует обновить", "необходимо обновить",
    "выпустила исправление", "выпустила патч", "выпустила обновление",
    "установите последнее обновление", "установите последний патч",
    "обновитесь до последней версии", "перейдите на последнюю версию",
    "своевременно устанавливайте обновления", "не откладывайте обновления",
    
    # Пустые призывы
    "осторожность", "бдительность", "внимательность",
    "проявляйте бдительность", "будьте начеку", "не теряйте бдительность",
    
    # Формальности без сути
    "принять меры", "принять необходимые меры", "предпринять шаги",
    "предпринять действия", "предпринять меры", "осуществить меры",
    "обеспечить безопасность", "повысить безопасность", "усилить безопасность",
    
    # Резервное копирование (если без конкретики)
    "делайте резервные копии", "создавайте бэкапы", "резервное копирование",
    "backup", "регулярно бэкапьте", "храните резервные копии",
    
    # MFA/2FA банальности
    "включите двухфакторную аутентификацию", "включите 2fa", "включите mfa",
    "используйте двухфакторную", "настройте двухфакторную",
    
    # Сетевые банальности
    "используйте vpn", "подключайтесь через vpn", "включите firewall",
    "настройте firewall", "используйте межсетевой экран",
    
    # Парольные банальности
    "меняйте пароли", "регулярно меняйте", "используйте уникальные пароли",
    "не используйте одинаковые пароли", "уникальный пароль для каждого",
    "парольный менеджер", "менеджер паролей", "используйте менеджер",
]

# === ЗАПРЕЩЕННЫЕ ШАБЛОНЫ РЕКОМЕНДАЦИЙ ===
BANNED_ADVICE_PATTERNS = [
    r'обнови(те|ть)?\s+(сигнатуры|антивирус|защитник|базы)',
    r'включи(те|ть)?\s+(детекцию|обнаружение|защиту|функцию)',
    r'установи(те|ть)?\s+(последнее|новое|свежее)\s+(обновление|патч|исправление)',
    r'обнови(те|ться|ться)?\s+до\s+последней\s+версии',
    r'сканируй(те|ть)?\s+(систему|устройство|компьютер)',
    r'проведи(те|ть)?\s+(сканирование|проверку|аудит)',
    r'используй(те|ть)?\s+(антивирус|защитник|защитное\s+ПО)',
    r'будь(те)?\s+(осторожны|бдительны|внимательны)',
    r'проявляй(те)?\s+(осторожность|бдительность|внимательность)',
    r'проверяй(те|ть)?\s+(ссылки|вложения|письма|файлы)',
    r'не\s+(открывай(те)?|кликай(те)?)\s+подозрительные',
    r'создавай(те|ть)?\s+(резервные\s+копии|бэкапы)',
    r'делай(те|ть)?\s+резервные\s+копии',
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
    r'соблюдай(те)?\s+(правила|меры|рекомендации)',
    r'комплексн(ый|ого|ая)\s+(подход|меры|защита)',
    r'многоуровнев(ая|ую|ой)\s+защита',
    r'кибергигиен(а|ы|е|у)',
]

# === СЛОВА-МАРКЕРЫ КОНКРЕТИКИ (хорошие признаки) ===
SPECIFIC_INDICATORS = [
    # Технические детали
    r'CVE-\d{4}-\d+',  # CVE номера
    r'\d+\.\d+\.\d+[\.\d+]*',  # Версии ПО
    r'порт[ы]?\s*\d+',  # Порты
    r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}',  # IP адреса
    r'[a-f0-9]{32,64}',  # Хэши
    r'0x[a-f0-9]+',  # Hex значения
    r'\b[A-Z]:\\',  # Windows пути
    r'/etc/|/var/|/tmp/|/usr/|/opt/',  # Linux пути
    r'\.[exe|dll|apk|ps1|bat|sh|vbs|msi|doc|docx|pdf|zip|rar]{3,4}\b',  # Расширения файлов
    
    # Команды
    r'\b(powershell|cmd|bash|python|curl|wget|netsh|reg\s+add|chmod|chown|sudo|netstat|tasklist|sc\s+query)\b',
    
    # Технологии/протоколы
    r'\b(smb|rdp|ssh|ldap|kerberos|http|https|ftp|smtp|dns|vpn|ipsec|ssl|tls)\b',
    
    # Методы атаки
    r'\b(sql\s*injection|sqli|xss|csrf|ssrf|rce|lpe|rop|heap\s+spray|uaf|use.after.free)\b',
    
    # Инструменты
    r'\b(mimikatz|cobalt\s*strike|metasploit|burp|nmap|wireshark|volatility|yara|sigma)\b',
    
    # Термины
    r'\b(ioc|indicator\s+of\s+compromise|ttp|ttps|mitre\s+att&ck|cvss|epss)\b',
    
    # Домены/URL
    r'\[?\.\]?(com|net|org|ru|io|info|biz|xyz)\b',
    r'https?://[^\s]+',
]

# === СИЛЬНЫЕ ТЕХНИЧЕСКИЕ ИНДИКАТОРЫ ===
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

# === ФИЛЬТРЫ КОНТЕНТА (без изменений) ===
STOP_WORDS = [
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
    "phone review", "smartphone review", "tablet review",
    "camera review", "lens review", "best phone", "phone comparison",
    "unboxing", "hands-on review", "first impressions",
    "battery life test", "screen quality", "display review",
    "quarterly earnings", "quarterly results", "fiscal quarter", "fiscal year",
    "appointed ceo", "new ceo", "steps down as", "resigns as ceo",
    "marketing campaign", "brand ambassador", "product launch event",
    "ipo filing", "stock price", "shares rose", "shares fell", "market cap",
    "investor relations", "shareholder", "dividend",
    "bitcoin price", "ethereum price", "crypto trading", "nft trading",
    "forex trading", "investment advice", "trading strategy",
    "casino", "gambling", "betting", "poker", "slots",
    "price prediction", "bull run", "bear market",
    "weight loss", "diet pill", "supplement", "miracle cure",
    "free iphone", "you won", "congratulations you", "claim your prize",
    "work from home", "make money fast", "passive income",
    "game review", "movie review", "album review", "book review",
    "netflix series", "streaming service", "spotify playlist",
    "box office", "entertainment news", "celebrity",
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
    "security flaw", "security bug", "security hole", "security issue",
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


def count_specific_indicators(text: str) -> int:
    """Подсчет конкретных технических индикаторов в тексте"""
    count = 0
    text_lower = text.lower()
    
    for pattern in SPECIFIC_INDICATORS:
        matches = re.findall(pattern, text_lower, re.IGNORECASE)
        count += len(matches)
    
    # Дополнительно считаем сильные индикаторы
    for indicator in STRONG_TECH_INDICATORS:
        if indicator in text_lower:
            count += 2  # Сильные индикаторы весят больше
    
    return count


def has_banned_advice(text: str) -> tuple[bool, list]:
    """Проверяет наличие банальных советов и возвращает найденные"""
    text_lower = text.lower()
    found = []
    
    # Проверяем по точным фразам
    for phrase in BANNED_PHRASES:
        if phrase in text_lower:
            found.append(phrase)
    
    # Проверяем по паттернам
    for pattern in BANNED_ADVICE_PATTERNS:
        matches = re.findall(pattern, text_lower)
        found.extend(matches)
    
    return len(found) > 0, list(set(found))


def extract_advice_section(text: str) -> str:
    """Извлекает раздел 'Что делать' из поста"""
    # Ищем раздел с рекомендациями
    patterns = [
        r'👇\s*Что делать[:：]?(.*?)(?:\n\n|$)',
        r'👇\s*Рекомендации[:：]?(.*?)(?:\n\n|$)',
        r'🔧\s*Что делать[:：]?(.*?)(?:\n\n|$)',
        r'✅\s*Рекомендации[:：]?(.*?)(?:\n\n|$)',
        r'📌\s*Меры[:：]?(.*?)(?:\n\n|$)',
        r'•\s*\[?Конкретное действие\]?',  # Если AI не справился
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip()
    
    # Если не нашли явный раздел, ищем список в конце
    lines = text.split('\n')
    advice_lines = []
    in_advice = False
    
    for line in reversed(lines):
        if '•' in line or '-' in line or '*' in line:
            advice_lines.insert(0, line)
            in_advice = True
        elif in_advice:
            break
    
    return '\n'.join(advice_lines)


def is_too_generic(text: str) -> bool:
    """Усиленная проверка на банальности"""
    text_lower = text.lower()
    
    # Подсчет банальных фраз
    banned_count = sum(1 for phrase in BANNED_PHRASES if phrase in text_lower)
    
    # Проверка паттернов банальных советов
    has_banned_patterns, banned_found = has_banned_advice(text)
    
    # Подсчет конкретики
    specific_count = count_specific_indicators(text)
    strong_tech = sum(1 for t in STRONG_TECH_INDICATORS if t in text_lower)
    
    # Проверяем раздел "Что делать" отдельно
    advice_section = extract_advice_section(text)
    advice_has_banality, advice_banned = has_banned_advice(advice_section) if advice_section else (False, [])
    
    # Логирование для отладки
    logger.info(f"   📊 Analysis: {banned_count} banned phrases, {specific_count} specifics, {strong_tech} strong tech")
    if advice_section:
        logger.info(f"   📋 Advice section: {len(advice_section)} chars, banalities: {len(advice_banned)}")
    
    # === ПРАВИЛА ОТКЛОНЕНИЯ ===
    
    # 1. Слишком много банальных фраз
    if banned_count >= 2:
        logger.info(f"⚠️ Too many generic phrases: {banned_count}")
        return True
    
    # 2. Раздел "Что делать" полностью банальный
    if advice_section and advice_has_banality and len(advice_banned) >= 1:
        # Проверяем, есть ли в совете хоть что-то конкретное
        advice_specifics = count_specific_indicators(advice_section)
        if advice_specifics < 2:
            logger.info(f"⚠️ Advice section is generic: {advice_banned}")
            return True
    
    # 3. Нет конкретики вообще
    if specific_count == 0 and strong_tech < 1:
        logger.info(f"⚠️ No specifics, no strong tech")
        return True
    
    # 4. Мало технических терминов
    tech_count = sum(1 for term in TECH_INDICATORS if term in text_lower)
    if tech_count < 2:
        logger.info(f"⚠️ Few tech terms: {tech_count}/2")
        return True
    
    # 5. Слишком короткий текст
    words = re.sub(r'[^\w\s]', '', text).split()
    if len(words) < 25:
        logger.info(f"⚠️ Too short: {len(words)} words")
        return True
    
    # 6. Банальность + отсутствие конкретики
    if banned_count >= 1 and specific_count < 2 and strong_tech < 2:
        logger.info(f"⚠️ Generic + no specifics")
        return True
    
    logger.info(f"✅ Quality OK: {specific_count} specifics, {strong_tech} strong, {banned_count} banned")
    return False


def clean_banal_advice(text: str) -> str:
    """Пытается очистить текст от банальных советов, сохраняя полезное"""
    lines = text.split('\n')
    cleaned_lines = []
    removed_count = 0
    
    for line in lines:
        line_lower = line.lower()
        
        # Пропускаем банальные строки
        is_banal = False
        
        # Проверяем по точным фразам
        for phrase in BANNED_PHRASES:
            if phrase in line_lower:
                is_banal = True
                break
        
        # Проверяем по паттернам
        if not is_banal:
            for pattern in BANNED_ADVICE_PATTERNS:
                if re.search(pattern, line_lower):
                    is_banal = True
                    break
        
        # Проверяем маркерные фразы без конкретики
        if not is_banal:
            vague_markers = [
                r'^\s*[•\-\*]\s*обновите',
                r'^\s*[•\-\*]\s*используйте\s+антивирус',
                r'^\s*[•\-\*]\s*будьте\s+(осторожны|бдительны)',
                r'^\s*[•\-\*]\s*проверяйте\s+ссылки',
                r'^\s*[•\-\*]\s*не\s+открывайте',
                r'^\s*[•\-\*]\s*делайте\s+резервные',
                r'^\s*[•\-\*]\s*включите\s+двухфакторную',
                r'^\s*[•\-\*]\s*используйте\s+(vpn|менеджер)',
            ]
            for marker in vague_markers:
                if re.search(marker, line_lower):
                    is_banal = True
                    break
        
        if is_banal:
            removed_count += 1
            logger.info(f"   🗑️ Removed banal line: {line[:50]}...")
        else:
            cleaned_lines.append(line)
    
    if removed_count > 0:
        logger.info(f"   🧹 Cleaned {removed_count} banal lines")
    
    return '\n'.join(cleaned_lines)


# ============ СИСТЕМА ПРОВЕРКИ ДУБЛИКАТОВ ============

def normalize_title(title: str) -> str:
    """Нормализация заголовка для сравнения"""
    title = title.lower()
    title = re.sub(r'[^\w\s]', '', title)
    stop = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'in', 'on', 'at', 'to', 'for', 'of', 'and', 'or', 'new', 'how'}
    words = [w for w in title.split() if w not in stop and len(w) > 2]
    return ' '.join(words)


def extract_key_entities(text: str) -> set:
    """Извлекаем ключевые сущности из текста"""
    entities = set()
    
    cves = re.findall(r'CVE-\d{4}-\d+', text, re.I)
    entities.update(cve.upper() for cve in cves)
    
    malware_names = re.findall(r'\b([A-Z][a-z]+(?:Bot|Locker|Ware|Lock|Cat|Bear|Worm)?)\b', text)
    entities.update(m.lower() for m in malware_names if len(m) > 3)
    
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
    
    companies = ['microsoft', 'google', 'apple', 'cisco', 'fortinet', 'palo alto',
                 'vmware', 'citrix', 'adobe', 'oracle', 'sap', 'salesforce',
                 'chrome', 'firefox', 'edge', 'windows', 'linux', 'android', 'ios']
    for c in companies:
        if c in text_lower:
            entities.add(c)
    
    return entities


def calculate_similarity(title1: str, text1: str, title2: str, text2: str) -> float:
    """Комплексная проверка схожести двух новостей"""
    
    norm1 = normalize_title(title1)
    norm2 = normalize_title(title2)
    title_sim = SequenceMatcher(None, norm1, norm2).ratio()
    
    entities1 = extract_key_entities(title1 + " " + text1)
    entities2 = extract_key_entities(title2 + " " + text2)
    
    if entities1 and entities2:
        intersection = entities1 & entities2
        union = entities1 | entities2
        entity_sim = len(intersection) / len(union) if union else 0
        
        cve1 = {e for e in entities1 if e.startswith('CVE-')}
        cve2 = {e for e in entities2 if e.startswith('CVE-')}
        if cve1 and cve2 and cve1 & cve2:
            logger.info(f"   🔴 Same CVE detected: {cve1 & cve2}")
            return 1.0
    else:
        entity_sim = 0
    
    text_sim = SequenceMatcher(None, text1[:500].lower(), text2[:500].lower()).ratio()
    
    final_score = (title_sim * 0.5) + (entity_sim * 0.35) + (text_sim * 0.15)
    
    return final_score


class State:
    def __init__(self):
        self.data = {
            "posted_ids": {},
            "recent_titles": [],
            "recent_posts": []
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
        for old_title in self.data["recent_titles"][-30:]:
            norm_old = normalize_title(old_title)
            if SequenceMatcher(None, norm_new, norm_old).ratio() > 0.65:
                logger.info(f"🔄 Title duplicate: {title[:50]}...")
                return True
        
        for old_post in self.data["recent_posts"][-20:]:
            old_title = old_post.get("title", "")
            old_text = old_post.get("text", "")
            
            similarity = calculate_similarity(title, text, old_title, old_text)
            
            if similarity > 0.5:
                logger.info(f"🔄 Content duplicate ({similarity:.2f}): {title[:50]}...")
                return True
        
        return False
    
    def mark_posted(self, uid: str, title: str, text: str = ""):
        if len(self.data["posted_ids"]) > MAX_POSTED_IDS:
            self.data["posted_ids"] = dict(sorted(
                self.data["posted_ids"].items(),
                key=lambda x: x[1]
            )[-400:])
        
        self.data["posted_ids"][uid] = int(time.time())
        
        self.data["recent_titles"].append(title)
        if len(self.data["recent_titles"]) > 50:
            self.data["recent_titles"] = self.data["recent_titles"][-50:]
        
        self.data["recent_posts"].append({
            "title": title,
            "text": text[:1000],
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


# ============ УЛУЧШЕННАЯ ГЕНЕРАЦИЯ ПОСТА ============

async def generate_post(item, session: aiohttp.ClientSession) -> Optional[str]:
    full_text = item.text
    if len(item.text) < 500:
        extra = await fetch_full_article(item.link, session)
        if extra:
            full_text = item.text + " " + extra
            logger.info(f"   📄 +{len(extra)} chars")
    
    # УСИЛЕННЫЙ ПРОМПТ с примерами
    prompt = f"""You are an editor for a Russian-language Telegram channel about cybersecurity (30k+ subscribers).

SOURCE (English):
Title: {item.title}
Text: {full_text[:3500]}

TASK: Write a post in RUSSIAN with SPECIFIC technical details.

=== CRITICAL RULES ===
❌ FORBIDDEN (will be rejected):
• "Обновите сигнатуры антивируса" — TOO GENERIC, will be rejected
• "Используйте антивирус" — TOO GENERIC, will be rejected  
• "Обновитесь до последней версии" — TOO VAGUE, will be rejected
• "Будьте бдительны" — EMPTY PHRASE, will be rejected
• "Проверяйте ссылки" — TOO OBVIOUS, will be rejected
• "Используйте сложные пароли" — BANNED, will be rejected
• "Включите 2FA" — BANNED, will be rejected
• "Делайте резервные копии" — BANNED without specifics

✅ GOOD EXAMPLES (what to write):
• "Заблокируйте исходящие соединения на порт 445 для сегментов с ICS"
• "Проверьте наличие файла C:\\Windows\\Temp\\update.exe с хэшем SHA256: a1b2c3..."
• "Отключите SMBv1 через GPO: Computer Configuration > Policies > Windows Settings > Security Settings"
• "Добавьте IOC в блоклист: домен malicious-c2.com, IP 185.220.101.42"
• "Примените временный патч: Set-ItemProperty -Path 'HKLM:\\...' -Name 'Disable...' -Value 1"
• "Проверьте логи на наличие события Event ID 4624 с LogonType 10 из подсети 10.0.0.0/8"

=== FORMAT ===
🔥 [Конкретный заголовок с CVE/названием малвари]

[2-4 предложения с техническими деталями: версии, порты, пути, хэши, команды]

👇 Что делать:
• [Конкретное действие с техническими деталями]
• [Еще одно конкретное действие]

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
    
    # Проверка на банальности
    if is_too_generic(text):
        # Пробуем очистить от банальностей
        cleaned = clean_banal_advice(text)
        
        # Если после очистки осталось мало — отклоняем
        if len(cleaned) < len(text) * 0.7:
            logger.info(f"⏩ Too generic even after cleaning")
            return None
        
        # Проверяем, остались ли банальности
        if is_too_generic(cleaned):
            logger.info("⏩ Still too generic after cleaning")
            return None
        
        text = cleaned
        logger.info("🧹 Post cleaned from banalities")
    
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
    logger.info("🚀 Starting KiberSOS (Enhanced Anti-Banality)")
    
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

