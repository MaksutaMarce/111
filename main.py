import logging
import sqlite3
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from threading import Thread

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler
)
from flask import Flask, request, jsonify, g, send_from_directory
from flask_cors import CORS
import jwt

# ========== КОНФИГУРАЦИЯ ==========
TOKEN = os.getenv("TOKEN", "8020809344:AAGeYoyT-dHCMmCwBPcGhMO1UWqe_Ot4VoY")
ADMIN_IDS = [int(id) for id in os.getenv("ADMIN_IDS", "6392591727").split(",")]
CHANNEL_ID = os.getenv("CHANNEL_ID", "-1003275553562")
DEPOSIT_FIXED = 500
MAX_PHOTOS = 10
BOT_USERNAME = "mizimarketbot"

SECRET_KEY = os.getenv("SECRET_KEY", "super-secret-key-change-me")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

STATIC_DIR = Path('/app/static/items')
STATIC_DIR.mkdir(parents=True, exist_ok=True)
DATABASE = '/app/data/apple_store.db'

# ========== FLASK API ==========
flask_app = Flask(__name__)
CORS(flask_app)

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@flask_app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def query_db(query, args=(), one=False):
    cur = get_db().execute(query, args)
    rv = cur.fetchall()
    cur.close()
    return (rv[0] if rv else None) if one else rv

def commit_db():
    get_db().commit()

def token_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization')
        if not token:
            return jsonify({'message': 'Требуется токен'}), 401
        try:
            token = token.split()[1]
            data = jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
            g.admin_id = data['sub']
        except:
            return jsonify({'message': 'Неверный токен'}), 401
        return f(*args, **kwargs)
    return decorated

@flask_app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.get_json()
    if not data or data.get('password') != ADMIN_PASSWORD:
        return jsonify({'message': 'Неверный пароль'}), 401
    token = jwt.encode({
        'sub': 'admin',
        'exp': datetime.utcnow() + timedelta(hours=24)
    }, SECRET_KEY, algorithm='HS256')
    return jsonify({'token': token})

@flask_app.route('/api/items')
def get_items():
    page = request.args.get('page', 1, type=int)
    limit = 20
    offset = (page - 1) * limit
    category = request.args.get('category')
    search = request.args.get('search')
    status = request.args.get('status', 'available')

    query = "SELECT * FROM lots WHERE status = ?"
    params = [status]
    if category:
        query += " AND category = ?"
        params.append(category)
    if search:
        query += " AND (name LIKE ? OR description LIKE ?)"
        params.extend([f'%{search}%', f'%{search}%'])

    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    items = [dict(row) for row in query_db(query, params)]

    count_query = "SELECT COUNT(*) as count FROM lots WHERE status = ?"
    count_params = [status]
    if category:
        count_query += " AND category = ?"
        count_params.append(category)
    if search:
        count_query += " AND (name LIKE ? OR description LIKE ?)"
        count_params.extend([f'%{search}%', f'%{search}%'])
    total = query_db(count_query, count_params, one=True)['count']

    return jsonify({
        'items': items,
        'total': total,
        'page': page,
        'pages': (total + limit - 1) // limit
    })

@flask_app.route('/api/items/<lot_id>')
def get_item(lot_id):
    item = query_db("SELECT * FROM lots WHERE lot_id = ?", [lot_id], one=True)
    if item is None:
        return jsonify({'error': 'Товар не найден'}), 404
    return jsonify(dict(item))

@flask_app.route('/api/admin/items', methods=['GET'])
@token_required
def admin_get_items():
    items = query_db("SELECT * FROM lots ORDER BY created_at DESC LIMIT 200")
    return jsonify([dict(row) for row in items])

@flask_app.route('/api/admin/items', methods=['POST'])
@token_required
def admin_create_item():
    data = request.get_json()
    if not all(k in data for k in ('name', 'price', 'category')):
        return jsonify({'error': 'Не все поля'}), 400
    db = get_db()
    cur = db.execute("SELECT value FROM counters WHERE name='lot_counter'")
    row = cur.fetchone()
    next_num = row[0] + 1
    db.execute("UPDATE counters SET value=? WHERE name='lot_counter'", (next_num,))
    db.commit()
    lot_id = f"MIZI-MARKET {next_num}"
    photos_str = ','.join(data.get('photos', []))
    db.execute('''INSERT INTO lots (lot_id, name, description, price, photos, category, status)
                  VALUES (?,?,?,?,?,?,?)''',
               (lot_id, data['name'], data.get('description',''), data['price'],
                photos_str, data['category'], 'available'))
    db.commit()
    return jsonify({'lot_id': lot_id, 'message': 'Создан'}), 201

@flask_app.route('/api/admin/items/<lot_id>', methods=['PUT'])
@token_required
def admin_update_item(lot_id):
    data = request.get_json()
    db = get_db()
    fields = []
    params = []
    for f in ['name', 'description', 'price', 'category', 'status']:
        if f in data:
            fields.append(f"{f}=?")
            params.append(data[f])
    if 'photos' in data:
        fields.append("photos=?")
        params.append(','.join(data['photos']))
    if not fields:
        return jsonify({'error': 'Нет полей'}), 400
    params.append(lot_id)
    db.execute(f"UPDATE lots SET {', '.join(fields)} WHERE lot_id=?", params)
    db.commit()
    return jsonify({'message': 'Обновлено'})

@flask_app.route('/api/admin/items/<lot_id>', methods=['DELETE'])
@token_required
def admin_delete_item(lot_id):
    db = get_db()
    db.execute("DELETE FROM lots WHERE lot_id=?", [lot_id])
    db.commit()
    return jsonify({'message': 'Удалено'})

@flask_app.route('/api/admin/upload', methods=['POST'])
@token_required
def upload_photo():
    if 'file' not in request.files:
        return jsonify({'error': 'Нет файла'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'Имя файла пустое'}), 400
    ext = os.path.splitext(file.filename)[1]
    filename = f"{uuid.uuid4()}{ext}"
    save_path = STATIC_DIR / filename
    file.save(str(save_path))
    return jsonify({'path': f"static/items/{filename}"}), 201

@flask_app.route('/static/items/<path:filename>')
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)

def run_flask():
    flask_app.run(host='0.0.0.0', port=5000, debug=False)

# ========== ЛОГИРОВАНИЕ ==========
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ========== КАТЕГОРИИ И СТАТУСЫ ==========
CATEGORIES = {
    'phones': '📱 Смартфоны',
    'laptops': '💻 Ноутбуки',
    'tablets': '📱 Планшеты',
    'watches': '⌚ Часы',
    'accessories': '🎧 Аксессуары',
    'audio': '🎵 Аудио'
}
CATEGORY_EMOJIS = {
    'phones': '📱',
    'laptops': '💻',
    'tablets': '📱',
    'watches': '⌚',
    'accessories': '🎧',
    'audio': '🎵'
}
STATUSES = {
    'available': {'text': '✅ АКТУАЛЬНО', 'emoji': '✅'},
    'reserved': {'text': '🔄 Забронирован', 'emoji': '🔄'},
    'sold': {'text': '❌ Продан', 'emoji': '❌'},
    'pending': {'text': '⏳ Ожидание', 'emoji': '⏳'},
    'approved': {'text': '✅ Подтвержден', 'emoji': '✅'},
    'rejected': {'text': '❌ Отклонен', 'emoji': '❌'}
}

# ========== СОСТОЯНИЯ РАЗГОВОРА ==========
(
    DEVICE_MODEL, DEVICE_CONDITION, DEVICE_DESCRIPTION,
    DEVICE_LOCATION, DEVICE_CONTACT, DEVICE_PHOTOS,
    LOT_NAME, LOT_PRICE, LOT_DESCRIPTION, LOT_PHOTOS,
    LOT_CATEGORY, LOT_EDIT_FIELD, SEARCH
) = range(13)

# ========== РАБОТА С БД (ОРИГИНАЛЬНЫЙ КЛАСС) ==========
class Database:
    def __init__(self):
        self.conn = sqlite3.connect(DATABASE, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.init_db()

    def init_db(self):
        cursor = self.conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER DEFAULT 0)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS lots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lot_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            price REAL NOT NULL,
            photos TEXT,
            category TEXT NOT NULL,
            status TEXT DEFAULT 'available',
            views INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            channel_message_id INTEGER
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lot_id TEXT, user_id INTEGER, user_name TEXT, user_username TEXT,
            screenshot TEXT, status TEXT DEFAULT 'pending',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(lot_id) REFERENCES lots(lot_id)
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS buyback_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, user_name TEXT, user_username TEXT,
            device_model TEXT NOT NULL, device_condition TEXT NOT NULL,
            description TEXT, photos TEXT, location TEXT,
            contact_info TEXT NOT NULL, status TEXT DEFAULT 'pending',
            estimated_price REAL, notes TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            last_name TEXT, is_admin BOOLEAN DEFAULT FALSE,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_active DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS views (
            user_id INTEGER, lot_id TEXT,
            viewed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id, lot_id)
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS favorites (
            user_id INTEGER, lot_id TEXT,
            added_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id, lot_id)
        )''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_lots_status ON lots(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_lots_category ON lots(category)")
        cursor.execute("INSERT OR IGNORE INTO counters (name, value) VALUES ('lot_counter', 0)")
        self.conn.commit()

    def get_next_lot_number(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT value FROM counters WHERE name = 'lot_counter'")
        result = cursor.fetchone()
        current = result[0] if result else 0
        next_number = current + 1
        cursor.execute("UPDATE counters SET value = ? WHERE name = 'lot_counter'", (next_number,))
        self.conn.commit()
        return next_number

    def execute(self, query, params=()):
        cursor = self.conn.cursor()
        cursor.execute(query, params)
        self.conn.commit()
        return cursor

    def fetchone(self, query, params=()):
        cursor = self.conn.cursor()
        cursor.execute(query, params)
        result = cursor.fetchone()
        return dict(result) if result else None

    def fetchall(self, query, params=()):
        cursor = self.conn.cursor()
        cursor.execute(query, params)
        results = cursor.fetchall()
        return [dict(row) for row in results]

    def close(self):
        self.conn.close()

db = Database()

def generate_lot_id():
    lot_number = db.get_next_lot_number()
    return f"MIZI-MARKET {lot_number}"

def format_price(price):
    return f"{price:,.0f}".replace(",", " ")

def validate_price(price_str):
    try:
        price = float(price_str.replace(',', '.').replace(' ', ''))
        if price < 100: return False, None
        return True, price
    except ValueError:
        return False, None

def split_list(lst, n):
    return [lst[i:i + n] for i in range(0, len(lst), n)]

def get_channel_tag(status):
    if status == 'available': return '✅ #АКТУАЛЬНО'
    elif status == 'reserved': return '🔄 #ЗАБРОНИРОВАН'
    elif status == 'sold': return '❌ #ПРОДАН'
    return ''

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
async def download_photo(file_id, context):
    """Скачивает фото из Telegram и возвращает относительный путь static/items/..."""
    file = await context.bot.get_file(file_id)
    ext = os.path.splitext(file.file_path)[1] or '.jpg'
    filename = f"{uuid.uuid4()}{ext}"
    local_path = STATIC_DIR / filename
    await file.download_to_drive(custom_path=local_path)
    return f"static/items/{filename}"

async def edit_message_smart(query, text, reply_markup=None, parse_mode=None):
    if query.message.photo:
        await query.edit_message_caption(caption=text, reply_markup=reply_markup, parse_mode=parse_mode)
    else:
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)

async def update_channel_caption(context, lot):
    if lot['channel_message_id']:
        try:
            balance = lot['price'] - DEPOSIT_FIXED
            category_name = CATEGORIES.get(lot['category'], lot['category'])
            caption = f"""
🌟 *ТОВАР* 🌟

📱 *{lot['name']}*
📂 Категория: {category_name}
💰 Цена: {format_price(lot['price'])}₽
🆔 Артикул: `{lot['lot_id']}`

📝 *Описание:*
{lot['description']}

🔐 *Условия:*
• 💳 Предоплата: {DEPOSIT_FIXED}₽
• 💰 Остаток: {format_price(balance)}₽
• 🚚 Доставка: бесплатно

⚠️ *Важно:*
❌Имеется недостаток товара: невозможно установить и использовать RuStore

{get_channel_tag(lot['status'])}
💬 @mizirf
            """
            await context.bot.edit_message_caption(
                chat_id=CHANNEL_ID,
                message_id=lot['channel_message_id'],
                caption=caption,
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Ошибка обновления канала: {e}")

# ========== КЛАВИАТУРЫ ==========
def main_menu_keyboard(user_id):
    keyboard = [
        [InlineKeyboardButton("🛍️ Магазин", callback_data="shop"),
         InlineKeyboardButton("📦 Мои заказы", callback_data="my_orders")],
        [InlineKeyboardButton("💰 Продать устройство", callback_data="sell_device")],
        [InlineKeyboardButton("⭐ Избранное", callback_data="favorites"),
         InlineKeyboardButton("🔍 Поиск", callback_data="search")],
        [InlineKeyboardButton("📞 Поддержка", url=f"https://t.me/mizirf"),
         InlineKeyboardButton("📋 Правила", callback_data="terms")]
    ]
    if user_id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("⚙️ Админ панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(keyboard)

def admin_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить товар", callback_data="add_lot"),
         InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("📦 Управление товарами", callback_data="manage_lots"),
         InlineKeyboardButton("📋 Заказы", callback_data="view_orders")],
        [InlineKeyboardButton("💰 Заявки на выкуп", callback_data="view_buyback_requests"),
         InlineKeyboardButton("👥 Пользователи", callback_data="view_users")],
        [InlineKeyboardButton("🏠 В меню", callback_data="main_menu")]
    ])

def shop_keyboard():
    keyboard = []
    categories = list(CATEGORIES.items())
    for i in range(0, len(categories), 2):
        row = []
        if i < len(categories):
            cat_id, cat_name = categories[i]
            row.append(InlineKeyboardButton(cat_name, callback_data=f"category_{cat_id}"))
        if i + 1 < len(categories):
            cat_id, cat_name = categories[i + 1]
            row.append(InlineKeyboardButton(cat_name, callback_data=f"category_{cat_id}"))
        keyboard.append(row)
    keyboard.extend([
        [InlineKeyboardButton("🔍 Все товары", callback_data="all_items")],
        [InlineKeyboardButton("⭐ Популярные", callback_data="popular")],
        [InlineKeyboardButton("💎 Новинки", callback_data="new")],
        [InlineKeyboardButton("🏠 В меню", callback_data="main_menu")]
    ])
    return InlineKeyboardMarkup(keyboard)

def category_keyboard(category, page=0, total_pages=1):
    keyboard = []
    pagination = []
    if page > 0:
        pagination.append(InlineKeyboardButton("← Назад", callback_data=f"cat_back_{category}_{page}"))
    pagination.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        pagination.append(InlineKeyboardButton("Вперед →", callback_data=f"cat_next_{category}_{page}"))
    if pagination:
        keyboard.append(pagination)
    keyboard.append([
        InlineKeyboardButton("🛍️ Все категории", callback_data="shop"),
        InlineKeyboardButton("🏠 В меню", callback_data="main_menu")
    ])
    return InlineKeyboardMarkup(keyboard)

def lot_keyboard(lot_id, status, user_id=None):
    keyboard = []
    is_favorite = False
    if user_id:
        fav = db.fetchone("SELECT * FROM favorites WHERE user_id = ? AND lot_id = ?", (user_id, lot_id))
        is_favorite = bool(fav)
    if status == 'available':
        keyboard.append([
            InlineKeyboardButton("💰 Забронировать", callback_data=f"buy_{lot_id}"),
            InlineKeyboardButton("⭐" if not is_favorite else "★", callback_data=f"fav_{lot_id}")
        ])
    elif status == 'reserved':
        keyboard.append([InlineKeyboardButton("🔄 Забронирован", callback_data="noop")])
    elif status == 'sold':
        keyboard.append([InlineKeyboardButton("❌ Продан", callback_data="noop")])
    keyboard.append([
        InlineKeyboardButton("🔄 Обновить", callback_data=f"refresh_{lot_id}"),
        InlineKeyboardButton("📞 Контакты", url=f"https://t.me/mizirf")
    ])
    keyboard.append([InlineKeyboardButton("🛍️ В магазин", callback_data="shop")])
    return InlineKeyboardMarkup(keyboard)

def order_keyboard(order_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📞 Связаться", callback_data=f"contact_{order_id}"),
         InlineKeyboardButton("🔄 Статус", callback_data=f"status_{order_id}")],
        [InlineKeyboardButton("💬 Поддержка", url=f"https://t.me/mizirf")],
        [InlineKeyboardButton("📦 Мои заказы", callback_data="my_orders")]
    ])

def payment_keyboard(lot_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Перевести 500₽", url=f"https://t.me/mizirf")],
        [InlineKeyboardButton("📸 Отправить скриншот", callback_data=f"pay_{lot_id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"cancel_payment_{lot_id}")]
    ])

def cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data="cancel")]])

def photos_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Готово", callback_data="finish_photos"),
         InlineKeyboardButton("❌ Отмена", callback_data="cancel")]
    ])

def search_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Отмена поиска", callback_data="cancel_search")],
        [InlineKeyboardButton("🏠 В меню", callback_data="main_menu")]
    ])

def admin_order_action_keyboard(order_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Подтвердить бронь", callback_data=f"admin_approve_{order_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"admin_reject_{order_id}")
        ],
        [
            InlineKeyboardButton("📞 Связаться", callback_data=f"admin_contact_{order_id}"),
            InlineKeyboardButton("📋 Детали", callback_data=f"admin_order_details_{order_id}")
        ]
    ])

# ========== ОБРАБОТЧИКИ КОМАНД ==========
async def start(update, context):
    user = update.effective_user
    user_id = user.id
    db.execute('''INSERT OR IGNORE INTO users (id, username, first_name, last_name, is_admin)
                  VALUES (?, ?, ?, ?, ?)''',
               (user_id, user.username, user.first_name, user.last_name, user_id in ADMIN_IDS))
    db.execute('UPDATE users SET last_active = CURRENT_TIMESTAMP WHERE id = ?', (user_id,))
    if context.args:
        lot_id = context.args[0]
        lot = db.fetchone("SELECT * FROM lots WHERE lot_id = ?", (lot_id,))
        if lot:
            db.execute('UPDATE lots SET views = views + 1 WHERE lot_id = ?', (lot_id,))
            db.execute('INSERT OR REPLACE INTO views (user_id, lot_id, viewed_at) VALUES (?, ?, CURRENT_TIMESTAMP)',
                       (user_id, lot_id))
            await show_lot(update, context, lot_id)
            return
    welcome_text = f"""
✨ *Добро пожаловать в MIZI MARKET, {user.first_name}!* ✨

🏆 *Премиум магазин техники Apple*

🎯 *Что мы предлагаем:*
• 🛍️ Широкий выбор проверенной техники
• 💰 Выкуп ваших устройств по лучшей цене
• 🚚 Бесплатная доставка по РФ
• ⚡ Быстрое оформление за 5 минут
• 🔒 Гарантия на все товары

💫 *Начните прямо сейчас:*
    """
    if update.message:
        await update.message.reply_text(welcome_text, reply_markup=main_menu_keyboard(user_id), parse_mode='Markdown')
    else:
        await edit_message_smart(update.callback_query, welcome_text, reply_markup=main_menu_keyboard(user_id),
                                 parse_mode='Markdown')

async def help_command(update, context):
    help_text = """
🆘 *Помощь по боту*

*Основные команды:*
/start - Главное меню
/help - Эта справка
/menu - Вернуться в меню

*Как купить:*
1. 🛍️ Выберите товар в магазине
2. 💰 Нажмите "Забронировать"
3. 💳 Оплатите предоплату 500₽
4. 📸 Отправьте скриншот оплаты
5. ✅ Получите подтверждение менеджера

*Как продать:*
1. 💰 Нажмите "Продать устройство"
2. 📝 Заполните форму заявки
3. ⏳ Ожидайте оценку стоимости
4. 🤝 Согласуйте встречу

*Контакты поддержки:*
📞 @mizirf
⏰ 10:00 - 20:00 (МСК)
    """
    await update.message.reply_text(help_text, parse_mode='Markdown',
                                    reply_markup=main_menu_keyboard(update.effective_user.id))

async def menu_command(update, context):
    await start(update, context)

# ========== ОБРАБОТЧИК CALLBACK ==========
async def handle_callback(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    logger.info(f"Callback: {data} от {user_id}")

    if data == "main_menu":
        await edit_message_smart(query, "🏠 *Главное меню:*", reply_markup=main_menu_keyboard(user_id), parse_mode='Markdown')
    elif data == "shop":
        await edit_message_smart(query, "🛍️ *Выберите категорию:*", reply_markup=shop_keyboard(), parse_mode='Markdown')
    elif data == "admin_panel":
        if user_id in ADMIN_IDS:
            await edit_message_smart(query, "⚙️ *Админ-панель:*", reply_markup=admin_menu_keyboard(), parse_mode='Markdown')
        else:
            await query.answer("⛔ Доступ запрещен", show_alert=True)
    elif data.startswith("category_"):
        category = data.split("_", 1)[1]
        await show_category(update, context, category)
    elif data.startswith("cat_back_"):
        parts = data.split("_")
        category = parts[2]
        page = int(parts[3])
        await show_category(update, context, category, max(0, page - 1))
    elif data.startswith("cat_next_"):
        parts = data.split("_")
        category = parts[2]
        page = int(parts[3])
        await show_category(update, context, category, page + 1)
    elif data.startswith("lot_"):
        lot_id = data.split("_", 1)[1]
        await show_lot(update, context, lot_id)
    elif data.startswith("buy_"):
        lot_id = data.split("_", 1)[1]
        await start_payment(update, context, lot_id)
    elif data.startswith("pay_"):
        lot_id = data.split("_", 1)[1]
        context.user_data['awaiting_screenshot'] = lot_id
        await edit_message_smart(query, "📸 *Отправьте скриншот оплаты:*", reply_markup=cancel_keyboard(), parse_mode='Markdown')
    elif data == "my_orders":
        await show_user_orders(update, context)
    elif data == "sell_device":
        await start_buyback_conversation(update, context)
    elif data == "terms":
        await show_terms(update, context)
    elif data.startswith("refresh_"):
        lot_id = data.split("_", 1)[1]
        await show_lot(update, context, lot_id)
    elif data.startswith("fav_"):
        lot_id = data.split("_", 1)[1]
        await toggle_favorite(update, context, lot_id)
    elif data == "favorites":
        await show_favorites(update, context)
    elif data == "all_items":
        await show_all_items(update, context)
    elif data == "popular":
        await show_popular_items(update, context)
    elif data == "new":
        await show_new_items(update, context)
    elif data == "search":
        await start_search(update, context)
    elif data == "cancel_search":
        await cancel_conversation(update, context)
    elif data == "add_lot" and user_id in ADMIN_IDS:
        await start_add_lot_conversation(update, context)
    elif data == "manage_lots" and user_id in ADMIN_IDS:
        await manage_lots(update, context)
    elif data == "view_orders" and user_id in ADMIN_IDS:
        await view_orders(update, context)
    elif data == "view_buyback_requests" and user_id in ADMIN_IDS:
        await view_buyback_requests(update, context)
    elif data == "stats" and user_id in ADMIN_IDS:
        await show_stats(update, context)
    elif data == "view_users" and user_id in ADMIN_IDS:
        await view_users(update, context)
    elif data.startswith("admin_lot_") and user_id in ADMIN_IDS:
        lot_id = data.split("_", 2)[2]
        await admin_show_lot(update, context, lot_id)
    elif data.startswith("set_status_") and user_id in ADMIN_IDS:
        parts = data.split("_")
        lot_id = parts[2]
        new_status = parts[3]
        await set_lot_status(update, context, lot_id, new_status)
    elif data.startswith("admin_order_") and user_id in ADMIN_IDS:
        order_id = int(data.split("_", 2)[2])
        await admin_show_order(update, context, order_id)
    elif data.startswith("approve_order_") and user_id in ADMIN_IDS:
        order_id = int(data.split("_", 2)[2])
        await approve_order(update, context, order_id)
    elif data.startswith("reject_order_") and user_id in ADMIN_IDS:
        order_id = int(data.split("_", 2)[2])
        await reject_order(update, context, order_id)
    elif data.startswith("admin_approve_") and user_id in ADMIN_IDS:
        order_id = int(data.split("_", 2)[2])
        await admin_approve_order(update, context, order_id)
    elif data.startswith("admin_reject_") and user_id in ADMIN_IDS:
        order_id = int(data.split("_", 2)[2])
        await admin_reject_order(update, context, order_id)
    elif data.startswith("admin_contact_") and user_id in ADMIN_IDS:
        order_id = int(data.split("_", 2)[2])
        await admin_contact_order(update, context, order_id)
    elif data.startswith("admin_order_details_") and user_id in ADMIN_IDS:
        order_id = int(data.split("_", 3)[3])
        await admin_show_order_details(update, context, order_id)
    elif data.startswith("cancel_payment_"):
        lot_id = data.split("_", 2)[2]
        await show_lot(update, context, lot_id)
    elif data == "cancel":
        await cancel_conversation(update, context)
    elif data == "noop":
        pass
    else:
        await edit_message_smart(query, "❌ *Неизвестная команда*", reply_markup=main_menu_keyboard(user_id), parse_mode='Markdown')

# ========== ФУНКЦИИ КАТЕГОРИЙ И ТОВАРОВ ==========
async def show_category(update, context, category, page=0):
    query = update.callback_query
    limit = 10
    offset = page * limit
    items = db.fetchall('''SELECT * FROM lots WHERE category = ? AND status = 'available' ORDER BY created_at DESC LIMIT ? OFFSET ?''',
                        (category, limit, offset))
    total_items = db.fetchone("SELECT COUNT(*) as count FROM lots WHERE category = ? AND status = 'available'", (category,))['count']
    total_pages = max((total_items + limit - 1) // limit, 1)
    if not items:
        category_name = CATEGORIES.get(category, category)
        await edit_message_smart(query, f"📭 *В категории '{category_name}' пока нет товаров*", reply_markup=shop_keyboard(), parse_mode='Markdown')
        return
    keyboard = []
    for item in items:
        emoji = CATEGORY_EMOJIS.get(item['category'], '📦')
        keyboard.append([InlineKeyboardButton(f"{emoji} {item['name']} - {format_price(item['price'])}₽", callback_data=f"lot_{item['lot_id']}")])
    keyboard.append(category_keyboard(category, page, total_pages).inline_keyboard[0])
    keyboard.append([InlineKeyboardButton("🛍️ Все категории", callback_data="shop"), InlineKeyboardButton("🏠 В меню", callback_data="main_menu")])
    category_name = CATEGORIES.get(category, category)
    await edit_message_smart(query, f"🛍️ *{category_name}* (стр. {page+1}/{total_pages}):\n📊 Найдено товаров: {total_items}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def show_lot(update, context, lot_id):
    query = update.callback_query if update.callback_query else None
    user_id = update.effective_user.id
    lot = db.fetchone("SELECT * FROM lots WHERE lot_id = ?", (lot_id,))
    if not lot:
        text = "❌ *Товар не найден*"
        if query:
            await edit_message_smart(query, text, reply_markup=shop_keyboard(), parse_mode='Markdown')
        else:
            await update.message.reply_text(text, parse_mode='Markdown', reply_markup=shop_keyboard())
        return
    db.execute('UPDATE lots SET views = views + 1 WHERE lot_id = ?', (lot_id,))
    db.execute('INSERT OR REPLACE INTO views (user_id, lot_id, viewed_at) VALUES (?, ?, CURRENT_TIMESTAMP)', (user_id, lot_id))
    category_name = CATEGORIES.get(lot['category'], lot['category'])
    status_info = STATUSES.get(lot['status'], {'text': lot['status'], 'emoji': '📦'})
    balance = lot['price'] - DEPOSIT_FIXED
    text = f"""{status_info['emoji']} *{lot['name']}*
📋 *Характеристики:*
• 📂 Категория: {category_name}
• 💰 Цена: {format_price(lot['price'])}₽
• 🆔 Артикул: `{lot['lot_id']}`
• 👀 Просмотров: {lot['views']}
• 📅 Добавлен: {lot['created_at'][:10]}

📝 *Описание:*
{lot['description']}

🔐 *Условия покупки:*
• 💳 Предоплата: {DEPOSIT_FIXED}₽
• 💰 Остаток при получении: {format_price(balance)}₽
• 🚚 Доставка: бесплатно по России
• ⏰ Бронь на: 24 часов

⚠️ *Важно:*
❌Имеется недостаток товара: невозможно установить и использовать RuStore

{status_info['emoji']} *Статус:* {status_info['text']}
    """
    reply_markup = lot_keyboard(lot_id, lot['status'], user_id)
    photos = lot['photos'].split(',') if lot['photos'] else []
    if photos:
        try:
            if query:
                await query.message.delete()
            with open(STATIC_DIR / photos[0].split('/')[-1], 'rb') as f:
                message = await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=f,
                    caption=text,
                    parse_mode='Markdown',
                    reply_markup=reply_markup
                )
            if len(photos) > 1:
                media_group = []
                for p in photos[1:]:
                    with open(STATIC_DIR / p.split('/')[-1], 'rb') as fp:
                        media_group.append(InputMediaPhoto(media=fp))
                await context.bot.send_media_group(chat_id=update.effective_chat.id, media=media_group)
        except Exception as e:
            logger.error(f"Ошибка отправки фото: {e}")
            if query:
                await edit_message_smart(query, text, reply_markup=reply_markup, parse_mode='Markdown')
            else:
                await update.message.reply_text(text, parse_mode='Markdown', reply_markup=reply_markup)
    else:
        if query:
            await edit_message_smart(query, text, reply_markup=reply_markup, parse_mode='Markdown')
        else:
            await update.message.reply_text(text, parse_mode='Markdown', reply_markup=reply_markup)

async def start_payment(update, context, lot_id):
    query = update.callback_query
    lot = db.fetchone("SELECT * FROM lots WHERE lot_id = ?", (lot_id,))
    if not lot or lot['status'] != 'available':
        await query.answer("❌ Товар недоступен", show_alert=True)
        return
    balance = lot['price'] - DEPOSIT_FIXED
    payment_text = f"""💳 *ОФОРМЛЕНИЕ ЗАКАЗА*
📱 *Товар:* {lot['name']}
🆔 *Артикул:* `{lot['lot_id']}`
💰 *Цена:* {format_price(lot['price'])}₽
🔐 *Условия оплаты:*
• 💵 Предоплата: {DEPOSIT_FIXED}₽
• 💰 Остаток: {format_price(balance)}₽
• 🚚 Доставка: бесплатно
⚠️ *Важно:* ❌Имеется недостаток товара: невозможно установить и использовать RuStore

🏦 *Реквизиты:* Сбербанк, карта: `2202 2068 3661 5885`, получатель: МАКСИМ М.
"""
    await edit_message_smart(query, payment_text, reply_markup=payment_keyboard(lot_id), parse_mode='Markdown')

async def show_user_orders(update, context):
    query = update.callback_query
    user_id = update.effective_user.id
    orders = db.fetchall('''SELECT o.*, l.name as product_name, l.price, l.lot_id
                            FROM orders o JOIN lots l ON o.lot_id = l.lot_id
                            WHERE o.user_id = ? ORDER BY o.created_at DESC''', (user_id,))
    if not orders:
        await edit_message_smart(query, "📭 *У вас пока нет заказов*", reply_markup=shop_keyboard(), parse_mode='Markdown')
        return
    text = "📦 *Ваши заказы:*\n\n"
    for order in orders:
        status_info = STATUSES.get(order['status'], {'text': order['status'], 'emoji': '📦'})
        text += f"{status_info['emoji']} *Заказ #{order['id']}*\n📱 {order['product_name']}\n💰 {format_price(order['price'])}₽\n📅 {order['created_at'][:10]}\n📊 Статус: {status_info['text']}\n\n"
    await edit_message_smart(query, text, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🛍️ В магазин", callback_data="shop")],
        [InlineKeyboardButton("🏠 В меню", callback_data="main_menu")]
    ]), parse_mode='Markdown')

async def toggle_favorite(update, context, lot_id):
    query = update.callback_query
    user_id = query.from_user.id
    fav = db.fetchone("SELECT * FROM favorites WHERE user_id = ? AND lot_id = ?", (user_id, lot_id))
    if fav:
        db.execute("DELETE FROM favorites WHERE user_id = ? AND lot_id = ?", (user_id, lot_id))
        await query.answer("❌ Удалено из избранного", show_alert=True)
    else:
        db.execute("INSERT INTO favorites (user_id, lot_id) VALUES (?, ?)", (user_id, lot_id))
        await query.answer("⭐ Добавлено в избранное", show_alert=True)
    await show_lot(update, context, lot_id)

async def show_favorites(update, context):
    query = update.callback_query
    user_id = query.from_user.id
    favorites = db.fetchall('''SELECT l.* FROM lots l
                               JOIN favorites f ON l.lot_id = f.lot_id
                               WHERE f.user_id = ? AND l.status = 'available'
                               ORDER BY f.added_at DESC''', (user_id,))
    if not favorites:
        await edit_message_smart(query, "⭐ *Избранное пусто*", reply_markup=shop_keyboard(), parse_mode='Markdown')
        return
    text = "⭐ *Избранные товары:*\n\n"
    for item in favorites:
        emoji = CATEGORY_EMOJIS.get(item['category'], '📦')
        text += f"{emoji} *{item['name']}*\n💰 {format_price(item['price'])}₽\n📂 {CATEGORIES.get(item['category'], item['category'])}\n🆔 `{item['lot_id']}`\n────────\n"
    await edit_message_smart(query, text, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🛍️ В магазин", callback_data="shop")],
        [InlineKeyboardButton("🏠 В меню", callback_data="main_menu")]
    ]), parse_mode='Markdown')

async def start_search(update, context):
    query = update.callback_query
    await edit_message_smart(query, "🔍 *Поиск товаров*\nВведите название или ключевые слова:", reply_markup=search_keyboard(), parse_mode='Markdown')
    return SEARCH

async def handle_search(update, context):
    search_query = update.message.text.strip()
    if len(search_query) < 2:
        await update.message.reply_text("❌ *Слишком короткий запрос*", parse_mode='Markdown', reply_markup=search_keyboard())
        return SEARCH
    items = db.fetchall('''SELECT * FROM lots WHERE (name LIKE ? OR description LIKE ?) AND status = 'available' ORDER BY created_at DESC LIMIT 20''',
                        (f'%{search_query}%', f'%{search_query}%'))
    if not items:
        await update.message.reply_text(f"🔍 *По запросу \"{search_query}\" ничего не найдено*", parse_mode='Markdown',
                                        reply_markup=InlineKeyboardMarkup([
                                            [InlineKeyboardButton("🛍️ В магазин", callback_data="shop")],
                                            [InlineKeyboardButton("🔍 Новый поиск", callback_data="search")],
                                            [InlineKeyboardButton("🏠 В меню", callback_data="main_menu")]
                                        ]))
        return ConversationHandler.END
    text = f"🔍 *Результаты поиска \"{search_query}\":*\n\n"
    keyboard = []
    for item in items:
        emoji = CATEGORY_EMOJIS.get(item['category'], '📦')
        keyboard.append([InlineKeyboardButton(f"{emoji} {item['name']} - {format_price(item['price'])}₽", callback_data=f"lot_{item['lot_id']}")])
    keyboard.append([InlineKeyboardButton("🛍️ В магазин", callback_data="shop"), InlineKeyboardButton("🔍 Новый поиск", callback_data="search")])
    keyboard.append([InlineKeyboardButton("🏠 В меню", callback_data="main_menu")])
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END

# ========== ПРОДАЖА УСТРОЙСТВА ==========
async def start_buyback_conversation(update, context):
    query = update.callback_query
    context.user_data['buyback'] = {}
    context.user_data['buyback_photos'] = []
    await edit_message_smart(query, "💰 *ПРОДАЖА УСТРОЙСТВА APPLE*\n\n*Введите модель:*", reply_markup=cancel_keyboard(), parse_mode='Markdown')
    return DEVICE_MODEL

async def handle_device_model(update, context):
    context.user_data['buyback']['model'] = update.message.text
    await update.message.reply_text("*Опишите состояние:*", parse_mode='Markdown', reply_markup=cancel_keyboard())
    return DEVICE_CONDITION

async def handle_device_condition(update, context):
    context.user_data['buyback']['condition'] = update.message.text
    await update.message.reply_text("*Дополнительные детали:*", parse_mode='Markdown', reply_markup=cancel_keyboard())
    return DEVICE_DESCRIPTION

async def handle_device_description(update, context):
    context.user_data['buyback']['description'] = update.message.text
    await update.message.reply_text("*Ваше местоположение:*", parse_mode='Markdown', reply_markup=cancel_keyboard())
    return DEVICE_LOCATION

async def handle_device_location(update, context):
    context.user_data['buyback']['location'] = update.message.text
    await update.message.reply_text("*Контактные данные:*", parse_mode='Markdown', reply_markup=cancel_keyboard())
    return DEVICE_CONTACT

async def handle_device_contact(update, context):
    context.user_data['buyback']['contact'] = update.message.text
    await update.message.reply_text("📸 *Отправьте фото устройства (или пропустите):*", parse_mode='Markdown',
                                    reply_markup=InlineKeyboardMarkup([
                                        [InlineKeyboardButton("⏭️ Пропустить", callback_data="skip_photos")],
                                        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")]
                                    ]))
    return DEVICE_PHOTOS

async def handle_device_photos(update, context):
    if 'buyback_photos' not in context.user_data:
        context.user_data['buyback_photos'] = []
    if update.message.photo:
        photo = update.message.photo[-1]
        if len(context.user_data['buyback_photos']) >= MAX_PHOTOS:
            await update.message.reply_text(f"❌ Максимум {MAX_PHOTOS} фото", reply_markup=photos_keyboard())
            return DEVICE_PHOTOS
        local_path = await download_photo(photo.file_id, context)
        context.user_data['buyback_photos'].append(local_path)
        count = len(context.user_data['buyback_photos'])
        await update.message.reply_text(f"✅ Фото добавлено! ({count}/{MAX_PHOTOS})", reply_markup=photos_keyboard())
    return DEVICE_PHOTOS

async def finish_buyback(update, context, with_photos=True):
    query = update.callback_query
    user = update.effective_user
    buyback_data = context.user_data.get('buyback', {})
    if not all(k in buyback_data for k in ('model', 'condition', 'contact')):
        await edit_message_smart(query, "❌ *Ошибка: обязательные поля не заполнены*", reply_markup=main_menu_keyboard(user.id), parse_mode='Markdown')
        return ConversationHandler.END
    photos = ','.join(context.user_data.get('buyback_photos', [])) if with_photos else ''
    cursor = db.execute('''INSERT INTO buyback_requests
                           (user_id, user_name, user_username, device_model, device_condition,
                            description, photos, location, contact_info, status)
                           VALUES (?,?,?,?,?,?,?,?,?,?)''',
                        (user.id, user.full_name, user.username, buyback_data['model'],
                         buyback_data['condition'], buyback_data.get('description', ''),
                         photos, buyback_data.get('location', ''), buyback_data['contact'], 'pending'))
    request_id = cursor.lastrowid
    for admin_id in ADMIN_IDS:
        try:
            text = f"💰 *НОВАЯ ЗАЯВКА НА ВЫКУП #{request_id}*\n👤 {user.full_name}\n📱 {buyback_data['model']}\n📞 {buyback_data['contact']}"
            await context.bot.send_message(admin_id, text, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Ошибка отправки админу {admin_id}: {e}")
    await edit_message_smart(query, "✅ *Заявка отправлена!*", reply_markup=main_menu_keyboard(user.id), parse_mode='Markdown')
    for key in list(context.user_data.keys()):
        if key.startswith('buyback'):
            del context.user_data[key]
    return ConversationHandler.END

async def finish_buyback_without_photos(update, context):
    return await finish_buyback(update, context, with_photos=False)

# ========== АДМИН-ФУНКЦИИ ==========
async def start_add_lot_conversation(update, context):
    query = update.callback_query
    context.user_data['adding_lot'] = {}
    context.user_data['lot_photos'] = []
    keyboard = []
    categories = list(CATEGORIES.items())
    for i in range(0, len(categories), 2):
        row = []
        if i < len(categories):
            cat_id, cat_name = categories[i]
            row.append(InlineKeyboardButton(cat_name, callback_data=f"add_cat_{cat_id}"))
        if i + 1 < len(categories):
            cat_id, cat_name = categories[i + 1]
            row.append(InlineKeyboardButton(cat_name, callback_data=f"add_cat_{cat_id}"))
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    await edit_message_smart(query, "➕ *ДОБАВЛЕНИЕ ТОВАРА*\n📂 *Выберите категорию:*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return LOT_CATEGORY

async def handle_lot_category(update, context):
    query = update.callback_query
    category = query.data.split("_")[2]
    context.user_data['adding_lot']['category'] = category
    await edit_message_smart(query, "📝 *Введите название товара:*", reply_markup=cancel_keyboard(), parse_mode='Markdown')
    return LOT_NAME

async def handle_lot_name(update, context):
    context.user_data['adding_lot']['name'] = update.message.text
    await update.message.reply_text("💰 *Введите цену в рублях:*", reply_markup=cancel_keyboard(), parse_mode='Markdown')
    return LOT_PRICE

async def handle_lot_price(update, context):
    price_str = update.message.text
    valid, price = validate_price(price_str)
    if not valid:
        await update.message.reply_text("❌ *Неверная цена. Минимум 100₽*", reply_markup=cancel_keyboard(), parse_mode='Markdown')
        return LOT_PRICE
    context.user_data['adding_lot']['price'] = price
    await update.message.reply_text("📝 *Введите описание:*", reply_markup=cancel_keyboard(), parse_mode='Markdown')
    return LOT_DESCRIPTION

async def handle_lot_description(update, context):
    context.user_data['adding_lot']['description'] = update.message.text
    await update.message.reply_text("📸 *Отправьте фото (до 10 шт.):*", reply_markup=photos_keyboard(), parse_mode='Markdown')
    return LOT_PHOTOS

async def handle_lot_photos(update, context):
    if 'lot_photos' not in context.user_data:
        context.user_data['lot_photos'] = []
    if update.message.photo:
        photo = update.message.photo[-1]
        if len(context.user_data['lot_photos']) >= MAX_PHOTOS:
            await update.message.reply_text(f"❌ Максимум {MAX_PHOTOS} фото", reply_markup=photos_keyboard())
            return LOT_PHOTOS
        # Сохраняем file_id временно, потом скачаем
        context.user_data['lot_photos'].append(photo.file_id)
        count = len(context.user_data['lot_photos'])
        await update.message.reply_text(f"✅ Фото добавлено! ({count}/{MAX_PHOTOS})", reply_markup=photos_keyboard())
    return LOT_PHOTOS

async def finish_add_lot(update, context):
    query = update.callback_query
    lot_data = context.user_data.get('adding_lot', {})
    photo_file_ids = context.user_data.get('lot_photos', [])
    if not lot_data or not photo_file_ids:
        await edit_message_smart(query, "❌ *Не все данные*", reply_markup=admin_menu_keyboard(), parse_mode='Markdown')
        return ConversationHandler.END
    # Скачиваем фото и сохраняем локальные пути
    photo_paths = []
    for file_id in photo_file_ids:
        path = await download_photo(file_id, context)
        photo_paths.append(path)
    photos_str = ','.join(photo_paths)
    lot_id = generate_lot_id()
    db.execute('''INSERT INTO lots (lot_id, name, description, price, photos, category, status)
                  VALUES (?,?,?,?,?,?,?)''',
               (lot_id, lot_data['name'], lot_data['description'], lot_data['price'], photos_str, lot_data['category'], 'available'))
    # Публикация в канал
    try:
        balance = lot_data['price'] - DEPOSIT_FIXED
        caption = f"""🌟 *НОВЫЙ ТОВАР!* 🌟

📱 *{lot_data['name']}*
📂 Категория: {CATEGORIES.get(lot_data['category'], lot_data['category'])}
💰 Цена: {format_price(lot_data['price'])}₽
🆔 Артикул: `{lot_id}`

📝 *Описание:*
{lot_data['description']}

🔐 *Условия:*
• 💳 Предоплата: {DEPOSIT_FIXED}₽
• 💰 Остаток: {format_price(balance)}₽
• 🚚 Доставка: бесплатно

⚠️ *Важно:*
❌Имеется недостаток товара: невозможно установить и использовать RuStore

{get_channel_tag('available')}
💬 @mizirf
        """
        with open(STATIC_DIR / photo_paths[0].split('/')[-1], 'rb') as f:
            message = await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=f,
                caption=caption,
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🛒 КУПИТЬ", url=f"https://t.me/{BOT_USERNAME}?start={lot_id}")
                ]])
            )
        db.execute("UPDATE lots SET channel_message_id = ? WHERE lot_id = ?", (message.message_id, lot_id))
    except Exception as e:
        logger.error(f"Ошибка публикации в канале: {e}")
    await edit_message_smart(query, f"✅ *Товар {lot_id} добавлен!*", reply_markup=admin_menu_keyboard(), parse_mode='Markdown')
    for key in ['adding_lot', 'lot_photos']:
        if key in context.user_data: del context.user_data[key]
    return ConversationHandler.END

async def manage_lots(update, context):
    query = update.callback_query
    lots = db.fetchall("SELECT * FROM lots ORDER BY created_at DESC LIMIT 20")
    if not lots:
        await edit_message_smart(query, "📭 *Товаров нет*", reply_markup=admin_menu_keyboard(), parse_mode='Markdown')
        return
    keyboard = []
    for lot in lots:
        status_info = STATUSES.get(lot['status'], {'text': lot['status'], 'emoji': '📦'})
        keyboard.append([InlineKeyboardButton(f"{status_info['emoji']} {lot['name'][:30]} - {format_price(lot['price'])}₽", callback_data=f"admin_lot_{lot['lot_id']}")])
    keyboard.append([InlineKeyboardButton("🏠 В меню", callback_data="admin_panel")])
    await edit_message_smart(query, "📊 *Управление товарами:*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def admin_show_lot(update, context, lot_id):
    query = update.callback_query
    lot = db.fetchone("SELECT * FROM lots WHERE lot_id = ?", (lot_id,))
    if not lot:
        await edit_message_smart(query, "❌ Товар не найден", reply_markup=admin_menu_keyboard(), parse_mode='Markdown')
        return
    status_info = STATUSES.get(lot['status'], {'text': lot['status'], 'emoji': '📦'})
    text = f"⚙️ *Управление товаром*\n\n{lot['name']}\n{status_info['emoji']} Статус: {status_info['text']}\n💰 Цена: {format_price(lot['price'])}₽\n🆔 {lot['lot_id']}"
    keyboard = [
        [InlineKeyboardButton("✅ Доступен", callback_data=f"set_status_{lot_id}_available")],
        [InlineKeyboardButton("🔄 Забронирован", callback_data=f"set_status_{lot_id}_reserved")],
        [InlineKeyboardButton("❌ Продан", callback_data=f"set_status_{lot_id}_sold")],
        [InlineKeyboardButton("← Назад", callback_data="manage_lots")]
    ]
    await edit_message_smart(query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def set_lot_status(update, context, lot_id, new_status):
    query = update.callback_query
    db.execute("UPDATE lots SET status = ? WHERE lot_id = ?", (new_status, lot_id))
    lot = db.fetchone("SELECT * FROM lots WHERE lot_id = ?", (lot_id,))
    await update_channel_caption(context, lot)
    await query.answer(f"Статус изменен на {STATUSES.get(new_status, {'text': new_status})['text']}")
    await admin_show_lot(update, context, lot_id)

async def view_orders(update, context):
    query = update.callback_query
    orders = db.fetchall('''SELECT o.*, l.name as product_name, l.price, l.lot_id
                            FROM orders o JOIN lots l ON o.lot_id = l.lot_id
                            ORDER BY o.created_at DESC LIMIT 20''')
    if not orders:
        await edit_message_smart(query, "📭 *Нет заказов*", reply_markup=admin_menu_keyboard(), parse_mode='Markdown')
        return
    text = "📋 *Заказы:*\n\n"
    keyboard = []
    for order in orders:
        status_info = STATUSES.get(order['status'], {'text': order['status'], 'emoji': '📦'})
        text += f"{status_info['emoji']} *Заказ #{order['id']}*\n📱 {order['product_name']}\n👤 {order['user_name']}\n📅 {order['created_at'][:16]}\n\n"
        keyboard.append([InlineKeyboardButton(f"#{order['id']} {status_info['text']}", callback_data=f"admin_order_{order['id']}")])
    keyboard.append([InlineKeyboardButton("🔄 Обновить", callback_data="view_orders")])
    keyboard.append([InlineKeyboardButton("🏠 В меню", callback_data="admin_panel")])
    await edit_message_smart(query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def admin_show_order(update, context, order_id):
    query = update.callback_query
    order = db.fetchone('''SELECT o.*, l.name as product_name, l.lot_id
                            FROM orders o JOIN lots l ON o.lot_id = l.lot_id
                            WHERE o.id = ?''', (order_id,))
    if not order:
        await edit_message_smart(query, "❌ Заказ не найден", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Назад", callback_data="view_orders")]]), parse_mode='Markdown')
        return
    status_info = STATUSES.get(order['status'], {'text': order['status'], 'emoji': '📦'})
    text = f"📋 *Заказ #{order_id}*\n\n📱 Товар: {order['product_name']}\n🆔 Артикул: {order['lot_id']}\n💰 Цена: {format_price(order['price'])}₽\n👤 Пользователь: {order['user_name']} @{order['user_username'] or ''}\n🆔 ID: {order['user_id']}\n📅 Дата: {order['created_at']}\n📊 Статус: {status_info['text']}\n"
    keyboard = []
    if order['status'] == 'pending':
        keyboard.append([InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve_order_{order_id}"),
                         InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_order_{order_id}")])
    keyboard.append([InlineKeyboardButton("← Назад", callback_data="view_orders")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    if order['screenshot']:
        try:
            await query.message.delete()
            # скриншот может храниться как file_id, но мы используем локальный путь? упростим
            with open(STATIC_DIR / order['screenshot'].split('/')[-1], 'rb') as f:
                await context.bot.send_photo(chat_id=query.message.chat_id, photo=f, caption=text, parse_mode='Markdown', reply_markup=reply_markup)
        except:
            await edit_message_smart(query, text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await edit_message_smart(query, text, reply_markup=reply_markup, parse_mode='Markdown')

async def admin_show_order_details(update, context, order_id):
    query = update.callback_query
    order = db.fetchone('''SELECT o.*, l.name as product_name, l.lot_id, l.price
                            FROM orders o JOIN lots l ON o.lot_id = l.lot_id WHERE o.id = ?''', (order_id,))
    if not order:
        await query.answer("❌ Заказ не найден", show_alert=True)
        return
    status_info = STATUSES.get(order['status'], {'text': order['status'], 'emoji': '📦'})
    text = f"""📋 *Детали заказа #{order_id}*
📱 *Товар:* {order['product_name']}
🆔 *Артикул:* {order['lot_id']}
💰 *Цена:* {format_price(order['price'])}₽
💵 *Предоплата:* {DEPOSIT_FIXED}₽
👤 *Покупатель:*
• Имя: {order['user_name']}
• Username: @{order['user_username'] or 'не указан'}
• ID: {order['user_id']}
📅 *Дата создания:* {order['created_at'][:19]}
📊 *Статус:* {status_info['text']}
    """
    await edit_message_smart(query, text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Назад", callback_data=f"admin_order_{order_id}")]]), parse_mode='Markdown')

async def approve_order(update, context, order_id):
    query = update.callback_query
    order = db.fetchone("SELECT * FROM orders WHERE id = ?", (order_id,))
    if order and order['status'] == 'pending':
        db.execute("UPDATE orders SET status = 'approved' WHERE id = ?", (order_id,))
        db.execute("UPDATE lots SET status = 'reserved' WHERE lot_id = ?", (order['lot_id'],))
        lot = db.fetchone("SELECT * FROM lots WHERE lot_id = ?", (order['lot_id'],))
        await update_channel_caption(context, lot)
        try:
            await context.bot.send_message(order['user_id'], f"✅ *Ваш заказ #{order_id} подтвержден!*\nСвяжитесь с @mizirf", parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Ошибка уведомления: {e}")
    await query.answer("Заказ подтвержден")
    await view_orders(update, context)

async def reject_order(update, context, order_id):
    query = update.callback_query
    order = db.fetchone("SELECT * FROM orders WHERE id = ?", (order_id,))
    if order and order['status'] == 'pending':
        db.execute("UPDATE orders SET status = 'rejected' WHERE id = ?", (order_id,))
        try:
            await context.bot.send_message(order['user_id'], f"❌ *Ваш заказ #{order_id} отклонен.* Товар снова доступен.", parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Ошибка уведомления: {e}")
    await query.answer("Заказ отклонен")
    await view_orders(update, context)

async def admin_approve_order(update, context, order_id):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("⛔ Нет прав", show_alert=True)
        return
    order = db.fetchone("SELECT * FROM orders WHERE id = ?", (order_id,))
    if not order or order['status'] != 'pending':
        await query.answer("❌ Заказ уже обработан", show_alert=True)
        return
    db.execute("UPDATE orders SET status = 'approved' WHERE id = ?", (order_id,))
    db.execute("UPDATE lots SET status = 'reserved' WHERE lot_id = ?", (order['lot_id'],))
    lot = db.fetchone("SELECT * FROM lots WHERE lot_id = ?", (order['lot_id'],))
    await update_channel_caption(context, lot)
    try:
        await context.bot.send_message(order['user_id'], f"✅ *Ваш заказ #{order_id} подтвержден!*", parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Ошибка уведомления: {e}")
    await query.edit_message_caption(caption=query.message.caption + "\n\n✅ *Бронь подтверждена*", parse_mode='Markdown', reply_markup=None)
    await query.answer("✅ Подтверждено")

async def admin_reject_order(update, context, order_id):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("⛔ Нет прав", show_alert=True)
        return
    order = db.fetchone("SELECT * FROM orders WHERE id = ?", (order_id,))
    if not order or order['status'] != 'pending':
        await query.answer("❌ Заказ уже обработан", show_alert=True)
        return
    db.execute("UPDATE orders SET status = 'rejected' WHERE id = ?", (order_id,))
    try:
        await context.bot.send_message(order['user_id'], f"❌ *Ваш заказ #{order_id} отклонен.*", parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Ошибка уведомления: {e}")
    await query.edit_message_caption(caption=query.message.caption + "\n\n❌ *Бронь отклонена*", parse_mode='Markdown', reply_markup=None)
    await query.answer("❌ Отклонено")

async def admin_contact_order(update, context, order_id):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("⛔ Нет прав", show_alert=True)
        return
    order = db.fetchone("SELECT * FROM orders WHERE id = ?", (order_id,))
    if not order:
        await query.answer("❌ Заказ не найден", show_alert=True)
        return
    contact_info = f"""📞 *Контакты покупателя #{order_id}*
👤 *Имя:* {order['user_name']}
🆔 *ID:* {order['user_id']}
📱 *Username:* @{order['user_username'] or 'не указан'}
💬 *Ссылка:* [Написать](tg://user?id={order['user_id']})"""
    await edit_message_smart(query, contact_info, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("✉️ Написать", url=f"tg://user?id={order['user_id']}")],
        [InlineKeyboardButton("← Назад", callback_data=f"admin_approve_{order_id}")]
    ]), parse_mode='Markdown')

async def view_buyback_requests(update, context):
    query = update.callback_query
    requests = db.fetchall("SELECT * FROM buyback_requests WHERE status = 'pending' ORDER BY created_at DESC LIMIT 20")
    if not requests:
        await edit_message_smart(query, "📭 *Нет заявок*", reply_markup=admin_menu_keyboard(), parse_mode='Markdown')
        return
    text = "💰 *Заявки на выкуп:*\n\n"
    for req in requests:
        text += f"🆔 *#{req['id']}*\n📱 {req['device_model']}\n👤 {req['user_name']}\n📍 {req['location'] or 'Не указано'}\n📅 {req['created_at'][:16]}\n────────\n"
    await edit_message_smart(query, text, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Обновить", callback_data="view_buyback_requests")],
        [InlineKeyboardButton("🏠 В меню", callback_data="admin_panel")]
    ]), parse_mode='Markdown')

async def show_stats(update, context):
    query = update.callback_query
    stats = {
        'users': db.fetchone("SELECT COUNT(*) as c FROM users")['c'],
        'lots': db.fetchone("SELECT COUNT(*) as c FROM lots")['c'],
        'available': db.fetchone("SELECT COUNT(*) as c FROM lots WHERE status='available'")['c'],
        'reserved': db.fetchone("SELECT COUNT(*) as c FROM lots WHERE status='reserved'")['c'],
        'sold': db.fetchone("SELECT COUNT(*) as c FROM lots WHERE status='sold'")['c'],
        'orders': db.fetchone("SELECT COUNT(*) as c FROM orders")['c'],
        'pending_orders': db.fetchone("SELECT COUNT(*) as c FROM orders WHERE status='pending'")['c'],
        'approved_orders': db.fetchone("SELECT COUNT(*) as c FROM orders WHERE status='approved'")['c'],
        'rejected_orders': db.fetchone("SELECT COUNT(*) as c FROM orders WHERE status='rejected'")['c'],
        'buyback_requests': db.fetchone("SELECT COUNT(*) as c FROM buyback_requests")['c'],
        'pending_buyback': db.fetchone("SELECT COUNT(*) as c FROM buyback_requests WHERE status='pending'")['c'],
    }
    text = "📊 *Статистика:*\n\n"
    text += f"👥 Пользователей: {stats['users']}\n📦 Товаров всего: {stats['lots']}\n✅ В наличии: {stats['available']}\n🔄 Забронировано: {stats['reserved']}\n❌ Продано: {stats['sold']}\n📋 Заказов всего: {stats['orders']}\n⏳ Ожидающих: {stats['pending_orders']}\n✅ Подтвержденных: {stats['approved_orders']}\n❌ Отклоненных: {stats['rejected_orders']}\n💰 Заявок на выкуп всего: {stats['buyback_requests']}\n⏳ Ожидающих: {stats['pending_buyback']}\n"
    await edit_message_smart(query, text, reply_markup=admin_menu_keyboard(), parse_mode='Markdown')

async def view_users(update, context):
    query = update.callback_query
    users = db.fetchall("SELECT * FROM users ORDER BY last_active DESC LIMIT 50")
    if not users:
        await edit_message_smart(query, "👥 *Нет пользователей*", reply_markup=admin_menu_keyboard(), parse_mode='Markdown')
        return
    text = "👥 *Последние активные пользователи:*\n\n"
    for u in users:
        name = u['first_name'] + (f" {u['last_name']}" if u['last_name'] else '')
        username = f"@{u['username']}" if u['username'] else ''
        text += f"{name} {username}\n🆔 ID: {u['id']}\n⏰ Активен: {u['last_active'][:16]}\n\n"
    await edit_message_smart(query, text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 В меню", callback_data="admin_panel")]]), parse_mode='Markdown')

async def show_terms(update, context):
    query = update.callback_query
    terms_text = """📋 *ПОЛЬЗОВАТЕЛЬСКОЕ СОГЛАШЕНИЕ* ..."""
    await edit_message_smart(query, terms_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 В меню", callback_data="main_menu")]]), parse_mode='Markdown')

async def cancel_conversation(update, context):
    query = update.callback_query if update.callback_query else None
    for key in list(context.user_data.keys()):
        if any(key.startswith(prefix) for prefix in ['buyback', 'adding_lot', 'lot_', 'awaiting_screenshot']):
            del context.user_data[key]
    user_id = update.effective_user.id
    text = "❌ *Операция отменена*"
    if query:
        await edit_message_smart(query, text, reply_markup=main_menu_keyboard(user_id), parse_mode='Markdown')
    else:
        await update.message.reply_text(text, reply_markup=main_menu_keyboard(user_id), parse_mode='Markdown')
    return ConversationHandler.END

async def show_all_items(update, context):
    query = update.callback_query
    items = db.fetchall("SELECT * FROM lots WHERE status = 'available' ORDER BY created_at DESC LIMIT 20")
    if not items:
        await edit_message_smart(query, "📭 *Товаров пока нет*", reply_markup=shop_keyboard(), parse_mode='Markdown')
        return
    text = "🛍️ *Все товары:*\n\n"
    for item in items:
        emoji = CATEGORY_EMOJIS.get(item['category'], '📦')
        text += f"{emoji} *{item['name']}*\n💰 {format_price(item['price'])}₽\n📂 {CATEGORIES.get(item['category'], item['category'])}\n🆔 `{item['lot_id']}`\n────────\n"
    await edit_message_smart(query, text, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🛍️ В магазин", callback_data="shop")],
        [InlineKeyboardButton("🏠 В меню", callback_data="main_menu")]
    ]), parse_mode='Markdown')

async def show_popular_items(update, context):
    query = update.callback_query
    items = db.fetchall("SELECT * FROM lots WHERE status = 'available' ORDER BY views DESC LIMIT 10")
    if not items:
        await edit_message_smart(query, "📭 *Товаров пока нет*", reply_markup=shop_keyboard(), parse_mode='Markdown')
        return
    text = "⭐ *Популярные товары:*\n\n"
    for item in items:
        emoji = CATEGORY_EMOJIS.get(item['category'], '📦')
        text += f"{emoji} *{item['name']}*\n💰 {format_price(item['price'])}₽\n👀 Просмотров: {item['views']}\n🆔 `{item['lot_id']}`\n────────\n"
    await edit_message_smart(query, text, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🛍️ В магазин", callback_data="shop")],
        [InlineKeyboardButton("🏠 В меню", callback_data="main_menu")]
    ]), parse_mode='Markdown')

async def show_new_items(update, context):
    query = update.callback_query
    items = db.fetchall("SELECT * FROM lots WHERE status = 'available' ORDER BY created_at DESC LIMIT 10")
    if not items:
        await edit_message_smart(query, "📭 *Товаров пока нет*", reply_markup=shop_keyboard(), parse_mode='Markdown')
        return
    text = "💎 *Новинки:*\n\n"
    for item in items:
        emoji = CATEGORY_EMOJIS.get(item['category'], '📦')
        text += f"{emoji} *{item['name']}*\n💰 {format_price(item['price'])}₽\n📅 Добавлен: {item['created_at'][:10]}\n🆔 `{item['lot_id']}`\n────────\n"
    await edit_message_smart(query, text, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🛍️ В магазин", callback_data="shop")],
        [InlineKeyboardButton("🏠 В меню", callback_data="main_menu")]
    ]), parse_mode='Markdown')

# ========== ОБРАБОТКА СООБЩЕНИЙ (СКРИНШОТЫ И ПРОЧЕЕ) ==========
async def handle_text(update, context):
    if 'awaiting_screenshot' in context.user_data:
        await handle_screenshot(update, context)
        return
    await update.message.reply_text("👋 *Используйте кнопки меню!*", parse_mode='Markdown', reply_markup=main_menu_keyboard(update.effective_user.id))

async def handle_screenshot(update, context):
    if not update.message.photo:
        await update.message.reply_text("❌ *Отправьте скриншот как фото*", reply_markup=cancel_keyboard(), parse_mode='Markdown')
        return
    lot_id = context.user_data['awaiting_screenshot']
    lot = db.fetchone("SELECT * FROM lots WHERE lot_id = ?", (lot_id,))
    if not lot:
        await update.message.reply_text("❌ *Товар не найден*", reply_markup=main_menu_keyboard(update.effective_user.id), parse_mode='Markdown')
        del context.user_data['awaiting_screenshot']
        return
    user = update.effective_user
    photo = update.message.photo[-1]
    # Сохраняем скриншот локально
    local_screenshot = await download_photo(photo.file_id, context)
    cursor = db.execute('''INSERT INTO orders (lot_id, user_id, user_name, user_username, screenshot, status)
                           VALUES (?,?,?,?,?,?)''',
                        (lot_id, user.id, user.full_name, user.username, local_screenshot, 'pending'))
    order_id = cursor.lastrowid
    # Уведомление админам
    for admin_id in ADMIN_IDS:
        try:
            text = f"🛒 *НОВЫЙ ЗАКАЗ #{order_id}!*\n📱 {lot['name']}\n💰 {format_price(lot['price'])}₽\n👤 {user.full_name} (@{user.username or ''})"
            with open(STATIC_DIR / local_screenshot.split('/')[-1], 'rb') as f:
                await context.bot.send_photo(admin_id, photo=f, caption=text, parse_mode='Markdown', reply_markup=admin_order_action_keyboard(order_id))
        except Exception as e:
            logger.error(f"Ошибка отправки админу: {e}")
    balance = lot['price'] - DEPOSIT_FIXED
    await update.message.reply_text(
        f"✅ *Скриншот отправлен!*\n📋 Заказ #{order_id}\n💰 Остаток: {format_price(balance)}₽\n⏳ Ожидайте подтверждения",
        parse_mode='Markdown', reply_markup=order_keyboard(order_id))
    del context.user_data['awaiting_screenshot']

async def handle_photo(update, context):
    if 'awaiting_screenshot' in context.user_data:
        await handle_screenshot(update, context)
    else:
        await update.message.reply_text("📸 *Фото получено, но я не знаю, что с ним делать*", parse_mode='Markdown', reply_markup=main_menu_keyboard(update.effective_user.id))

# ========== ЗАПУСК ==========
def main():
    application = Application.builder().token(TOKEN).build()

    # Conversation handlers
    sell_conversation = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_buyback_conversation, pattern='^sell_device$')],
        states={
            DEVICE_MODEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_device_model)],
            DEVICE_CONDITION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_device_condition)],
            DEVICE_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_device_description)],
            DEVICE_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_device_location)],
            DEVICE_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_device_contact)],
            DEVICE_PHOTOS: [
                MessageHandler(filters.PHOTO, handle_device_photos),
                CallbackQueryHandler(finish_buyback_without_photos, pattern='^skip_photos$'),
                CallbackQueryHandler(finish_buyback, pattern='^finish_photos$')
            ],
        },
        fallbacks=[CallbackQueryHandler(cancel_conversation, pattern='^cancel$'), CommandHandler('cancel', cancel_conversation)]
    )
    application.add_handler(sell_conversation)

    add_lot_conversation = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_add_lot_conversation, pattern='^add_lot$')],
        states={
            LOT_CATEGORY: [CallbackQueryHandler(handle_lot_category, pattern='^add_cat_')],
            LOT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_lot_name)],
            LOT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_lot_price)],
            LOT_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_lot_description)],
            LOT_PHOTOS: [
                MessageHandler(filters.PHOTO, handle_lot_photos),
                CallbackQueryHandler(finish_add_lot, pattern='^finish_photos$')
            ],
        },
        fallbacks=[CallbackQueryHandler(cancel_conversation, pattern='^cancel$'), CommandHandler('cancel', cancel_conversation)]
    )
    application.add_handler(add_lot_conversation)

    search_conversation = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_search, pattern='^search$')],
        states={SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search)]},
        fallbacks=[CallbackQueryHandler(cancel_conversation, pattern='^cancel_search$'),
                   CallbackQueryHandler(cancel_conversation, pattern='^cancel$'),
                   CommandHandler('cancel', cancel_conversation)]
    )
    application.add_handler(search_conversation)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_photo))

    # Запуск Flask в отдельном потоке
    flask_thread = Thread(target=run_flask)
    flask_thread.start()
    logger.info("🚀 Бот и API запущены")
    application.run_polling()

if __name__ == "__main__":
    main()
