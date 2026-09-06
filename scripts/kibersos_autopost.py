#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import asyncio
import random
import re
import time
import hashlib
import html
import tempfile
import shutil
import logging
from dataclasses import dataclass
from typing import Literal, Optional
from difflib import SequenceMatcher

import aiohttp
import feedparser
from youtube_transcript_api import YouTubeTranscriptApi
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from groq import Groq

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('KiberSOS')
logging.getLogger('httpx').setLevel(logging.WARNING)


def get_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f'❌ Missing environment variable: {name}')
    return value


GROQ_API_KEY = get_env('GROQ_API_KEY')
TELEGRAM_BOT_TOKEN = get_env('TELEGRAM_BOT_TOKEN')
CHANNEL_ID = get_env('CHANNEL_ID')

CACHE_DIR = os.getenv('CACHE_DIR', 'cache_sec')
os.makedirs(CACHE_DIR, exist_ok=True)
STATE_FILE = os.path.join(CACHE_DIR, 'state_groq_v3.json')
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=35)
MAX_POSTED_IDS = 500
RECENT_POSTS_CHECK = 10
RECENT_SIMILARITY_THRESHOLD = 0.50


@dataclass
class ModelConfig:
    name: str
    rpm: int
    daily_tokens: int


MODELS = {
    'main': ModelConfig(
        name='openai/gpt-oss-120b',
        rpm=30,
        daily_tokens=500000,
    )
}


class GroqBudget:
    def __init__(self):
        self.state_file = os.path.join(CACHE_DIR, 'groq_budget.json')
        self.data = self._load()

    def _load(self) -> dict:
        today = time.strftime('%Y-%m-%d')
        default = {
            'daily_tokens': {},
            'last_reset': today,
            'last_request_time': {},
            'request_count': {},
            'minute_start': {},
        }
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    saved = json.load(f)
                if saved.get('last_reset') != today:
                    saved['daily_tokens'] = {}
                    saved['last_reset'] = today
                    logger.info('🔄 New day: Groq token budget reset')
                default.update(saved)
        except Exception as exc:
            logger.warning(f'Budget state load error: {exc}')
        return default

    def save(self) -> None:
        try:
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False)
        except Exception as exc:
            logger.warning(f'Budget state save error: {exc}')

    def add_tokens(self, model_name: str, tokens: int) -> None:
        self.data['daily_tokens'][model_name] = self.data['daily_tokens'].get(model_name, 0) + tokens
        self.save()

    def can_use_model(self, model_key: str) -> bool:
        cfg = MODELS[model_key]
        used = self.data['daily_tokens'].get(cfg.name, 0)
        remaining = cfg.daily_tokens - used
        if remaining <= cfg.daily_tokens * 0.10:
            logger.warning(f'⚠️ Groq daily token budget low: {remaining} tokens left')
        return remaining > cfg.daily_tokens * 0.05

    async def wait_for_rate_limit(self, model_key: str) -> None:
        cfg = MODELS[model_key]
        model = cfg.name
        now = time.time()
        minute_start = self.data['minute_start'].get(model, 0)

        if now - minute_start >= 60:
            self.data['minute_start'][model] = now
            self.data['request_count'][model] = 0

        request_count = self.data['request_count'].get(model, 0)
        if request_count >= cfg.rpm - 2:
            wait = max(1, 61 - (now - self.data['minute_start'][model]))
            logger.info(f'⏳ Groq RPM limit. Waiting {wait:.1f}s')
            await asyncio.sleep(wait)
            self.data['minute_start'][model] = time.time()
            self.data['request_count'][model] = 0

        last_request = self.data['last_request_time'].get(model, 0)
        delay = 2 - (time.time() - last_request)
        if delay > 0:
            await asyncio.sleep(delay)

        self.data['request_count'][model] = self.data['request_count'].get(model, 0) + 1
        self.data['last_request_time'][model] = time.time()
        self.save()


budget = GroqBudget()
groq_client = Groq(api_key=GROQ_API_KEY)
bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)


class Config:
    min_post_length = 700
    max_post_length = 1900
    max_article_age_hours = 720
    max_posts_per_run = 1
    max_attempts = 15


config = Config()

STOP_WORDS = [
    'headphone', 'earbuds', 'airpods', 'bluetooth speaker', 'jbl', 'bose',
    'sony wh-', 'beats', 'sennheiser', 'noise canceling', 'audio quality',
    'phone review', 'camera review', 'unboxing', 'quarterly earnings',
    'appointed ceo', 'stock price', 'bitcoin price', 'casino', 'gambling',
    'weight loss', 'free iphone', 'work from home',
]

SECURITY_KEYWORDS = [
    'vulnerability', 'exploit', 'malware', 'ransomware', 'phishing', 'breach',
    'leak', 'hack', 'attack', 'patch', 'cve', '0day', 'zero-day', 'apt',
    'threat actor', 'lockbit', 'blackcat', 'alert', 'ioc', 'vpn', 'dpi',
    'proxy', 'censorship', 'block', 'уязвимост', 'эксплойт', 'вредонос',
    'вымогател', 'фишинг', 'утечк', 'хакер', 'атак', 'блокировк', 'прокси',
]

GENERIC_ADVICE_PHRASES = [
    'будьте бдительны', 'будьте осторожны', 'будьте внимательны',
    'соблюдайте осторожность', 'регулярно обновляйте',
    'используйте сложные пароли', 'используйте надежные пароли',
    'используйте уникальные пароли', 'защитить свои данные',
    'комплексный подход', 'многоуровневая защита', 'повышение осведомленности',
    'обучение сотрудников', 'проверяйте ссылки', 'проверяйте вложения',
    'не открывайте подозрительные', 'делайте резервные копии',
    'создавайте бэкапы', 'своевременно устанавливайте обновления',
]


def clean_text(text: str) -> str:
    if not text:
        return ''
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    return re.sub(r'\s+', ' ', text).strip()


def passes_local_filters(title: str, text: str) -> bool:
    content = f'{title} {text}'.lower()
    if any(word in content for word in STOP_WORDS):
        return False
    if len(text) < 50:
        return False
    return any(keyword in content for keyword in SECURITY_KEYWORDS)


def normalize_title(title: str) -> str:
    title = re.sub(r'[^\w\s]', ' ', title.lower())
    stop_words = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'in', 'on', 'at', 'to', 'for', 'of', 'and', 'or', 'new', 'how'}
    return ' '.join(word for word in title.split() if word not in stop_words and len(word) > 2)


def extract_key_entities(text: str) -> set[str]:
    entities = {value.upper() for value in re.findall(r'CVE-\d{4}-\d+', text, re.I)}
    lower = text.lower()
    for name in [
        'lockbit', 'blackcat', 'lazarus', 'apt28', 'sandworm', 'emotet',
        'trickbot', 'cobalt strike', 'microsoft', 'google', 'apple', 'cisco',
        'fortinet', 'vmware', 'windows', 'linux', 'android', 'telegram',
    ]:
        if name in lower:
            entities.add(name)
    return entities


def detect_topic(title: str, text: str) -> str:
    content = f'{title} {text}'.lower()
    if any(value in content for value in ['ransomware', 'lockbit', 'blackcat', 'вымогател']):
        return 'ransomware'
    if any(value in content for value in ['apt', 'lazarus', 'apt28', 'sandworm']):
        return 'apt'
    if re.search(r'cve-\d{4}-\d+', content):
        return 'vulnerability'
    if any(value in content for value in ['phishing', 'social engineering', 'фишинг']):
        return 'phishing'
    if any(value in content for value in ['ddos', 'botnet']):
        return 'ddos'
    if any(value in content for value in ['breach', 'leak', 'exposed', 'утечк']):
        return 'breach'
    if any(value in content for value in ['vpn', 'dpi', 'proxy', 'block', 'блокировк', 'прокси']):
        return 'privacy_network'
    if any(value in content for value in ['patch', 'update', 'обновлен', 'патч']):
        return 'patch'
    return 'general'


def calculate_similarity(title1: str, text1: str, title2: str, text2: str) -> float:
    title_similarity = SequenceMatcher(None, normalize_title(title1), normalize_title(title2)).ratio()
    entities1 = extract_key_entities(f'{title1} {text1}')
    entities2 = extract_key_entities(f'{title2} {text2}')
    entity_similarity = len(entities1 & entities2) / len(entities1 | entities2) if entities1 and entities2 else 0.0
    cve1 = {entity for entity in entities1 if entity.startswith('CVE-')}
    cve2 = {entity for entity in entities2 if entity.startswith('CVE-')}
    if cve1 and cve2 and cve1 & cve2:
        return 1.0
    text_similarity = SequenceMatcher(None, text1[:700].lower(), text2[:700].lower()).ratio()
    return title_similarity * 0.50 + entity_similarity * 0.35 + text_similarity * 0.15


class State:
    def __init__(self):
        self.data = {
            'posted_ids': {},
            'recent_titles': [],
            'recent_posts': [],
            'recent_topics': [],
        }
        self._load()

    def _load(self) -> None:
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, 'r', encoding='utf-8') as f:
                    self.data.update(json.load(f))
        except Exception as exc:
            logger.warning(f'State load error: {exc}')

    def save(self) -> None:
        fd, temp_path = tempfile.mkstemp(dir=CACHE_DIR, suffix='.json')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False)
            shutil.move(temp_path, STATE_FILE)
        except Exception as exc:
            logger.warning(f'State save error: {exc}')
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    def is_posted(self, uid: str) -> bool:
        return uid in self.data['posted_ids']

    def is_duplicate(self, title: str, text: str) -> bool:
        new_cves = set(re.findall(r'CVE-\d{4}-\d+', text, re.I))
        for old in self.data['recent_posts'][-20:]:
            if SequenceMatcher(None, normalize_title(title), normalize_title(old.get('title', ''))).ratio() > 0.65:
                return True
            old_cves = set(re.findall(r'CVE-\d{4}-\d+', old.get('text', ''), re.I))
            if new_cves and old_cves and new_cves & old_cves:
                return True
            if calculate_similarity(title, text, old.get('title', ''), old.get('text', '')) > 0.58:
                return True
        return False

    def is_too_similar_to_recent(self, title: str, text: str) -> bool:
        recent = self.data['recent_posts'][-RECENT_POSTS_CHECK:]
        new_topic = detect_topic(title, text)
        new_entities = extract_key_entities(f'{title} {text}')
        for old in recent:
            old_title = old.get('title', '')
            old_text = old.get('text', '')
            if calculate_similarity(title, text, old_title, old_text) > RECENT_SIMILARITY_THRESHOLD:
                return True
            if new_topic == old.get('topic') and new_topic != 'general':
                old_entities = extract_key_entities(f'{old_title} {old_text}')
                if len(new_entities & old_entities) >= 2:
                    return True
        return False

    def needs_diversity(self) -> str:
        topics = self.data['recent_topics'][-5:]
        if len(topics) < 5:
            return ''
        for topic in set(topics):
            if topics.count(topic) >= 3:
                return topic
        return ''

    def mark_posted(self, uid: str, title: str, text: str, topic: str) -> None:
        if len(self.data['posted_ids']) >= MAX_POSTED_IDS:
            ordered = sorted(self.data['posted_ids'].items(), key=lambda pair: pair[1])[-400:]
            self.data['posted_ids'] = dict(ordered)
        self.data['posted_ids'][uid] = int(time.time())
        self.data['recent_titles'].append(title)
        self.data['recent_titles'] = self.data['recent_titles'][-50:]
        self.data['recent_posts'].append({
            'title': title,
            'text': text[:1200],
            'topic': topic,
            'time': int(time.time()),
        })
        self.data['recent_posts'] = self.data['recent_posts'][-30:]
        self.data['recent_topics'].append(topic)
        self.data['recent_topics'] = self.data['recent_topics'][-15:]
        self.save()


state = State()


@dataclass
class NewsItem:
    type: Literal['news', 'video']
    title: str
    text: str
    link: str
    source: str
    uid: str


async def call_groq(prompt: str, max_tokens: int = 1400) -> tuple[str, int]:
    model_key = 'main'
    if not budget.can_use_model(model_key):
        return '', 0
    cfg = MODELS[model_key]
    try:
        await budget.wait_for_rate_limit(model_key)
        response = await asyncio.to_thread(
            lambda: groq_client.chat.completions.create(
                model=cfg.name,
                messages=[{'role': 'user', 'content': prompt}],
                max_tokens=max_tokens,
                temperature=0.45,
            )
        )
        result = (response.choices[0].message.content or '').strip()
        tokens = response.usage.total_tokens if response.usage else 0
        budget.add_tokens(cfg.name, tokens)
        logger.info(f'✅ Groq: {tokens} tokens')
        return result, tokens
    except Exception as exc:
        logger.warning(f'⚠️ Groq error: {exc}')
        return '', 0


async def call_groq_with_retry(prompt: str, max_tokens: int, retries: int = 2) -> tuple[str, int]:
    for attempt in range(retries):
        result, tokens = await call_groq(prompt, max_tokens)
        if result:
            return result, tokens
        delay = 5 * (2 ** attempt)
        logger.warning(f'Groq retry {attempt + 1}/{retries} in {delay}s')
        await asyncio.sleep(delay)
    return '', 0


async def fetch_full_article(url: str, session: aiohttp.ClientSession) -> str:
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        async with session.get(url, timeout=HTTP_TIMEOUT, headers=headers, allow_redirects=True) as response:
            if response.status != 200:
                return ''
            page = await response.text(errors='ignore')
        paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', page, re.DOTALL | re.IGNORECASE)
        text = clean_text(' '.join(paragraphs))
        return text[:7000]
    except Exception as exc:
        logger.debug(f'Article fetch error: {exc}')
        return ''


def has_generic_advice(text: str) -> list[str]:
    lower = text.lower()
    return [phrase for phrase in GENERIC_ADVICE_PHRASES if phrase in lower]


def remove_links_and_html(text: str) -> str:
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'<[^>]+>', '', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def has_required_post_structure(text: str) -> bool:
    required = [
        'что случилось:',
        'где и кого это касается:',
        'почему это важно:',
        'что делать:',
        'подробнее: в первоисточнике по ссылке ниже.',
    ]
    lower = text.lower()
    if not all(section in lower for section in required):
        return False
    steps = re.findall(r'(?m)^\s*[123][.)]\s+\S.+', text)
    return len(steps) >= 3


async def generate_post(item: NewsItem, session: aiohttp.ClientSession) -> Optional[str]:
    full_text = item.text
    if len(full_text) < 1800:
        article_text = await fetch_full_article(item.link, session)
        if article_text:
            full_text = clean_text(f'{full_text} {article_text}')
            logger.info(f'   📄 Full article loaded: {len(article_text)} chars')

    prompt = f'''Ты — редактор Telegram-канала о кибербезопасности, VPN, приватности,
обходе блокировок, DPI и цифровой безопасности. Создай полноценный Telegram-пост
на русском языке, используя ТОЛЬКО факты из исходного материала ниже.

КРИТИЧЕСКИ ВАЖНО:
- Не делай короткий анонс и не пересказывай новость поверхностно.
- Не выдумывай версии, CVE, даты, страны, технические причины, последствия и инструкции.
- Сохраняй конкретику из источника: кто, что, где, когда, какие продукты, версии,
  CVE, цифры, методы атаки, условия, ограничения и официальные действия.
- Пиши простым разговорным русским, без канцелярита, рекламы, кликбейта и хештегов.
- Не используй HTML, Markdown, ссылки или слово «Источник» в самом тексте.

Верни ТОЛЬКО валидный JSON. Никаких пояснений, никаких тройных кавычек:
{{"reject": false, "post_text": "..."}}

Если материал не относится к кибербезопасности, уязвимостям, атакам, утечкам,
малвари, VPN, прокси, DPI, приватности или блокировкам — верни:
{{"reject": true, "post_text": ""}}

Структура post_text строго обязательна:

🛡 Короткий точный заголовок по сути новости

Что случилось:
1–2 абзаца: объясни событие и важный контекст. Не пропускай существенные детали.

Где и кого это касается:
Укажи названные в источнике компанию, сервис, страну, продукт, версию,
инфраструктуру и аудиторию. Если данные отсутствуют, напиши: «В источнике это не уточняется».

Почему это важно:
Покажи практическое значение и последствия именно этой новости.

Что делать:
1. Первое конкретное действие, которое подтверждается источником.
2. Второе конкретное действие, которое подтверждается источником.
3. Третье конкретное действие, которое подтверждается источником.

Если в источнике нет трёх точных действий, не придумывай универсальные советы.
В таком случае в пунктах можно указать только честные действия: проверить названный
продукт/версию/индикаторы, свериться с официальным бюллетенем, применить конкретное
исправление, если это упоминается в материале. Не пиши: «будьте бдительны»,
«используйте антивирус», «регулярно обновляйтесь», «проверяйте ссылки»,
«используйте сложные пароли».

Последняя строка post_text должна быть ТОЧНО такой:
Подробнее: в первоисточнике по ссылке ниже.

Размер post_text: от 700 до 1900 символов без ссылки.

НОВОСТЬ:
Заголовок: {item.title}
Площадка: {item.source}
Материал:
{full_text[:6000]}
'''

    raw_text, _ = await call_groq_with_retry(prompt, max_tokens=1400, retries=2)
    if not raw_text:
        return None

    try:
        result = json.loads(raw_text)
        if result.get('reject') is True:
            logger.info('⏩ AI rejected article')
            return None
        text = str(result.get('post_text', '')).strip()
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning(f'⏩ Invalid AI JSON: {exc}')
        return None

    text = remove_links_and_html(text)
    if not text or text.upper().startswith('SKIP'):
        return None

    if len(text) < config.min_post_length:
        logger.info(f'⏩ Rejected: text too short ({len(text)} chars)')
        return None

    if not has_required_post_structure(text):
        logger.info('⏩ Rejected: required structure or three steps are missing')
        return None

    generic_phrases = has_generic_advice(text)
    if generic_phrases:
        logger.info(f'⏩ Rejected: generic advice found: {generic_phrases[:2]}')
        return None

    source_url = html.escape(item.link, quote=True)
    suffix = f'\n\n🔗 <a href="{source_url}">Источник: подробности и официальные материалы</a>'
    max_length = 4096 - len(suffix)
    if len(text) > max_length:
        text = text[:max_length].rsplit(' ', 1)[0].rstrip(',.:;') + '…'

    return text + suffix


async def fetch_rss(source: dict, session: aiohttp.ClientSession) -> list[NewsItem]:
    items: list[NewsItem] = []
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (compatible; KiberSOSBot/3.0)'}
        async with session.get(source['url'], timeout=HTTP_TIMEOUT, headers=headers) as response:
            if response.status != 200:
                logger.warning(f"RSS {source['name']}: HTTP {response.status}")
                return []
            xml = await response.text(errors='ignore')
        feed = feedparser.parse(xml)
        passed = 0
        for entry in feed.entries[:10]:
            link = entry.get('link', '')
            title = clean_text(entry.get('title', ''))
            if not link or not title:
                continue
            uid = hashlib.md5(link.encode('utf-8')).hexdigest()
            if state.is_posted(uid):
                continue
            summary = clean_text(entry.get('summary', '') or entry.get('description', ''))
            content = ''
            if getattr(entry, 'content', None):
                content = clean_text(entry.content[0].get('value', ''))
            article_text = clean_text(f'{summary} {content}')
            if passes_local_filters(title, article_text):
                items.append(NewsItem('news', title, article_text, link, source['name'], uid))
                passed += 1
        logger.info(f"   {source['name']}: {passed}/{len(feed.entries)} passed")
    except Exception as exc:
        logger.warning(f"RSS error ({source['name']}): {exc}")
    return items


async def fetch_youtube(channel: dict, session: aiohttp.ClientSession) -> list[NewsItem]:
    items: list[NewsItem] = []
    try:
        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel['id']}"
        async with session.get(feed_url, timeout=HTTP_TIMEOUT) as response:
            if response.status != 200:
                return []
            xml = await response.text(errors='ignore')
        feed = feedparser.parse(xml)
        for entry in feed.entries[:2]:
            video_id = entry.get('yt_videoid', '')
            if not video_id:
                continue
            uid = f'yt_{video_id}'
            if state.is_posted(uid):
                continue
            try:
                transcript = await asyncio.to_thread(
                    lambda value=video_id: YouTubeTranscriptApi.list_transcripts(value)
                    .find_transcript(['en', 'ru'])
                    .fetch()
                )
                transcript_text = ' '.join(item['text'] for item in transcript)
                title = clean_text(entry.get('title', ''))
                if passes_local_filters(title, transcript_text):
                    items.append(NewsItem(
                        'video',
                        title,
                        transcript_text[:7000],
                        entry.get('link', f'https://www.youtube.com/watch?v={video_id}'),
                        f"YT:{channel['name']}",
                        uid,
                    ))
            except Exception as exc:
                logger.debug(f"YouTube transcript skipped ({channel['name']}): {exc}")
    except Exception as exc:
        logger.warning(f"YouTube feed error ({channel['name']}): {exc}")
    return items


RSS_SOURCES = [
    {'name': 'BleepingComputer', 'url': 'https://www.bleepingcomputer.com/feed/'},
    {'name': 'TheHackerNews', 'url': 'https://feeds.feedburner.com/TheHackersNews'},
    {'name': 'KrebsOnSecurity', 'url': 'https://krebsonsecurity.com/feed/'},
    {'name': 'DarkReading', 'url': 'https://www.darkreading.com/rss.xml'},
    {'name': 'SecurityWeek', 'url': 'https://www.securityweek.com/feed/'},
    {'name': 'ThreatPost', 'url': 'https://threatpost.com/feed/'},
    {'name': 'NakedSecurity', 'url': 'https://nakedsecurity.sophos.com/feed/'},
    {'name': 'WeLiveSecurity', 'url': 'https://www.welivesecurity.com/en/rss/feed/'},
    {'name': 'GrahamCluley', 'url': 'https://grahamcluley.com/feed/'},
    {'name': 'Schneier', 'url': 'https://www.schneier.com/feed/'},
    {'name': 'CyberScoop', 'url': 'https://www.cyberscoop.com/feed/'},
    {'name': 'HackRead', 'url': 'https://www.hackread.com/feed/'},
    {'name': 'InfoSecurity Magazine', 'url': 'https://www.infosecurity-magazine.com/rss/news/'},
    {'name': 'ZDNet Security', 'url': 'https://www.zdnet.com/topic/security/rss.xml'},
    {'name': 'Malwarebytes Labs', 'url': 'https://blog.malwarebytes.com/feed/'},
    {'name': 'RecordedFuture', 'url': 'https://www.recordedfuture.com/feed'},
    {'name': 'Kaspersky', 'url': 'https://www.kaspersky.com/blog/feed/'},
    {'name': 'Cisco Talos', 'url': 'https://blog.talosintelligence.com/feeds/posts/default'},
    {'name': 'Unit42', 'url': 'https://unit42.paloaltonetworks.com/feed/'},
    {'name': 'CERT-EU', 'url': 'https://cert.europa.eu/blog/atom.xml'},
]

YOUTUBE_CHANNELS = [
    {'name': 'JohnHammond', 'id': 'UCVeW9qkBjo3zosnqUbG7CFw'},
    {'name': 'NetworkChuck', 'id': 'UC9x0AN7BWHpXyPic4IQC74Q'},
    {'name': 'LiveOverflow', 'id': 'UClcE-kVhqyiHCcjYwcpfj9w'},
]


async def main() -> None:
    logger.info('🚀 Starting KiberSOS v3.1 — detailed structured posts')
    try:
        async with aiohttp.ClientSession() as session:
            logger.info('📡 Fetching RSS and YouTube sources...')
            tasks = [fetch_rss(source, session) for source in RSS_SOURCES]
            tasks += [fetch_youtube(channel, session) for channel in YOUTUBE_CHANNELS]
            results = await asyncio.gather(*tasks)
            all_items = [item for group in results for item in group]
            logger.info(f'📦 Articles after filters: {len(all_items)}')

            if not all_items:
                return

            dominant_topic = state.needs_diversity()
            if dominant_topic:
                different_topics = [item for item in all_items if detect_topic(item.title, item.text) != dominant_topic]
                same_topic = [item for item in all_items if detect_topic(item.title, item.text) == dominant_topic]
                random.shuffle(different_topics)
                random.shuffle(same_topic)
                all_items = different_topics + same_topic
                logger.info(f'⚖️ Topic diversity: avoiding {dominant_topic} first')
            else:
                random.shuffle(all_items)

            posted = 0
            attempts = 0
            for item in all_items:
                if posted >= config.max_posts_per_run or attempts >= config.max_attempts:
                    break
                if not budget.can_use_model('main'):
                    logger.warning('⚠️ Groq token budget exhausted')
                    break

                attempts += 1
                topic = detect_topic(item.title, item.text)
                logger.info(f'🔍 [{attempts}/{config.max_attempts}] {item.source}: {item.title[:80]}')

                if state.is_duplicate(item.title, item.text):
                    logger.info('⏩ Duplicate skipped')
                    state.mark_posted(item.uid, item.title, item.text, topic)
                    continue

                if state.is_too_similar_to_recent(item.title, item.text):
                    logger.info('⏩ Too similar to recent posts, skipped')
                    state.mark_posted(item.uid, item.title, item.text, topic)
                    continue

                post_text = await generate_post(item, session)
                if not post_text:
                    logger.info('⏩ Article not suitable for publication')
                    state.mark_posted(item.uid, item.title, item.text, topic)
                    continue

                try:
                    await bot.send_message(CHANNEL_ID, text=post_text, disable_web_page_preview=True)
                    logger.info('✅ Post published')
                    state.mark_posted(item.uid, item.title, item.text, topic)
                    posted += 1
                except Exception as exc:
                    logger.error(f'Telegram send error: {exc}')

            logger.info(f'🏁 Finished. Published: {posted}')
    finally:
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())
