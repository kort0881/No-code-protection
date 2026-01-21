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
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

CACHE_DIR = os.getenv("CACHE_DIR", "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

STATE_FILE = os.path.join(CACHE_DIR, "state.json")

RETENTION_DAYS = 14
MAX_ARTICLE_AGE_DAYS = 3

# ============ RSS ИСТОЧНИКИ ============

RSS_SOURCES = [
    {"name": "SecurityLab", "url": "https://www.securitylab.ru/rss/allnews/"},
    {"name": "AntiMalware", "url": "https://www.anti-malware.ru/news/feed"},
    {"name": "1275 Vulnerabilities", "url": "https://1275.ru/vulnerability/feed"},
    {"name": "1275 News", "url": "https://1275.ru/news/feed"},
    {"name": "1275 Security", "url": "https://1275.ru/security/feed"},
]

# ============ КЛЮЧЕВЫЕ СЛОВА ДЛЯ ОБЫЧНЫХ ЛЮДЕЙ ============

# Темы, которые касаются обычных пользователей
USER_RELEVANT_KEYWORDS = [
    # Устройства и приложения
    "android", "iphone", "ios", "смартфон", "телефон",
    "windows", "macos", "mac", "ноутбук", "компьютер",
    "chrome", "firefox", "safari", "браузер", "edge",
    "telegram", "whatsapp", "viber", "signal", "мессенджер",
    "instagram", "facebook", "вконтакте", "vk", "tiktok", "youtube",
    "gmail", "почта", "email", "outlook",
    "банк", "сбербанк", "тинькофф", "онлайн-банк", "карта", "оплата",
    "wi-fi", "wifi", "роутер", "bluetooth",
    
    # Угрозы для людей
    "фишинг", "phishing", "мошенник", "мошенничество", "развод",
    "пароль", "password", "взлом аккаунта", "украли аккаунт",
    "утечка данных", "слив", "персональные данные",
    "вирус", "троян", "шпион", "слежка", "stalkerware",
    "спам", "звонки мошенников", "смс мошенники",
    "кража денег", "списали деньги", "украли деньги",
    "шантаж", "вымогатель", "ransomware",
    "двухфакторная", "2fa", "sms-код", "подтверждение",
    "vpn", "приватность", "слежка", "tracking",
    "расширение браузера", "приложение", "вредоносное приложение",
    "qr-код", "qr код", "ссылка", "поддельный сайт",
]

# Темы, которые НЕ интересны обычным людям — пропускаем
SKIP_KEYWORDS = [
    # Корпоративное
    "корпоративн", "enterprise", "b2b", "soc ", "siem",
    "apt ", "apt-", "таргетированн", "целевая атака",
    "инфраструктур", "периметр", "сегментац",
    
    # Специфичные технологии
    "kubernetes", "docker", "контейнер", "облачн",
    "api ", "sdk", "middleware", "backend",
    "sql injection", "xss", "csrf", "ssrf",
    "cve-", "cvss", "nist", "mitre",
    
    # Серверное/админское
    "сервер", "server", "linux ", "unix", "freebsd",
    "apache", "nginx", "iis", "exchange",
    "active directory", "ldap", "kerberos",
    "ssh", "telnet", "ftp", "smtp",
    "firewall", "ids", "ips", "waf",
    
    # Бизнес-новости
    "акции", "биржа", "инвестиц", "ipo", "капитализац",
    "назначен", "покидает", "гендиректор", "ceo",
    "партнёрство", "сделка", "поглощен", "слияние",
    
    # Прочее нерелевантное
    "криптовалют", "биткоин", "майнинг",
    "военн", "армия", "разведка", "шпионаж государств",
]

# ============ СТИЛИ ПОСТОВ ДЛЯ ОБЫЧНЫХ ЛЮДЕЙ ============

POST_STYLES = [
    {
        "name": "protection_guide",
        "system": """Ты ведёшь Telegram-канал «KIBER SOS» для обычных людей — не айтишников.
Твоя задача: превращать новости о киберугрозах в ПРАКТИЧЕСКИЕ ИНСТРУКЦИИ.

Твой читатель: человек 25-50 лет, пользуется смартфоном и компьютером, 
но не разбирается в технических деталях. Ему важно понять: 
1) Касается ли это ЛИЧНО ЕГО? 
2) Что КОНКРЕТНО сделать прямо сейчас?

Тон: дружелюбный эксперт, старший брат/сестра, который объясняет просто, 
но не как ребёнку. Без запугивания, но с серьёзностью.""",
        
        "prompt": """Напиши пост-инструкцию для обычного человека.

СТРУКТУРА:

🔔 [Заголовок: о чём угроза простыми словами, 5-8 слов]

Кого касается:
Одно предложение — чётко определи, касается ли это обычных людей.
Например: «Если пользуетесь Chrome на телефоне или компьютере — читайте».

В чём опасность:
2-3 предложения БЕЗ технических терминов. Объясни как для друга:
— Что могут украсть/сломать/узнать?
— Как это происходит (в двух словах)?

📱 Что сделать прямо сейчас:

1. [Действие] 
   → Пошагово: куда нажать, что выбрать
   
2. [Действие]
   → Пошагово: куда нажать, что выбрать
   
3. [Действие]
   → Пошагово: куда нажать, что выбрать

⏱ Займёт: X минут

✅ После этого: [что изменится, почему станет безопасно]

ПРАВИЛА:
- Никаких CVE, CVSS, технических терминов
- Каждый шаг — конкретные действия с указанием меню/кнопок
- Если новость НЕ касается обычных людей — так и напиши в начале
- Объём: 700-1000 символов"""
    },
    {
        "name": "real_story",
        "system": """Ты ведёшь Telegram-канал «KIBER SOS» для обычных людей.
Твой формат: реальные истории + практические советы.

Умеешь превращать сухие новости в живые истории, которые показывают,
как это может случиться с любым человеком. Без драматизации, но наглядно.""",
        
        "prompt": """Напиши пост в формате «История + Защита».

СТРУКТУРА:

😰 Представь ситуацию:
3-4 предложения — опиши реалистичный сценарий от первого или третьего лица.
Как обычный человек мог попасть в эту ситуацию? 
Не фантастика, а то, что реально случается.

🎯 Что на самом деле происходит:
2 предложения — объясни суть угрозы простыми словами.
Без терминов, как будто объясняешь маме/папе.

🛡 Как защититься:

Шаг 1: [Название]
Что делать: конкретная инструкция — куда зайти, что нажать

Шаг 2: [Название]  
Что делать: конкретная инструкция

Шаг 3: [Название]
Что делать: конкретная инструкция

💡 Главное правило: [Одно предложение — ключевой вывод]

ПРАВИЛА:
- История должна быть узнаваемой и реалистичной
- Шаги — конкретные, с указанием где и что нажимать
- Объём: 800-1100 символов"""
    },
    {
        "name": "quick_check",
        "system": """Ты ведёшь Telegram-канал «KIBER SOS» — быстрые проверки безопасности.
Формат: минимум текста, максимум действий. Человек должен за 5 минут 
проверить и защитить себя.""",
        
        "prompt": """Напиши пост-чеклист для быстрой проверки.

СТРУКТУРА:

⚡️ Проверь за 5 минут: [тема проверки]

Почему важно: 1-2 предложения — что случилось и кого касается.

✅ Чеклист:

□ [Проверка 1]
  Как: [конкретно куда зайти и что проверить]
  
□ [Проверка 2]
  Как: [конкретно куда зайти и что проверить]
  
□ [Проверка 3]
  Как: [конкретно куда зайти и что проверить]
  
□ [Проверка 4]
  Как: [конкретно куда зайти и что проверить]

🔒 Бонус для параноиков: [дополнительный совет для тех, кто хочет максимум защиты]

⏱ Время: 5 минут
📱 Устройства: [на каких устройствах проверить]

ПРАВИЛА:
- Каждый пункт — конкретное действие
- Указывай путь: Настройки → Раздел → Пункт
- Объём: 600-900 символов"""
    },
    {
        "name": "warning_simple", 
        "system": """Ты ведёшь Telegram-канал «KIBER SOS» — предупреждения об угрозах.
Пишешь срочные посты, когда нужно быстро предупредить людей.
Без паники, но с ясным призывом к действию.""",
        
        "prompt": """Напиши пост-предупреждение для обычных людей.

СТРУКТУРА:

🚨 [Короткий заголовок — суть угрозы]

Что случилось:
2-3 предложения простым языком. Без технических деталей.
Главное — объяснить, чем это грозит обычному человеку.

Кто в зоне риска:
Одно предложение — чётко определи, кого это касается.
Например: «Все, кто пользуется WhatsApp на Android».

⚠️ Признаки проблемы:
— Как понять, что тебя это коснулось?
— На что обратить внимание?

🛡 Что делать:

1. [Срочное действие]
   → Куда зайти, что нажать
   
2. [Следующее действие]  
   → Куда зайти, что нажать
   
3. [Защитное действие]
   → Куда зайти, что нажать

📌 Запомни: [главный вывод одним предложением]

ПРАВИЛА:
- Пиши так, чтобы понял человек без технического образования
- Конкретные шаги с указанием меню и кнопок
- Объём: 700-1000 символов"""
    },
    {
        "name": "myth_buster",
        "system": """Ты ведёшь Telegram-канал «KIBER SOS» — разрушаешь мифы о безопасности.
Берёшь новость и показываешь, какие заблуждения есть у людей по этой теме.
Стиль: умный друг, который объясняет, как на самом деле.""",
        
        "prompt": """Напиши пост-разоблачение мифа на основе новости.

СТРУКТУРА:

🤔 Миф: «[распространённое заблуждение по теме]»

Многие думают: 1-2 предложения — опиши типичное заблуждение.

❌ На самом деле:
3-4 предложения — объясни, почему это не так.
Приведи пример из новости. Без технических терминов.

✅ Как правильно:
2-3 предложения — что нужно понимать на самом деле.

🛡 Что сделать:

1. [Действие с инструкцией]
2. [Действие с инструкцией]  
3. [Действие с инструкцией]

💡 Вывод: [одно предложение — главная мысль]

ПРАВИЛА:
- Миф должен быть реальным и распространённым
- Объяснение — простое и понятное
- Объём: 700-1000 символов"""
    },
]

# ============ STATE MANAGER ============

class State:
    def __init__(self):
        self.data = {
            "posted_ids": {},
            "source_index": 0,
            "style_index": 0,
            "last_run": None,
            "stats": {}
        }
        self._load()
    
    def _load(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    self.data.update(loaded)
                print(f"📂 Загружено: {len(self.data['posted_ids'])} постов в истории")
                print(f"   Следующий источник: {RSS_SOURCES[self.data['source_index'] % len(RSS_SOURCES)]['name']}")
                print(f"   Следующий стиль: {POST_STYLES[self.data['style_index'] % len(POST_STYLES)]['name']}")
            except Exception as e:
                print(f"⚠️ Ошибка загрузки state: {e}")
    
    def save(self):
        self.data["last_run"] = datetime.now().isoformat()
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            print(f"💾 Состояние сохранено")
        except Exception as e:
            print(f"❌ Ошибка сохранения: {e}")
    
    def is_posted(self, article_id: str) -> bool:
        return article_id in self.data["posted_ids"]
    
    def mark_posted(self, article_id: str, source: str, title: str):
        self.data["posted_ids"][article_id] = {
            "ts": datetime.now().timestamp(),
            "source": source,
            "title": title[:100]
        }
        stats = self.data.get("stats", {})
        stats[source] = stats.get(source, 0) + 1
        self.data["stats"] = stats
        self.save()
    
    def cleanup_old(self):
        cutoff = datetime.now().timestamp() - (RETENTION_DAYS * 86400)
        old_count = len(self.data["posted_ids"])
        self.data["posted_ids"] = {
            k: v for k, v in self.data["posted_ids"].items()
            if isinstance(v, dict) and v.get("ts", 0) > cutoff
        }
        removed = old_count - len(self.data["posted_ids"])
        if removed > 0:
            print(f"🧹 Очищено {removed} старых записей")
    
    def get_next_source_order(self) -> List[Dict]:
        idx = self.data.get("source_index", 0) % len(RSS_SOURCES)
        ordered = RSS_SOURCES[idx:] + RSS_SOURCES[:idx]
        self.data["source_index"] = (idx + 1) % len(RSS_SOURCES)
        print(f"📍 Порядок источников: {[s['name'] for s in ordered]}")
        return ordered
    
    def get_next_style(self) -> Dict:
        idx = self.data.get("style_index", 0) % len(POST_STYLES)
        style = POST_STYLES[idx]
        self.data["style_index"] = (idx + 1) % len(POST_STYLES)
        return style


state = State()


# ============ HELPERS ============

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&\w+;', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def get_article_id(title: str, link: str) -> str:
    content = f"{title}|{link}"
    return hashlib.sha256(content.encode()).hexdigest()[:20]


def is_relevant_for_users(title: str, summary: str) -> bool:
    """Проверяет, релевантна ли новость для обычных пользователей."""
    text = f"{title} {summary}".lower()
    
    # Сначала проверяем на исключения — корпоративные/серверные темы
    for skip_word in SKIP_KEYWORDS:
        if skip_word.lower() in text:
            return False
    
    # Теперь проверяем на релевантность для пользователей
    for keyword in USER_RELEVANT_KEYWORDS:
        if keyword.lower() in text:
            return True
    
    return False


def get_random_hashtags() -> str:
    pools = [
        ["#безопасность", "#защита", "#киберсос"],
        ["#смартфон", "#телефон", "#android", "#iphone"],
        ["#советы", "#инструкция", "#чтоделать"],
    ]
    tags = [random.choice(pool) for pool in random.sample(pools, 2)]
    return " ".join(tags)


def build_final_post(text: str, link: str) -> str:
    text = re.sub(r'\n{3,}', '\n\n', text.strip())
    
    cta_options = [
        "\n\n📲 Перешли тем, кого это тоже касается",
        "\n\n💾 Сохрани, чтобы не забыть",
        "\n\n📢 Расскажи близким — пусть тоже проверят",
        "\n\n👆 Отправь друзьям и родителям",
    ]
    
    footer = random.choice(cta_options)
    footer += f"\n\n{get_random_hashtags()}"
    footer += f'\n\n<a href="{link}">Подробнее</a>'
    
    max_text = 1024 - len(footer) - 50
    
    if len(text) > max_text:
        text = text[:max_text]
        for end in ['. ', '! ', '? ', '.\n']:
            pos = text.rfind(end)
            if pos > max_text * 0.6:
                text = text[:pos+1]
                break
    
    return text + footer


# ============ RSS LOADING ============

def load_rss(url: str, source_name: str) -> List[Dict]:
    articles = []
    
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception as e:
        print(f"❌ {source_name}: ошибка — {e}")
        return []
    
    if not feed.entries:
        print(f"⚪ {source_name}: пустой фид")
        return []
    
    now = datetime.now()
    max_age = timedelta(days=MAX_ARTICLE_AGE_DAYS)
    
    for entry in feed.entries[:25]:
        title = clean_text(entry.get("title", ""))
        link = entry.get("link", "")
        
        if not title or not link:
            continue
        
        article_id = get_article_id(title, link)
        
        if state.is_posted(article_id):
            continue
        
        pub_date = now
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try:
                pub_date = datetime(*entry.published_parsed[:6])
            except:
                pass
        
        if now - pub_date > max_age:
            continue
        
        summary = clean_text(entry.get("summary", "") or entry.get("description", ""))
        
        # ГЛАВНАЯ ПРОВЕРКА: подходит ли для обычных людей?
        if not is_relevant_for_users(title, summary):
            continue
        
        articles.append({
            "id": article_id,
            "title": title,
            "summary": summary[:1500],
            "link": link,
            "source": source_name,
            "date": pub_date,
        })
    
    if articles:
        print(f"✅ {source_name}: {len(articles)} статей для людей")
    else:
        print(f"⚪ {source_name}: нет подходящих статей")
    
    return articles


# ============ TEXT GENERATION ============

def generate_post(article: Dict, style: Dict) -> Optional[str]:
    print(f"  🎨 Стиль: {style['name']}")
    
    user_prompt = style["prompt"] + f"""

---
НОВОСТЬ:

Заголовок: {article['title']}

Содержание: {article['summary']}
---

КРИТИЧЕСКИ ВАЖНО:
1. Пиши для ОБЫЧНОГО ЧЕЛОВЕКА, не для айтишника
2. Никаких терминов: CVE, RCE, XSS, API, бэкенд, инфраструктура
3. Каждый шаг инструкции — конкретный: «Откройте Настройки → Безопасность → ...»
4. Если новость не касается обычных людей — честно скажи это в начале
5. Примеры устройств: телефон, компьютер, браузер, приложение
6. Примеры действий: обновить приложение, сменить пароль, включить защиту
"""

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": style["system"]},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.7,
            max_tokens=1000,
        )
        
        text = response.choices[0].message.content.strip()
        
        if len(text) < 300:
            print(f"  ⚠️ Слишком короткий: {len(text)}")
            return None
        
        final = build_final_post(text, article["link"])
        print(f"  ✅ Готово: {len(final)} символов")
        return final
        
    except Exception as e:
        print(f"  ❌ Ошибка: {e}")
        return None


# ============ IMAGE GENERATION ============

IMAGE_STYLES = [
    "friendly illustration about {topic}, warm colors, simple, modern",
    "clean vector art, {topic}, blue and white, safe feeling",
    "smartphone and protection concept, {topic}, minimal style",
    "digital safety illustration, {topic}, friendly, non-threatening",
    "modern flat design, {topic}, security shield, positive mood",
]


def generate_image(title: str) -> Optional[str]:
    style = random.choice(IMAGE_STYLES)
    seed = random.randint(1, 999999999)
    
    keywords = re.sub(r'[^\w\s]', '', title)[:40]
    prompt = style.format(topic=keywords) + ", no text, no letters, 4k quality"
    
    url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(prompt)}?seed={seed}&width=1024&height=1024&nologo=true"
    
    print(f"  🖼 Генерация изображения...")
    
    for attempt in range(2):
        try:
            resp = requests.get(url, timeout=60, headers=HEADERS)
            if resp.status_code == 200 and len(resp.content) > 10000:
                filename = f"img_{seed}.jpg"
                with open(filename, "wb") as f:
                    f.write(resp.content)
                print(f"  ✅ Изображение готово")
                return filename
        except Exception as e:
            print(f"  ⚠️ Попытка {attempt+1}: {e}")
            time.sleep(2)
    
    return None


# ============ MAIN ============

async def autopost():
    print("=" * 50)
    print(f"🚀 KIBER SOS — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)
    
    state.cleanup_old()
    sources = state.get_next_source_order()
    
    print("\n📡 Загрузка RSS...")
    
    all_articles = []
    for src in sources:
        articles = load_rss(src["url"], src["name"])
        all_articles.extend(articles)
    
    if not all_articles:
        print("\n❌ Нет новых статей для обычных людей")
        state.save()
        return
    
    print(f"\n📊 Найдено статей: {len(all_articles)}")
    
    # Сортируем по дате
    all_articles.sort(key=lambda x: x["date"], reverse=True)
    
    # Выбираем согласно ротации
    article = None
    for src in sources:
        for art in all_articles:
            if art["source"] == src["name"]:
                article = art
                break
        if article:
            break
    
    if not article:
        article = all_articles[0]
    
    print(f"\n📝 Выбрана:")
    print(f"   {article['title'][:70]}...")
    print(f"   Источник: {article['source']}")
    
    style = state.get_next_style()
    post_text = generate_post(article, style)
    
    if not post_text:
        print("❌ Не удалось создать пост")
        state.save()
        return
    
    image_path = generate_image(article["title"])
    
    try:
        if image_path:
            await bot.send_photo(
                CHANNEL_ID,
                photo=FSInputFile(image_path),
                caption=post_text
            )
        else:
            await bot.send_message(CHANNEL_ID, text=post_text)
        
        state.mark_posted(article["id"], article["source"], article["title"])
        
        print(f"\n✅ ОПУБЛИКОВАНО!")
        print(f"   Стиль: {style['name']}")
        
    except Exception as e:
        print(f"\n❌ Ошибка: {e}")
    finally:
        if image_path and os.path.exists(image_path):
            os.remove(image_path)
    
    print(f"\n📈 Статистика:")
    for src, count in state.data.get("stats", {}).items():
        print(f"   {src}: {count}")


async def main():
    try:
        await autopost()
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
