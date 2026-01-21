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

# ============ ПРОФЕССИОНАЛЬНЫЕ СТИЛИ ПОСТОВ ============

POST_STYLES = [
    {
        "name": "analytical",
        "system": """Ты — эксперт по кибербезопасности с 15-летним опытом. 
Пишешь для Telegram-канала «KIBER SOS». Твоя аудитория — взрослые люди 25-45 лет, 
которые разбираются в технологиях на среднем уровне. 

Стиль: профессиональный, но доступный. Без сюсюканья и примитивных объяснений.
Используй конкретные технические детали, но объясняй их суть.""",
        
        "prompt": """Напиши аналитический пост о данной угрозе.

СТРУКТУРА:

⚡️ [Цепляющий заголовок — суть угрозы в 5-8 словах]

Что обнаружено:
Конкретно опиши уязвимость/угрозу. Укажи технические детали: какой компонент затронут, 
тип уязвимости (RCE, XSS, privilege escalation и т.д.), CVE если есть. 2-3 предложения.

Почему это серьёзно:
Объясни реальные последствия для пользователя. Не абстрактно «могут украсть данные», 
а конкретно: какие данные, как это происходит, какой вектор атаки. 2-3 предложения.

Кто под угрозой:
Чётко определи группу риска: пользователи какой версии ПО, какой ОС, при каких условиях.

🛡 Действия:
• [Конкретный шаг 1 с указанием где и что нажать]
• [Конкретный шаг 2]
• [Конкретный шаг 3]
• [Конкретный шаг 4 если нужно]

Объём: 800-1100 символов."""
    },
    {
        "name": "urgent",
        "system": """Ты — редактор отдела кибербезопасности в крупном IT-издании.
Пишешь срочные новости для Telegram-канала «KIBER SOS».
Аудитория — технически грамотные пользователи.

Стиль: журналистский, чёткий, без воды. Факты и действия.""",
        
        "prompt": """Напиши срочный пост-предупреждение.

СТРУКТУРА:

🚨 [ЗАГОЛОВОК КАПСОМ — 5-7 слов о сути угрозы]

Ситуация:
Что произошло, когда обнаружено, кто обнаружил (если известно). 
Масштаб проблемы — сколько пользователей/устройств затронуто. 3-4 предложения с фактами.

Техническая суть:
Кратко и точно — какой механизм уязвимости, через что эксплуатируется. 1-2 предложения.

⚠️ Группа риска:
Кто именно уязвим — версии ПО, условия эксплуатации.

✅ Немедленные действия:
1. [Первый шаг — самый важный]
2. [Второй шаг]
3. [Третий шаг]
4. [Четвёртый если нужно]

📅 Патч: [информация о патче — вышел/ожидается/workaround]

Объём: 850-1150 символов."""
    },
    {
        "name": "practical",
        "system": """Ты — практикующий специалист по ИБ, консультируешь компании и частных клиентов.
Ведёшь Telegram-канал «KIBER SOS» с практическими советами.
Аудитория ценит конкретику и пошаговые инструкции.

Стиль: практичный, без лишней теории. Каждое предложение — польза.""",
        
        "prompt": """Напиши практический гайд на основе этой новости.

СТРУКТУРА:

🔧 [Заголовок-действие: «Как защититься от...» или «Проверьте настройки...»]

Контекст:
Кратко — что случилось и почему это важно именно сейчас. 2 предложения максимум.

Суть проблемы:
Технически точно, но понятно — что именно уязвимо и как атакуют. 2-3 предложения.

📋 Пошаговая защита:

Шаг 1: [Название]
→ Конкретная инструкция: куда зайти, что нажать, что ввести.

Шаг 2: [Название]  
→ Конкретная инструкция.

Шаг 3: [Название]
→ Конкретная инструкция.

Шаг 4: [Название] (если нужно)
→ Конкретная инструкция.

⏱ Время: X минут

💡 Бонус: [Дополнительный совет для продвинутых]

Объём: 900-1200 символов."""
    },
    {
        "name": "explanatory", 
        "system": """Ты — технический журналист, специализирующийся на кибербезопасности.
Умеешь объяснять сложные вещи понятно, но без упрощения до примитива.
Пишешь для канала «KIBER SOS».

Стиль: информативный, с примерами из реальной жизни. Уважаешь интеллект читателя.""",
        
        "prompt": """Напиши объясняющий пост — разбор угрозы.

СТРУКТУРА:

🔍 [Заголовок-вопрос или заголовок с сутью открытия]

Что нашли:
Подробно опиши находку/уязвимость. Кто обнаружил, в каком компоненте, 
какой тип уязвимости. 3-4 предложения с деталями.

Как это работает:
Объясни механизм атаки. Не «хакеры могут взломать», а конкретно: 
какой вектор, какие условия нужны, что получает атакующий. 2-3 предложения.

Реальный риск:
Оцени вероятность и последствия для обычного пользователя. Честно — 
если риск низкий, скажи об этом. Если высокий — объясни почему. 2 предложения.

🛡 Рекомендации:
• [Действие 1 — с обоснованием почему]
• [Действие 2]
• [Действие 3]
• [Действие 4 для параноиков/продвинутых]

Объём: 900-1200 символов."""
    },
    {
        "name": "news_digest",
        "system": """Ты — главред кибербезопасного медиа. 
Пишешь новостные дайджесты для Telegram-канала «KIBER SOS».
Умеешь выделить главное и подать сухую новость интересно.

Стиль: новостной, динамичный, с акцентом на важном.""",
        
        "prompt": """Напиши новостной пост с акцентом на практике.

СТРУКТУРА:

📰 [Новостной заголовок — факт в 6-10 словах]

Главное:
Суть новости в 2-3 предложениях. Отвечай на вопросы: что, где, когда, 
кого затрагивает, насколько серьёзно.

Детали:
Технические подробности для понимания масштаба. CVE, CVSS score если есть,
количество затронутых пользователей/устройств. 2-3 предложения.

Что известно об эксплуатации:
Есть ли случаи атак в дикой природе (in the wild)? Существует ли публичный эксплойт? 
1-2 предложения.

🔐 Что делать:
1. [Приоритетное действие]
2. [Следующее по важности]
3. [Дополнительная мера]

📌 Статус: [Патч выпущен / Ожидается / Есть workaround]

Объём: 800-1100 символов."""
    },
]

# ============ КЛЮЧЕВЫЕ СЛОВА ============

SECURITY_KEYWORDS = [
    "уязвимость", "уязвимости", "vulnerability", "vulnerabilities", "cve",
    "утечка", "утечка данных", "data breach", "leak", "breach",
    "взлом", "взломали", "hack", "hacked", "компрометация",
    "фишинг", "phishing", "scam", "мошенничество",
    "malware", "вредоносное", "ransomware", "троян", "backdoor",
    "пароль", "password", "credentials", "аутентификация",
    "rce", "remote code execution", "privilege escalation",
    "zero-day", "0-day", "нулевого дня",
    "эксплойт", "exploit", "патч", "patch", "обновление безопасности"
]

SENSATIONAL_KEYWORDS = [
    "критическ", "critical", "срочно", "urgent",
    "массов", "миллион", "million",
    "0-day", "zero-day", "нулевого дня",
    "активно эксплуатируется", "in the wild",
    "rce", "remote code execution",
    "утечка", "breach", "взлом"
]

EXCLUDE_KEYWORDS = [
    "акции", "биржа", "котировки", "инвестиции", "ipo",
    "капитализация", "выручка", "прибыль квартал",
    "политик", "выборы", "санкции",
    "футбол", "спорт", "чемпионат",
    "биткоин", "криптовалют", "токен",
    "назначен директором", "покидает пост"
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
                print(f"📂 Загружено состояние: {len(self.data['posted_ids'])} постов в истории")
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
        
        # Статистика
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
        """Возвращает источники, начиная со следующего по очереди."""
        idx = self.data.get("source_index", 0) % len(RSS_SOURCES)
        ordered = RSS_SOURCES[idx:] + RSS_SOURCES[:idx]
        
        # Сдвигаем индекс для следующего запуска
        self.data["source_index"] = (idx + 1) % len(RSS_SOURCES)
        
        print(f"📍 Порядок источников: {[s['name'] for s in ordered]}")
        return ordered
    
    def get_next_style(self) -> Dict:
        """Возвращает следующий стиль поста."""
        idx = self.data.get("style_index", 0) % len(POST_STYLES)
        style = POST_STYLES[idx]
        
        # Сдвигаем для следующего раза
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


def get_random_hashtags() -> str:
    pools = [
        ["#кибербезопасность", "#cybersecurity", "#инфобез"],
        ["#уязвимость", "#security", "#защита"],
        ["#приватность", "#privacy", "#данные"],
    ]
    tags = [random.choice(pool) for pool in random.sample(pools, 2)]
    return " ".join(tags)


def build_final_post(text: str, link: str) -> str:
    # Убираем лишние пробелы и переносы
    text = re.sub(r'\n{3,}', '\n\n', text.strip())
    
    cta_options = [
        "\n\n→ Сохрани и отправь коллегам",
        "\n\n→ Перешли тем, кому актуально",
        "\n\n→ Поделись с теми, кто должен знать",
    ]
    
    footer = random.choice(cta_options)
    footer += f"\n\n{get_random_hashtags()}"
    footer += f'\n\n<a href="{link}">Источник</a>'
    
    max_text = 1024 - len(footer) - 50
    
    if len(text) > max_text:
        text = text[:max_text]
        # Обрезаем до последнего завершённого предложения
        for end in ['. ', '! ', '? ', '.\n', '!\n', '?\n']:
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
        print(f"❌ {source_name}: ошибка загрузки — {e}")
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
        
        # Проверяем дату
        pub_date = now
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try:
                pub_date = datetime(*entry.published_parsed[:6])
            except:
                pass
        
        if now - pub_date > max_age:
            continue
        
        # Проверяем на исключения
        text_lower = title.lower()
        if any(kw in text_lower for kw in EXCLUDE_KEYWORDS):
            continue
        
        # Проверяем на релевантность
        has_security = any(kw in text_lower for kw in SECURITY_KEYWORDS)
        summary = clean_text(entry.get("summary", "") or entry.get("description", ""))
        
        if not has_security:
            summary_lower = summary.lower()
            has_security = any(kw in summary_lower for kw in SECURITY_KEYWORDS)
        
        if not has_security:
            continue
        
        # Определяем важность
        is_hot = any(kw in text_lower or kw in summary.lower() for kw in SENSATIONAL_KEYWORDS)
        
        articles.append({
            "id": article_id,
            "title": title,
            "summary": summary[:1500],
            "link": link,
            "source": source_name,
            "date": pub_date,
            "is_hot": is_hot
        })
    
    if articles:
        print(f"✅ {source_name}: {len(articles)} релевантных статей")
    else:
        print(f"⚪ {source_name}: нет подходящих статей")
    
    return articles


# ============ TEXT GENERATION ============

def generate_post(article: Dict, style: Dict) -> Optional[str]:
    print(f"  🎨 Стиль: {style['name']}")
    
    user_prompt = style["prompt"] + f"""

---
НОВОСТЬ ДЛЯ ОБРАБОТКИ:

Заголовок: {article['title']}

Содержание: {article['summary']}

Источник: {article['source']}
Дата: {article['date'].strftime('%d.%m.%Y')}
---

ВАЖНЫЕ ПРАВИЛА:
1. Используй ТОЛЬКО факты из новости, не придумывай детали
2. Если чего-то не знаешь точно — не пиши об этом
3. Давай КОНКРЕТНЫЕ инструкции: какое меню, какая кнопка, какая команда
4. Не используй фразы: «важно помнить», «не забывайте», «будьте осторожны»
5. Пиши для умных взрослых людей, не для детей
6. Объём: 800-1200 символов основного текста
"""

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": style["system"]},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.6,
            max_tokens=1000,
        )
        
        text = response.choices[0].message.content.strip()
        
        # Базовые проверки
        if len(text) < 300:
            print(f"  ⚠️ Слишком короткий: {len(text)} символов")
            return None
        
        final = build_final_post(text, article["link"])
        print(f"  ✅ Готово: {len(final)} символов")
        return final
        
    except Exception as e:
        print(f"  ❌ Ошибка генерации: {e}")
        return None


# ============ IMAGE GENERATION ============

IMAGE_STYLES = [
    "dark tech illustration, glowing circuits, {topic}, professional, 4k",
    "cybersecurity concept art, {topic}, blue neon accents, minimal, modern",
    "digital security visualization, {topic}, abstract geometric, corporate style",
    "hacker aesthetic, {topic}, dark background, code fragments, artistic",
    "infosec themed illustration, {topic}, shield motif, professional design",
]


def generate_image(title: str) -> Optional[str]:
    style = random.choice(IMAGE_STYLES)
    seed = random.randint(1, 999999999)
    
    # Ключевые слова из заголовка
    keywords = re.sub(r'[^\w\s]', '', title)[:40]
    prompt = style.format(topic=keywords) + ", no text, no watermark"
    
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
    
    # Очистка старых записей
    state.cleanup_old()
    
    # Получаем порядок источников
    sources = state.get_next_source_order()
    
    print("\n📡 Загрузка RSS...")
    
    all_articles = []
    for src in sources:
        articles = load_rss(src["url"], src["name"])
        all_articles.extend(articles)
    
    if not all_articles:
        print("\n❌ Нет новых подходящих статей")
        state.save()
        return
    
    print(f"\n📊 Всего кандидатов: {len(all_articles)}")
    
    # Сортируем: сначала горячие, потом по дате
    all_articles.sort(key=lambda x: (x["is_hot"], x["date"]), reverse=True)
    
    # Берём статью согласно ротации источников
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
    
    print(f"\n📝 Выбрана статья:")
    print(f"   {article['title'][:70]}...")
    print(f"   Источник: {article['source']}")
    print(f"   Горячая: {'Да' if article['is_hot'] else 'Нет'}")
    
    # Генерация текста
    style = state.get_next_style()
    post_text = generate_post(article, style)
    
    if not post_text:
        print("❌ Не удалось сгенерировать пост")
        state.save()
        return
    
    # Генерация картинки
    image_path = generate_image(article["title"])
    
    # Публикация
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
        print(f"   Источник: {article['source']}")
        print(f"   Стиль: {style['name']}")
        
    except Exception as e:
        print(f"\n❌ Ошибка публикации: {e}")
    finally:
        if image_path and os.path.exists(image_path):
            os.remove(image_path)
    
    # Показываем статистику
    print(f"\n📈 Статистика публикаций:")
    for src, count in state.data.get("stats", {}).items():
        print(f"   {src}: {count}")


async def main():
    try:
        await autopost()
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
