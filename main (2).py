# main.py — версия с постоянными уведомлениями при InStock
import asyncio
import hashlib
from bs4 import BeautifulSoup
import aiohttp
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ============================
# Конфигурация
# ============================
DB_PATH = "tracked_links.db"
TOKEN = '7940336601:AAG5pZuN1zMsow06w0uAjCxtgF_htrsFP9o'
CHECK_INTERVAL = 5  # каждые 5 секунд проверка и (при необходимости) уведомление

# ============================
# База данных
# ============================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS tracked_links
                 (chat_id INTEGER, url TEXT, title TEXT, PRIMARY KEY (chat_id, url))''')
    conn.commit()
    conn.close()

def add_link(chat_id, url, title):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO tracked_links VALUES (?, ?, ?)', (chat_id, url, title))
    conn.commit()
    conn.close()

def remove_link(chat_id, url):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM tracked_links WHERE chat_id = ? AND url = ?', (chat_id, url))
    conn.commit()
    conn.close()

def get_all_links():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT chat_id, url, title FROM tracked_links')
    rows = c.fetchall()
    conn.close()
    return rows

# ============================
# Парсинг сайта
# ============================
async def check_product(url: str):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=15) as resp:
                if resp.status != 200:
                    return "Ошибка", "—", False
                text = await resp.text()
                soup = BeautifulSoup(text, 'html.parser')

                title = soup.find("h1", class_="nom-name")
                title = title.get_text(strip=True) if title else "Без названия"

                price_meta = soup.find("meta", itemprop="price")
                price = (price_meta["content"] + " ₽") if price_meta and price_meta.get("content") else "—"

                avail_meta = soup.find("meta", itemprop="availability")
                in_stock = avail_meta and avail_meta.get("content") == "InStock"

                return title, price, in_stock

    except Exception as e:
        return "Ошибка загрузки", "—", False

# ============================
# Мониторинг — теперь шлёт уведомление КАЖДЫЙ РАЗ пока InStock
# ============================
ACTIVE_TASKS = {}  # (chat_id, url) → task

async def monitor_task(chat_id: int, url: str, bot):
    last_state = None  # None / True / False

    while (chat_id, url) in ACTIVE_TASKS:
        title, price, in_stock = await check_product(url)

        # Уведомляем только при InStock и каждый раз, пока он есть
        if in_stock:
            msg = f"in_stock В НАЛИЧИИ ПРЯМО СЕЙЧАС!\n\n{title}\nЦена: {price}\n{url}"
            keyboard = [
                [InlineKeyboardButton("Перейти к товару", url=url)],
                [InlineKeyboardButton("Остановить спам", callback_data=f"stop_{hashlib.md5(url.encode()).hexdigest()[:12]}")]
            ]
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=msg,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    disable_web_page_preview=True
                )
            except Exception as e:
                print(f"Не удалось отправить сообщение пользователю {chat_id}: {e}")

        # Если был в наличии, а стал нет — можно дополнительно оповестить (по желанию)
        # elif last_state is True and not in_stock:
        #     await bot.send_message(chat_id=chat_id, text=f"Товар закончился:\n{title}\n{url}")

        last_state = in_stock
        await asyncio.sleep(CHECK_INTERVAL)

# ============================
# Команды
# ============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Бот запущен!\n\n"
        "Теперь при появлении товара в наличии — буду писать КАЖДЫЕ 5 СЕКУНД, пока он есть.\n\n"
        "/add https://...\n/list\n/remove https://..."
    )

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Пример: /add https://rozavdohnoveniya.ru/roza-...")
        return

    url = context.args[0].strip()
    chat_id = update.effective_chat.id

    title, price, in_stock = await check_product(url)
    if title.startswith("Ошибка"):
        await update.message.reply_text(f"Не смог открыть страницу:\n{url}")
        return

    add_link(chat_id, url, title)

    key = (chat_id, url)
    if key not in ACTIVE_TASKS or ACTIVE_TASKS[key].done():
        ACTIVE_TASKS[key] = asyncio.create_task(monitor_task(chat_id, url, context.bot))

    status = "В НАЛИЧИИ" if in_stock else "нет в наличии"
    await update.message.reply_text(f"Начинаю отслеживать:\n{title}\nСейчас: {status}\nЦена: {price}")

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT url, title FROM tracked_links WHERE chat_id = ?', (chat_id,))
    rows = c.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("Нет отслеживаемых товаров.")
        return

    text = "Твои товары (спам включён пока в наличии):\n\n"
    for url, title in rows:
        _, _, in_stock = await check_product(url)
        status = "В НАЛИЧИИ" if in_stock else "нет"
        text += f"{status} {title}\n{url}\n\n"
    await update.message.reply_text(text)

async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    # Кнопка "Остановить спам"
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data.startswith("stop_"):
            uid = query.data[5:]
            for (cid, url), task in list(ACTIVE_TASKS.items()):
                if cid == chat_id and hashlib.md5(url.encode()).hexdigest()[:12] == uid:
                    task.cancel()
                    del ACTIVE_TASKS[(cid, url)]
                    remove_link(chat_id, url)
                    await query.edit_message_text(f"Спам остановлен и ссылка удалена:\n{url}")
                    return
        return

    # Удаление по команде
    if not context.args:
        await update.message.reply_text("Укажи ссылку: /remove https://...")
        return

    url = context.args[0].strip()
    key = (chat_id, url)
    if key in ACTIVE_TASKS:
        ACTIVE_TASKS[key].cancel()
        del ACTIVE_TASKS[key]
    remove_link(chat_id, url)
    await update.message.reply_text(f"Удалено из отслеживания:\n{url}")

# ============================
# Восстановление задач при старте
# ============================
async def restore_tasks(app):
    await asyncio.sleep(2)
    for chat_id, url, title in get_all_links():
        key = (chat_id, url)
        if key not in ACTIVE_TASKS or ACTIVE_TASKS[key].done():
            ACTIVE_TASKS[key] = asyncio.create_task(monitor_task(chat_id, url, app.bot))

# ============================
# Запуск
# ============================
def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("remove", remove))
    app.add_handler(CallbackQueryHandler(remove, pattern="^stop_"))

    app.job_queue.run_once(lambda ctx: asyncio.create_task(restore_tasks(app)), 1)

    print("Бот запущен — спам включён при наличии!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()