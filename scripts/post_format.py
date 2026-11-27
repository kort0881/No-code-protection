#!/usr/bin/env python3
"""Промт, валидатор и ранжирование для KIBER SOS («Защита Без Кода»).

Без сетевых вызовов и сторонних зависимостей — тестируется офлайн (`pytest`).
Главный скрипт собирает материалы, зовёт Groq и отправляет пост.

Три вещи, за которые отвечает модуль:
1. `build_prompt` — промт, который даёт пост, читаемый за 30–40 секунд.
2. `sanitize_post` / `validate_post` — проверка формата и фактов до отправки.
3. `score_candidate` / `rank_candidates` — выбор САМОГО значимого материала,
   а не первого попавшегося из перемешанного списка.
"""

from __future__ import annotations

import html
import random
import re
import time
from collections.abc import Iterable, Sequence

# ─────────────────────────────── формат поста ───────────────────────────────

MIN_POST_LENGTH = 500
MAX_POST_LENGTH = 1500

ALLOWED_TAGS = ('b', 'i', 'code', 'blockquote')

TITLE_EMOJI = ('🛡', '🚨', '💰', '🔍', '🧪', '⚠️')

# Заглушки и вода: раньше модель вставляла их почти в каждый пост.
BANNED_PHRASES = (
    'в источнике это не уточняется',
    'в источнике не уточняется',
    'источник не уточняет',
    'точное количество устройств не раскрывается',
    'информация отсутствует',
    'подробности не сообщаются',
    'в современном мире',
    'не секрет, что',
    'стоит отметить',
    'важно понимать',
    'как известно',
    'данная уязвимость',
    'данный инцидент',
    'линий обороны',
    'критично для построения',
    'является',
    'осуществля',
    'в рамках',
    'будьте бдительны',
    'будьте осторожны',
    'будьте внимательны',
    'соблюдайте осторожность',
    'регулярно обновляйте',
    'своевременно устанавливайте',
    'используйте сложные пароли',
    'используйте надежные пароли',
    'используйте уникальные пароли',
    'защитить свои данные',
    'комплексный подход',
    'многоуровневая защита',
    'повышение осведомленности',
    'обучение сотрудников',
    'проверяйте ссылки',
    'проверяйте url',
    'проверяйте адрес',
    'проверяйте домен',
    'не открывайте подозрительные',
    'делайте резервные копии',
    'создавайте бэкапы',
    'в дикой природе',
    'в дикой сети',
)

SERVICE_LABELS = ('лид:', 'вступление:', 'что случилось:', 'почему это важно:',
                  'где и кого это касается:', 'вывод:')

CLICKBAIT_PHRASES = (
    'шок', 'вы не поверите', 'срочно!!!', 'просто представьте',
    'это изменит всё', 'все пропало',
)

# «Действия», которые ничего не дают читателю: реклама вебинара, чтение
# исследования, обещание «следить за обновлениями».
FAKE_ACTIONS = (
    'вебинар',
    'ознакомиться',
    'ознакомьтесь',
    'прочитать исследование',
    'изучить исследование',
    'следить за',
    'следите за',
    'подписаться на бюллетень',
    'подписаться на рассылку',
    'оставаться в курсе',
    'мониторить',
)

# Символы, из-за которых текст невозможно нормально скопировать и найти в поиске.
BAD_CHARS = {
    '\u2011': '-',   # non-breaking hyphen — главный враг прошлых постов
    '\u2010': '-',
    '\u2012': '-',
    '\u2013': '—',
    '\u2212': '-',
    '\u00a0': ' ',
    '\u200b': '',
    '\u2009': ' ',
}

CVE_RE = re.compile(r'CVE-\d{4}-\d{4,7}', re.IGNORECASE)


def _balance_tags(text: str) -> str:
    """Закрывает открытые теги и выкидывает лишние закрывающие."""
    tokens = re.split(r'(</?(?:b|i|code|blockquote)>)', text)
    out: list[str] = []
    stack: list[str] = []
    for token in tokens:
        match = re.fullmatch(r'<(/?)(b|i|code|blockquote)>', token)
        if not match:
            out.append(token)
            continue
        closing, name = match.group(1), match.group(2)
        if not closing:
            stack.append(name)
            out.append(token)
            continue
        if name in stack:
            while stack and stack[-1] != name:
                out.append(f'</{stack.pop()}>')
            stack.pop()
            out.append(token)
    while stack:
        out.append(f'</{stack.pop()}>')
    return ''.join(out)


def sanitize_post(text: str) -> str:
    """Чистка вывода модели: разрешённые теги, обычные дефисы, аккуратные абзацы."""
    if not text:
        return ''
    for bad, good in BAD_CHARS.items():
        text = text.replace(bad, good)
    text = text.replace('**', '')

    # Ссылки в тексте не нужны: первоисточник добавляется отдельной строкой.
    text = re.sub(r'https?://[^\s"\'<>]+', '', text)

    def keep_or_drop(match: re.Match) -> str:
        body = match.group(1)
        name = re.match(r'/?\s*([a-zA-Z0-9]+)', body)
        tag = name.group(1).lower() if name else ''
        return match.group(0) if tag in ALLOWED_TAGS else ''

    text = re.sub(r'</?([^<>]{1,120})>', keep_or_drop, text)
    text = _balance_tags(text)
    text = re.sub(r'&(?!(?:amp|lt|gt|quot|#\d+);)', '&amp;', text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'\n +', '\n', text)
    return text.strip()


def _strip_tags(text: str) -> str:
    return re.sub(r'<[^>]+>', '', text)


def _section_after_label(text: str, labels: Sequence[str]) -> str:
    """Кусок текста после метки «Что делать» до конца поста."""
    low = text.lower()
    for label in labels:
        idx = low.find(label)
        if idx != -1:
            return text[idx:]
    return ''


def validate_post(text: str, source_text: str = '') -> list[str]:
    """Список нарушений. Пустой список — пост можно публиковать."""
    errors: list[str] = []
    if not text:
        return ['пустой текст']

    plain = _strip_tags(text).strip()
    low = plain.lower()

    if not (MIN_POST_LENGTH <= len(plain) <= MAX_POST_LENGTH):
        errors.append(f'длина {len(plain)} вне {MIN_POST_LENGTH}–{MAX_POST_LENGTH}')

    first_line = plain.split('\n', 1)[0].strip()
    if not first_line:
        errors.append('нет заголовка')
    else:
        if not first_line.startswith(TITLE_EMOJI):
            errors.append('заголовок без тематического эмодзи')
        if '<b>' not in text.split('\n', 1)[0]:
            errors.append('заголовок не выделен <b>')
        if len(first_line) > 110:
            errors.append('заголовок длиннее 110 символов')

    for phrase in BANNED_PHRASES:
        if phrase in low:
            errors.append(f'запрещённая фраза: «{phrase}»')
    for phrase in CLICKBAIT_PHRASES:
        if phrase in low:
            errors.append(f'кликбейт: «{phrase}»')
    for label in SERVICE_LABELS:
        if label in low:
            errors.append(f'служебная подпись в тексте: «{label}»')

    if 'подробнее' in low or 'первоисточник' in low or 'источник:' in low:
        errors.append('строку про первоисточник добавляет код, в тексте её быть не должно')
    if re.search(r'(^|\s)#\w{3,}', plain):
        errors.append('хештеги запрещены')
    if re.search(r'https?://', plain):
        errors.append('ссылки в тексте запрещены')

    for tag in ALLOWED_TAGS:
        if text.count(f'<{tag}>') != text.count(f'</{tag}>'):
            errors.append(f'незакрытый тег <{tag}>')
    if len(re.findall(r'<b>', text)) > 4:
        errors.append('слишком много жирного текста (>4)')
    if len(re.findall(r'<code>', text)) > 6:
        errors.append('слишком много <code> (>6)')

    bullets = [ln for ln in text.split('\n') if ln.strip().startswith(('•', '—', '- '))]
    if len(bullets) < 2:
        errors.append('меньше двух пунктов списка')
    if len(bullets) > 8:
        errors.append('больше восьми пунктов списка')
    blocks = [b for b in re.split(r'\n\s*\n', text) if b.strip()]
    for block in blocks:
        block_bullets = [ln for ln in block.split('\n') if ln.strip().startswith(('•', '—', '- '))]
        if len(block_bullets) > 4:
            errors.append('в одном блоке больше четырёх пунктов — читатель устанет')
    for line in bullets:
        if len(_strip_tags(line)) > 170:
            errors.append(f'пункт списка длиннее 170 символов: «{_strip_tags(line)[:40]}…»')

    paragraphs = [p for p in re.split(r'\n\s*\n', plain) if p.strip()]
    if len(paragraphs) < 2:
        errors.append('пост не разбит на блоки')
    for para in paragraphs:
        if len(para) > 700:
            errors.append('абзац длиннее 700 символов — стена текста')

    sentences = [s for s in re.split(r'(?<=[.!?])\s+', plain) if s.strip()]
    if len([s for s in sentences if len(s.split()) > 32]) > 1:
        errors.append('больше одного предложения длиннее 32 слов')

    # Год из головы — второй по цене косяк после придуманного CVE.
    if source_text:
        for year in set(re.findall(r'\b20\d{2}\b', plain)):
            if year not in source_text:
                errors.append(f'года {year} нет в материале — дата придумана')

    for line in bullets:
        body = _strip_tags(line).lstrip('•—- ').strip()
        if body and body[0].isalpha() and body[0].islower():
            errors.append(f'пункт списка с маленькой буквы: «{body[:40]}…»')

    # Придуманные CVE — самая дорогая ошибка: читатель ищет несуществующий патч.
    for cve in set(CVE_RE.findall(plain)):
        if source_text and cve.upper() not in source_text.upper():
            errors.append(f'CVE {cve} нет в материале — номер придуман')
    for match in re.findall(r'CVE-\d{4}-\d+', plain, re.IGNORECASE):
        digits = match.split('-')[-1]
        if len(digits) < 4 or len(digits) > 5:
            errors.append(f'неправдоподобный номер CVE: {match}')

    actions = _section_after_label(text, ('что делать', 'что сделать'))
    if actions:
        actions_low = actions.lower()
        for fake in FAKE_ACTIONS:
            if fake in actions_low:
                errors.append(f'в «Что делать» не действие, а заглушка: «{fake}»')
                break

    return errors


# ──────────────────────────────── промт ─────────────────────────────────────

PROFILE_TOPICS = (
    'кибербезопасность: уязвимости и патчи, атаки и взломы, утечки данных, '
    'вредоносное ПО и шифровальщики, фишинг и социальная инженерия, '
    'защита личных данных, приватность, безопасность устройств и сервисов'
)

SYSTEM_RULES = f'''Ты — редактор Telegram-канала «Защита Без Кода» о {PROFILE_TOPICS}.
Читатели — обычные люди с домашними устройствами и небольшие админы, не корпоративный SOC.
Задача — пост, который прочитают за 30–40 секунд и сразу поймут три вещи:
что случилось, касается ли это лично читателя, что он может сделать.

ГЛАВНЫЙ ПРИНЦИП
Самый интересный или самый важный факт материала идёт в лид — первым.
Не придерживай факты «на потом» и не начинай с того, кто опубликовал отчёт.

ФАКТЫ
- Только то, что есть в материале. Нет данных — не упоминай их и не пиши заглушек
  («в источнике не уточняется», «точное количество не раскрывается»).
- Переноси точно: номера CVE (только те, что есть в материале — придуманный номер
  хуже, чем его отсутствие), названия версий и продуктов, суммы, страны, даты и годы.
  Год бери из материала: если в нём 2026, то и в тексте 2026, а не «2023».
- Определи, кого это касается на самом деле, и скажи прямо в первом-втором
  предложении: домашние устройства, телефоны и браузеры, конкретные сервисы,
  корпоративные серверы или промышленное оборудование. Если угроза только
  корпоративная — так и напиши, чтобы человек не искал её у себя дома.
- Если из материала следует, что проблема уже используется в реальных атаках,
  а не «теоретически возможна», — это ключевой факт для лида.

ЧИТАЕМОСТЬ (важнее объёма)
- Заголовок: 6–10 слов, суть и последствие, начинается с эмодзи (🛡 уязвимость
  и патч, 🚨 активная атака или утечка, 💰 деньги, штрафы, выкуп, 🔍 исследование
  и разбор, 🧪 техника или инструмент, ⚠️ предупреждение регулятора) и выделен <b>…</b>.
- Абзацы по 1–3 предложения, между блоками пустая строка.
- Список 2–4 пункта, каждый — одна строка до 120 символов, одна мысль:
  продукт и версия, масштаб, кто атакует, чем грозит, сроки.
- Каждый пункт — с заглавной буквы и законченной мыслью, без обрывов
  («стековый переполнение» — так нельзя, «в контроллере переполнение стека» — можно).
- Пункты дополняют лид, а не пересказывают его теми же словами.
- Предложение короче 20 слов. Длинную мысль разбивай на две.
- Не переводи дословно: «in the wild» — «в реальных атаках», «out-of-band» —
  «внеплановое», «stop error» — «синий экран», «breakthrough rate» — «доля атак,
  которые проходят защиту».

ФОРМАТ (ровно такой, без своих заголовков и без нумерации)
🛡 <b>Заголовок</b>

Первое предложение — сразу текст, без служебных подписей «Лид:», «Вступление:»,
«Что случилось:». 1–2 предложения: что произошло, кого касается, чем грозит.

• конкретный факт
• конкретный факт
• конкретный факт

<b>Что делать</b>
• действие
• действие

<i>Одна фраза-вывод: что это значит на практике.</i>

- Блок «Что делать» — только если в материале есть настоящие действия читателя:
  установить патч с версией, отключить конкретную функцию, сменить настройку,
  проверить конкретный файл или журнал, отказаться от конкретной схемы входа.
- Нельзя выдавать за действие: регистрацию на вебинар, чтение исследования,
  «следите за обновлениями», «следите за бюллетенями», «будьте внимательны»,
  «используйте антивирус». Если настоящих действий нет — блока нет вообще.
- Вывод не нужен, если он повторяет лид.

РАЗМЕТКА TELEGRAM (parse_mode=HTML)
- Разрешены только <b>, <i>, <code>, <blockquote>. Других тегов и Markdown нет.
- <code>…</code> — для номера CVE, версии, пути, порта, домена, названия функции.
  Не больше 6 таких вставок.
- Ссылок, хештегов, гирлянд из эмодзи и слова «Источник» в тексте нет: строку
  со ссылкой на первоисточник добавляет код.

ЯЗЫК
- Живой короткий русский. Термины по-русски; английский в скобках — только если
  без него читатель не найдёт информацию в поиске.
- Запрещено: канцелярит («данный», «является», «осуществлять», «в рамках»),
  штампы («в современном мире», «стоит отметить», «важно понимать»),
  кликбейт, мораль и общие советы, реклама и упоминание наших сервисов.
- Дефис обычный «-», кавычки «ёлочки». Неразрывные дефисы и пробелы нельзя.

ОБЪЁМ: 500–1400 символов без ссылки. Короткий плотный пост лучше длинного.

Верни ТОЛЬКО валидный JSON без пояснений и без ```:
{{"reject": false, "post_text": "..."}}

Если материал не про безопасность, приватность, утечки, атаки или защиту
данных — верни {{"reject": true, "post_text": ""}}.
'''


def build_prompt(title: str, text: str, source: str = '',
                 recent_titles: Sequence[str] = ()) -> str:
    """Собирает промт под конкретный материал."""
    parts = [SYSTEM_RULES]
    if recent_titles:
        recent = '\n'.join(f'- {t}' for t in list(recent_titles)[:8])
        parts.append(f'Уже выходило в канале (не повторяй эти сюжеты и формулировки):\n{recent}')
    parts.append(
        'МАТЕРИАЛ\n'
        f'Заголовок: {title}\n'
        f'Площадка: {source or "не указана"}\n'
        f'Текст:\n{text[:6000]}'
    )
    return '\n\n'.join(parts)


# ─────────────────────── выбор значимого материала ──────────────────────────

MASS_PLATFORMS = (
    'windows', 'android', 'ios', 'iphone', 'chrome', 'firefox', 'edge', 'safari',
    'telegram', 'whatsapp', 'signal', 'gmail', 'outlook', 'router', 'tp-link',
    'mikrotik', 'd-link', 'dlink', 'keenetic', 'asus', 'wi-fi', 'wifi',
    'wordpress', 'drupal', 'joomla', 'bitrix', 'chrome extension', 'steam',
    'discord', 'zoom', '1password', 'bitwarden', 'lastpass',
)

ENTERPRISE_ONLY = (
    'sap', 'oracle e-business', 'oracle ebs', 'citrix', 'vmware esxi', 'vcenter',
    'industrial', 'ics', 'scada', 'plc', 'siemens', 'schneider', 'honeywell',
    'hospital', 'healthcare', 'government agency', 'federal agency',
    'airline', 'railway', 'utility', 'power grid',
)

CORE_TOPIC = (
    'утечк', 'шифровальщик', 'ransomware', 'фишинг', 'phishing', 'вредонос',
    'malware', 'взлом', 'мошенник', 'уязвимост', 'cve-', 'патч', 'patch',
    'приватн', 'персональн', 'парол', 'passkey', 'mfa', '2fa',
)

IMPACT_HIGH = (
    'actively exploited', 'in the wild', 'exploited in attacks', 'zero-day',
    '0day', 'critical', 'ransomware', 'remote code execution', 'rce',
    'authentication bypass', 'privilege escalation', 'data breach',
    'millions of users', 'supply chain',
)


def score_candidate(title: str, text: str, published_ts: float | None = None,
                    now_ts: float | None = None) -> float:
    """Насколько материал важен читателю канала. Больше — важнее."""
    now = now_ts if now_ts is not None else time.time()
    content = f'{title} {text}'.lower()
    score = 0.0

    if CVE_RE.search(content):
        score += 12
    for marker in IMPACT_HIGH:
        if marker in content:
            score += 20
            break
    if any(word in content for word in MASS_PLATFORMS):
        score += 25
    if any(word in content for word in CORE_TOPIC):
        score += 30
    if any(word in content for word in ENTERPRISE_ONLY):
        score -= 20

    if published_ts:
        age_hours = max(0.0, (now - published_ts) / 3600)
        if age_hours <= 18:
            score += 25
        elif age_hours <= 36:
            score += 15
        elif age_hours <= 72:
            score += 5
        else:
            score -= 25

    return score


def rank_candidates(items: Iterable, now_ts: float | None = None,
                    jitter: float = 6.0) -> list:
    """Сортирует материалы по значимости. Объекты — любые с .title/.text,
    опционально .published_ts."""
    scored = []
    for item in items:
        base = score_candidate(
            getattr(item, 'title', ''),
            getattr(item, 'text', ''),
            getattr(item, 'published_ts', None),
            now_ts,
        )
        scored.append((base + random.uniform(0, jitter), item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored]


def html_escape(text: str) -> str:
    return html.escape(text, quote=True)
