import os
import logging
import requests
import json
import asyncio
import pymysql
from flask import Flask, request, abort
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# ==================== НАСТРОЙКИ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ====================
BOT_TOKEN = os.environ['BOT_TOKEN']
CHAT_ID = int(os.environ['CHAT_ID'])  # ID чата менеджера

# Данные для подключения к твоей MySQL БД (airone)
MYSQL_HOST = os.environ['MYSQL_HOST']
MYSQL_USER = os.environ['MYSQL_USER']
MYSQL_PASS = os.environ['MYSQL_PASS']
MYSQL_DB = os.environ['MYSQL_DB']

# URL сервиса на Render (для webhook)
SERVICE_NAME = os.environ.get('RENDER_SERVICE_NAME')
if not SERVICE_NAME:
    raise ValueError("RENDER_SERVICE_NAME не найден")
RENDER_URL = f"https://{SERVICE_NAME}.onrender.com"

app = Flask(__name__)

# Настройка бота
application = Application.builder().token(BOT_TOKEN) \
    .read_timeout(30).write_timeout(30).connect_timeout(30).pool_timeout(30).build()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Словарь: order_id -> True (ожидаем ответа менеджера)
waiting_for_response = {}

# Асинхронный цикл
loop = asyncio.get_event_loop()

# Подключение к MySQL
def get_db_connection():
    return pymysql.connect(
        host=MYSQL_HOST,
        user=MYSQL_USER,
        password=MYSQL_PASS,
        database=MYSQL_DB,
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor
    )

# Инициализация и установка webhook при старте
async def init_bot():
    await application.initialize()
    await application.start()
    webhook_url = f"{RENDER_URL}/{BOT_TOKEN}"
    success = await application.bot.set_webhook(url=webhook_url)
    if success:
        logger.info(f"Webhook установлен: {webhook_url}")
    else:
        logger.error("Не удалось установить webhook")

loop.run_until_complete(init_bot())

# ==================== НОВЫЙ ЗАКАЗ ОТ САЙТА ====================
@app.route('/new_order', methods=['POST'])
def new_order_webhook():
    data = request.get_json(force=True)

    order_id = data.get('id')
    name = data.get('name', 'Не указано')
    phone = data.get('phone', 'Не указано')
    email = data.get('email', 'Не указано')
    total = data.get('total', 0)
    products = data.get('products', [])

    if not order_id:
        return 'No order_id', 400

    # Формируем список товаров
    products_text = "\n".join([
        f"• {item['name']} — {item['quantity']} шт. × {item['price']} ₽"
        for item in products
    ]) or "Товары не указаны"

    message_text = (
        f"🛒 *Новый заказ #{order_id}*\n\n"
        f"👤 Имя: {name}\n"
        f"📞 Телефон: {phone}\n"
        f"✉️ Email: {email}\n"
        f"💰 Сумма: {total} ₽\n\n"
        f"📦 Товары:\n{products_text}"
    )

    # Кнопка "Ответить"
    keyboard = [[InlineKeyboardButton("📩 Ответить на заказ", callback_data=f"reply_{order_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Отправляем менеджеру
    loop.run_until_complete(
        application.bot.send_message(
            chat_id=CHAT_ID,
            text=message_text,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    )

    return 'OK', 200

# ==================== ОБРАБОТКА КНОПКИ "ОТВЕТИТЬ" ====================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith('reply_'):
        order_id = int(query.data.split('_')[1])
        waiting_for_response[order_id] = True

        await query.edit_message_text(
            text=query.message.text + "\n\n✏️ Напишите ответ покупателю (текст или фото):",
            parse_mode='Markdown'
        )

# ==================== ОБРАБОТКА СООБЩЕНИЯ ОТ МЕНЕДЖЕРА ====================
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id != CHAT_ID:
        return

    # Ищем активный заказ
    active_order_id = next((oid for oid in waiting_for_response if waiting_for_response.get(oid)), None)
    if not active_order_id:
        await update.message.reply_text("❌ Нет активного заказа для ответа.")
        return

    text = update.message.caption or update.message.text or ""
    photo_url = None

    if update.message.photo:
        file = await update.message.photo[-1].get_file()
        photo_url = file.file_path
        text = (text + "\n\n" if text else "") + f"📷 Фото от менеджера:\n{photo_url}"

    note = f"Ответ от менеджера:\n\n{text.strip()}" if text.strip() else f"📷 Фото от менеджера:\n{photo_url}"

    # Сохраняем в БД
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("UPDATE orders SET note = %s WHERE id = %s", (note, active_order_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Ошибка БД: {e}")
        await update.message.reply_text("❌ Ошибка сохранения в базу данных.")
        return

    await update.message.reply_text(f"✅ Ответ сохранён в заказ #{active_order_id}")
    del waiting_for_response[active_order_id]

# Регистрация хендлеров
application.add_handler(CallbackQueryHandler(button_handler))
application.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.CAPTION, message_handler))

# ==================== WEBHOOK ОТ TELEGRAM ====================
@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def telegram_webhook():
    update_json = request.get_json(force=True)
    if not update_json:
        abort(400)
    update = Update.de_json(update_json, application.bot)
    loop.run_until_complete(application.process_update(update))
    return 'OK', 200

# ==================== ЗАПУСК ====================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
