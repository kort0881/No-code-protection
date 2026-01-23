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

# Попытка импортировать Copilot SDK (опционально)
try:
    from github_copilot_sdk import CopilotClient
    COPILOT_SDK_AVAILABLE = True
except ImportError:
    COPILOT_SDK_AVAILABLE = False
    print("⚠️ GitHub Copilot SDK не установлен, используется стандартный OpenAI API")

# ============ CONFIG ============

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
USE_COPILOT_SDK = os.getenv("USE_COPILOT_SDK", "false").lower() == "true"

if not all([OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, CHANNEL_ID]):
    raise ValueError("❌ Не все ENV переменные установлены!")

bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# Инициализация Copilot SDK если доступен и включен
copilot_client = None
if COPILOT_SDK_AVAILABLE and USE_COPILOT_SDK:
    try:
        copilot_client = CopilotClient()
        print("✅ GitHub Copilot SDK инициализирован")
    except Exception as e:
        print(f"⚠️ Не удалось инициализировать Copilot SDK: {e}")
        print("   Используется стандартный OpenAI API")

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

# ============ RSS ИСТОЧНИКИ ============

RSS_SOURCES = [
    # Безопасность
    {"name": "SecurityLab", "url": "https://www.securitylab.ru/rss/allnews/", "category": "security"},
    {"name": "AntiMalware", "url": "https://www.anti-malware.ru/news/feed", "category": "security"},
    
    # AI/Tech
    {"name": "Habr AI", "url": "https://habr.com/ru/rss/hub/artificial_intelligence/all/?fl=ru", "category": "ai"},
    {"name": "Habr ML", "url": "https://habr.com/ru/rss/hub/machine_learning/all/?fl=ru", "category": "ai"},
    {"name": "Habr News", "url": "https://habr.com/ru/rss/news/?fl=ru", "category": "tech"},
    
    # Российские IT новости
    {"name": "CNews", "url": "https://www.cnews.ru/inc/rss/news.xml", "category": "tech_ru"},
    {"name": "3DNews", "url": "https://3dnews.ru/news/rss/", "category": "tech_ru"},
    {"name": "iXBT", "url": "https://www.ixbt.com/export/news.rss", "category": "tech_ru"},
]

# ============ ТИПЫ ПОСТОВ (ЧЕРЕДУЮТСЯ) ============

POST_TYPES = [
    {
        "type": "deep_analysis",
        "name": "Глубокий разбор",
        "description": "Подробно разбираем что случилось, почему это важно, технические детали",
        "system": """Ты — технический журналист, который пишет глубокие разборы для Telegram-канала.
Твоя задача: взять новость и РАЗОБРАТЬ её по косточкам.

Читатель: умный человек, который хочет ПОНЯТЬ суть, а не получить поверхностную новость.
Он ценит: факты, контекст, объяснение "почему это важно", технические детали простым языком.

НЕ ВЫДУМЫВАЙ ничего. Пиши ТОЛЬКО то, что есть в источнике. 
Если чего-то нет — не пиши об этом.""",
        
        "template": """Напиши глубокий разбор новости.

СТРУКТУРА:

🔍 [Заголовок — суть новости, 6-10 слов]

**Что произошло**
3-4 предложения. Изложи факты из новости. Кто, что, когда, где.
Никаких домыслов — только информация из источника.

**Почему это важно**
2-3 предложения. Объясни контекст:
- Что это меняет?
- На кого влияет?
- Какой масштаб?

**Технические детали** (если есть в источнике)
2-3 предложения. Объясни КАК это работает простыми словами.
Если в источнике нет технических деталей — пропусти этот блок.

**Что это значит для [пользователей/индустрии/рынка]**
2 предложения. Практический вывод.

💬 [Твой короткий комментарий или вопрос к читателям]

ПРАВИЛА:
- Объём: 800-1200 символов
- Только факты из источника
- Если информации мало — пиши короче, но не выдумывай
- 1-2 эмодзи максимум"""
    },
    
    {
        "type": "practical_guide",
        "name": "Практическое руководство", 
        "description": "Конкретные действия, которые может сделать читатель",
        "system": """Ты — эксперт по кибербезопасности/технологиям, который пишет практические руководства.

КРИТИЧЕСКИ ВАЖНО:
- Пиши ТОЛЬКО те инструкции, которые РЕАЛЬНО следуют из новости
- Если в новости сказано "обновите Chrome" — напиши КАК обновить Chrome
- Если в новости НЕТ конкретных рекомендаций — НЕ ВЫДУМЫВАЙ их
- Лучше честно написать "следите за обновлениями" чем выдумать 10 шагов

Читатель: обычный человек с телефоном/компьютером.""",
        
        "template": """Напиши практический пост на основе новости.

СНАЧАЛА ОПРЕДЕЛИ: есть ли в новости КОНКРЕТНЫЕ рекомендации?

ЕСЛИ ДА — используй структуру:

⚡️ [Заголовок: что нужно сделать]

**Кого касается:** [одно предложение]

**Суть проблемы:** [2-3 предложения — что случилось]

**Что делать:**

1. [Действие из новости]
   → Как: [конкретные шаги, если они есть в источнике]

2. [Действие из новости]
   → Как: [конкретные шаги]

[Только если в источнике есть эта информация!]

⏱ Займёт: [время]

---

ЕСЛИ НЕТ конкретных рекомендаций в источнике — используй структуру:

📰 [Заголовок новости]

**Что случилось:** [3-4 предложения с фактами]

**Кого затрагивает:** [1-2 предложения]

**Текущий статус:** [что известно на данный момент]

**Следите за:** [на что обратить внимание в будущем]

---

ПРАВИЛА:
- НЕ ВЫДУМЫВАЙ инструкции, которых нет в источнике
- Если новость информационная — так и подай её
- Объём: 600-1000 символов"""
    },
    
    {
        "type": "context_explainer",
        "name": "Контекст и объяснение",
        "description": "Объясняем сложную тему простыми словами, даём контекст",
        "system": """Ты — технический писатель, который объясняет сложное простым языком.

Твоя задача: взять новость и ОБЪЯСНИТЬ контекст. 
Почему это произошло? Что было раньше? Что будет дальше?

Пиши как умный друг, который разбирается в теме и объясняет тебе за чашкой кофе.
Без снисходительности, но доступно.""",
        
        "template": """Напиши объясняющий пост.

СТРУКТУРА:

🧠 [Заголовок — о чём пойдёт речь]

**Новость:** [1-2 предложения — что случилось]

**Контекст — почему это важно:**
3-4 предложения. Объясни:
- Что стояло за этим решением/событием?
- Какая предыстория?
- Почему именно сейчас?

**Простыми словами:**
2-3 предложения. Объясни суть для человека не в теме.
Используй аналогии, если уместно.

**Что дальше:**
1-2 предложения. К чему это может привести?
(Только если это следует из источника, не гадай)

💭 [Вопрос к читателям или твоя мысль]

ПРАВИЛА:
- Контекст бери из источника, не выдумывай
- Если предыстории нет — сосредоточься на объяснении сути
- Объём: 700-1000 символов"""
    },
    
    {
        "type": "news_digest",
        "name": "Новостной дайджест",
        "description": "Краткая, но информативная подача новости",
        "system": """Ты — редактор новостного канала. Пишешь краткие, но ёмкие новости.

Стиль: информационный, без воды, каждое слово на месте.
Задача: человек за 30 секунд понимает что произошло и почему это важно.""",
        
        "template": """Напиши краткую новость.

СТРУКТУРА:

📌 [Заголовок — главный факт]

[Первый абзац — 2-3 предложения]
Кто? Что сделал? Когда? Главный факт новости.

[Второй абзац — 2-3 предложения]  
Детали: цифры, масштаб, участники.

[Третий абзац — 1-2 предложения]
Значение: почему это важно, что это меняет.

📊 Ключевое: [одна цифра или факт, который запомнится]

ПРАВИЛА:
- Максимум конкретики, минимум воды
- Только факты из источника
- Объём: 500-700 символов"""
    },
    
    {
        "type": "comparison_analysis",
        "name": "Сравнительный анализ",
        "description": "Сравниваем с аналогами, конкурентами, прошлыми версиями",
        "system": """Ты — аналитик, который умеет сравнивать и находить различия.

Если в новости есть с чем сравнить (прошлая версия, конкуренты, аналоги) — 
построй пост вокруг сравнения. Людям нравится понимать разницу.""",
        
        "template": """Напиши сравнительный пост.

СТРУКТУРА:

⚖️ [Заголовок с элементом сравнения]

**Что нового:**
2-3 предложения — суть новости/продукта/события.

**Сравнение:**

| Было/Старое | Стало/Новое |
|-------------|-------------|
| [пункт 1]   | [пункт 1]   |
| [пункт 2]   | [пункт 2]   |
| [пункт 3]   | [пункт 3]   |

(Или текстом, если таблица не подходит)

**Главное отличие:**
1-2 предложения — что принципиально изменилось.

**Вывод:**
1-2 предложения — стоит ли обращать внимание, для кого актуально.

ПРАВИЛА:
- Сравнивай только если есть с чем (из источника)
- Если сравнивать не с чем — используй другой формат
- Объём: 600-900 символов"""
    },
]

# ============ ФИЛЬТРЫ ============

EXCLUDE_KEYWORDS = [
    "акции", "биржа", "инвестиц", "ipo", "капитализац",
    "выручка", "прибыль", "назначен", "отставка", "ceo",
    "футбол", "хоккей", "спорт", "чемпионат",
    "playstation", "xbox", "видеоигр",
    "кино", "фильм", "сериал", "netflix",
    "выборы", "президент", "политик", "санкции",
    "bitcoin", "криптовалют", "nft",
    "суд", "арест", "приговор"
]

SOURCE_PROMO_PATTERNS = [
    r"скидк[аи]", r"промокод", r"акция\b", r"распродажа",
    r"только сегодня", r"успей", r"предзаказ",
    r"цена от", r"₽\d+", r"\$\d+", r"€\d+",
]

def is_excluded(title: str, summary: str) -> Tuple[bool, str]:
    text = f"{title} {summary}".lower()
    
    for kw in EXCLUDE_KEYWORDS:
        if kw in text:
            return True, f"excluded: {kw}"
    
    for pattern in SOURCE_PROMO_PATTERNS:
        if re.search(pattern, text):
            return True, "promo"
    
    return False, ""

# ============ STATE ============

class State:
    def __init__(self):
        self.data = {
            "posted_ids": {},
            "source_index": 0,
            "post_type_index": 0,
            "last_run": None,
        }
        self._load()
    
    def _load(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    self.data.update(json.load(f))
                print(f"📂 История: {len(self.data['posted_ids'])} постов")
            except:
                pass
    
    def save(self):
        self.data["last_run"] = datetime.now().isoformat()
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except:
            pass
    
    def is_posted(self, article_id: str) -> bool:
        return article_id in self.data["posted_ids"]
    
    def mark_posted(self, article_id: str, source: str, title: str):
        self.data["posted_ids"][article_id] = {
            "ts": datetime.now().timestamp(),
            "source": source,
            "title": title[:100]
        }
        self.save()
    
    def cleanup_old(self):
        cutoff = datetime.now().timestamp() - (RETENTION_DAYS * 86400)
        self.data["posted_ids"] = {
            k: v for k, v in self.data["posted_ids"].items()
            if isinstance(v, dict) and v.get("ts", 0) > cutoff
        }
    
    def get_next_source_order(self) -> List[Dict]:
        idx = self.data["source_index"] % len(RSS_SOURCES)
        ordered = RSS_SOURCES[idx:] + RSS_SOURCES[:idx]
        self.data["source_index"] = (idx + 1) % len(RSS_SOURCES)
        return ordered
    
    def get_next_post_type(self) -> Dict:
        idx = self.data["post_type_index"] % len(POST_TYPES)
        post_type = POST_TYPES[idx]
        self.data["post_type_index"] = (idx + 1) % len(POST_TYPES)
        print(f"📝 Тип поста: {post_type['name']}")
        return post_type

state = State()

# ============ ПАРСИНГ ПОЛНОГО ТЕКСТА ============

def fetch_full_article(url: str) -> Optional[str]:
    """Пытается получить полный текст статьи"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Удаляем ненужное
        for tag in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'form']):
            tag.decompose()
        
        # Ищем основной контент
        content = None
        
        # Habr
        if 'habr.com' in url:
            content = soup.find('div', class_='tm-article-body')
        # SecurityLab
        elif 'securitylab.ru' in url:
            content = soup.find('div', class_='article-body') or soup.find('div', class_='news-body')
        # CNews
        elif 'cnews.ru' in url:
            content = soup.find('div', class_='news_container')
        # 3DNews
        elif '3dnews.ru' in url:
            content = soup.find('div', class_='article-entry')
        
        # Общий fallback
        if not content:
            content = soup.find('article') or soup.find('main') or soup.find('div', class_=re.compile(r'article|content|post|entry'))
        
        if content:
            text = content.get_text(separator='\n', strip=True)
            # Чистим
            text = re.sub(r'\n{3,}', '\n\n', text)
            text = re.sub(r'[ \t]+', ' ', text)
            return text[:4000]  # Ограничиваем
        
        return None
        
    except Exception as e:
        print(f"  ⚠️ Не удалось получить полный текст: {e}")
        return None

# ============ HELPERS ============

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&\w+;', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def get_article_id(title: str, link: str) -> str:
    return hashlib.sha256(f"{title}|{link}".encode()).hexdigest()[:20]

def force_complete_sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return text
    
    for pattern in [r'\s+и$', r'\s+а$', r'\s+но$', r'\s+что$', r':$', r';$', r',$']:
        text = re.sub(pattern, '', text)
    
    if text and text[-1] in '.!?':
        return text
    
    last_end = max(text.rfind('.'), text.rfind('!'), text.rfind('?'))
    if last_end > len(text) * 0.6:
        return text[:last_end + 1]
    
    return text + '.'

def get_hashtags(category: str) -> str:
    mapping = {
        "security": "#безопасность #киберугрозы",
        "ai": "#AI #нейросети #технологии",
        "tech": "#технологии #IT",
        "tech_ru": "#технологии #Россия #IT",
    }
    return mapping.get(category, "#технологии")

def build_final_post(text: str, link: str, category: str) -> str:
    text = force_complete_sentence(text.strip())
    hashtags = get_hashtags(category)
    source = f'\n\n🔗 <a href="{link}">Источник</a>'
    tags = f"\n\n{hashtags}"
    
    service_len = len(source) + len(tags) + 5
    max_text = TELEGRAM_CAPTION_LIMIT - service_len
    
    if len(text) > max_text:
        text = text[:max_text]
        last_end = max(text.rfind('. '), text.rfind('! '), text.rfind('? '))
        if last_end > max_text * 0.6:
            text = text[:last_end + 1]
    
    return text + tags + source

# ============ RSS LOADING ============

def load_rss(source: Dict) -> List[Dict]:
    articles = []
    
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=20)
        feed = feedparser.parse(resp.content)
    except Exception as e:
        print(f"❌ {source['name']}: {e}")
        return []
    
    if not feed.entries:
        print(f"⚪ {source['name']}: пусто")
        return []
    
    now = datetime.now()
    max_age = timedelta(days=MAX_ARTICLE_AGE_DAYS)
    
    for entry in feed.entries[:30]:
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
        
        excluded, reason = is_excluded(title, summary)
        if excluded:
            continue
        
        articles.append({
            "id": article_id,
            "title": title,
            "summary": summary[:1500],
            "link": link,
            "source": source["name"],
            "category": source["category"],
            "date": pub_date,
        })
    
    if articles:
        print(f"✅ {source['name']}: {len(articles)} статей")
    
    return articles

# ============ TEXT GENERATION ============

async def generate_post_with_copilot_sdk(article: Dict, post_type: Dict) -> Optional[str]:
    """Генерация поста через Copilot SDK"""
    if not copilot_client:
        return None
    
    try:
        # Получаем полный текст
        full_text = fetch_full_article(article["link"])
        content = full_text[:3000] if full_text else article["summary"]
        
        if full_text:
            print(f"  📄 Получен полный текст: {len(full_text)} символов")
        
        user_message = f"""{post_type['template']}

---
ИСТОЧНИК:
Заголовок: {article['title']}
Текст: {content}
Ссылка: {article['link']}
---

КРИТИЧЕСКИ ВАЖНО:
1. Пиши ТОЛЬКО на основе информации из источника
2. Если информации мало — напиши короче, но честно
3. Никаких выдуманных инструкций
"""
        
        # Создаём сессию с агентом
        session = copilot_client.create_session(
            system=post_type["system"],
            temperature=0.6,
            max_tokens=900
        )
        
        # Отправляем запрос
        response = await session.send_message(user_message)
        text = response.text.strip()
        
        # Убираем кавычки
        if text.startswith(('"', '«')) and text.endswith(('"', '»')):
            text = text[1:-1].strip()
        
        if len(text) < 200:
            return None
        
        final = build_final_post(text, article["link"], article["category"])
        print(f"  ✅ SDK: {len(final)} символов")
        return final
        
    except Exception as e:
        print(f"  ❌ SDK ошибка: {e}")
        return None

def generate_post(article: Dict, post_type: Dict) -> Optional[str]:
    """Генерация поста (с fallback на OpenAI если SDK недоступен)"""
    
    # Пробуем Copilot SDK если включен
    if copilot_client and USE_COPILOT_SDK:
        print("  🤖 Используется Copilot SDK")
        result = asyncio.run(generate_post_with_copilot_sdk(article, post_type))
        if result:
            return result
        print("  ⚠️ SDK не сработал, переключаемся на OpenAI")
    
    # Fallback на стандартный OpenAI
    full_text = fetch_full_article(article["link"])
    
    content_for_gpt = article["summary"]
    if full_text and len(full_text) > len(article["summary"]):
        content_for_gpt = full_text[:3000]
        print(f"  📄 Получен полный текст: {len(full_text)} символов")
    
    user_prompt = f"""{post_type['template']}

---
ИСТОЧНИК:

Заголовок: {article['title']}

Текст: {content_for_gpt}

Ссылка: {article['link']}
---

КРИТИЧЕСКИ ВАЖНО:
1. Пиши ТОЛЬКО на основе информации из источника
2. Если в источнике нет конкретных рекомендаций — НЕ ВЫДУМЫВАЙ их
3. Если информации мало — напиши короче, но честно
4. Никаких "обновите приложение" если это НЕ сказано в источнике
5. Лучше информативный пост без инструкций, чем выдуманные инструкции
"""

    for attempt in range(2):
        try:
            response = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": post_type["system"]},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.6,
                max_tokens=900,
            )
            
            text = response.choices[0].message.content.strip()
            
            # Убираем кавычки
            if text.startswith(('"', '«')) and text.endswith(('"', '»')):
                text = text[1:-1].strip()
            
            if len(text) < 200:
                print(f"  ⚠️ Слишком коротко: {len(text)}")
                continue
            
            # Проверка на мусорные шаблоны
            garbage_patterns = [
                r"откройте app store",
                r"откройте google play", 
                r"зайдите в настройки.*обновления",
                r"💾\s*сохрани",
                r"что делать:\s*\n\s*1\.\s*обнови",
            ]
            
            is_garbage = any(re.search(p, text.lower()) for p in garbage_patterns)
            if is_garbage and "обновл" not in article["summary"].lower():
                print(f"  🗑️ Выдуманные инструкции, пробуем снова")
                continue
            
            final = build_final_post(text, article["link"], article["category"])
            print(f"  ✅ Готово: {len(final)} символов")
            return final
            
        except Exception as e:
            print(f"  ❌ Ошибка: {e}")
            time.sleep(2)
    
    return None

# ============ IMAGE ============

def generate_image(title: str) -> Optional[str]:
    styles = [
        "modern tech illustration, clean, minimal",
        "futuristic digital art, blue tones",
        "abstract technology concept, professional",
    ]
    
    seed = random.randint(1, 999999999)
    keywords = re.sub(r'[^\w\s]', '', title)[:40]
    prompt = f"{random.choice(styles)}, {keywords}, no text, 4k"
    
    url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(prompt)}?seed={seed}&width=1024&height=1024&nologo=true"
    
    print(f"  🖼 Генерация картинки...")
    
    try:
        resp = requests.get(url, timeout=60, headers=HEADERS)
        if resp.status_code == 200 and len(resp.content) > 10000:
            filename = f"img_{seed}.jpg"
            with open(filename, "wb") as f:
                f.write(resp.content)
            return filename
    except Exception as e:
        print(f"  ⚠️ Картинка: {e}")
    
    return None

def cleanup_image(path: Optional[str]):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except:
            pass

# ============ MAIN ============

async def autopost():
    state.cleanup_old()
    print("🔄 Загрузка новостей...\n")
    
    if copilot_client and USE_COPILOT_SDK:
        print("🤖 Режим: GitHub Copilot SDK")
    else:
        print("🔧 Режим: OpenAI API")
    
    # Собираем статьи из всех источников
    all_articles = []
    sources = state.get_next_source_order()
    
    for source in sources:
        articles = load_rss(source)
        all_articles.extend(articles)
    
    if not all_articles:
        print("\n❌ Нет новых статей")
        return
    
    # Сортируем по дате
    all_articles.sort(key=lambda x: x["date"], reverse=True)
    
    print(f"\n📊 Всего: {len(all_articles)} статей")
    
    # Получаем тип поста для этого запуска
    post_type = state.get_next_post_type()
    
    # Пробуем статьи по очереди
    for article in all_articles[:15]:
        print(f"\n📰 {article['title'][:60]}...")
        print(f"   Источник: {article['source']}")
        
        post_text = generate_post(article, post_type)
        
        if not post_text:
            print("   ⏭️ Пропускаем")
            continue
        
        img = generate_image(article["title"])
        
        try:
            if img:
                await bot.send_photo(CHANNEL_ID, photo=FSInputFile(img), caption=post_text)
            else:
                await bot.send_message(CHANNEL_ID, text=post_text)
            
            state.mark_posted(article["id"], article["source"], article["title"])
            print("\n✅ Опубликовано!")
            return
            
        except Exception as e:
            print(f"   ❌ Ошибка отправки: {e}")
        finally:
            cleanup_image(img)
    
    print("\n⚠️ Не удалось опубликовать")

async def main():
    try:
        await autopost()
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())

