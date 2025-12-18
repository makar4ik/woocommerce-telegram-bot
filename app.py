import os
import logging
import requests
import asyncio
import json
from flask import Flask, request, abort, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# Настройки
BOT_TOKEN = os.environ['BOT_TOKEN']
CHAT_ID = int(os.environ['CHAT_ID'])
WC_URL = os.environ['WC_URL'].rstrip('/')
WC_KEY = os.environ['WC_CONSUMER_KEY']
WC_SECRET = os.environ['WC_CONSUMER_SECRET']
RENDER_URL = f"https://{os.environ['RENDER_INSTANCE_NAME'] or os.environ['RENDER_SERVICE_NAME']}.onrender.com"  # Авто URL

app = Flask(__name__)
application = Application.builder().token(BOT_TOKEN).build()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

waiting_for_response = {}
webhook_set = False

def setup_webhook():
    global webhook_set
    if webhook_set:
        return
    webhook_url = f"{RENDER_URL}/{BOT_TOKEN}"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(application.bot.set_webhook(url=webhook_url))
    loop.close()
    webhook_set = True
    logger.info(f"Webhook установлен: {webhook_url}")

# Универсальная обработка WooCommerce webhook
@app.route('/wc_webhook', methods=['POST'])
def wc_webhook():
    setup_webhook()
    
    # Принимаем любой Content-Type и парсим JSON вручную
    try:
        if request.content_type and 'application/json' in request.content_type:
            data = request.get_json(force=True)
        else:
            # Для form-urlencoded или других — берём raw body и парсим как JSON
            data = json.loads(request.data.decode('utf-8'))
    except json.JSONDecodeError:
        logger.error("Не удалось распарсить JSON из webhook")
        abort(400)
    
    if not data or not data.get('id'):
        return jsonify(success=True)
    
    order_id = data['id']
    total = data.get('total', '0')
    currency = data.get('currency', 'USD')
    customer = f"{data['billing'].get('first_name', '')} {data['billing'].get('last_name', '')}".strip() or "Не указано"
    
    items = "\n".join([f"• {item.get('name', 'Товар')} × {item.get('quantity', 1)} = {item.get('subtotal', '0')} {currency}"
                      for item in data.get('line_items', [])])
    
    message_text = f"Новый заказ #{order_id}\n\nКлиент: {customer}\n\nТовары:\n{items or 'Нет товаров'}\n\nИтого: {total} {currency}"
    
    keyboard = [[InlineKeyboardButton("Отправить информацию покупателю", callback_data=f"send_{order_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    asyncio.run(application.bot.send_message(chat_id=CHAT_ID, text=message_text, reply_markup=reply_markup))
    return jsonify(success=True), 200

# Обработчики Telegram (без изменений)
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data.startswith('send_'):
        order_id = query.data.split('_', 1)[1]
        waiting_for_response[order_id] = True
        await query.edit_message_text(text=f"Заказ #{order_id} — жду ваш ответ (текст и/или фото):")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id != CHAT_ID:
        return
    
    active_order_id = next((oid for oid in waiting_for_response if waiting_for_response.get(oid)), None)
    if not active_order_id:
        await update.message.reply_text("❌ Нет активного заказа.")
        return
    
    text = update.message.caption or update.message.text or ""
    photo_url = None
    if update.message.photo:
        file = await update.message.photo[-1].get_file()
        photo_url = file.file_path
        text = (text + "\n\n" if text else "") + f"Фото:\n{photo_url}"
    
    note = f"Информация для покупателя:\n\n{text.strip()}" if text.strip() else f"Фото от менеджера:\n{photo_url}"
    
    url = f"{WC_URL}/wp-json/wc/v3/orders/{active_order_id}?consumer_key={WC_KEY}&consumer_secret={WC_SECRET}"
    payload = {"customer_note": note}
    response = requests.put(url, json=payload)  # PUT вместо POST для обновления
    
    if response.status_code == 200:
        await update.message.reply_text(f"✅ Отправлено в заказ #{active_order_id}")
        del waiting_for_response[active_order_id]
    else:
        await update.message.reply_text(f"❌ Ошибка WooCommerce: {response.status_code}")

application.add_handler(CallbackQueryHandler(button_handler))
application.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.CAPTION, message_handler))

@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def telegram_webhook():
    setup_webhook()
    update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.run(application.process_update(update))
    return 'OK'

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
