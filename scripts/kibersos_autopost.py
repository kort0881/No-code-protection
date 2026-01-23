import os
import json
import asyncio
import random
import re
import time
import hashlib
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

import requests
import feedparser
import urllib.parse
from bs4 import BeautifulSoup
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile
from openai import OpenAI

# Попытка импортировать Copilot SDK
try:
    from github_copilot_sdk import CopilotClient
    COPILOT_SDK_AVAILABLE = True
except ImportError:
    COPILOT_SDK_AVAILABLE = False
    print("⚠️ GitHub Copilot SDK не установлен, используется OpenAI API")

# ============ CONFIG ============

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
# Включаем SDK, если он доступен и разрешен
USE_COPILOT_SDK = os.getenv("USE_COPILOT_SDK", "false").lower() == "true" and COPILOT_SDK_AVAILABLE

if not all([OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, CHANNEL_ID]):
    raise ValueError("❌ Не все ENV переменные установлены!")

bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# Инициализация Copilot SDK
copilot_client = None
if USE_COPILOT_SDK:
    try:
        copilot_client = CopilotClient()
        print("✅ GitHub Copilot SDK инициализирован")
    except Exception as e:
        print(f"⚠️ Не удалось инициализировать Copilot SDK: {e}")
        USE_COPILOT_SDK = False

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

CACHE_DIR = os.getenv("CACHE_DIR", "cache")
os.makedirs(CACHE_DIR, exist_ok=True)
STATE_FILE = os.path.join(CACHE_DIR, "state.json")

RETENTION_DAYS = 14
MAX_ARTICLE_AGE_DAYS = 3
TELEGRAM_CAPTION_LIMIT = 1024

# ============ ИСТОЧНИКИ (Безопасность) ============

RSS_SOURCES = [
    {"name": "SecurityLab", "url": "https://www.securitylab.ru/rss/allnews/", "category": "security"},
    {"name": "AntiMalware", "url": "https://www.anti-malware.ru/news/feed", "category": "security"},
    {"name": "Habr News", "url": "https://habr.com/ru/rss/news/?fl=ru", "category": "tech"},
    {"name": "CNews", "url": "https://www.cnews.ru/inc/rss/news.xml", "category": "tech_ru"},
    {"name": "3DNews", "url": "https://3dnews.ru/news/rss/", "category": "tech_ru"},
    {"name": "iXBT", "url": "https://www.ixbt.com/export/news.rss", "category": "tech_ru"},
]

# ============ ФОРМАТ ПОСТА ============

POST_FORMAT = {
    "system": """Ты — эксперт по кибербезопасности. Пишешь понятно и по делу.
ЦЕЛЬ: объяснить обычным людям угрозу и дать четкую инструкцию.
ЧИТАТЕЛЬ: не программист, обычный пользователь смартфона.

ВАЖНО:
- Если есть решение — пиши пошагово.
- Если решения нет — просто предупреди.
- Не выдумывай инструкции, которых нет в тексте.""",

    "template": """Напиши пост для Telegram.

СНАЧАЛА: это касается ОБЫЧНЫХ ЛЮДЕЙ? (Если про CVE/багбаунти для профи — НЕ пиши).

СТРУКТУРА:
⚠️ [ЗАГОЛОВОК: суть одной строкой]

**Угроза:**
2-3 предложения — что случилось, в чём опасность.

**Кого касается:**
Конкретно: какие устройства/программы (например: "iPhone с iOS 16").

**Что делать:**
1. [Шаг 1]
2. [Шаг 2]
(Если решения нет, напиши "Ждем обновлений").

⏱ Займёт: [время]

ПРАВИЛА:
- Объём: 600-800 символов
- Без технарского жаргона
- Закончи полным предложением"""
}

# ============ ФИЛЬТРЫ ============

EXCLUDE_KEYWORDS = [
    "акции", "биржа", "инвестиц", "ipo", "капитализац", "выручка", "прибыль",
    "назначен", "отставка", "ceo", "hr", "кадр", "персонал",
    "футбол", "хоккей", "спорт", "кино", "фильм", "сериал",
    "выборы", "президент", "политик", "санкции",
    "bitcoin", "криптовалют", "nft", "суд", "арест", "приговор",
    "hackerone", "bugcrowd", "bug bounty", "cvss", "cve-",
    "исследовател безопасности", "security researcher"
]

SOURCE_PROMO_PATTERNS = [
    r"скидк[аи]", r"промокод", r"акция\b", r"распродажа",
    r"только сегодня", r"успей", r"предзаказ", r"цена от"
]

def is_excluded(title: str, summary: str) -> bool:
    text = f"{title} {summary}".lower()
    for kw in EXCLUDE_KEYWORDS:
        if kw in text: return True
    for pattern in SOURCE_PROMO_PATTERNS:
        if re.search(pattern, text): return True
    return False

def is_security_related(title: str, summary: str) -> bool:
    text = f"{title} {summary}".lower()
    keywords = [
        "вирус", "малвар", "троян", "ransomware", "шифровальщик",
        "фишинг", "мошен", "утечка", "взлом", "уязвим",
        "вредонос", "шпион", "червь", "эксплоит", "ddos",
        "пароль", "двухфактор", "аутентифик", "шифрован",
        "vpn", "антивирус", "безопасность", "приватность",
        "телеграм", "whatsapp", "android", "ios", "iphone",
        "аккаунт", "взлом", "слежк", "мошенн"
    ]
    for kw in keywords:
        if kw in text: return True
    return False

# ============ STATE ============

class State:
    def __init__(self):
        self.data = {"posted_ids": {}, "source_index": 0}
        self._load()
    
    def _load(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f: self.data.update(json.load(f))
            except: pass
    
    def save(self):
        try:
            with open(STATE_FILE, "w") as f: json.dump(self.data, f, indent=2)
        except: pass
    
    def is_posted(self, article_id: str) -> bool:
        return article_id in self.data["posted_ids"]
    
    def mark_posted(self, article_id: str):
        self.data["posted_ids"][article_id] = {"ts": datetime.now().timestamp()}
        self.save()
    
    def cleanup_old(self):
        cutoff = datetime.now().timestamp() - (RETENTION_DAYS * 86400)
        self.data["posted_ids"] = {k: v for k, v in self.data["posted_ids"].items() if v.get("ts", 0) > cutoff}
        self.save()
    
    def get_next_source_order(self) -> List[Dict]:
        idx = self.data["source_index"] % len(RSS_SOURCES)
        ordered = RSS_SOURCES[idx:] + RSS_SOURCES[:idx]
        self.data["source_index"] = (idx + 1) % len(RSS_SOURCES)
        return ordered

state = State()

# ============ PARSING ============

def get_article_id(title: str, link: str) -> str:
    return hashlib.sha256(f"{title}|{link}".encode()).hexdigest()[:20]

def clean_text(text: str) -> str:
    if not text: return ""
    return re.sub(r'<[^>]+>', ' ', text).strip()

def fetch_full_article(url: str) -> Optional[str]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for tag in soup(['script', 'style', 'nav', 'header', 'footer']): tag.decompose()
        content = soup.find('div', class_=re.compile(r'article|content|post|entry'))
        if content: return content.get_text(separator='\n', strip=True)[:4000]
    except: pass
    return None

def build_final_post(text: str, link: str) -> str:
    source = f'\n\n🔗 <a href="{link}">Источник</a>'
    tags = "\n\n#безопасность #киберугрозы"
    if len(text) + len(source) + len(tags) > TELEGRAM_CAPTION_LIMIT:
        text = text[:TELEGRAM_CAPTION_LIMIT - len(source) - len(tags) - 20] + "..."
    return text + tags + source

def load_rss(source: Dict) -> List[Dict]:
    articles = []
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=20)
        feed = feedparser.parse(resp.content)
    except: return []
    
    now = datetime.now()
    for entry in feed.entries[:30]:
        title = clean_text(entry.get("title", ""))
        link = entry.get("link", "")
        if not title or not link: continue
        
        aid = get_article_id(title, link)
        if state.is_posted(aid): continue
        
        pub_date = now
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try: pub_date = datetime(*entry.published_parsed[:6])
            except: pass
        if now - pub_date > timedelta(days=MAX_ARTICLE_AGE_DAYS): continue
        
        summary = clean_text(entry.get("summary", "") or entry.get("description", ""))
        
        if is_excluded(title, summary): continue
        if not is_security_related(title, summary): continue
        
        articles.append({
            "id": aid,
            "title": title,
            "summary": summary[:1500],
            "link": link,
            "source": source["name"],
            "date": pub_date
        })
    return articles

# ============ GENERATION ============

async def generate_post_copilot(article: Dict) -> Optional[str]:
    if not copilot_client: return None
    try:
        full_text = fetch_full_article(article["link"])
        content = full_text[:3000] if full_text else article["summary"]
        
        msg = f"{POST_FORMAT['template']}\n\nИСТОЧНИК:\nЗаголовок: {article['title']}\nТекст: {content}"
        
        session = copilot_client.create_session(
            system=POST_FORMAT["system"],
            temperature=0.6,
            max_tokens=800
        )
        response = await session.send_message(msg)
        text = response.text.strip().strip('"')
        if len(text) < 100: return None
        print(f"  ✅ SDK сгенерировал: {len(text)} симв.")
        return build_final_post(text, article["link"])
    except Exception as e:
        print(f"  ⚠️ Ошибка SDK: {e}")
        return None

def generate_post_openai(article: Dict) -> Optional[str]:
    full_text = fetch_full_article(article["link"])
    content = full_text[:3000] if full_text else article["summary"]
    
    msg = f"{POST_FORMAT['template']}\n\nИСТОЧНИК:\nЗаголовок: {article['title']}\nТекст: {content}"
    
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": POST_FORMAT["system"]}, {"role": "user", "content": msg}],
            temperature=0.6,
            max_tokens=800
        )
        text = resp.choices[0].message.content.strip().strip('"')
        if len(text) < 100: return None
        print(f"  ✅ OpenAI сгенерировал: {len(text)} симв.")
        return build_final_post(text, article["link"])
    except: return None

# ============ IMAGE ============

def generate_image(title: str) -> Optional[str]:
    prompt = f"cybersecurity, hacking threat illustration, minimal style, {re.sub(r'[^a-zA-Z]', '', title)[:30]}, 4k, no text"
    url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(prompt)}?seed={random.randint(0,10**7)}&width=1024&height=1024&nologo=true"
    try:
        resp = requests.get(url, timeout=40, headers=HEADERS)
        if resp.status_code == 200 and len(resp.content) > 10000:
            fname = f"img_{int(time.time())}.jpg"
            with open(fname, "wb") as f: f.write(resp.content)
            return fname
    except: pass
    return None

def cleanup_image(path):
    if path and os.path.exists(path): os.remove(path)

# ============ MAIN ============

async def autopost():
    state.cleanup_old()
    print("🔄 Загрузка новостей (Security)...")
    
    if USE_COPILOT_SDK: print("🤖 Режим: Copilot SDK")
    else: print("🔧 Режим: OpenAI API")

    all_articles = []
    for source in state.get_next_source_order():
        all_articles.extend(load_rss(source))
    
    if not all_articles:
        print("❌ Нет новых статей")
        return

    all_articles.sort(key=lambda x: x["date"], reverse=True)
    
    for article in all_articles[:15]:
        print(f"\n📰 {article['title'][:50]}...")
        
        post_text = None
        if USE_COPILOT_SDK:
            post_text = await generate_post_copilot(article)
        
        if not post_text:
            post_text = generate_post_openai(article)
            
        if not post_text: continue
        
        img = generate_image(article["title"])
        try:
            if img: await bot.send_photo(CHANNEL_ID, photo=FSInputFile(img), caption=post_text)
            else: await bot.send_message(CHANNEL_ID, text=post_text)
            
            state.mark_posted(article["id"])
            print("✅ Опубликовано!")
            cleanup_image(img)
            return
        except Exception as e:
            print(f"❌ Ошибка отправки: {e}")
            cleanup_image(img)

async def main():
    try: await autopost()
    finally: await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())



