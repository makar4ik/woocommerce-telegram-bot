import os
import logging
import requests
import json
import asyncio
from flask import Flask, request, abort, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# Настройки
BOT_TOKEN = os.environ['BOT_TOKEN']
CHAT_ID = int(os.environ['CHAT_ID'])
WC_URL = os.environ['WC_URL'].rstrip('/')
WC_KEY = os.environ['WC_CONSUMER_KEY']
WC_SECRET = os.environ['WC_CONSUMER_SECRET']

SERVICE_NAME = os.environ.get('RENDER_SERVICE_NAME')
if not SERVICE_NAME:
    raise ValueError("RENDER_SERVICE_NAME не найден")
RENDER_URL = f"https://{SERVICE_NAME}.onrender.com"

app = Flask(__name__)
application = Application.builder().token(BOT_TOKEN).build()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

waiting_for_response = {}
webhook_set = False

# Глобальный event loop
loop = asyncio.get_event_loop()

async def init_app():
    await application.initialize()
    await application.start()
    webhook_url = f"{RENDER_URL}/{BOT_TOKEN}"
    await application.bot.set_webhook(url=webhook_url)
    logger.info(f"Application initialized and webhook set to {webhook_url}")

# Инициализация при старте
loop.run_until_complete(init_app())

@app.route('/wc_webhook', methods=['POST'])
def wc_webhook():
    try:
        raw_data = request.data.decode('utf-8')
        if not raw_data:
            abort(400)
        data = json.loads(raw_data)
    except json.JSONDecodeError as e:
        logger.error(f"JSON error: {e}")
        abort(400)
    
    order_id = data.get('id')
    if not order_id:
        return jsonify(success=True), 200
    
    total = data.get('total', '0')
    currency = data.get('currency', '')
    customer = f"{data['billing'].get('first_name', '')} {data['billing'].get('last_name', '')}".strip() or "Не указано"
    
    items = "\n".join([
        f"• {item.get('name', 'Товар')} × {item.get('quantity', 1)} = {item.get('subtotal', '0')} {currency}"
        for item in data.get('line_items', [])
    ]) or "Нет товаров"
    
    message_text = f"Новый заказ #{order_id}\n\nКлиент: {customer}\n\nТовары:\n{items}\n\nИтого: {total} {currency}"
    
    keyboard = [[InlineKeyboardButton("Отправить информацию покупателю", callback_data=f"send_{order_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    loop.run_until_complete(
        application.bot.send_message(chat_id=CHAT_ID, text=message_text, reply_markup=reply_markup)
    )
    return jsonify(success=True), 200

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data.startswith('send_'):
        order_id = query.data.split('_', 1)[1]
        waiting_for_response[order_id] = True
        await query.edit_message_text(text=f"Заказ #{order_id} — отправьте текст и/или фото для покупателя:")

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
        text = (text + "\n\n" if text else "") + f"Фото от менеджера:\n{photo_url}"
    
    note = f"Информация для покупателя:\n\n{text.strip()}" if text.strip() else f"Фото от менеджера:\n{photo_url}"
    
    url = f"{WC_URL}/wp-json/wc/v3/orders/{active_order_id}"
    auth = (WC_KEY, WC_SECRET)
    payload = {"customer_note": note}
    response = requests.post(url, auth=auth, json=payload)
    
    if response.status_code == 200:
        await update.message.reply_text(f"✅ Информация отправлена в заказ #{active_order_id}")
        del waiting_for_response[active_order_id]
    else:
        await update.message.reply_text(f"❌ Ошибка: {response.status_code} — {response.text}")

application.add_handler(CallbackQueryHandler(button_handler))
application.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.CAPTION, message_handler))

@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def telegram_webhook():
    update_json = request.get_json(force=True)
    if not update_json:
        abort(400)
    update = Update.de_json(update_json, application.bot)
    loop.run_until_complete(application.process_update(update))
    return 'OK', 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
