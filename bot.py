import os
import logging
import requests
from flask import Flask, request, abort, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# Настройки из env vars
BOT_TOKEN = os.environ['BOT_TOKEN']
CHAT_ID = int(os.environ['CHAT_ID'])
WC_URL = os.environ['WC_URL'].rstrip('/')  # https://shsw-realty.store
WC_KEY = os.environ['WC_CONSUMER_KEY']
WC_SECRET = os.environ['WC_CONSUMER_SECRET']

app = Flask(__name__)
application = Application.builder().token(BOT_TOKEN).build()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Словарь: order_id → True (ждём ответ для этого заказа)
waiting_for_response = {}

# Webhook от WooCommerce — новый заказ
@app.route('/wc_webhook', methods=['POST'])
def wc_webhook():
    if request.headers.get('Content-Type') != 'application/json':
        abort(400)
    
    data = request.get_json()
    if not data or data.get('arg') != 'order.created':  # иногда приходит тестовый ping
        return jsonify(success=True)
    
    order_id = data['id']
    total = data['total']
    currency = data['currency']
    customer = f"{data['billing']['first_name']} {data['billing']['last_name']}".strip() or "Не указано"
    
    items = "\n".join([f"• {item['name']} × {item['quantity']} = {item['subtotal']} {currency}" 
                      for item in data['line_items']])
    
    message_text = f"Новый заказ #{order_id}\n\nКлиент: {customer}\n\nТовары:\n{items}\n\nИтого: {total} {currency}"
    
    keyboard = [[InlineKeyboardButton("Отправить информацию покупателю", 
                                      callback_data=f"send_{order_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    application.bot.send_message(chat_id=CHAT_ID, text=message_text, reply_markup=reply_markup)
    return jsonify(success=True)

# Нажатие кнопки
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith('send_'):
        order_id = query.data.split('_', 1)[1]
        waiting_for_response[order_id] = True
        await query.edit_message_text(
            text=f"Заказ #{order_id} — жду ваш ответ (текст и/или фото) для покупателя:"
        )

# Ваш ответ текстом или фото
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id != CHAT_ID:
        return
    
    # Ищем заказ, для которого ждём ответ
    active_order_id = None
    for oid in list(waiting_for_response):
        if waiting_for_response[oid]:
            active_order_id = oid
            break
    
    if not active_order_id:
        await update.message.reply_text("❌ Нет активного заказа, на который нужно ответить.")
        return
    
    # Формируем текст заметки
    text = update.message.caption or update.message.text or ""
    photo_url = None
    
    if update.message.photo:
        file = await update.message.photo[-1].get_file()
        photo_url = file.file_path
        text = (text + "\n\n" if text else "") + f"Фото от менеджера:\n{photo_url}"
    
    note = f"Информация для покупателя:\n\n{text}" if text else "Фото от менеджера:\n{photo_url}"
    
    # Обновляем заказ в WooCommerce
    url = f"{WC_URL}/wp-json/wc/v3/orders/{active_order_id}"
    auth = (WC_KEY, WC_SECRET)
    payload = {"customer_note": note}
    
    response = requests.post(url, auth=auth, json=payload)
    
    if response.status_code in (200, 201):
        await update.message.reply_text(f"✅ Информация успешно отправлена в заказ #{active_order_id}")
        del waiting_for_response[active_order_id]
    else:
        await update.message.reply_text(f"❌ Ошибка отправки в WooCommerce: {response.status_code} {response.text}")

# Регистрация хендлеров
application.add_handler(CallbackQueryHandler(button_handler))
application.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.CAPTION, message_handler))

# Webhook для Telegram
@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def telegram_webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    application.run_polling = lambda: None  # отключить polling
    asyncio.run(application.process_update(update))
    return 'OK'

# Установка webhook при старте
async def set_webhook():
    webhook_url = f"https://{os.environ['RENDER_EXTERNAL_URL']}/{BOT_TOKEN}"
    await application.bot.set_webhook(url=webhook_url)

@app.before_first_request
def startup():
    import asyncio
    asyncio.create_task(set_webhook())

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
