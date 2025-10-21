# olx_monitor_final_v2.py
"""
OLX Monitor — финальная стабильная версия для:
  - город: Актау (aktau_5633)
  - категория: Электроника -> Игры и игровые приставки
Команды в боте: /start /stop /status
Сохраняет уже отправлённые ссылки в seen_links.json, чтобы не спамить.
"""
import os
import json
import re
import asyncio
from datetime import datetime
from typing import List, Tuple, Optional

import aiohttp
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ========== НАСТРОЙКИ (не меняй, если не нужно) ==========
BOT_TOKEN = "8380564038:AAGfn2ULRPSSMRZzS3nudXOZjPrleRo6xK0"
CHAT_ID = 7238085445
CHECK_INTERVAL = 60  # seconds between checks
SEEN_FILE = "seen_links.json"

# Fixed category+city from your request
CATEGORY_PATH = "elektronika/igry-i-igrovye-pristavki"
CITY_SLUG = "aktau_5633"
OLX_URL = f"https://www.olx.kz/{CATEGORY_PATH}/{CITY_SLUG}/?search%5Border%5D=created_at%3Adesc"
# =========================================================

# runtime state
monitoring = False
job_instance = None
seen_links = set()


# ---------- Helpers: load/save seen ----------
def load_seen() -> None:
    global seen_links
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                seen_links = set(data)
            else:
                seen_links = set()
            print(f"[load_seen] загружено {len(seen_links)} ссылок из {SEEN_FILE}")
        except Exception as e:
            print("[load_seen] ошибка загрузки seen file:", e)
            seen_links = set()
    else:
        seen_links = set()
        print("[load_seen] файл не найден, начнём с пустого набора.")


def save_seen() -> None:
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(list(seen_links), f, ensure_ascii=False, indent=2)
        # print("[save_seen] сохранено", len(seen_links))
    except Exception as e:
        print("[save_seen] ошибка сохранения seen file:", e)


# ---------- Fetch & parse OLX page ----------
async def fetch_page_text(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    try:
        async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=25) as resp:
            if resp.status != 200:
                print(f"[fetch_page_text] HTTP {resp.status} for {url}")
                return None
            return await resp.text()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print("[fetch_page_text] Ошибка запроса:", e)
        return None


def normalize_link(raw: str) -> str:
    """Нормализовать ссылку — убрать якоря и utm-метки"""
    link = raw.split("#")[0]
    link = re.sub(r"\?.*$", "", link)
    return link


def extract_ad_id_from_link(link: str) -> str:
    """
    Попробовать получить уникальный id из ссылки (если есть), иначе вернуть сам link.
    OLX ссылки часто содержат '-IDxxxxx' или похожие.
    """
    m = re.search(r'-(ID[a-zA-Z0-9_-]+)\.html', link)
    if m:
        return m.group(1)
    # иногда id только как число/слово в конце
    m2 = re.search(r'/([^/]+)\.html$', link)
    if m2:
        return m2.group(1)
    return link


async def parse_links_from_html(html: str) -> List[Tuple[str, Optional[int]]]:
    """
    Возвращает список (link, utime) где utime — unix timestamp (если найден), иначе None.
    Эта функция старается поддержать разные структуры OLX.
    """
    soup = BeautifulSoup(html, "html.parser")

    results: List[Tuple[str, Optional[int]]] = []

    # 1) Новый OLX: карточки с data-cy="l-card"
    cards = soup.select('div[data-cy="l-card"]')
    if cards:
        for card in cards:
            # find link inside
            a = card.select_one("a[href*='/d/obyavlenie/'], a[href*='/obyavlenie/']")
            if not a:
                continue
            href = a.get("href")
            if not href:
                continue
            if href.startswith("/"):
                href = "https://www.olx.kz" + href
            href = normalize_link(href)

            # time
            utime = None
            # OLX иногда кладёт data-utime на карточку
            if card.has_attr("data-utime"):
                try:
                    utime = int(card["data-utime"])
                except:
                    utime = None
            else:
                # иногда есть <span data-testid="ad-date"> с текстом, но парсить текст ненадёжно
                utime = None

            results.append((href, utime))

    else:
        # 2) fallback: искать все ссылки с /d/obyavlenie/
        anchors = soup.find_all("a", href=re.compile(r"/d/obyavlenie/|/obyavlenie/"))
        for a in anchors:
            href = a.get("href")
            if not href:
                continue
            if href.startswith("/"):
                href = "https://www.olx.kz" + href
            href = normalize_link(href)
            results.append((href, None))

    # dedupe preserving order
    seen = set()
    ordered = []
    for link, ut in results:
        if link not in seen:
            seen.add(link)
            ordered.append((link, ut))
    return ordered


async def fetch_current_listings() -> List[Tuple[str, Optional[int]]]:
    """Возвращает список (link, utime) текущих объявлений на странице"""
    async with aiohttp.ClientSession() as session:
        html = await fetch_page_text(session, OLX_URL)
        if not html:
            return []
        parsed = await parse_links_from_html(html)
        return parsed


# ---------- Job: monitoring ----------
async def monitor_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Проверяет страницу, отправляет новые объявления.
    Работает под job_queue (telegram.ext).
    """
    global seen_links
    try:
        listings = await fetch_current_listings()
        if not listings:
            print(f"[{datetime.utcnow().isoformat()}] Мониторинг: не получили список.")
            return

        # Отбираем новые ссылки (которые нет в seen_links)
        new_items = []
        for link, utime in listings:
            key = extract_ad_id_from_link(link)
            # use key primarily, but if key equals link (no ID extracted), fall back to link
            identifier = key or link
            if identifier not in seen_links:
                new_items.append((identifier, link, utime))

        if not new_items:
            print(f"[{datetime.utcnow().isoformat()}] Проверено — новых нет.")
            return

        # Отправляем новые (сначала старые из нового блока)
        for identifier, link, utime in reversed(new_items):
            text = f"🆕 Новое объявление:\n{link}"
            # добавим время публикации в сообщение, если есть
            if utime:
                try:
                    dt = datetime.utcfromtimestamp(int(utime)).strftime("%Y-%m-%d %H:%M:%S UTC")
                    text = f"🆕 Новое объявление ({dt}):\n{link}"
                except:
                    pass
            try:
                await context.bot.send_message(chat_id=context.application.bot_data.get("target_chat", CHAT_ID), text=text)
                print("[monitor_job] Sent:", link)
            except Exception as e:
                print("[monitor_job] Ошибка отправки:", e)

        # add to seen and persist
        for identifier, link, ut in new_items:
            seen_links.add(identifier)
        save_seen()

    except Exception as e:
        print("[monitor_job] unexpected error:", e)


# ---------- Command handlers ----------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitoring
    chat_id = update.effective_chat.id
    # save user's chat id to bot_data so job can use it
    context.application.bot_data["target_chat"] = chat_id

    if monitoring:
        await update.message.reply_text("⚠️ Мониторинг уже запущен.")
        return

    # load previous seen set
    load_seen()

    # Initialization: mark current items as seen (do NOT send)
    await update.message.reply_text("🔁 Инициализация — запоминаю текущее состояние объявлений, старые не будут присылаться.")
    current = await fetch_current_listings()
    init_count = 0
    for link, utime in current:
        identifier = extract_ad_id_from_link(link) or link
        if identifier not in seen_links:
            seen_links.add(identifier)
            init_count += 1
    save_seen()
    await update.message.reply_text(f"✅ Пропущено {init_count} существующих объявлений. Мониторинг запускается — буду присылать только новые.")

    # schedule repeating job
    job_queue = context.application.job_queue
    # remove existing jobs if any
    for j in job_queue.get_jobs_by_name("olx_monitor"):
        j.schedule_removal()
    job_queue.run_repeating(monitor_job, interval=CHECK_INTERVAL, first=10, name="olx_monitor")

    monitoring = True
    print("[start_handler] monitoring started for chat", chat_id)


async def stop_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitoring
    job_queue = context.application.job_queue
    jobs = job_queue.get_jobs_by_name("olx_monitor")
    if jobs:
        for j in jobs:
            j.schedule_removal()
    monitoring = False
    await update.message.reply_text("🛑 Мониторинг остановлен.")
    print("[stop_handler] monitoring stopped.")


async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Мониторинг: {'ВКЛ' if monitoring else 'ВЫКЛ'}\n"
        f"Город: Актау\nКатегория: Игры и приставки\nПамять ссылок: {len(seen_links)}"
    )


# ---------- Entrypoint ----------
def main():
    # fix for environments where event loop already running (IDLE etc.)
    try:
        import nest_asyncio
        nest_asyncio.apply()
    except Exception:
        pass

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("stop", stop_handler))
    app.add_handler(CommandHandler("status", status_handler))

    print("✅ OLX Monitor ready. Send /start in bot to initialize.")
    # Blocking call — will manage asyncio loop internally
    app.run_polling()


if __name__ == "__main__":
    main()
