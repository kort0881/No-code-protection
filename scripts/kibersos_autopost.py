import os
import json
import asyncio
import random
import re
import time
import subprocess
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
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

POSTED_FILE = "posted_articles.json"
RETENTION_DAYS = 7
LAST_TYPE_FILE = "last_post_type.json"
LAST_SECURITY_FILE = "last_security_post.json"

# свежесть новости (в днях)
MAX_ARTICLE_AGE_DAYS = 3

# ============ СТИЛЬ KIBER SOS ============

SECURITY_POST_PROMPT = """
Ты ведёшь Telegram-канал «KIBER SOS» про цифровую безопасность для обычных людей.
Твоя задача — перевести сухую новость по информационной безопасности на простой язык.

Формат поста (строго соблюдай структуру и порядок блоков):

🔴 Опасность:
1–2 предложения. Простыми словами опиши, что именно может пойти не так (без корп.жаргона).

⚠️ Почему это реально:
1 короткий пример из жизни, как обычный человек может попасть в эту ситуацию.
Никакой фантастики, только реалистичный сценарий.

🛠 Что сделать прямо сейчас:
Сделай 2–4 пункта простых действий, которые человек может выполнить САМ за 5–10 минут.
Это могут быть шаги вида:
• проверить настройки;
• отключить/удалить что‑то;
• включить защитную функцию;
• поменять пароли;
• включить 2FA;
• пересмотреть права приложений.

✅ Итог:
1 предложение: чего человек избегает, если сделает эти действия (кража денег, захват аккаунта, слив фото, шантаж и т.п.).

Важно:
• Пост должен легко поместиться в ~600–900 символов.
• Язык: только русский, максимально простой.
• Не выдумывай деталей, которых нет в новости, опирайся на общие практики цифровой безопасности.
• Не упоминай бизнес, корпорации, ISO, SOC и т.п. — только обычные люди, их устройства и аккаунты.

Запрещено:
• Любые инструкции по атакам, взлому, эксплуатации уязвимостей.
• Формулировки «как взломать», «как обойти защиту», «эксплойт».
• Рекламный тон и фразы «идеальное решение», «уникальный продукт», «комплексное решение».

Верни ТОЛЬКО текст поста с этими блоками, без хештегов и ссылок.
"""

# ============ КЛЮЧЕВЫЕ СЛОВА ============

SECURITY_KEYWORDS = [
    "уязвимость", "уязвимости", "vulnerability", "vulnerabilities",
    "утечка", "утечка данных", "data breach", "leak", "breach",
    "взлом", "взломали", "hack", "was hacked",
    "фишинг", "phishing", "scam", "мошенничество",
    "malware", "вредоносное по", "ransomware",
    "пароль", "password", "password manager", "менеджер паролей",
    "браузер", "browser extension", "расширение браузера",
    "android", "ios", "windows", "macos", "telegram", "телеграм"
]

SENSATIONAL_KEYWORDS = [
    "взлом", "взломали", "утечка", "утечка данных", "data breach", "leak",
    "ransomware", "шантаж", "выкуп", "шифровальщик",
    "кибератака", "атака", "ddos", "фишинг", "phishing",
    "0-day", "нулевого дня"
]

EXCLUDE_KEYWORDS = [
    "акции", "биржа", "котировки", "инвестиции", "ipo",
    "капитализация", "выручка", "прибыль", "убыток",
    "курс доллара", "курс евро", "политик", "выборы",
    "теннис", "футбол", "спорт", "фильм", "сериал",
    "биткоин", "bitcoin", "криптовалюта",
    "суд", "арест", "приговор", "штраф"
]

BAD_PHRASES = [
    "предлагает решение",
    "предлагает уникальное решение",
    "обеспечивает высококачественную защиту",
    "обеспечивает надёжную защиту",
    "обеспечивает надежную защиту",
    "обеспечивает защиту",
    "комплексное решение для",
    "идеальное решение для",
    "помогает бизнесу эффективнее работать",
]


def is_too_promotional(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in BAD_PHRASES)


# ============ STATE ============

posted_articles: Dict[str, Optional[float]] = {}

if os.path.exists(POSTED_FILE):
    with open(POSTED_FILE, "r", encoding="utf-8") as f:
        try:
            posted_data = json.load(f)
            posted_articles = {item["id"]: item.get("timestamp") for item in posted_data}
        except Exception:
            posted_articles = {}


def save_posted_articles() -> None:
    data = [{"id": id_str, "timestamp": ts} for id_str, ts in posted_articles.items()]
    with open(POSTED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def clean_old_posts() -> None:
    global posted_articles
    now = datetime.now().timestamp()
    cutoff = now - (RETENTION_DAYS * 86400)
    posted_articles = {
        id_str: ts for id_str, ts in posted_articles.items()
        if ts is None or ts > cutoff
    }
    save_posted_articles()


def save_posted(article_id: str) -> None:
    posted_articles[article_id] = datetime.now().timestamp()
    save_posted_articles()


def commit_and_push_posted_articles() -> None:
    """Коммитит обновленный posted_articles.json в репозиторий"""
    try:
        # Конфигурируем git
        subprocess.run(["git", "config", "user.email", "action@github.com"], check=True)
        subprocess.run(["git", "config", "user.name", "GitHub Action"], check=True)
        
        # Добавляем файл
        subprocess.run(["git", "add", POSTED_FILE], check=True)
        
        # Проверяем, есть ли изменения
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            capture_output=True
        )
        
        if result.returncode != 0:  # Если есть изменения
            subprocess.run(
                ["git", "commit", "-m", "📝 Update posted articles"],
                check=True
            )
            subprocess.run(["git", "push"], check=True)
            print("✅ Сохранено в репозиторий")
        else:
            print("ℹ️ Нет изменений для коммита")
            
    except subprocess.CalledProcessError as e:
        print(f"⚠️ Ошибка git: {e}")
    except Exception as e:
        print(f"⚠️ Ошибка при сохранении в git: {e}")


def load_last_post_type() -> Optional[str]:
    if not os.path.exists(LAST_TYPE_FILE):
        return None
    try:
        with open(LAST_TYPE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("type")
    except Exception:
        return None


def save_last_post_type(post_type: str) -> None:
    try:
        with open(LAST_TYPE_FILE, "w", encoding="utf-8") as f:
            json.dump({"type": post_type}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_last_security_ts() -> Optional[float]:
    if not os.path.exists(LAST_SECURITY_FILE):
        return None
    try:
        with open(LAST_SECURITY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("ts")
    except Exception:
        return None


def save_last_security_ts() -> None:
    try:
        with open(LAST_SECURITY_FILE, "w", encoding="utf-8") as f:
            json.dump({"ts": datetime.now().timestamp()}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ============ HELPERS ============

def clean_text(text: str) -> str:
    return " ".join(text.replace("\n", " ").replace("\r", " ").split())


def ensure_complete_sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return text
    if text[-1] in ".!?":
        return text
    last_period = text.rfind(".")
    last_exclaim = text.rfind("!")
    last_question = text.rfind("?")
    last_end = max(last_period, last_exclaim, last_question)
    if last_end > 0:
        return text[: last_end + 1]
    return text + "."


def trim_core_text_to_limit(core_text: str, max_core_length: int) -> str:
    core_text = core_text.strip()
    if len(core_text) <= max_core_length:
        return ensure_complete_sentence(core_text)
    sentence_pattern = r"(?<=[.!?])\s+"
    sentences = re.split(sentence_pattern, core_text)
    result = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        candidate = (result + " " + sentence).strip() if result else sentence
        if len(candidate) <= max_core_length:
            result = candidate
        else:
            break
    if not result and sentences:
        result = sentences[0][:max_core_length]
        if len(result) == max_core_length and " " in result:
            result = result.rsplit(" ", 1)[0]
    return ensure_complete_sentence(result)


def get_hashtags() -> str:
    return "#безопасность #приватность #кибербезопасность"


def build_final_post(core_text: str, link: str, max_total: int = 1024) -> str:
    cta_line = "\n\nЕсли полезно — сохрани пост и перешли близким."
    source_line = f'\n\n🔗 <a href="{link}">Источник</a>'
    hashtag_line = f"\n\n{get_hashtags()}"
    service_length = len(cta_line) + len(source_line) + len(hashtag_line)
    max_core_length = max_total - service_length - 10
    trimmed_core = trim_core_text_to_limit(core_text, max_core_length)
    final = trimmed_core + cta_line + hashtag_line + source_line
    if len(final) > max_total:
        overflow = len(final) - max_total
        trimmed_core = trim_core_text_to_limit(core_text, max_core_length - overflow - 20)
        final = trimmed_core + cta_line + hashtag_line + source_line
    return final


# ============ PARSERS (РУССКИЕ ИСТОЧНИКИ) ============

def load_rss(url: str, source: str) -> List[Dict]:
    articles = []
    try:
        feed = feedparser.parse(url)
        if feed.bozo and not feed.entries:
            print(f"⚠️ RSS недоступен: {source}")
            return articles
    except Exception as e:
        print(f"❌ Ошибка загрузки RSS {source}: {e}")
        return articles

    now = datetime.now()
    max_age = timedelta(days=MAX_ARTICLE_AGE_DAYS)

    for entry in feed.entries[:50]:
        link = entry.get("link", "")
        if not link or link in posted_articles:
            continue

        pub_dt = now
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            pub_dt = datetime(*entry.published_parsed[:6])
        elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
            pub_dt = datetime(*entry.updated_parsed[:6])

        if now - pub_dt > max_age:
            continue

        articles.append(
            {
                "id": link,
                "title": clean_text(entry.get("title") or ""),
                "summary": clean_text(
                    entry.get("summary") or entry.get("description") or ""
                )[:700],
                "link": link,
                "source": source,
                "published_parsed": pub_dt,
            }
        )

    if articles:
        print(f"✅ {source}: {len(articles)} свежих статей")

    return articles


def load_articles_from_sites() -> List[Dict]:
    articles: List[Dict] = []

    articles.extend(
        load_rss("https://www.securitylab.ru/rss/allnews/", "SecurityLab")
    )

    articles.extend(
        load_rss("https://1275.ru/vulnerability/feed", "1275 Vulnerabilities")
    )
    articles.extend(load_rss("https://1275.ru/news/feed", "1275 News"))
    articles.extend(load_rss("https://1275.ru/security/feed", "1275 Security"))

    articles.extend(
        load_rss("https://www.anti-malware.ru/news/feed", "AntiMalware News")
    )

    return articles


def filter_articles(articles: List[Dict]) -> List[Dict]:
    sensational = []
    general = []

    for e in articles:
        text = f"{e['title']} {e['summary']}".lower()

        if any(kw in text for kw in EXCLUDE_KEYWORDS):
            continue

        is_sensational = any(kw in text for kw in SENSATIONAL_KEYWORDS)
        has_security = any(kw in text for kw in SECURITY_KEYWORDS)

        if not has_security and not is_sensational:
            continue

        if is_sensational:
            e["post_type"] = "sensational"
            sensational.append(e)
        else:
            e["post_type"] = "security"
            general.append(e)

    sensational.sort(key=lambda x: x["published_parsed"], reverse=True)
    general.sort(key=lambda x: x["published_parsed"], reverse=True)

    return sensational + general


# ============ ГЕНЕРАЦИЯ ТЕКСТА ============

def build_security_prompt(title: str, summary: str) -> str:
    news_text = f"Заголовок: {title}\n\nТекст: {summary}"
    return SECURITY_POST_PROMPT + "\n\nНОВОСТЬ:\n" + news_text


def validate_generated_text(text: str) -> tuple[bool, str]:
    text = text.strip()
    if not text:
        return False, "Пустой текст"
    if len(text) < 200:
        return False, f"Слишком короткий текст ({len(text)} символов)"
    if text.count("(") != text.count(")"):
        return False, "Незакрытые скобки"
    if text.count("«") != text.count("»"):
        return False, "Незакрытые кавычки"
    return True, "OK"


def short_summary(title: str, summary: str, link: str) -> Optional[str]:
    prompt = build_security_prompt(title, summary)
    max_attempts = 2

    for attempt in range(max_attempts):
        try:
            res = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Ты — автор Telegram-канала «KIBER SOS» про цифровую безопасность "
                            "для обычных людей. Строго соблюдаешь заданный шаблон блоков "
                            "и не даёшь инструкции по атакам, только по защите."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.4,
                max_tokens=650,
            )
            core = res.choices[0].message.content.strip()

            if core.startswith('"') and core.endswith('"'):
                core = core[1:-1]
            if core.startswith("«") and core.endswith("»"):
                core = core[1:-1]

            core = core.strip()

            is_valid, reason = validate_generated_text(core)
            if not is_valid:
                print(f"  ⚠️ Попытка {attempt + 1}: {reason}")
                if attempt < max_attempts - 1:
                    continue
                core = ensure_complete_sentence(core)

            if is_too_promotional(core):
                print("  ⚠️ Текст слишком рекламный, пропускаем")
                return None

            final = build_final_post(core, link, max_total=1024)
            print(f"  ✅ Сгенерирован пост: {len(final)} символов")
            return final

        except Exception as e:
            print(f"❌ OpenAI ошибка: {e}")
            if attempt < max_attempts - 1:
                time.sleep(2)
                continue
            return None

    return None


# ============ ГЕНЕРАЦИЯ КАРТИНОК (опционально) ============

def generate_image(title: str, max_retries: int = 3) -> Optional[str]:
    image_styles = [
        "minimalist flat illustration, cyber security, lock, shield, ",
        "clean infographic style, privacy, devices, ",
        "modern digital art, protection, safe internet, ",
    ]

    style = random.choice(image_styles)

    for attempt in range(max_retries):
        seed = random.randint(0, 10**7)
        clean_title = title[:60].replace('"', "").replace("'", "").replace("\n", " ")

        prompt = (
            f"{style}{clean_title}, "
            "4k quality, no text, no letters, no words, "
            "clean composition, professional"
        )

        try:
            encoded = urllib.parse.quote(prompt)
            url = f"https://image.pollinations.ai/prompt/{encoded}?seed={seed}&width=1024&height=1024&nologo=true"

            print(f"  🎨 Генерация изображения (попытка {attempt + 1}/{max_retries})...")

            resp = requests.get(url, timeout=90, headers=HEADERS)

            if resp.status_code == 200:
                content_type = resp.headers.get("content-type", "")
                if "image" in content_type and len(resp.content) > 10000:
                    fname = f"img_{seed}.jpg"
                    with open(fname, "wb") as f:
                        f.write(resp.content)
                    print(f"  ✅ Изображение сохранено: {fname}")
                    return fname
                else:
                    print(f"  ⚠️ Неверный контент (size: {len(resp.content)})")
            else:
                print(f"  ⚠️ HTTP {resp.status_code}")

        except requests.Timeout:
            print("  ⚠️ Таймаут при генерации изображения")
        except requests.RequestException as e:
            print(f"  ⚠️ Ошибка сети: {e}")
        except Exception as e:
            print(f"  ❌ Неожиданная ошибка: {e}")

        if attempt < max_retries - 1:
            await_time = (attempt + 1) * 2
            print(f"  ⏳ Ждём {await_time}с...")
            time.sleep(await_time)

    print("  ❌ Не удалось сгенерировать изображение")
    return None


def cleanup_image(filepath: Optional[str]) -> None:
    if filepath and os.path.exists(filepath):
        try:
            os.remove(filepath)
        except Exception as e:
            print(f"  ⚠️ Не удалось удалить {filepath}: {e}")


# ============ АВТОПОСТ ============

async def autopost():
    clean_old_posts()
    print("🔄 Загрузка статей...")
    articles = load_articles_from_sites()
    candidates = filter_articles(articles)

    if not candidates:
        print("❌ Нет подходящих свежих новостей.")
        return

    print(f"📊 Найдено кандидатов: {len(candidates)}")

    posted_count = 0
    max_posts = 1

    sensational_candidates = [c for c in candidates if c.get("post_type") == "sensational"]
    other_candidates = [c for c in candidates if c.get("post_type") != "sensational"]

    def pick_next_article() -> Optional[Dict]:
        if sensational_candidates:
            return sensational_candidates.pop(0)
        if other_candidates:
            return other_candidates.pop(0)
        return None

    while posted_count < max_posts:
        art = pick_next_article()
        if not art:
            break

        print(f"\n🔍 Обработка: {art['title'][:80]}... [{art['source']}]")

        post_text = short_summary(art["title"], art["summary"], art["link"])

        if not post_text:
            print("  ⚠️ Не удалось сгенерировать текст, пробуем следующую")
            continue

        img = generate_image(art["title"])

        try:
            if img:
                await bot.send_photo(
                    CHANNEL_ID,
                    photo=FSInputFile(img),
                    caption=post_text,
                )
            else:
                await bot.send_message(CHANNEL_ID, text=post_text)

            save_posted(art["id"])
            posted_count += 1

            # 🔥 СОХРАНЯЕМ В GIT ПОСЛЕ КАЖДОГО ПОСТА
            commit_and_push_posted_articles()

            save_last_security_ts()
            last_type = art.get("post_type", "security")
            save_last_post_type(last_type)
            print(f"✅ Опубликовано: {art['source']} (type={last_type})")

        except Exception as e:
            print(f"❌ Ошибка отправки в Telegram: {e}")
        finally:
            cleanup_image(img)

    if posted_count == 0:
        print("⚠️ Не удалось опубликовать ни одного поста")
    else:
        print(f"\n🎉 Успешно опубликовано постов: {posted_count}")


async def main():
    try:
        await autopost()
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
