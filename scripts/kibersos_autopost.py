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

CACHE_DIR = os.getenv("CACHE_DIR", "cache_sec")
os.makedirs(CACHE_DIR, exist_ok=True)
STATE_FILE = os.path.join(CACHE_DIR, "state_kiber.json")

RETENTION_DAYS = 14
MAX_ARTICLE_AGE_DAYS = 2
TELEGRAM_CAPTION_LIMIT = 1024

# ============ НОВЫЕ ИСТОЧНИКИ (Для людей) ============

RSS_SOURCES = [
    # Kaspersky Daily (Блог для пользователей, идеально для советов)
    {"name": "Kaspersky Daily", "url": "https://www.kaspersky.ru/blog/feed/", "category": "consumer"},
    
    # Код Дурова (Часто пишут про Telegram, утечки, блокировки)
    {"name": "Kod.ru", "url": "https://kod.ru/rss/", "category": "tech"},
    
    # 3DNews (Раздел Software/Security - бывают новости про виндовс/софт)
    {"name": "3DNews Soft", "url": "https://3dnews.ru/software/rss/", "category": "tech"},
    
    # Раздел безопасности на Хабре (оставляем, но будем фильтровать через GPT)
    {"name": "Habr InfoSec", "url": "https://habr.com/ru/rss/hub/infosecurity/all/?fl=ru", "category": "security"},
    
    # Добавляем англоязычные (GPT переведет), там больше про Apple/Android/Scams
    {"name": "BleepingComputer", "url": "https://www.bleepingcomputer.com/feed/", "category": "security"},
    {"name": "9to5Mac Security", "url": "https://9to5mac.com/guides/security/feed/", "category": "apple"},
]

# Дни недели, когда разрешены новости про БИЗНЕС (0=Пн, 1=Вт, ... 6=Вс)
# Например: Вторник (1) и Четверг (3)
BUSINESS_NEWS_DAYS = [1, 3] 

# ============ ПРОМПТ ДЛЯ ФИЛЬТРАЦИИ И НАПИСАНИЯ ============

POST_FORMAT = {
    "system": """Ты — редактор канала "Кибербез для обычных людей". 
Твоя задача — отобрать новость и переписать её просто и полезно.

ГЛАВНОЕ ПРАВИЛО ФИЛЬТРАЦИИ:
1. Если новость про: настройки серверов, Linux, DevOps, отчеты директоров, B2B рынок, сложные корпоративные взломы, которые не касаются данных физлиц — ответь одним словом: SKIP.
2. Если новость про: WhatsApp, Telegram, iOS, Android, карты, мошенников, Wi-Fi, пароли, утечки данных пользователей, VPN — ПИШИ ПОСТ.

ИСКЛЮЧЕНИЕ (Дни бизнеса):
Если в поле SYSTEM_INSTRUCTION сказано "BUSINESS_ALLOWED", ты можешь написать про крупный взлом компании, но только если объяснишь, как это влияет на обычного человека.

ФОРМАТ ПОСТА:
- Заголовок с эмодзи.
- Простым языком: что случилось.
- Почему это важно мне (читателю).
- Чёткая инструкция: что сделать прямо сейчас (обновить, сменить пароль, не нажимать).
- Хештеги: #Кибербез #Советы
""",
    "template": """Проанализируй новость.
Если это скучная корпоративная чушь — верни просто слово SKIP.
Если это полезно для человека с телефоном/ноутбуком — напиши пост на русском языке.

Title: {title}
Summary: {summary}
Full Text Fragment: {text_fragment}
"""
}

# ============ ФИЛЬТРЫ (Первичные) ============
# Сразу выкидываем мусор, чтобы не тратить деньги на API

EXCLUDE_KEYWORDS = [
    "назначен директором", "квартальный отчет", "акции упали", "маркетинг", 
    "конференция", "вебинар", "cisco", "oracle", "vmware", "kubernetes", 
    "devops", "selectel", "data center", "цод", "импортозамещ"
]

def is_potentially_interesting(title: str, summary: str) -> bool:
    text = f"{title} {summary}".lower()
    # Если есть стоп-слова
    if any(k in text for k in EXCLUDE_KEYWORDS): return False
    return True

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

    # Ротация источников, чтобы не спамить только с одного
    def get_shuffled_sources(self) -> List[Dict]:
        src = RSS_SOURCES.copy()
        random.shuffle(src)
        return src

state = State()

# ============ TEXT TOOLS ============

def clean_text(text: str) -> str:
    if not text: return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    return " ".join(text.split())

def fetch_full_article(url: str) -> Optional[str]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        # Удаляем лишнее
        for tag in soup(['script', 'style', 'nav', 'header', 'footer', 'aside']): 
            tag.decompose()
        # Ищем основной текст (универсальный поиск)
        content = soup.find('div', class_=re.compile(r'article|content|post|entry|news-body'))
        if not content:
            # Fallback - берем все параграфы
            ps = soup.find_all('p')
            return " ".join([p.get_text() for p in ps])[:3000]
            
        return clean_text(content.get_text())[:3000]
    except: return None

def build_final_post(text: str, link: str) -> str:
    # Безопасно для HTML
    # text = html.escape(text) # GPT обычно возвращает норм текст, но можно включить если будут баги
    source = f'\n\n🔗 <a href="{link}">Источник</a>'
    return text + source

# ============ RSS LOAD ============

def load_rss(source: Dict) -> List[Dict]:
    articles = []
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=15)
        feed = feedparser.parse(resp.content)
    except Exception as e: 
        print(f"Error loading {source['name']}: {e}")
        return []
    
    now = datetime.now()
    for entry in feed.entries[:10]: # Берем только свежие 10
        title = clean_text(entry.get("title", ""))
        link = entry.get("link", "")
        
        if not title or not link: continue
        if state.is_posted(title, link): continue
        
        # Проверка даты
        pub_date = now
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try: pub_date = datetime(*entry.published_parsed[:6])
            except: pass
        if now - pub_date > timedelta(days=MAX_ARTICLE_AGE_DAYS): continue
        
        summary = clean_text(entry.get("summary", "") or entry.get("description", ""))
        
        # Первичный фильтр по ключевым словам
        if not is_potentially_interesting(title, summary): 
            continue
            
        articles.append({
            "title": title, "summary": summary[:1000], "link": link,
            "source": source["name"], "date": pub_date
        })
    return articles

# ============ GENERATION ============

async def generate_post_content(article: Dict) -> Optional[str]:
    full_text = fetch_full_article(article["link"])
    text_fragment = full_text if full_text else article["summary"]
    
    # Проверяем, день бизнеса сегодня или нет
    weekday = datetime.now().weekday()
    system_instruction = POST_FORMAT["system"]
    
    if weekday in BUSINESS_NEWS_DAYS:
        system_instruction += "\n\nSYSTEM_INSTRUCTION: BUSINESS_ALLOWED"
    else:
        system_instruction += "\n\nSYSTEM_INSTRUCTION: CONSUMER_ONLY (STRICT)"

    user_msg = POST_FORMAT["template"].format(
        title=article['title'],
        summary=article['summary'],
        text_fragment=text_fragment[:2000]
    )
    
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_msg}
            ],
            temperature=0.5,
            max_tokens=1000
        )
        content = resp.choices[0].message.content.strip()
        
        # Если GPT решил пропустить новость
        if "SKIP" in content or len(content) < 50:
            print(f"   🤖 AI решил пропустить: {article['title']}")
            return None
            
        content = content.replace("**", "").replace('"', '')
        return build_final_post(content, article["link"])
    except Exception as e:
        print(f"❌ OpenAI Error: {e}")
        return None

# ============ IMAGE ============

def generate_image(title: str) -> Optional[str]:
    # Делаем промпт более "домашним", меньше матрицы, больше защиты гаджетов
    clean_title = re.sub(r'[^a-zA-Z0-9]', ' ', title)[:50]
    prompt = f"cybersecurity illustration, 3d icon style, simple, minimalist, shield protecting smartphone or laptop, soft lighting, blue and orange colors, {clean_title}"
    
    encoded = urllib.parse.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&nologo=true&seed={random.randint(0,99999)}"
    
    try:
        resp = requests.get(url, timeout=20)
        if resp.status_code == 200 and len(resp.content) > 5000:
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
    print("🛡 [KiberSOS] Поиск новостей для обычных людей...")
    
    # Получаем список статей со всех источников в случайном порядке
    all_candidates = []
    sources = state.get_shuffled_sources()
    
    for source in sources:
        print(f"📡 {source['name']}...")
        all_candidates.extend(load_rss(source))
    
    # Сортируем: сначала свежие
    all_candidates.sort(key=lambda x: x["date"], reverse=True)
    
    print(f"🔍 Найдено {len(all_candidates)} кандидатов. Фильтруем через GPT...")

    posts_done = 0
    
    for article in all_candidates:
        if posts_done >= 1: break # Постим только 1 новость за запуск
        
        print(f"📝 Анализ: {article['title']}")
        
        post_text = await generate_post_content(article)
        
        if not post_text:
            # GPT вернул SKIP или ошибку - помечаем как "просмотрено", чтобы не дергать снова
            # Но можно и не помечать, если хотите дать второй шанс. 
            # Лучше пометить, чтобы экономить API.
            state.mark_posted(article["title"], article["link"])
            continue 
        
        # Если пост сгенерировался - отправляем
        print("   📸 Генерирую картинку...")
        img = generate_image(article["title"])
        
        try:
            if img and len(post_text) <= TELEGRAM_CAPTION_LIMIT:
                await bot.send_photo(CHANNEL_ID, photo=FSInputFile(img), caption=post_text)
                cleanup_image(img)
            else:
                await bot.send_message(CHANNEL_ID, text=post_text, disable_web_page_preview=False)
                if img: cleanup_image(img)
            
            print("✅ Опубликовано!")
            state.mark_posted(article["title"], article["link"])
            posts_done += 1
            
        except Exception as e:
            print(f"❌ Ошибка отправки: {e}")

async def main():
    try: await autopost()
    finally: await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
