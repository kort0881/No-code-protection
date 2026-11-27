"""Офлайн-тесты формата поста. Запуск: pytest -q"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import post_format as pf

SAMPLE = """🛡 <b>Критическая уязвимость в роутерах TP-Link</b>

Атака без пароля: <code>CVE-2026-12345</code> позволяет удалённо попасть в панель управления роутеров TP-Link Archer AX21. Проблема уже используется в реальных атаках, а не существует в теории.

• Затронуты прошивки до <code>1.3.2</code> включительно.
• Атакующие меняют DNS и уводят трафик на свои сайты.
• Обновление вышло 25 ноября 2026 года, установка занимает минуту.

<b>Что делать</b>
• Обновите прошивку до <code>1.3.3</code> через веб-панель роутера.
• Отключите удалённое управление из интернета (WAN) в настройках."""


def test_valid_post_passes():
    assert pf.validate_post(pf.sanitize_post(SAMPLE), 'CVE-2026-12345 TP-Link 1.3.3') == []


def test_non_breaking_hyphens_normalized():
    raw = SAMPLE.replace('CVE-2026-12345', 'CVE\u20112026\u201112345')
    out = pf.sanitize_post(raw)
    assert '\u2011' not in out
    assert 'CVE-2026-12345' in out


def test_service_labels_rejected():
    raw = SAMPLE.replace('Атака без пароля:', 'Что случилось:')
    assert any('служебная подпись' in e for e in pf.validate_post(pf.sanitize_post(raw)))


def test_placeholder_phrase_rejected():
    raw = SAMPLE.replace('Атакующие меняют DNS', 'В источнике это не уточняется. Атакующие меняют DNS')
    errors = pf.validate_post(pf.sanitize_post(raw))
    assert any('не уточняется' in e for e in errors)


def test_generic_advice_rejected():
    raw = SAMPLE.replace('Отключите удалённое управление из интернета (WAN) в настройках.',
                         'Будьте бдительны и регулярно обновляйте всё.')
    errors = pf.validate_post(pf.sanitize_post(raw))
    assert any('бдительны' in e or 'обновляйте' in e for e in errors)


def test_fake_action_webinar_rejected():
    raw = SAMPLE.replace('Обновите прошивку до <code>1.3.3</code> через веб-панель роутера.',
                         'Зарегистрируйтесь на вебинар по безопасности роутеров.')
    errors = pf.validate_post(pf.sanitize_post(raw))
    assert any('вебинар' in e for e in errors)


def test_invented_cve_rejected():
    errors = pf.validate_post(pf.sanitize_post(SAMPLE), 'TP-Link Archer AX21, обновление 1.3.3')
    assert any('придуман' in e for e in errors)


def test_malformed_cve_rejected():
    raw = SAMPLE.replace('CVE-2026-12345', 'CVE-2026-132165')
    errors = pf.validate_post(pf.sanitize_post(raw), 'CVE-2026-132165')
    assert any('неправдоподобный номер CVE' in e for e in errors)


def test_invented_year_rejected():
    raw = SAMPLE.replace('25 ноября 2026 года', '25 ноября 2023 года')
    errors = pf.validate_post(pf.sanitize_post(raw), 'TP-Link 2026 CVE-2026-12345')
    assert any('2023' in e for e in errors)


def test_unbalanced_tags_closed():
    raw = SAMPLE.replace('</b>', '', 1)
    out = pf.sanitize_post(raw)
    assert out.count('<b>') == out.count('</b>')


def test_foreign_tags_removed():
    raw = SAMPLE.replace('<b>Критическая', '<script>x</script><a href="http://x">Критическая', 1)
    out = pf.sanitize_post(raw)
    assert '<script' not in out and '<a ' not in out


def test_links_and_hashtags_rejected():
    raw = SAMPLE + '\n\n#безопасность подробности http://evil.example'
    errors = pf.validate_post(pf.sanitize_post(raw))
    assert any('хештег' in e or 'ссылки' in e for e in errors)


def test_wall_of_text_rejected():
    flat = SAMPLE.replace('\n\n', '\n')
    assert any('не разбит' in e for e in pf.validate_post(flat))


def test_short_post_rejected():
    assert any('длина' in e for e in pf.validate_post('🛡 <b>Коротко</b>\n\n• раз\n• два'))


def test_ranking_prefers_mass_audience():
    class Item:
        def __init__(self, title, text, ts=None):
            self.title, self.text, self.published_ts = title, text, ts

    niche = Item('SAP NetWeaver patch', 'sap enterprise vulnerability CVE-2026-1000')
    mass = Item('Windows update breaks printing', 'windows update patch bug affects PCs')
    ranked = pf.rank_candidates([niche, mass], now_ts=1000.0, jitter=0)
    assert ranked[0] is mass


def test_ranking_prefers_fresh():
    class Item:
        def __init__(self, title, ts):
            self.title, self.text, self.published_ts = title, 'vulnerability', ts

    now = 1_000_000.0
    fresh = Item('Уязвимость в Chrome', now - 3600)
    stale = Item('Уязвимость в Chrome', now - 96 * 3600)
    ranked = pf.rank_candidates([stale, fresh], now_ts=now, jitter=0)
    assert ranked[0] is fresh


def test_prompt_is_security_not_vpn():
    prompt = pf.build_prompt('Заголовок', 'текст', 'BleepingComputer')
    assert 'VPN' not in prompt.upper()
    assert 'Защита Без Кода' in prompt
    assert 'parse_mode=HTML' in prompt
    assert 'придуманный номер' in prompt
