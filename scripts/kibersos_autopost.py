import os
import json
import asyncio
import random
import re
import time
import hashlib
from datetime import datetime, timedelta
from typing import List, Dict, Optional

import requests
import feedparser
import urllib.parse
from bs4 import BeautifulSoup
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile
from openai import OpenAI

# ============ COPILOT SDK SETUP ============
try:
    from github_copilot_sdk import CopilotClient
    COPILOT_SDK_AVAILABLE = True
    print("✅ GitHub Copilot SDK найден")
except ImportError:
    COPILOT_SDK_AVAILABLE = False
    print("⚠️ SDK не найден, работаем через OpenAI")

# ============ CONFIG ============

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
# Включаем SDK только если он доступен и разрешен
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
        print("🤖 Copilot Client инициализирован")
    except Exception as e:
        print(f"❌ Ошибка инициализации Copilot: {e}")
        USE_COPILOT_SDK = False

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

CACHE_DIR = os.getenv("CACHE_DIR", "cache")
os.makedirs(CACHE_DIR, exist_ok=True)
STATE_FILE = os.path.join(CACHE_DIR, "state_sec_pro.json")

RETENTION_DAYS = 14
MAX_ARTICLE_AGE_DAYS = 2
TELEGRAM_CAPTION_LIMIT = 1024

# ============ ИСТОЧНИКИ (PROFESSIONAL SECURITY) ============

RSS_SOURCES = [
    # Основные профильные
    {"name": "SecurityLab", "url": "https://www.securitylab.ru/rss/allnews/", "category": "security"},
    {"name": "AntiMalware", "url": "https://www.anti-malware.ru/news/feed", "category": "security"},
    {"name": "Habr InfoSec", "url": "https://habr.com/ru/rss/hub/infosecurity/all/?fl=ru", "category": "security"},
    
    # Хакерские и технические
    {"name": "Xakep.ru", "url": "https://xakep.ru/feed/", "category": "security"},
    {"name": "OpenNET", "url": "https://www.opennet.ru/opennews/opennews_all_utf.rss", "category": "linux_sec"},
    
    # Новости IT (фильтруем только безопасность)
    {"name": "CNews Security", "url": "https://www.cnews.ru/inc/rss/news_security.xml", "category": "security"},
]

# ============ ФОРМАТ ПОСТА (ANALYST MODE) ============

POST_FORMAT = {
    "system": """Ты — ведущий аналитик Threat Intelligence. Ведешь канал "Защита без кода".
Твоя ЦЕЛЬ: Дать сухую, технически точную выжимку инцидента для профессионалов и продвинутых пользователей.

АУДИТОРИЯ: Сисадмины, DevOps, безопасники, предприниматели.
Они знают базу. Им не нужны советы "не кликайте по ссылкам". Им нужна суть: ЧТО сломали и КАК починить.

СТИЛЬ:
- Тон: Сдержанный, экспертный, без эмоций и паники.
- Терминология: Используй CVE, RCE, 0-day, эксплойт, фишинг, бэкдор смело.
- Структура: Четкие разделы.
- Запрещено: "Будьте бдительны", "Шок", "Кошмар".
""",

    "template": """Напиши пост.

ЗАГОЛОВОК:
🛡 [Краткая суть: Уязвимость в X / Утечка в Y]

ИНЦИДЕНТ:
[Техническое описание: В чем суть уязвимости/атаки? Какой компонент затронут?]

ВЕКТОР АТАКИ:
[Как злоумышленник проникает? Фишинг, открытый порт, supply chain?]

MITIGATION (ЧТО ДЕЛАТЬ):
• [Конкретно: Патч до версии X.X]
• [Конкретно: Отключить службу Y]
• [Конкретно: Настроить правило Z]

РЕЗЮМЕ:
[Оценка риска: Критично/Умеренно. Почему?]

#InfoSec #CyberSecurity #ThreatIntel
"""
}

# ============ ФИЛЬТРЫ ============

EXCLUDE_KEYWORDS = [
    "акции", "инвестиц", "квартальный отчет", "назначен", "маркетинг", 
    "футбол", "хоккей", "фильм", "выборы", "криптовалют", "bitcoin", "nft", 
    "распродажа", "скидк", "гейминг", "playstation", "xbox", "кино", "сериал"
]

def is_excluded(title: str, summary: str) -> bool:
    text = f"{title} {summary}".lower()
    return any(k in text for k in EXCLUDE_KEYWORDS)

def is_security_related(title: str, summary: str) -> bool:
    # Расширенный список ключевых слов для фильтрации общих лент
    kw = ["уязвим", "атак", "взлом", "patch", "update", "шифровал", "spyware", 
          "backdoor", "rce", "cve", "фишинг", "ddos", "leak", "утечка", "троян", 
          "0-day", "exploit", "ботнет", "linux", "root", "permission", "security",
          "malware", "ransomware", "apt", "soc", "siem"]
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
        idx = self.data["source_index"] % len(RSS_SOURCES)
        ordered = RSS_SOURCES[idx:] + RSS_SOURCES[:idx]
        self.data["source_index"] = (idx + 1) % len(RSS_SOURCES)
        return ordered

state = State()

# ============ TEXT TOOLS ============

def clean_text(text: str) -> str:
    if not text: return ""
    return re.sub(r'<[^>]+>', ' ', text).strip()

def force_complete_sentence(text: str) -> str:
    """Умная обрезка: ищет конец предложения, чтобы не обрывать мысль"""
    if not text: return ""
    text = text.strip()
    
    # Если уже заканчивается на знак препинания
    if text[-1] in ".!?": return text
    
    # Ищем последнюю точку/восклицательный знак
    last_p = text.rfind('.')
    last_e = text.rfind('!')
    last_q = text.rfind('?')
    
    cut_pos = max(last_p, last_e, last_q)
    
    # Если знак найден ближе к концу (последние 30% текста), режем по нему
    if cut_pos > len(text) * 0.7:
        return text[:cut_pos+1]
    
    # Если знаков нет, просто ставим многоточие
    return text + "..."

def fetch_full_article(url: str) -> Optional[str]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for tag in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'form']): tag.decompose()
        
        # Попытка найти контент по умным селекторам
        content = soup.find('div', class_=re.compile(r'article|content|post|entry|news-body'))
        if content: 
            return content.get_text(separator='\n', strip=True)[:4000]
    except: pass
    return None

def build_final_post(text: str, link: str) -> str:
    text = force_complete_sentence(text)
    source = f'\n\n🔗 <a href="{link}">Читать источник</a>'
    
    if len(text) + len(source) > TELEGRAM_CAPTION_LIMIT:
        text = text[:TELEGRAM_CAPTION_LIMIT - len(source) - 50]
        text = force_complete_sentence(text)
        
    return text + source

# ============ PARSING & LOGIC ============

def load_rss(source: Dict) -> List[Dict]:
    articles = []
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=20)
        feed = feedparser.parse(resp.content)
    except: return []
    
    now = datetime.now()
    # Просматриваем больше статей, чтобы точно найти свежее
    for entry in feed.entries[:25]:
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
        
        if is_excluded(title, summary): continue
        if not is_security_related(title, summary): continue
        
        articles.append({
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
        
        msg = f"{POST_FORMAT['template']}\n\nDATA:\nTitle: {article['title']}\nSource: {article['source']}\nText: {content}"
        
        session = copilot_client.create_session(
            system=POST_FORMAT["system"],
            temperature=0.4, # Строгий режим
            max_tokens=900
        )
        response = await session.send_message(msg)
        text = response.text.strip().strip('"')
        if len(text) < 50: return None
        return build_final_post(text, article["link"])
    except: return None

def generate_post_openai(article: Dict) -> Optional[str]:
    full_text = fetch_full_article(article["link"])
    content = full_text[:3000] if full_text else article["summary"]
    
    msg = f"{POST_FORMAT['template']}\n\nDATA:\nTitle: {article['title']}\nSource: {article['source']}\nText: {content}"
    
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": POST_FORMAT["system"]}, {"role": "user", "content": msg}],
            temperature=0.4, # Строгий режим
            max_tokens=900
        )
        text = resp.choices[0].message.content.strip().strip('"')
        if len(text) < 50: return None
        return build_final_post(text, article["link"])
    except: return None

# ============ IMAGE ============

def generate_image(title: str) -> Optional[str]:
    # Промпт для абстрактной кибербезопасности
    styles = [
        "abstract cybersecurity data flow, dark background, red glitch",
        "digital shield concept, binary rain, matrix style, professional",
        "network security visualization, isometric server room, dark blue"
    ]
    prompt = f"{random.choice(styles)}, {re.sub(r'[^a-zA-Z]', '', title)[:40]}, 4k, no text, unreal engine render"
    
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
    print("🛡 [SecBot Pro] Система запущена...")
    
    if USE_COPILOT_SDK: print("🤖 Mode: Copilot SDK")
    else: print("🔧 Mode: OpenAI API")

    all_articles = []
    # Загружаем по очереди, с ротацией
    for source in state.get_next_source_order():
        found = load_rss(source)
        all_articles.extend(found)
    
    if not all_articles:
        print("✅ Новых инцидентов не обнаружено.")
        return

    # Сортируем: сначала самые свежие
    all_articles.sort(key=lambda x: x["date"], reverse=True)
    
    # Берем топ-15 кандидатов
    for article in all_articles[:15]:
        print(f"\n📝 Анализ: {article['title'][:50]}...")
        
        post_text = None
        if USE_COPILOT_SDK: post_text = await generate_post_copilot(article)
        if not post_text: post_text = generate_post_openai(article)
            
        if not post_text: continue
        
        img = generate_image(article["title"])
        try:
            if img: await bot.send_photo(CHANNEL_ID, photo=FSInputFile(img), caption=post_text)
            else: await bot.send_message(CHANNEL_ID, text=post_text)
            
            state.mark_posted(article["title"], article["link"])
            print("✅ Отчет опубликован!")
            cleanup_image(img)
            return # Публикуем 1 пост за запуск
        except Exception as e:
            print(f"❌ Ошибка отправки: {e}")
            cleanup_image(img)

async def main():
    try: await autopost()
    finally: await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())




