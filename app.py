import os
import logging
import requests
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
RENDER_URL = os.environ.get('RENDER_EXTERNAL_URL') or f"https://{os.environ['RENDER_SERVICE_NAME']}.onrender.com"  # Автоопределение URL

app = Flask(__name__)
application = Application.builder().token(BOT_TOKEN).build()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Словарь для ожидающих ответов (поддержка нескольких заказов)
waiting_for_response = {}

# Установка webhook один раз при первом запросе (альтернатива удалённому before_first_request)
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

# Webhook от WooCommerce (новый заказ)
@app.route('/wc_webhook', methods=['POST'])
def wc_webhook():
    setup_webhook()  # Проверяем и устанавливаем webhook при первом трафике
    
    data = request.get_json()
    if not data:
        abort(400)
    
    order_id = data.get('id')
    if not order_id:
        return jsonify(success=True)
    
    total = data['total']
    currency = data['currency']
    customer = f"{data['billing'].get('first_name', '')} {data['billing'].get('last_name', '')}".strip() or "Не указано"
    
    items = "\n".join([f"• {item['name']} × {item['quantity']} = {item['subtotal']} {currency}"
                      for item in data.get('line_items', [])])
    
    message_text = f"Новый заказ #{order_id}\n\nКлиент: {customer}\n\nТовары:\n{items}\n\nИтого: {total} {currency}"
    
    keyboard = [[InlineKeyboardButton("Отправить информацию покупателю", callback_data=f"send_{order_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    asyncio.run(application.bot.send_message(chat_id=CHAT_ID, text=message_text, reply_markup=reply_markup))
    return jsonify(success=True)

# Обработка нажатия кнопки
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith('send_'):
        order_id = query.data.split('_', 1)[1]
        waiting_for_response[order_id] = True
        await query.edit_message_text(text=f"Заказ #{order_id} — жду ваш ответ (текст и/или фото) для покупателя:")

# Обработка вашего ответа
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id != CHAT_ID:
        return
    
    active_order_id = None
    for oid in list(waiting_for_response):
        if waiting_for_response.get(oid):
            active_order_id = oid
            break
    
    if not active_order_id:
        await update.message.reply_text("❌ Нет активного заказа для ответа.")
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
    
    if response.status_code in (200, 201):
        await update.message.reply_text(f"✅ Информация отправлена в заказ #{active_order_id}")
        del waiting_for_response[active_order_id]
    else:
        await update.message.reply_text(f"❌ Ошибка: {response.status_code} {response.text}")

# Регистрация хендлеров
application.add_handler(CallbackQueryHandler(button_handler))
application.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.CAPTION, message_handler))

# Webhook для Telegram
@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def telegram_webhook():
    setup_webhook()  # Устанавливаем при первом запросе от Telegram
    
    update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.run(application.process_update(update))
    return 'OK'

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
