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

# ============ ОСНОВНОЙ ФОРМАТ ПОСТА ============

POST_FORMAT = {
    "system": """Ты — эксперт по кибербезопасности для обычных людей. Пишешь понятно и по делу.

ТВОЯ ЗАДАЧА: взять новость об угрозе безопасности и объяснить человеку ЧТО ДЕЛАТЬ.

ЧИТАТЕЛЬ: обычный человек с телефоном/компьютером, не технарь, не программист.
Он хочет: узнать об угрозе + получить четкие действия для защиты СВОИХ данных и устройств.

КРИТИЧЕСКИ ВАЖНО:
- Пиши ТОЛЬКО про угрозы, которые касаются обычных пользователей
- Если в новости ЕСТЬ конкретные рекомендации — напиши их пошагово
- Если в новости НЕТ рекомендаций — просто объясни угрозу, без выдумывания инструкций
- НЕ ВЫДУМЫВАЙ шаги типа "откройте App Store", если это не написано в источнике
- Простой язык, без технического жаргона""",

    "template": """Напиши пост для Telegram-канала о безопасности для обычных людей.

СНАЧАЛА: проверь, касается ли эта новость ОБЫЧНЫХ ПОЛЬЗОВАТЕЛЕЙ?
Если это про багбаунти платформы, CVE идентификаторы, программы для исследователей — НЕ ПИШИ пост.

СНАЧАЛА: есть ли в новости КОНКРЕТНЫЕ рекомендации/решение?

━━━━━━━━━━━━━━━━━━━━━━
ЕСЛИ ЕСТЬ РЕШЕНИЕ:
━━━━━━━━━━━━━━━━━━━━━━

⚠️ [ЗАГОЛОВОК: суть угрозы одной строкой]

**Угроза:**
2-3 предложения — что случилось, в чём опасность для обычного человека.

**Кого касается:**
Конкретно: какие устройства/программы/версии. Например: "iPhone с iOS 16", "Телеграм на Android".

**Что делать СЕЙЧАС:**
1. [Конкретный шаг из источника]
2. [Конкретный шаг из источника]
3. [Конкретный шаг из источника]

[Только реальные шаги из новости! Никаких выдуманных инструкций!]

⏱ Займёт: [время]

━━━━━━━━━━━━━━━━━━━━━━
ЕСЛИ НЕТ РЕШЕНИЯ:
━━━━━━━━━━━━━━━━━━━━━━

⚠️ [ЗАГОЛОВОК: суть угрозы одной строкой]

**Что случилось:**
3-4 предложения — опиши проблему понятным языком для обычного человека.

**Кого касается:**
Конкретно: какие устройства/программы. Например: "Пользователи Chrome на Windows".

**Что известно:**
- Масштаб проблемы (сколько пострадало)
- Текущий статус (патч вышел? разработчики работают?)
- Что можно сделать прямо сейчас

**Следите за обновлениями** — как только появится решение, сообщим.

━━━━━━━━━━━━━━━━━━━━━━

ПРАВИЛА:
- Объём: 600-900 символов (СТРОГО, не больше!)
- Только факты из источника
- Простой язык без технического жаргона
- Максимум 2 эмодзи
- Никаких выдуманных инструкций
- Закончи текст полным предложением, не обрывай на полуслове"""
}

# ============ ФИЛЬТРЫ ============

EXCLUDE_KEYWORDS = [
    # Бизнес и HR
    "акции", "биржа", "инвестиц", "ipo", "капитализац",
    "выручка", "прибыль", "назначен", "отставка", "ceo",
    "hr", "кадр", "персонал", "сотрудник", "компани",
    "бизнес", "менеджмент", "управлен", "резерв", "рекрутинг",
    "маркетинг", "продаж", "стратег",
    
    # Профессиональный хакинг (не для обычных людей)
    "hackerone", "bugcrowd", "bug bounty программ", "программ вознаграждени",
    "репутаци", "signal", "cvss", "cve-", "cwe-",
    "исследовател безопасности", "security researcher", "багхантер",
    "pwn2own", "zerodium",
    
    # Развлечения
    "футбол", "хоккей", "спорт", "чемпионат",
    "playstation", "xbox", "видеоигр",
    "кино", "фильм", "сериал", "netflix",
    
    # Политика
    "выборы", "президент", "политик", "санкции",
    
    # Крипта
    "bitcoin", "криптовалют", "nft",
    
    # Юридическое
    "суд", "арест", "приговор"
]

SOURCE_PROMO_PATTERNS = [
    r"скидк[аи]", r"промокод", r"акция\b", r"распродажа",
    r"только сегодня", r"успей", r"предзаказ",
    r"цена от", r"₽\d+", r"\$\d+", r"€\d+",
]

def is_excluded(title: str, summary: str) -> Tuple[bool, str]:
    """Проверяет, нужно ли исключить статью"""
    text = f"{title} {summary}".lower()
    
    for kw in EXCLUDE_KEYWORDS:
        if kw in text:
            return True, f"excluded: {kw}"
    
    for pattern in SOURCE_PROMO_PATTERNS:
        if re.search(pattern, text):
            return True, "promo"
    
    return False, ""

def is_security_related(title: str, summary: str) -> bool:
    """Проверяет, относится ли статья к кибербезопасности для обычных пользователей"""
    text = f"{title} {summary}".lower()
    
    # Ключевые слова безопасности ДЛЯ ОБЫЧНЫХ ЛЮДЕЙ
    security_keywords = [
        # Угрозы
        "вирус", "малвар", "троян", "ransomware", "шифровальщик",
        "фишинг", "мошен", "утечка", "взлом", "уязвим",
        "вредонос", "шпион", "червь", "эксплоит", "ddos",
        "кибератак", "киберугроз", "хакер", "атак",
        
        # Защита
        "пароль", "двухфактор", "аутентифик", "шифрован",
        "vpn", "антивирус", "безопасность", "приватность",
        "конфиденциальн", "защит", "обновлен", "патч",
        
        # Устройства и сервисы (то, чем пользуются обычные люди)
        "телефон", "смартфон", "android", "ios", "iphone",
        "браузер", "chrome", "firefox", "safari", "edge",
        "windows", "mac", "telegram", "whatsapp",
        "аккаунт", "учетн", "google", "apple", "microsoft",
        "instagram", "facebook", "вконтакте", "tiktok",
        
        # Данные
        "персональн", "данны", "информац", "cookie",
        "трекинг", "слежк", "биометр", "фото", "видео",
        "контакт", "сообщени", "звонк"
    ]
    
    # Проверяем наличие хотя бы одного ключевого слова
    for keyword in security_keywords:
        if keyword in text:
            return True
    
    return False

# ============ STATE ============

class State:
    def __init__(self):
        self.data = {
            "posted_ids": {},
            "source_index": 0,
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
    """Гарантирует, что текст заканчивается полным предложением"""
    text = text.strip()
    if not text:
        return text
    
    # Убираем незавершённые союзы в конце
    for pattern in [r'\s+и$', r'\s+а$', r'\s+но$', r'\s+что$', r'\s+с$', r'\s+на$', r':$', r';$', r',$']:
        text = re.sub(pattern, '', text)
    
    # Если уже есть финальная пунктуация — отлично
    if text and text[-1] in '.!?':
        return text
    
    # Находим последнее законченное предложение
    last_period = text.rfind('.')
    last_exclamation = text.rfind('!')
    last_question = text.rfind('?')
    last_end = max(last_period, last_exclamation, last_question)
    
    # Если нашли точку в последних 40% текста — обрезаем до неё
    if last_end > len(text) * 0.6:
        return text[:last_end + 1]
    
    # В крайнем случае добавляем точку
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
    """Строит финальный пост с правильным обрезанием текста"""
    hashtags = get_hashtags(category)
    source = f'\n\n🔗 <a href="{link}">Источник</a>'
    tags = f"\n\n{hashtags}"
    
    # Считаем длину служебных частей
    service_len = len(source) + len(tags) + 10  # +10 запас
    max_text_len = TELEGRAM_CAPTION_LIMIT - service_len
    
    # Обрезаем текст если нужно
    if len(text) > max_text_len:
        text = text[:max_text_len]
        # Находим последнее полное предложение
        last_period = text.rfind('. ')
        last_exclamation = text.rfind('! ')
        last_question = text.rfind('? ')
        last_end = max(last_period, last_exclamation, last_question)
        
        if last_end > max_text_len * 0.6:
            text = text[:last_end + 1]
        else:
            # Если не нашли точку — ищем хотя бы конец абзаца
            last_newline = text.rfind('\n\n')
            if last_newline > max_text_len * 0.5:
                text = text[:last_newline]
    
    # Гарантируем полное предложение
    text = force_complete_sentence(text)
    
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
        
        # Проверка на исключения
        excluded, reason = is_excluded(title, summary)
        if excluded:
            print(f"  ⏭️ Пропущено ({reason}): {title[:50]}")
            continue
        
        # Проверка на тему безопасности
        if not is_security_related(title, summary):
            print(f"  ⏭️ Не по теме: {title[:50]}")
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

async def generate_post_with_copilot_sdk(article: Dict) -> Optional[str]:
    """Генерация поста через Copilot SDK"""
    if not copilot_client:
        return None
    
    try:
        # Получаем полный текст
        full_text = fetch_full_article(article["link"])
        content = full_text[:3000] if full_text else article["summary"]
        
        if full_text:
            print(f"  📄 Получен полный текст: {len(full_text)} символов")
        
        user_message = f"""{POST_FORMAT['template']}

---
ИСТОЧНИК:
Заголовок: {article['title']}
Текст: {content}
Ссылка: {article['link']}
---

КРИТИЧЕСКИ ВАЖНО:
1. Пиши ТОЛЬКО если это касается ОБЫЧНЫХ ПОЛЬЗОВАТЕЛЕЙ
2. Если про багбаунти платформы/CVE/исследователей — ОТКАЖИСЬ писать пост
3. Объём строго 600-900 символов
4. Закончи полным предложением, не обрывай текст
5. НЕ ВЫДУМЫВАЙ инструкции
"""
        
        # Создаём сессию с агентом
        session = copilot_client.create_session(
            system=POST_FORMAT["system"],
            temperature=0.6,
            max_tokens=800
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

def generate_post(article: Dict) -> Optional[str]:
    """Генерация поста (с fallback на OpenAI если SDK недоступен)"""
    
    # Пробуем Copilot SDK если включен
    if copilot_client and USE_COPILOT_SDK:
        print("  🤖 Используется Copilot SDK")
        result = asyncio.run(generate_post_with_copilot_sdk(article))
        if result:
            return result
        print("  ⚠️ SDK не сработал, переключаемся на OpenAI")
    
    # Fallback на стандартный OpenAI
    full_text = fetch_full_article(article["link"])
    
    content_for_gpt = article["summary"]
    if full_text and len(full_text) > len(article["summary"]):
        content_for_gpt = full_text[:3000]
        print(f"  📄 Получен полный текст: {len(full_text)} символов")
    
    user_prompt = f"""{POST_FORMAT['template']}

---
ИСТОЧНИК:

Заголовок: {article['title']}

Текст: {content_for_gpt}

Ссылка: {article['link']}
---

КРИТИЧЕСКИ ВАЖНО:
1. Пиши ТОЛЬКО если это касается ОБЫЧНЫХ ПОЛЬЗОВАТЕЛЕЙ
2. Если про багбаунти платформы/CVE/программы для исследователей — ОТКАЖИСЬ писать пост
3. Объём строго 600-900 символов
4. Закончи полным предложением, не обрывай текст на полуслове
5. НЕ ВЫДУМЫВАЙ инструкции типа "откройте App Store"
"""

    for attempt in range(2):
        try:
            response = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": POST_FORMAT["system"]},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.6,
                max_tokens=800,
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
            ]
            
            is_garbage = any(re.search(p, text.lower()) for p in garbage_patterns)
            if is_garbage and "обновл" not in article["summary"].lower():
                print(f"  🗑️ Выдуманные инструкции, пробуем снова")
                continue
            
            final = build_final_post(text, article["link"], article["category"])
            
            # Проверяем, что не обрезано на полуслове
            if final.count('**') % 2 != 0:
                print(f"  ⚠️ Текст обрезан некорректно, пробуем снова")
                continue
            
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
    
    # Пробуем статьи по очереди
    for article in all_articles[:15]:
        print(f"\n📰 {article['title'][:60]}...")
        print(f"   Источник: {article['source']}")
        
        post_text = generate_post(article)
        
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


