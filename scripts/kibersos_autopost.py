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
STATE_FILE = os.path.join(CACHE_DIR, "state_youtube_rss.json")

# ============ ИСТОЧНИКИ (Я ВСЕ НАСТРОИЛ ЗА ТЕБЯ) ============

# 1. RSS: Блоги про безопасность для людей
RSS_SOURCES = [
    # Kaspersky Daily (Простым языком)
    {"name": "Kaspersky Daily", "url": "https://www.kaspersky.ru/blog/feed/", "type": "rss"},
    # Код Дурова (Про Телеграм и соцсети)
    {"name": "Kod.ru", "url": "https://kod.ru/rss/", "type": "rss"},
    # BleepingComputer (Тут самые свежие новости про вирусы, GPT переведет)
    {"name": "BleepingComputer", "url": "https://www.bleepingcomputer.com/feed/", "type": "rss"},
    # 3DNews Soft (Иногда бывает полезное про Windows/Android)
    {"name": "3DNews Soft", "url": "https://3dnews.ru/software/rss/", "type": "rss"},
]

# 2. YOUTUBE: Я добавил топ каналов про хакинг и защиту
YOUTUBE_CHANNELS = [
    # Overbafer1 (Русский, очень популярный про схемы развода)
    {"name": "Overbafer1", "id": "UC-lHJ97lqoOGgsLFuQ8Y8_g"},
    
    # NetworkChuck (Англ, супер просто про хакинг - GPT переведет)
    {"name": "NetworkChuck", "id": "UC9x0AN7BWHpXyPic4IQC74Q"},
    
    # The Hated One (Англ, всё про анонимность)
    {"name": "The Hated One", "id": "UCjr2bPAyPV7t35mVihRBCzw"},
    
    # NN (Русский, новости технологий кратко)
    {"name": "NN", "id": "UCfJkM0E6qT8j6w6q5x5x_9A"},
]

# ============ ИНИЦИАЛИЗАЦИЯ ============

bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
openai_client = OpenAI(api_key=OPENAI_API_KEY)

class State:
    def __init__(self):
        self.data = {"posted_ids": {}}
        self._load()
    
    def _load(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except: pass
    
    def save(self):
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
    
    def is_posted(self, uid):
        return uid in self.data["posted_ids"]
    
    def mark_posted(self, uid):
        # Храним последние 300 записей
        if len(self.data["posted_ids"]) > 300:
            sorted_ids = sorted(self.data["posted_ids"].items(), key=lambda x: x[1])
            self.data["posted_ids"] = dict(sorted_ids[-200:])
        self.data["posted_ids"][uid] = int(time.time())
        self.save()

state = State()

# ============ ПАРСЕРЫ ============

def clean_text(text):
    if not text: return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    return html.unescape(text).strip()

def fetch_rss(source):
    """Качает новости с сайтов"""
    items = []
    try:
        feed = feedparser.parse(source['url'])
        for entry in feed.entries[:3]:
            uid = hashlib.md5(entry.link.encode()).hexdigest()
            if state.is_posted(uid): continue
            
            items.append({
                "type": "news", 
                "title": entry.title, 
                "text": clean_text(entry.get("summary", "")),
                "link": entry.link, 
                "source": source['name'], 
                "uid": uid
            })
    except: pass
    return items

def fetch_youtube():
    """Качает субтитры с видео"""
    items = []
    for channel in YOUTUBE_CHANNELS:
        try:
            # Получаем RSS ленту канала (последние видео)
            rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel['id']}"
            feed = feedparser.parse(rss_url)
            
            for entry in feed.entries[:2]: # Проверяем 2 последних видео
                vid = entry.yt_videoid
                uid = f"yt_{vid}"
                if state.is_posted(uid): continue
                
                try:
                    # Пытаемся достать субтитры (на любом языке)
                    transcript_list = YouTubeTranscriptApi.list_transcripts(vid)
                    # Ищем русские или английские
                    transcript = transcript_list.find_transcript(['ru', 'en', 'de']).fetch()
                    full_text = " ".join([t['text'] for t in transcript])
                    
                    items.append({
                        "type": "video", 
                        "title": entry.title, 
                        "text": full_text[:4000], # Ограничиваем длину для GPT
                        "link": entry.link, 
                        "source": f"YouTube ({channel['name']})", 
                        "uid": uid
                    })
                except: 
                    # Часто у видео нет субтитров, это нормально, пропускаем
                    pass
        except: pass
    return items

# ============ GPT ============

async def process_item(item):
    """Превращает сырой текст в пост"""
    
    if item['type'] == 'video':
        # Промпт для Видео
        prompt = """Ты — автор канала "Кибербез".
Тебе дали расшифровку видео с YouTube. 
Твоя задача — сделать из этого короткий полезный пост-выжимку.
1. Убери "воду" и приветствия.
2. Выдели главную угрозу или совет.
3. Напиши четкую инструкцию.

Формат:
🎥 [Название видео]
💡 О чем речь: ...
📝 Главные советы:
• ...
• ..."""
    else:
        # Промпт для Новостей
        prompt = """Ты редактор канала "Кибербез".
Прочитай новость.
Если это скучный отчет компании или про сервера/бизнес — ответь одним словом SKIP.
Если это касается обычных людей (мошенники, телефоны, утечки паролей) — напиши пост.
Стиль: простой, заботливый.

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

def generate_image(title):
    try:
        # Рисуем абстракцию
        clean_t = re.sub(r'[^a-zA-Z0-9]', ' ', title)[:50]
        enc = urllib.parse.quote(f"cybersecurity 3d render shield smartphone protection {clean_t}")
        url = f"https://image.pollinations.ai/prompt/{enc}?width=1024&height=1024&nologo=true&seed={random.randint(0,999)}"
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            path = os.path.join(CACHE_DIR, "temp_img.jpg")
            with open(path, "wb") as f: f.write(r.content)
            return path
    except: pass
    return None

# ============ START ============

async def main():
    print("🚀 Запуск (YouTube + RSS)...")
    
    # 1. Сбор данных
    all_items = []
    
    print("...Сканирую YouTube каналы")
    all_items.extend(fetch_youtube())
    
    print("...Сканирую RSS ленты")
    for src in RSS_SOURCES:
        all_items.extend(fetch_rss(src))
        
    print(f"📦 Найдено материалов: {len(all_items)}")
    
    # 2. Перемешиваем
    random.shuffle(all_items)
    
    # 3. Публикация (1 пост)
    for item in all_items:
        print(f"⚙️ Проверка: {item['title']}")
        post_text = await process_item(item)
        
        if post_text:
            print("   ✅ Пост готов! Отправка...")
            img_path = generate_image(item['title'])
            
            try:
                if img_path:
                    await bot.send_photo(CHANNEL_ID, photo=FSInputFile(img_path), caption=post_text)
                    try: os.remove(img_path)
                    except: pass
                else:
                    await bot.send_message(CHANNEL_ID, text=post_text)
                
                state.mark_posted(item['uid'])
                print("   🎉 Успешно!")
                break # Выходим после 1 успешного поста
                
            except Exception as e:
                print(f"❌ Ошибка Telegram: {e}")
        else:
            # Если GPT вернул SKIP
            state.mark_posted(item['uid'])

    await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
