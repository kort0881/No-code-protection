import os
import json
import asyncio
import random
import re
import time
import hashlib
import html
import urllib.parse
from datetime import datetime, timedelta
from typing import List, Dict, Optional

import requests
import feedparser
from bs4 import BeautifulSoup
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile
from openai import OpenAI

# ============ CONFIG ============

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

if not all([OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, CHANNEL_ID]):
    print("⚠️ WARNING: Keys not found!")

bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
openai_client = OpenAI(api_key=OPENAI_API_KEY)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# Папка для кэша
CACHE_DIR = os.getenv("CACHE_DIR", "cache_sec")
os.makedirs(CACHE_DIR, exist_ok=True)
STATE_FILE = os.path.join(CACHE_DIR, "state_kiber.json")

RETENTION_DAYS = 14
MAX_ARTICLE_AGE_DAYS = 2
TELEGRAM_CAPTION_LIMIT = 1024 # Лимит для фото
TELEGRAM_TEXT_LIMIT = 4096    # Лимит для простого сообщения

# ============ ИСТОЧНИКИ ============

RSS_SOURCES = [
    {"name": "SecurityLab", "url": "https://www.securitylab.ru/rss/allnews/", "category": "security"},
    {"name": "AntiMalware", "url": "https://www.anti-malware.ru/news/feed", "category": "security"},
    {"name": "Habr InfoSec", "url": "https://habr.com/ru/rss/hub/infosecurity/all/?fl=ru", "category": "security"},
    {"name": "Xakep.ru", "url": "https://xakep.ru/feed/", "category": "security"},
    {"name": "CNews Security", "url": "https://www.cnews.ru/inc/rss/news_security.xml", "category": "security"},
]

# ============ СТИЛЬ ПОСТА (ДЛЯ ЛЮДЕЙ) ============

POST_FORMAT = {
    "system": """Ты — эксперт по цифровой безопасности, который пишет для обычных людей.
Твоя ЦЕЛЬ: Простым языком объяснить сложную угрозу и подсказать, как защититься.

АУДИТОРИЯ: Обычные пользователи.
Им важно: "Украдут ли мои деньги?", "Взломают ли соцсети?".

СТИЛЬ:
- Тон: Заботливый, предупреждающий, понятный.
- Объясняй термины.
- Используй эмодзи (⚠️, 🛑, 🛡).
- Пиши подробно, мысль должна быть законченной.
- Язык: Русский.
""",
    "template": """Напиши пост по этой структуре:

⚠️ [Заголовок: Суть угрозы понятными словами]

🛑 ЧТО СЛУЧИЛОСЬ:
[Опиши ситуацию просто. Кто атакует? Кого взломали?]

🤔 ЧЕМ ЭТО ОПАСНО:
[Последствия: Кража паролей? Потеря денег? Слежка?]

🛡 КАК ЗАЩИТИТЬСЯ:
• [Совет 1]
• [Совет 2]

#Кибербез #Безопасность #KiberSOS
"""
}

# ============ ФИЛЬТРЫ ============

EXCLUDE_KEYWORDS = [
    "акции", "инвестиц", "квартальный отчет", "назначен", "маркетинг", 
    "футбол", "хоккей", "фильм", "выборы", "криптовалют", "bitcoin", "nft", 
    "распродажа", "скидк", "гейминг", "playstation", "xbox"
]

def is_security_related(title: str, summary: str) -> bool:
    kw = ["уязвим", "атак", "взлом", "patch", "update", "шифровал", "spyware", 
          "backdoor", "rce", "cve", "фишинг", "ddos", "leak", "утечка", "троян", 
          "0-day", "exploit", "ботнет", "linux", "root", "permission", "security",
          "malware", "ransomware", "apt", "soc", "siem", "хакер", "мошенни"]
    text = f"{title} {summary}".lower()
    return any(k in text for k in kw)

# ============ STATE MANAGEMENT ============

class State:
    def __init__(self):
        self.data = {"posted_ids": {}, "source_index": 0}
        self._load()
    
    def _load(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f: 
                    self.data.update(json.load(f))
            except: pass
    
    def save(self):
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f: 
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except: pass
    
    def get_article_id(self, title: str, link: str) -> str:
        return hashlib.sha256(f"{title}|{link}".encode()).hexdigest()[:20]

    def is_posted(self, title: str, link: str) -> bool:
        return self.get_article_id(title, link) in self.data["posted_ids"]
    
    def mark_posted(self, title: str, link: str):
        aid = self.get_article_id(title, link)
        self.data["posted_ids"][aid] = {"ts": datetime.now().timestamp()}
        self.save()
    
    def cleanup_old(self):
        cutoff = datetime.now().timestamp() - (RETENTION_DAYS * 86400)
        self.data["posted_ids"] = {k: v for k, v in self.data["posted_ids"].items() if v.get("ts", 0) > cutoff}
        self.save()
    
    def get_next_source_order(self) -> List[Dict]:
        idx = self.data.get("source_index", 0) % len(RSS_SOURCES)
        ordered = RSS_SOURCES[idx:] + RSS_SOURCES[:idx]
        self.data["source_index"] = (idx + 1) % len(RSS_SOURCES)
        self.save()
        return ordered

state = State()

# ============ TEXT TOOLS ============

def clean_text(text: str) -> str:
    if not text: return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    return " ".join(text.split())

def fetch_full_article(url: str) -> Optional[str]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for tag in soup(['script', 'style', 'nav', 'header', 'footer']): tag.decompose()
        content = soup.find('div', class_=re.compile(r'article|content|post|entry|news-body'))
        if content: return clean_text(content.get_text())[:3500]
    except: pass
    return None

def build_final_post(text: str, link: str) -> str:
    text = html.escape(text)
    source = f'\n\n🔗 <a href="{link}">Читать источник</a>'
    return text + source

# ============ RSS LOAD ============

def load_rss(source: Dict) -> List[Dict]:
    articles = []
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=20)
        feed = feedparser.parse(resp.content)
    except: return []
    
    now = datetime.now()
    for entry in feed.entries[:20]:
        title = clean_text(entry.get("title", ""))
        link = entry.get("link", "")
        if not title or not link: continue
        
        if state.is_posted(title, link): continue
        
        pub_date = now
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try: pub_date = datetime(*entry.published_parsed[:6])
            except: pass
            
        if now - pub_date > timedelta(days=MAX_ARTICLE_AGE_DAYS): continue
        
        summary = clean_text(entry.get("summary", "") or entry.get("description", ""))
        
        if any(k in (title+summary).lower() for k in EXCLUDE_KEYWORDS): continue
        if not is_security_related(title, summary): continue
        
        articles.append({
            "title": title, "summary": summary[:1500], "link": link,
            "source": source["name"], "date": pub_date
        })
    return articles

# ============ GENERATION ============

async def generate_post(article: Dict) -> Optional[str]:
    full_text = fetch_full_article(article["link"])
    content = full_text if full_text else article["summary"]
    
    msg = f"{POST_FORMAT['template']}\n\nDATA:\nTitle: {article['title']}\nSource: {article['source']}\nText: {content[:2500]}"
    
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": POST_FORMAT["system"]},
                {"role": "user", "content": msg}
            ],
            temperature=0.6,
            max_tokens=1500 # Даем свободу писать длиннее, если нужно
        )
        text = resp.choices[0].message.content.strip()
        text = text.replace("**", "").replace('"', '')
        return build_final_post(text, article["link"])
    except Exception as e:
        print(f"❌ OpenAI Error: {e}")
        return None

# ============ IMAGE ============

def generate_image(title: str) -> Optional[str]:
    clean_title = re.sub(r'[^a-zA-Z0-9]', ' ', title)[:40]
    prompt = f"cybersecurity digital protection shield lock safety concept art, blue and white colors, high quality 8k render, {clean_title}"
    
    encoded = urllib.parse.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&nologo=true&seed={random.randint(0,99999)}"
    
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200 and len(resp.content) > 10000:
            fname = f"img_{int(time.time())}.jpg"
            with open(fname, "wb") as f: f.write(resp.content)
            return fname
    except: pass
    return None

def cleanup_image(path):
    if path and os.path.exists(path):
        try: os.remove(path)
        except: pass

# ============ MAIN ============

async def autopost():
    state.cleanup_old()
    print("🛡 [KiberSOS] Запуск анализа...")
    
    all_articles = []
    # Ротация
    for source in state.get_next_source_order():
        print(f"📡 Скан: {source['name']}")
        all_articles.extend(load_rss(source))
    
    if not all_articles:
        print("✅ Новых инцидентов нет.")
        return

    all_articles.sort(key=lambda x: x["date"], reverse=True)
    
    for article in all_articles[:10]:
        print(f"\n📝 Обработка: {article['title']}")
        
        post_text = await generate_post(article)
        if not post_text: continue
        
        # === ГЛАВНАЯ ЛОГИКА ВЫБОРА (ФОТО или ТЕКСТ) ===
        
        # Если текст короткий (влезает под картинку) -> Генерируем фото
        if len(post_text) <= TELEGRAM_CAPTION_LIMIT:
            print("   📸 Текст короткий, генерирую картинку...")
            img = generate_image(article["title"])
            
            try:
                if img:
                    await bot.send_photo(CHANNEL_ID, photo=FSInputFile(img), caption=post_text)
                    cleanup_image(img)
                else:
                    await bot.send_message(CHANNEL_ID, text=post_text, disable_web_page_preview=False)
            except Exception as e:
                print(f"❌ Ошибка отправки фото: {e}")
                # Если с фото не вышло, пробуем просто текст
                await bot.send_message(CHANNEL_ID, text=post_text)

        # Если текст длинный -> Отправляем только текст (без картинки)
        else:
            print("   📜 Текст длинный, отправляю БЕЗ картинки (чтобы не резать)...")
            try:
                await bot.send_message(CHANNEL_ID, text=post_text, disable_web_page_preview=False)
            except Exception as e:
                print(f"❌ Ошибка отправки текста: {e}")
        
        # Если успешно отправили
        state.mark_posted(article["title"], article["link"])
        print("✅ Опубликовано!")
        return # 1 пост за запуск

async def main():
    try: await autopost()
    finally: await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
