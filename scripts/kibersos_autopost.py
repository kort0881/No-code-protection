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
    raise ValueError("❌ Не все ENV переменные установлены!")

bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
openai_client = OpenAI(api_key=OPENAI_API_KEY)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

CACHE_DIR = os.getenv("CACHE_DIR", "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

POSTED_FILE = os.path.join(CACHE_DIR, "posted_articles.json")
SOURCE_ROTATION_FILE = os.path.join(CACHE_DIR, "source_rotation.json")

RETENTION_DAYS = 14
MAX_ARTICLE_AGE_DAYS = 3

# ============ RSS ИСТОЧНИКИ С РОТАЦИЕЙ ============

RSS_SOURCES = [
    {"name": "1275 Vulnerabilities", "url": "https://1275.ru/vulnerability/feed", "priority": 1},
    {"name": "1275 News", "url": "https://1275.ru/news/feed", "priority": 2},
    {"name": "1275 Security", "url": "https://1275.ru/security/feed", "priority": 2},
    {"name": "AntiMalware News", "url": "https://www.anti-malware.ru/news/feed", "priority": 1},
    {"name": "SecurityLab", "url": "https://www.securitylab.ru/rss/allnews/", "priority": 1},
]

# ============ РАСШИРЕННЫЙ ПРОМПТ ДЛЯ КРЕАТИВНОСТИ ============

POST_STYLES = [
    {
        "name": "story",
        "prompt": """
Ты ведёшь Telegram-канал «KIBER SOS» про цифровую безопасность.
Напиши пост в стиле ИСТОРИИ — начни с короткого сценария от первого лица.

Формат:
📱 История:
2-3 предложения от лица обычного человека, который чуть не попался (или попался).

🔴 Что произошло:
1-2 предложения — суть угрозы простым языком.

🛡 Как защититься:
3-4 конкретных действия, которые можно сделать за 5 минут.

✅ Вывод:
1 мотивирующее предложение.

Объём: 700-1000 символов. Язык простой, живой, без корпоративщины.
"""
    },
    {
        "name": "checklist",
        "prompt": """
Ты ведёшь Telegram-канал «KIBER SOS» про цифровую безопасность.
Напиши пост в стиле ЧЕКЛИСТА — максимум практики, минимум воды.

Формат:
⚡️ [Броский заголовок про угрозу]

Что случилось: 1-2 предложения о проблеме.

✅ Чеклист защиты:
□ Действие 1 (конкретное)
□ Действие 2 (конкретное)
□ Действие 3 (конкретное)
□ Действие 4 (конкретное)
□ Действие 5 (если нужно)

⏱ Время: X минут
🎯 Результат: что получишь, если сделаешь.

Объём: 600-900 символов. Каждый пункт — конкретное действие.
"""
    },
    {
        "name": "myth_buster",
        "prompt": """
Ты ведёшь Telegram-канал «KIBER SOS» про цифровую безопасность.
Напиши пост в стиле РАЗРУШИТЕЛЬ МИФОВ.

Формат:
🤔 Миф: [Распространённое заблуждение по теме новости]

❌ Почему это неправда:
2-3 предложения с объяснением.

✅ Как на самом деле:
2-3 предложения правды.

🛠 Что делать:
3-4 практических шага.

Объём: 700-1000 символов. Стиль — дружелюбный эксперт.
"""
    },
    {
        "name": "warning",
        "prompt": """
Ты ведёшь Telegram-канал «KIBER SOS» про цифровую безопасность.
Напиши пост в стиле СРОЧНОЕ ПРЕДУПРЕЖДЕНИЕ.

Формат:
🚨 ВНИМАНИЕ: [Суть угрозы в 5-7 словах]

Что происходит:
2-3 предложения о проблеме — конкретно и страшновато (но без паники).

Кто в зоне риска:
1-2 предложения — кого это касается.

🛡 Защитись сейчас:
1. Действие (конкретное)
2. Действие (конкретное)
3. Действие (конкретное)
4. Действие (конкретное)

💪 Сделай это — и угроза тебя не коснётся.

Объём: 700-950 символов. Тон — срочный, но не паникёрский.
"""
    },
    {
        "name": "explainer",
        "prompt": """
Ты ведёшь Telegram-канал «KIBER SOS» про цифровую безопасность.
Напиши пост в стиле ОБЪЯСНЯЛКА — как будто рассказываешь другу.

Формат:
🔍 [Вопрос, который мог бы задать читатель]

Короткий ответ: 1 предложение.

Подробнее:
3-4 предложения простым языком — что, как, почему.

Что с этим делать:
• Совет 1
• Совет 2
• Совет 3
• Совет 4

📌 Запомни: [Ключевая мысль одним предложением]

Объём: 700-1000 символов. Тон — умный друг, не зануда.
"""
    },
]

# ============ КЛЮЧЕВЫЕ СЛОВА ============

SECURITY_KEYWORDS = [
    "уязвимость", "уязвимости", "vulnerability", "vulnerabilities",
    "утечка", "утечка данных", "data breach", "leak", "breach",
    "взлом", "взломали", "hack", "hacked",
    "фишинг", "phishing", "scam", "мошенничество",
    "malware", "вредоносное", "ransomware", "троян",
    "пароль", "password", "двухфакторная", "2fa",
    "браузер", "browser", "расширение",
    "android", "ios", "windows", "macos", "telegram", "whatsapp",
    "приватность", "privacy", "слежка", "tracking",
    "vpn", "шифрование", "encryption"
]

SENSATIONAL_KEYWORDS = [
    "взлом", "взломали", "утечка", "data breach", "leak",
    "ransomware", "шантаж", "выкуп", "шифровальщик",
    "кибератака", "атака", "ddos", "фишинг",
    "0-day", "нулевого дня", "критическ", "массов"
]

EXCLUDE_KEYWORDS = [
    "акции", "биржа", "котировки", "инвестиции", "ipo",
    "капитализация", "выручка", "прибыль",
    "курс доллара", "политик", "выборы",
    "футбол", "спорт", "фильм", "сериал",
    "биткоин", "криптовалют",
]

BAD_PHRASES = [
    "предлагает решение", "комплексное решение",
    "идеальное решение", "уникальное решение",
    "высококачественную защиту", "надёжную защиту",
]


# ============ STATE MANAGEMENT ============

def load_json_file(filepath: str, default: any) -> any:
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ Ошибка чтения {filepath}: {e}")
    return default


def save_json_file(filepath: str, data: any) -> None:
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ Ошибка записи {filepath}: {e}")


def get_article_hash(title: str, link: str) -> str:
    """Создаём уникальный хэш статьи."""
    content = f"{title}|{link}"
    return hashlib.md5(content.encode()).hexdigest()[:16]


class StateManager:
    def __init__(self):
        self.posted_articles: Dict[str, float] = {}
        self.source_rotation: Dict = {
            "last_source_index": -1,
            "last_style_index": -1,
            "source_post_counts": {},
            "daily_sources_used": [],
            "last_reset_date": None
        }
        self._load_state()
    
    def _load_state(self):
        # Загрузка опубликованных статей
        posted_data = load_json_file(POSTED_FILE, [])
        if isinstance(posted_data, list):
            self.posted_articles = {
                item.get("id", item.get("hash", "")): item.get("timestamp", 0) 
                for item in posted_data if item
            }
        
        # Загрузка ротации
        self.source_rotation = load_json_file(SOURCE_ROTATION_FILE, self.source_rotation)
        
        # Сброс дневной статистики если новый день
        today = datetime.now().strftime("%Y-%m-%d")
        if self.source_rotation.get("last_reset_date") != today:
            self.source_rotation["daily_sources_used"] = []
            self.source_rotation["last_reset_date"] = today
            print(f"📅 Новый день ({today}), сброс ротации источников")
    
    def save_state(self):
        # Сохранение опубликованных
        posted_list = [
            {"id": id_str, "timestamp": ts} 
            for id_str, ts in self.posted_articles.items()
        ]
        save_json_file(POSTED_FILE, posted_list)
        
        # Сохранение ротации
        save_json_file(SOURCE_ROTATION_FILE, self.source_rotation)
    
    def is_posted(self, article_id: str) -> bool:
        return article_id in self.posted_articles
    
    def mark_posted(self, article_id: str, source_name: str):
        self.posted_articles[article_id] = datetime.now().timestamp()
        
        # Обновляем счётчики
        counts = self.source_rotation.get("source_post_counts", {})
        counts[source_name] = counts.get(source_name, 0) + 1
        self.source_rotation["source_post_counts"] = counts
        
        # Добавляем в использованные сегодня
        if source_name not in self.source_rotation["daily_sources_used"]:
            self.source_rotation["daily_sources_used"].append(source_name)
        
        self.save_state()
    
    def clean_old_posts(self):
        now = datetime.now().timestamp()
        cutoff = now - (RETENTION_DAYS * 86400)
        old_count = len(self.posted_articles)
        self.posted_articles = {
            id_str: ts for id_str, ts in self.posted_articles.items()
            if ts and ts > cutoff
        }
        removed = old_count - len(self.posted_articles)
        if removed > 0:
            print(f"🧹 Удалено {removed} старых записей из кэша")
        self.save_state()
    
    def get_next_source_priority(self) -> List[str]:
        """Возвращает источники в порядке приоритета с учётом ротации."""
        used_today = set(self.source_rotation.get("daily_sources_used", []))
        counts = self.source_rotation.get("source_post_counts", {})
        
        # Сортируем: сначала неиспользованные сегодня, потом по количеству постов
        sources = []
        for src in RSS_SOURCES:
            name = src["name"]
            sources.append({
                "name": name,
                "url": src["url"],
                "used_today": name in used_today,
                "total_posts": counts.get(name, 0),
                "priority": src["priority"]
            })
        
        # Сначала неиспользованные сегодня, потом с меньшим количеством постов
        sources.sort(key=lambda x: (x["used_today"], x["total_posts"], -x["priority"]))
        
        return sources
    
    def get_next_style(self) -> Dict:
        """Возвращает следующий стиль поста с ротацией."""
        last_idx = self.source_rotation.get("last_style_index", -1)
        next_idx = (last_idx + 1) % len(POST_STYLES)
        self.source_rotation["last_style_index"] = next_idx
        return POST_STYLES[next_idx]


state = StateManager()


# ============ HELPERS ============

def clean_text(text: str) -> str:
    text = re.sub(r'<[^>]+>', '', text)  # Убираем HTML теги
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def ensure_complete_sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return text
    if text[-1] in ".!?":
        return text
    
    for end_char in [". ", "! ", "? "]:
        last_pos = text.rfind(end_char)
        if last_pos > len(text) * 0.5:  # Не обрезаем больше половины
            return text[:last_pos + 1]
    
    return text + "."


def get_hashtags() -> str:
    tags_pool = [
        "#безопасность", "#кибербезопасность", "#приватность",
        "#защита", "#пароли", "#фишинг", "#взлом", "#данные",
        "#смартфон", "#интернет", "#советы"
    ]
    selected = random.sample(tags_pool, min(3, len(tags_pool)))
    return " ".join(selected)


def build_final_post(core_text: str, link: str, max_total: int = 1024) -> str:
    cta_variants = [
        "\n\n💾 Сохрани и перешли тем, кому это важно.",
        "\n\n📲 Полезно? Перешли друзьям и родным.",
        "\n\n🔄 Поделись с близкими — пусть тоже будут в безопасности.",
        "\n\n👆 Сохрани пост — пригодится.",
    ]
    cta_line = random.choice(cta_variants)
    source_line = f'\n\n🔗 <a href="{link}">Источник</a>'
    hashtag_line = f"\n\n{get_hashtags()}"
    
    service_length = len(cta_line) + len(source_line) + len(hashtag_line)
    max_core = max_total - service_length - 20
    
    if len(core_text) > max_core:
        core_text = core_text[:max_core]
        core_text = ensure_complete_sentence(core_text)
    
    return core_text + cta_line + hashtag_line + source_line


# ============ RSS LOADING ============

def load_rss(url: str, source: str) -> List[Dict]:
    articles = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        feed = feedparser.parse(resp.content)
        
        if feed.bozo and not feed.entries:
            print(f"⚠️ RSS недоступен: {source}")
            return articles
    except Exception as e:
        print(f"❌ Ошибка загрузки RSS {source}: {e}")
        return articles

    now = datetime.now()
    max_age = timedelta(days=MAX_ARTICLE_AGE_DAYS)

    for entry in feed.entries[:30]:
        link = entry.get("link", "")
        title = clean_text(entry.get("title", ""))
        
        if not link or not title:
            continue
        
        article_id = get_article_hash(title, link)
        
        if state.is_posted(article_id):
            continue

        pub_dt = now
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try:
                pub_dt = datetime(*entry.published_parsed[:6])
            except:
                pass

        if now - pub_dt > max_age:
            continue

        summary = clean_text(entry.get("summary", "") or entry.get("description", ""))
        
        articles.append({
            "id": article_id,
            "title": title,
            "summary": summary[:1000],
            "link": link,
            "source": source,
            "published": pub_dt,
        })

    return articles


def load_all_articles() -> Dict[str, List[Dict]]:
    """Загружает статьи, группируя по источникам."""
    articles_by_source: Dict[str, List[Dict]] = {}
    
    for src in RSS_SOURCES:
        name = src["name"]
        url = src["url"]
        articles = load_rss(url, name)
        
        if articles:
            print(f"✅ {name}: {len(articles)} свежих статей")
            articles_by_source[name] = articles
        else:
            print(f"⚪ {name}: нет новых статей")
    
    return articles_by_source


def filter_article(article: Dict) -> Optional[str]:
    """Проверяет статью и возвращает тип (sensational/security) или None."""
    text = f"{article['title']} {article['summary']}".lower()
    
    if any(kw in text for kw in EXCLUDE_KEYWORDS):
        return None
    
    is_sensational = any(kw in text for kw in SENSATIONAL_KEYWORDS)
    has_security = any(kw in text for kw in SECURITY_KEYWORDS)
    
    if is_sensational:
        return "sensational"
    elif has_security:
        return "security"
    
    return None


def select_best_article(articles_by_source: Dict[str, List[Dict]]) -> Optional[Dict]:
    """Выбирает лучшую статью с учётом ротации источников."""
    
    source_priority = state.get_next_source_priority()
    print(f"\n📊 Приоритет источников: {[s['name'] for s in source_priority]}")
    
    for src_info in source_priority:
        source_name = src_info["name"]
        
        if source_name not in articles_by_source:
            continue
        
        articles = articles_by_source[source_name]
        
        # Сортируем: сначала sensational, потом по дате
        scored_articles = []
        for art in articles:
            art_type = filter_article(art)
            if art_type:
                score = 2 if art_type == "sensational" else 1
                scored_articles.append((score, art["published"], art, art_type))
        
        if not scored_articles:
            continue
        
        # Сортировка: по score (desc), потом по дате (desc)
        scored_articles.sort(key=lambda x: (x[0], x[1]), reverse=True)
        
        # Берём одну из топ-3 случайно (для разнообразия)
        top_n = min(3, len(scored_articles))
        selected = random.choice(scored_articles[:top_n])
        
        article = selected[2]
        article["post_type"] = selected[3]
        
        print(f"✅ Выбрана статья из {source_name}: {article['title'][:50]}...")
        return article
    
    return None


# ============ ГЕНЕРАЦИЯ ТЕКСТА ============

def generate_post_text(article: Dict) -> Optional[str]:
    """Генерирует креативный пост с ротацией стилей."""
    
    style = state.get_next_style()
    print(f"  🎨 Стиль поста: {style['name']}")
    
    news_context = f"""
НОВОСТЬ:
Заголовок: {article['title']}

Содержание: {article['summary']}

Источник: {article['source']}
"""
    
    full_prompt = style["prompt"] + "\n\n" + news_context + """

ВАЖНО:
- Не выдумывай факты, которых нет в новости
- Пиши только про защиту, никаких инструкций по взлому
- Язык простой, для обычных людей
- Без рекламы и корпоративного жаргона
"""

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты — автор популярного Telegram-канала о цифровой безопасности. "
                        "Пишешь живо, понятно, с заботой о читателе. "
                        "Никакой воды, только польза."
                    ),
                },
                {"role": "user", "content": full_prompt},
            ],
            temperature=0.7,  # Повышаем для креативности
            max_tokens=800,
        )
        
        text = response.choices[0].message.content.strip()
        
        # Очистка
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1]
        if text.startswith("«") and text.endswith("»"):
            text = text[1:-1]
        
        # Проверки
        if len(text) < 200:
            print(f"  ⚠️ Слишком короткий текст: {len(text)} символов")
            return None
        
        if any(phrase in text.lower() for phrase in BAD_PHRASES):
            print("  ⚠️ Обнаружен рекламный текст")
            return None
        
        final = build_final_post(text, article["link"])
        print(f"  ✅ Сгенерирован пост: {len(final)} символов")
        return final
        
    except Exception as e:
        print(f"  ❌ Ошибка OpenAI: {e}")
        return None


# ============ ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ ============

IMAGE_THEMES = [
    ("minimalist flat vector, cybersecurity", "blue and white"),
    ("3D isometric illustration, digital security", "purple gradient"),
    ("neon glow style, cyber protection", "dark with cyan"),
    ("modern geometric art, data privacy", "teal and orange"),
    ("clean infographic style, online safety", "green accents"),
    ("abstract digital art, secure technology", "blue and gold"),
    ("low poly 3D render, internet protection", "gradient mesh"),
    ("line art illustration, mobile security", "monochrome with red"),
]


def generate_image(title: str) -> Optional[str]:
    """Генерирует уникальное изображение."""
    
    theme = random.choice(IMAGE_THEMES)
    seed = random.randint(1, 999999999)
    
    # Извлекаем ключевые слова из заголовка
    keywords = title[:50].replace('"', '').replace("'", "")
    
    prompt = (
        f"{theme[0]}, {theme[1]} color scheme, "
        f"concept about: {keywords}, "
        "professional quality, no text, no letters, no watermark, "
        "clean composition, 4k"
    )
    
    encoded_prompt = urllib.parse.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?seed={seed}&width=1024&height=1024&nologo=true"
    
    print(f"  🎨 Генерация изображения (seed: {seed})...")
    
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=90, headers=HEADERS)
            
            if resp.status_code == 200 and len(resp.content) > 10000:
                filename = f"img_{seed}.jpg"
                with open(filename, "wb") as f:
                    f.write(resp.content)
                print(f"  ✅ Изображение сохранено: {filename}")
                return filename
            
        except Exception as e:
            print(f"  ⚠️ Попытка {attempt + 1}: {e}")
            time.sleep(3)
    
    print("  ❌ Не удалось создать изображение")
    return None


def cleanup_image(filepath: Optional[str]):
    if filepath and os.path.exists(filepath):
        try:
            os.remove(filepath)
        except:
            pass


# ============ MAIN ============

async def autopost():
    print("🚀 KIBER SOS Autopost запущен")
    print(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    state.clean_old_posts()
    
    print("\n🔄 Загрузка статей...")
    articles_by_source = load_all_articles()
    
    total_articles = sum(len(arts) for arts in articles_by_source.values())
    print(f"\n📊 Всего найдено: {total_articles} статей")
    
    if total_articles == 0:
        print("❌ Нет подходящих статей")
        return
    
    # Выбираем лучшую статью с ротацией
    article = select_best_article(articles_by_source)
    
    if not article:
        print("❌ Не найдено подходящих статей после фильтрации")
        return
    
    print(f"\n🔍 Обработка: {article['title'][:70]}...")
    print(f"   Источник: {article['source']}")
    print(f"   Тип: {article.get('post_type', 'unknown')}")
    
    # Генерируем текст
    post_text = generate_post_text(article)
    
    if not post_text:
        print("❌ Не удалось сгенерировать текст")
        return
    
    # Генерируем картинку
    image_path = generate_image(article["title"])
    
    # Публикуем
    try:
        if image_path:
            await bot.send_photo(
                CHANNEL_ID,
                photo=FSInputFile(image_path),
                caption=post_text,
            )
        else:
            await bot.send_message(CHANNEL_ID, text=post_text)
        
        state.mark_posted(article["id"], article["source"])
        print(f"\n✅ Опубликовано успешно!")
        print(f"   Источник: {article['source']}")
        print(f"   ID: {article['id']}")
        
    except Exception as e:
        print(f"❌ Ошибка публикации: {e}")
    finally:
        cleanup_image(image_path)


async def main():
    try:
        await autopost()
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
