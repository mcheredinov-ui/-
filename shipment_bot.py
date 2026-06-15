#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
БОТ ОТГРУЗОК ДПП Prodex.
Раз в час (06:00–21:00 МСК) собирает сделки основной воронки, созданные сегодня,
в нужных стадиях, делит по полю «Канал 1» на Вход/Исход, строит ТОП-10 менеджеров
по сумме отгрузки (OPPORTUNITY) и публикует оба рейтинга в Telegram.
В 21:00 дополнительно выводит итог за день по каждому направлению.

Секреты — через переменные окружения:
    B24_WEBHOOK  — входящий вебхук Битрикс24
    TG_TOKEN     — токен Telegram-бота
    TG_CHAT_ID   — id чата/канала для публикации
"""

import os
import sys
import requests
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ======================================================================
# КОНФИГ  (значения уже определены под портал prodex)
# ======================================================================

WEBHOOK  = os.environ["B24_WEBHOOK"].rstrip("/")
TG_TOKEN = os.environ["TG_TOKEN"]
TG_CHAT  = os.environ["TG_CHAT_ID"]

CATEGORY_ID = 0                      # основная воронка
FIELD_CHANNEL1 = "UF_CRM_1607325086" # поле «Канал 1» (строковое)

# Классификация по ТЕКСТУ значения «Канал 1» (нормализуем strip+lower).
INCOME_CHANNELS = {                  # Вход
    "газеты",
    "радио",            # покрывает «Радио» и «РАДИО»
    "тв",
    "сайт",             # digital
    "почтовая рассылка",
}
OUTBOUND_CHANNELS = {                # Исход
    "телефон",
}

# STATUS_ID нужных стадий основной воронки:
STAGE_IDS = [
    "NEW",   # Проверка контроллера на ошибки
    "1",     # Откат
    "30",    # Отсутствие товара
    "49",    # Предзаказ не выкуплен
    "3",     # Комплектация
    "31",    # На отгрузку
    "4",     # Передан в доставку
    "5",     # Доставляется
]

MSK = ZoneInfo("Europe/Moscow")
TOP_N = 10

SESSION = requests.Session()
SESSION.trust_env = False  # игнорируем системный/корпоративный прокси

# ======================================================================
# БИТРИКС24
# ======================================================================

def b24(method, params=None, timeout=120, retries=3):
    """Вызов REST Битрикс24 с увеличенным таймаутом и повтором при сбое сети."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = SESSION.post(f"{WEBHOOK}/{method}.json", json=params or {}, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                raise RuntimeError(f"{method}: {data.get('error')} {data.get('error_description')}")
            return data
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = e
            print(f"[ПОВТОР {attempt}/{retries}] {method}: {e}", file=sys.stderr)
            time.sleep(3)
    raise last_err


def b24_list(method, params):
    """Постраничная выгрузка (start/next). Безопасна: сегодняшних сделок немного."""
    items, start = [], 0
    while True:
        p = dict(params); p["start"] = start
        data = b24(method, p)
        items.extend(data.get("result", []))
        nxt = data.get("next")
        if not nxt:
            break
        start = nxt
    return items


def fetch_deals(day_start, day_end):
    """Сделки основной воронки, созданные сегодня, в нужных стадиях."""
    return b24_list("crm.deal.list", {
        "filter": {
            "CATEGORY_ID":   CATEGORY_ID,
            ">=DATE_CREATE": day_start.isoformat(),
            "<DATE_CREATE":  day_end.isoformat(),
            "@STAGE_ID":     STAGE_IDS,
        },
        "select": ["ID", "OPPORTUNITY", "ASSIGNED_BY_ID", FIELD_CHANNEL1],
    })


def get_user_names(ids):
    """ФИО ответственных только для нужных ID (ТОП-10 каждого направления)."""
    names = {}
    for uid in ids:
        res = b24("user.get", {"ID": uid}).get("result", [])
        if res:
            u = res[0]
            fio = f'{u.get("LAST_NAME","")} {u.get("NAME","")}'.strip()
            names[uid] = fio or f"ID {uid}"
        else:
            names[uid] = f"ID {uid}"
    return names


# ======================================================================
# АГРЕГАЦИЯ
# ======================================================================

def classify(channel_text):
    """Вернёт 'in' / 'out' / None по тексту поля «Канал 1»."""
    key = (channel_text or "").strip().lower()
    if key in INCOME_CHANNELS:
        return "in"
    if key in OUTBOUND_CHANNELS:
        return "out"
    return None


def aggregate(deals):
    """Сумма OPPORTUNITY по менеджеру отдельно для Вход и Исход."""
    income, outbound = {}, {}
    for d in deals:
        opp = float(d.get("OPPORTUNITY") or 0)
        mgr = str(d.get("ASSIGNED_BY_ID") or "")
        if not mgr:
            continue
        kind = classify(d.get(FIELD_CHANNEL1))
        if kind == "in":
            income[mgr] = income.get(mgr, 0.0) + opp
        elif kind == "out":
            outbound[mgr] = outbound.get(mgr, 0.0) + opp
    return income, outbound


def top(d, n=TOP_N):
    return sorted(d.items(), key=lambda x: -x[1])[:n]


# ======================================================================
# ФОРМАТ И ОТПРАВКА
# ======================================================================

def rub(x):
    return f"{int(round(x)):,}".replace(",", " ") + " ₽"


def render_block(title, ranking, names):
    lines = [f"<b>{title}</b>"]
    if not ranking:
        lines.append("— нет данных")
        return "\n".join(lines)
    for i, (mgr, amount) in enumerate(ranking, 1):
        lines.append(f"{i}. {names.get(mgr, 'ID ' + mgr)} — {rub(amount)}")
    return "\n".join(lines)


def build_message(now, income, outbound, is_final):
    inc_top, out_top = top(income), top(outbound)
    names = get_user_names([m for m, _ in inc_top] + [m for m, _ in out_top])

    parts = [
        f"🏆 <b>Отгрузка — нарастающим итогом</b>\nна {now:%H:%M} ({now:%d.%m})",
        "",
        render_block("📥 ВХОД — ТОП-10", inc_top, names),
        "",
        render_block("📤 ИСХОД — ТОП-10", out_top, names),
    ]
    if is_final:
        parts += [
            "",
            "— — —",
            "<b>ИТОГ ЗА ДЕНЬ</b>",
            f"Вход: {rub(sum(income.values()))}",
            f"Исход: {rub(sum(outbound.values()))}",
        ]
    return "\n".join(parts)


def send_telegram(text):
    r = SESSION.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
        timeout=30,
    )
    r.raise_for_status()


# ======================================================================
# MAIN
# ======================================================================

def main():
    now = datetime.now(MSK)
    # Тестовый запуск: python shipment_bot.py test  — публикует сразу, минуя окно времени.
    test_mode = "test" in sys.argv
    if not test_mode and not (6 <= now.hour <= 21):   # обычно публикуем только 06:00–21:00 МСК
        return
    is_final = (now.hour == 21) or ("final" in sys.argv)

    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end   = day_start + timedelta(days=1)

    deals = fetch_deals(day_start, day_end)
    income, outbound = aggregate(deals)
    send_telegram(build_message(now, income, outbound, is_final))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[ОШИБКА] {e}", file=sys.stderr)
        sys.exit(1)
