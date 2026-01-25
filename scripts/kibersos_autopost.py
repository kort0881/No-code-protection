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
STATE_FILE = os.path.join(CACHE_DIR, "state_smart_v3.json")

# Лимит символов, после которого мы отказываемся от картинки в пользу текста
TEXT_ONLY_THRESHOLD = 850 

# ============ ИСТОЧНИКИ ============

RSS_SOURCES = [
    {"name": "Kaspersky Daily", "url": "https://www.kaspersky.ru/blog/feed/", "type": "rss"},
    {"name": "Kod.ru", "url": "https://kod.ru/rss/", "type": "rss"},
    {"name": "BleepingComputer", "url": "https://www.bleepingcomputer.com/feed/", "type": "rss"},
    {"name": "3DNews Soft", "url": "https://3dnews.ru/software/rss/", "type": "rss"},
    {"name": "Habr Security", "url": "https://habr.com/ru/rss/hub/infosecurity/all/?fl=ru", "type": "rss"},
    {"name": "SecurityLab", "url": "https://www.securitylab.ru/rss/news/", "type": "rss"},
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
        self.data = {"posted_ids": {}, "recent_titles": []}
        self._load()
    
    def _load(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    self.data.update(json.load(f))
                    if "recent_titles" not in self.data: self.data["recent_titles"] = []
            except: pass
    
    def save(self):
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
    
    def is_posted(self, uid):
        return uid in self.data["posted_ids"]
    
    def mark_posted(self, uid, title):
        if len(self.data["posted_ids"]) > 300:
            sorted_ids = sorted(self.data["posted_ids"].items(), key=lambda x: x[1])
            self.data["posted_ids"] = dict(sorted_ids[-200:])
        self.data["posted_ids"][uid] = int(time.time())
        
        self.data["recent_titles"].append(title)
        if len(self.data["recent_titles"]) > 40:
            self.data["recent_titles"] = self.data["recent_titles"][-40:]
        self.save()

    def get_recent_titles_str(self):
        return "\n".join(f"- {t}" for t in self.data["recent_titles"])

state = State()

# ============ УТИЛИТЫ ============

def clean_text(text):
    if not text: return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    return html.unescape(text).strip()

# ============ ПРОВЕРКА ДУБЛЕЙ ============

async def check_duplicate_topic(new_title):
    recent_history = state.get_recent_titles_str()
    if not recent_history: return False

    prompt = f"""Ниже список последних новостей канала:
{recent_history}

Новая новость: "{new_title}"

Вопрос: Это дубликат недавней темы? (Речь про то же событие?)
Ответь YES или NO."""

    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0, max_tokens=10
        )
        return "YES" in resp.choices[0].message.content.strip().upper()
    except: return False

# ============ ГЕНЕРАЦИЯ КАРТИНОК ============

def generate_creative_image_prompt(title):
    # Убрали банальные щиты и замки
    styles = [
        "dark cyberpunk city atmosphere, neon rain, cinematic lighting",
        "abstract data flow visualization, matrix style, green and black",
        "minimalist glitch art, distorted reality, tech noir",
        "isometric server room, stylized 3d render, soft blue lighting",
        "retro vaporwave computer aesthetic, 80s style",
        "detailed blueprint schematic, white lines on dark blue",
        "double exposure, human silhouette filled with digital code"
    ]
    
    # Объекты более абстрактные
    objects = [
        "digital anomaly", "broken smartphone screen", "anonymous hacker hoodie", 
        "network cables tangle", "red warning hologram", "secure usb key glowing"
    ]
    
    clean_t = re.sub(r'[^a-zA-Z0-9]', ' ', title)[:40]
    return f"{random.choice(objects)}, {clean_t}, {random.choice(styles)}, high quality 8k"

def generate_image(title):
    try:
        prompt = generate_creative_image_prompt(title)
        enc = urllib.parse.quote(prompt)
        url = f"https://image.pollinations.ai/prompt/{enc}?width=1280&height=720&nologo=true&seed={random.randint(0,99999)}"
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

# ============ GPT: НАПИСАНИЕ ПОСТА ============

async def process_item(item):
    if item['type'] == 'video':
        system_prompt = """Ты — автор профессионального канала по кибербезопасности.
Тебе дали расшифровку видео.
Твоя задача — сделать подробный разбор (Squeeze).
Не пиши вступлений "В этом видео...". Сразу к сути.
Структурируй текст: Заголовок, Проблема, Технические детали, Решение."""
    else:
        # Промпт для новостей (УСИЛЕННЫЙ)
        system_prompt = """Ты — ведущий аналитик по информационной безопасности.
Твоя задача — написать глубокий, полезный пост для канала.

Правила:
1. Если исходная новость короткая — РАСШИРЬ её, используя свои общие знания по этой теме. Объясни техническую суть угрозы.
2. Избегай банальностей ("будьте бдительны", "не переходите по ссылкам"). Давай конкретные инструкции (какие настройки отключить, какой софт проверить).
3. Стиль: Профессиональный, но понятный. Без "детского сада" и лишних эмодзи.
4. Если новость про бизнес/отчеты/назначения — верни SKIP.

Структура поста:
🔥 [Цепляющий заголовок]

[Основной текст: суть проблемы, кого касается, технические детали]

👇 ЧТО ДЕЛАТЬ:
• [Конкретный совет 1]
• [Конкретный совет 2]
"""

    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Title: {item['title']}\n\nText: {item['text']}"}
            ],
            max_tokens=1500 # Разрешаем длинный ответ
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
    print(f"📦 Candidates: {len(all_items)}")

    for item in all_items:
        print(f"🔍 Analyzing: {item['title']}")
        
        # 1. Проверка дублей
        if await check_duplicate_topic(item['title']):
            print(f"   🚫 DUPLICATE TOPIC. Skipping.")
            state.mark_posted(item['uid'], item['title'])
            continue

        # 2. Генерация текста
        post_text = await process_item(item)
        
        if post_text:
            text_len = len(post_text)
            print(f"   ✅ Post ready. Length: {text_len} chars.")
            
            # 3. Решение: Картинка или Текст?
            # Если пост длинный (>850 символов), отправляем БЕЗ картинки, чтобы не резать текст
            if text_len > TEXT_ONLY_THRESHOLD:
                print("   📜 Long read detected. Sending TEXT ONLY.")
                try:
                    await bot.send_message(CHANNEL_ID, text=post_text, disable_web_page_preview=False)
                    print("   🎉 Posted text!")
                    state.mark_posted(item['uid'], item['title'])
                    break
                except Exception as e:
                    print(f"❌ Telegram Error: {e}")
            
            # Если пост короткий, делаем красивую картинку
            else:
                print("   📸 Short read. Generating IMAGE.")
                img_path = generate_image(item['title'])
                try:
                    if img_path:
                        await bot.send_photo(CHANNEL_ID, photo=FSInputFile(img_path), caption=post_text)
                        os.remove(img_path)
                    else:
                        await bot.send_message(CHANNEL_ID, text=post_text)
                    
                    print("   🎉 Posted with image!")
                    state.mark_posted(item['uid'], item['title'])
                    break
                except Exception as e:
                    print(f"❌ Telegram Error: {e}")

        else:
            state.mark_posted(item['uid'], item['title'])

    await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
