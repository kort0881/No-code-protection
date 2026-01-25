import os
import json
import asyncio
import random
import re
import time
import hashlib
import html
import urllib.parse
from datetime import datetime

import requests
import feedparser
from bs4 import BeautifulSoup
from youtube_transcript_api import YouTubeTranscriptApi
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile
from openai import OpenAI

# ============ КОНФИГУРАЦИЯ ============

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

CACHE_DIR = os.getenv("CACHE_DIR", "cache_sec")
os.makedirs(CACHE_DIR, exist_ok=True)
STATE_FILE = os.path.join(CACHE_DIR, "state_smart_v2.json") # Версия 2 (умная)

# ============ ИСТОЧНИКИ ============

RSS_SOURCES = [
    {"name": "Kaspersky Daily", "url": "https://www.kaspersky.ru/blog/feed/", "type": "rss"},
    {"name": "Kod.ru", "url": "https://kod.ru/rss/", "type": "rss"},
    {"name": "BleepingComputer", "url": "https://www.bleepingcomputer.com/feed/", "type": "rss"},
    {"name": "3DNews Soft", "url": "https://3dnews.ru/software/rss/", "type": "rss"},
    # Хабр часто пишет дубли, но мы их теперь отловим
    {"name": "Habr Security", "url": "https://habr.com/ru/rss/hub/infosecurity/all/?fl=ru", "type": "rss"},
]

YOUTUBE_CHANNELS = [
    {"name": "Overbafer1", "id": "UC-lHJ97lqoOGgsLFuQ8Y8_g"},
    {"name": "NetworkChuck", "id": "UC9x0AN7BWHpXyPic4IQC74Q"},
    {"name": "The Hated One", "id": "UCjr2bPAyPV7t35mVihRBCzw"},
    {"name": "NN", "id": "UCfJkM0E6qT8j6w6q5x5x_9A"},
]

# ============ ИНИЦИАЛИЗАЦИЯ ============

bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
openai_client = OpenAI(api_key=OPENAI_API_KEY)

class State:
    def __init__(self):
        # Храним ID постов И список последних заголовков для проверки дублей
        self.data = {"posted_ids": {}, "recent_titles": []}
        self._load()
    
    def _load(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    self.data.update(loaded)
                    # Если в старом файле не было ключа recent_titles, создадим
                    if "recent_titles" not in self.data:
                        self.data["recent_titles"] = []
            except: pass
    
    def save(self):
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
    
    def is_posted(self, uid):
        return uid in self.data["posted_ids"]
    
    def mark_posted(self, uid, title):
        # 1. Сохраняем ID (техническая уникальность ссылки)
        if len(self.data["posted_ids"]) > 300:
            sorted_ids = sorted(self.data["posted_ids"].items(), key=lambda x: x[1])
            self.data["posted_ids"] = dict(sorted_ids[-200:])
        self.data["posted_ids"][uid] = int(time.time())
        
        # 2. Сохраняем Заголовок (смысловая уникальность)
        # Храним последние 40 заголовков
        self.data["recent_titles"].append(title)
        if len(self.data["recent_titles"]) > 40:
            self.data["recent_titles"] = self.data["recent_titles"][-40:]
            
        self.save()

    def get_recent_titles_str(self):
        # Возвращаем список заголовков текстом для GPT
        return "\n".join(f"- {t}" for t in self.data["recent_titles"])

state = State()

# ============ ИНТЕЛЛЕКТУАЛЬНЫЕ ФУНКЦИИ ============

def clean_text(text):
    if not text: return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    return html.unescape(text).strip()

async def check_duplicate_topic(new_title):
    """
    Спрашивает у GPT, не писали ли мы об этом недавно.
    Это решает проблему '5 постов про наушники'.
    """
    recent_history = state.get_recent_titles_str()
    if not recent_history:
        return False # История пуста, дублей быть не может

    # Экономичный промпт для проверки
    prompt = f"""Ниже список последних новостей канала:
{recent_history}

Новая новость: "{new_title}"

Вопрос: Говорится ли в новой новости РОВНО О ТОМ ЖЕ ИНЦИДЕНТЕ, что и в одной из прошлых? 
(Например, если и там и там про 'взлом Bluetooth наушников JBL', ответь YES. Если темы похожи, но события разные - ответь NO).
Ответь строго одно слово: YES или NO."""

    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0, # Нужна строгость, 0 фантазии
            max_tokens=10
        )
        answer = resp.choices[0].message.content.strip().upper()
        if "YES" in answer:
            return True # Это дубль!
        return False
    except:
        return False

def generate_creative_image_prompt(title):
    """
    Создает разнообразные стили, чтобы не было скучных щитов.
    """
    
    # 1. Стили (Визуальный ряд)
    styles = [
        "minimalist vector art, flat design, orange and dark blue",
        "isometric 3d render, plastic material, soft lighting, pastel background",
        "futuristic synthwave, neon purple and grid background, retro 80s style",
        "digital watercolor painting, artistic, white background, abstract shapes",
        "paper cut craft style, layered paper, depth of field",
        "cinematic photorealistic close-up, dark moody lighting, bokeh",
        "blueprint technical drawing, white lines on blue background, schematic"
    ]
    
    # 2. Объекты (Сюжет)
    objects = [
        "abstract digital shield protection",
        "glowing padlock in digital space",
        "smartphone with holographic barrier",
        "laptop with warning glitch effect",
        "anonymous hacker silhouette in hoodie",
        "network nodes connecting safely",
        "red alert warning sign 3d",
        "matrix code rain falling on device"
    ]
    
    selected_style = random.choice(styles)
    selected_object = random.choice(objects)
    
    # Очищаем заголовок от мусора для промпта
    clean_t = re.sub(r'[^a-zA-Z0-9]', ' ', title)[:40]
    
    return f"{selected_object}, {clean_t}, {selected_style}, high quality, 4k"

def generate_image(title):
    try:
        # Используем креативный промпт
        prompt = generate_creative_image_prompt(title)
        enc = urllib.parse.quote(prompt)
        # Добавляем seed, чтобы генерации были уникальными
        url = f"https://image.pollinations.ai/prompt/{enc}?width=1024&height=1024&nologo=true&seed={random.randint(0,99999)}"
        
        r = requests.get(url, timeout=20)
        if r.status_code == 200:
            path = os.path.join(CACHE_DIR, "temp_img.jpg")
            with open(path, "wb") as f: f.write(r.content)
            return path
    except: pass
    return None

# ============ ПАРСЕРЫ ============

def fetch_rss(source):
    items = []
    try:
        feed = feedparser.parse(source['url'])
        for entry in feed.entries[:3]:
            uid = hashlib.md5(entry.link.encode()).hexdigest()
            # Проверка по ссылке (быстрая)
            if state.is_posted(uid): continue
            
            items.append({
                "type": "news", "title": entry.title, 
                "text": clean_text(entry.get("summary", "")),
                "link": entry.link, "source": source['name'], "uid": uid
            })
    except: pass
    return items

def fetch_youtube():
    items = []
    for channel in YOUTUBE_CHANNELS:
        try:
            feed = feedparser.parse(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel['id']}")
            for entry in feed.entries[:2]:
                vid = entry.yt_videoid
                uid = f"yt_{vid}"
                # Проверка по ссылке (быстрая)
                if state.is_posted(uid): continue
                try:
                    transcript = YouTubeTranscriptApi.list_transcripts(vid).find_transcript(['ru', 'en']).fetch()
                    full_text = " ".join([t['text'] for t in transcript])
                    items.append({
                        "type": "video", "title": entry.title, "text": full_text[:4000],
                        "link": entry.link, "source": f"YouTube {channel['name']}", "uid": uid
                    })
                except: pass
        except: pass
    return items

# ============ GPT ПРОЦЕССИНГ ============

async def process_item(item):
    # Промпты для написания текста
    if item['type'] == 'video':
        prompt = """Ты автор канала "Кибербез". Сделай из расшифровки видео короткий пост-выжимку.
Убери воду. Выдели главную угрозу и дай инструкцию.
Формат:
🎥 [Название]
💡 Суть: ...
📝 Советы: ..."""
    else:
        prompt = """Ты редактор канала "Кибербез". Прочитай новость.
1. Если это скучный отчет, B2B, сервера, конференции - ответь SKIP.
2. Если это касается обычных людей (развод, телефоны, утечки, VPN) - напиши пост.
Стиль: Простой, без паники, но полезный.
Формат:
⚠️ [Заголовок]
ℹ️ Что случилось: ...
🛡 Что делать: ..."""

    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"Title: {item['title']}\n\nText: {item['text']}"}
            ]
        )
        text = resp.choices[0].message.content.strip()
        if "SKIP" in text or len(text) < 50: return None
        return text + f"\n\n🔗 <a href='{item['link']}'>Источник</a>"
    except Exception as e:
        print(f"AI Error: {e}")
        return None

# ============ MAIN ============

async def main():
    print("🚀 Start scan...")
    all_items = []
    all_items.extend(fetch_youtube())
    for src in RSS_SOURCES:
        all_items.extend(fetch_rss(src))
    
    random.shuffle(all_items)
    print(f"📦 Candidates found: {len(all_items)}")

    for item in all_items:
        print(f"🔍 Analyzing: {item['title']}")
        
        # --- ЭТАП 1: ПРОВЕРКА НА СМЫСЛОВОЙ ДУБЛЬ ---
        # Спрашиваем у GPT, не было ли такой темы недавно
        is_semantic_dup = await check_duplicate_topic(item['title'])
        if is_semantic_dup:
            print(f"   🚫 DUPLICATE TOPIC! (GPT says YES). Skipping.")
            # Помечаем как обработанное, чтобы больше не тратить API на этот дубль
            state.mark_posted(item['uid'], item['title']) 
            continue

        # --- ЭТАП 2: ГЕНЕРАЦИЯ ТЕКСТА ---
        post_text = await process_item(item)
        
        if post_text:
            print("   ✅ Text generated. Creating image...")
            
            # --- ЭТАП 3: ГЕНЕРАЦИЯ КРАСИВОЙ КАРТИНКИ ---
            # Теперь тут работают рандомные стили
            img_path = generate_image(item['title'])
            
            try:
                if img_path:
                    await bot.send_photo(CHANNEL_ID, photo=FSInputFile(img_path), caption=post_text)
                    os.remove(img_path)
                else:
                    await bot.send_message(CHANNEL_ID, text=post_text)
                
                print("   🎉 Posted successfully!")
                # Записываем и ID, и Заголовок в историю
                state.mark_posted(item['uid'], item['title'])
                break # 1 пост за запуск
            except Exception as e:
                print(f"❌ Telegram Error: {e}")
        else:
            # Если GPT ответил SKIP (скучная новость)
            state.mark_posted(item['uid'], item['title'])

    await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
