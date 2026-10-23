import logging
import sqlite3
from datetime import datetime
from typing import List, Optional, Tuple

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

# ========== КОНФИГУРАЦИЯ ==========
TOKEN = "8020809344:AAEPXzXO3BgOwsPbYhqIx9v01XaByv7fXw8"
ADMIN_IDS = [6392591727]
CHANNEL_ID = "-1003275553562"
DEPOSIT_FIXED = 500  # Фиксированная предоплата 500 рублей
MAX_PHOTOS = 10  # Максимальное количество фото на товар
BOT_USERNAME = "mizimarketbot"  # Имя бота для ссылок
# ========== НАСТРОЙКА ЛОГИРОВАНИЯ ==========
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
# ========== КАТЕГОРИИ ТОВАРОВ ==========
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
# ========== СТАТУСЫ ==========
STATUSES = {
    'available': {'text': '✅ АКТУАЛЬНО', 'emoji': '✅'},
    'reserved': {'text': '🔄 Забронирован', 'emoji': '🔄'},
    'sold': {'text': '❌ Продан', 'emoji': '❌'},
    'pending': {'text': '⏳ Ожидание', 'emoji': '⏳'},
    'approved': {'text': '✅ Подтвержден', 'emoji': '✅'},
    'rejected': {'text': '❌ Отклонен', 'emoji': '❌'}
}
# ========== СОСТОЯНИЯ ДЛЯ РАЗГОВОРА ==========
(
    DEVICE_MODEL, DEVICE_CONDITION, DEVICE_DESCRIPTION,
    DEVICE_LOCATION, DEVICE_CONTACT, DEVICE_PHOTOS,
    LOT_NAME, LOT_PRICE, LOT_DESCRIPTION, LOT_PHOTOS,
    LOT_CATEGORY, LOT_EDIT_FIELD, SEARCH
) = range(13)


# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
class Database:
    """Класс для работы с базой данных"""

    def __init__(self):
        self.conn = sqlite3.connect('apple_store.db', check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.init_db()

    def init_db(self):
        cursor = self.conn.cursor()
        # Таблица счетчиков
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS counters (
                name TEXT PRIMARY KEY,
                value INTEGER DEFAULT 0
            )
        ''')
        # Таблица товаров
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS lots (
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
            )
        ''')
        # Таблица заказов
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lot_id TEXT,
                user_id INTEGER,
                user_name TEXT,
                user_username TEXT,
                screenshot TEXT,
                status TEXT DEFAULT 'pending',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(lot_id) REFERENCES lots(lot_id)
            )
        ''')
        # Таблица заявок на выкуп
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS buyback_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                user_name TEXT,
                user_username TEXT,
                device_model TEXT NOT NULL,
                device_condition TEXT NOT NULL,
                description TEXT,
                photos TEXT,
                location TEXT,
                contact_info TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                estimated_price REAL,
                notes TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Таблица пользователей
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                is_admin BOOLEAN DEFAULT FALSE,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_active DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Таблица просмотров
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS views (
                user_id INTEGER,
                lot_id TEXT,
                viewed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id, lot_id)
            )
        ''')
        # Таблица избранного
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS favorites (
                user_id INTEGER,
                lot_id TEXT,
                added_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id, lot_id)
            )
        ''')
        # Индексы для оптимизации
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_lots_status ON lots(status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_lots_category ON lots(category)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_buyback_status ON buyback_requests(status)')
        cursor.execute("INSERT OR IGNORE INTO counters (name, value) VALUES ('lot_counter', 0)")

        self.conn.commit()
        logger.info("✅ База данных инициализирована")

    def get_next_lot_number(self) -> int:
        """Получает следующий номер для артикула"""
        cursor = self.conn.cursor()
        cursor.execute("SELECT value FROM counters WHERE name = 'lot_counter'")
        result = cursor.fetchone()
        current = result[0] if result else 0
        next_number = current + 1
        cursor.execute("UPDATE counters SET value = ? WHERE name = 'lot_counter'", (next_number,))
        self.conn.commit()
        return next_number

    def execute(self, query: str, params: tuple = ()):
        cursor = self.conn.cursor()
        cursor.execute(query, params)
        self.conn.commit()
        return cursor

    def fetchone(self, query: str, params: tuple = ()):
        cursor = self.conn.cursor()
        cursor.execute(query, params)
        result = cursor.fetchone()
        return dict(result) if result else None

    def fetchall(self, query: str, params: tuple = ()):
        cursor = self.conn.cursor()
        cursor.execute(query, params)
        results = cursor.fetchall()
        return [dict(row) for row in results]

    def close(self):
        self.conn.close()


db = Database()


def generate_lot_id() -> str:
    """Генерирует уникальный ID для лота"""
    lot_number = db.get_next_lot_number()
    return f"MIZI-MARKET {lot_number}"


def format_price(price: float) -> str:
    """Форматирует цену с разделителями"""
    return f"{price:,.0f}".replace(",", " ")


def validate_price(price_str: str) -> Tuple[bool, Optional[float]]:
    """Проверяет корректность цены"""
    try:
        price = float(price_str.replace(',', '.').replace(' ', ''))
        if price < 100:
            return False, None
        return True, price
    except ValueError:
        return False, None


def split_list(lst: List, n: int) -> List[List]:
    """Разделяет список на части по n элементов"""
    return [lst[i:i + n] for i in range(0, len(lst), n)]


def get_channel_tag(status: str) -> str:
    if status == 'available':
        return '✅ #АКТУАЛЬНО'
    elif status == 'reserved':
        return '🔄 #ЗАБРОНИРОВАН'
    elif status == 'sold':
        return '❌ #ПРОДАН'
    return ''


# Helper function to smart edit message
async def edit_message_smart(query, text, reply_markup=None, parse_mode=None):
    if query.message.photo:
        await query.edit_message_caption(caption=text, reply_markup=reply_markup, parse_mode=parse_mode)
    else:
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)


async def update_channel_caption(context: ContextTypes.DEFAULT_TYPE, lot: dict):
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
💬 @MaksutaMarce
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
def main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура главного меню"""
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


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура админ-панели"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить товар", callback_data="add_lot"),
         InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("📦 Управление товарами", callback_data="manage_lots"),
         InlineKeyboardButton("📋 Заказы", callback_data="view_orders")],
        [InlineKeyboardButton("💰 Заявки на выкуп", callback_data="view_buyback_requests"),
         InlineKeyboardButton("👥 Пользователи", callback_data="view_users")],
        [InlineKeyboardButton("🏠 В меню", callback_data="main_menu")]
    ])


def shop_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура магазина"""
    keyboard = []
    categories = list(CATEGORIES.items())
    # Создаем кнопки категорий в 2 колонки
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


def category_keyboard(category: str, page: int = 0, total_pages: int = 1) -> InlineKeyboardMarkup:
    """Клавиатура категории с пагинацией"""
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


def lot_keyboard(lot_id: str, status: str, user_id: int = None) -> InlineKeyboardMarkup:
    """Клавиатура для товара"""
    keyboard = []
    # Проверяем, есть ли товар в избранном
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


def order_keyboard(order_id: int) -> InlineKeyboardMarkup:
    """Клавиатура для заказа"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📞 Связаться", callback_data=f"contact_{order_id}"),
         InlineKeyboardButton("🔄 Статус", callback_data=f"status_{order_id}")],
        [InlineKeyboardButton("💬 Поддержка", url=f"https://t.me/mizirf")],
        [InlineKeyboardButton("📦 Мои заказы", callback_data="my_orders")]
    ])


def payment_keyboard(lot_id: str) -> InlineKeyboardMarkup:
    """Клавиатура оплаты"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Перевести 500₽", url=f"https://t.me/mizirf")],
        [InlineKeyboardButton("📸 Отправить скриншот", callback_data=f"pay_{lot_id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"cancel_payment_{lot_id}")]
    ])


def cancel_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура отмены"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Отменить", callback_data="cancel")]
    ])


def photos_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для загрузки фото"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Готово", callback_data="finish_photos"),
         InlineKeyboardButton("❌ Отмена", callback_data="cancel")]
    ])


def search_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура поиска"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Отмена поиска", callback_data="cancel_search")],
        [InlineKeyboardButton("🏠 В меню", callback_data="main_menu")]
    ])


def admin_order_action_keyboard(order_id: int) -> InlineKeyboardMarkup:
    """Клавиатура действий администратора для заказа"""
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
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    user_id = user.id

    # Сохраняем пользователя в БД
    db.execute('''
        INSERT OR IGNORE INTO users (id, username, first_name, last_name, is_admin)
        VALUES (?, ?, ?, ?, ?)
    ''', (user_id, user.username, user.first_name, user.last_name, user_id in ADMIN_IDS))
    db.execute('UPDATE users SET last_active = CURRENT_TIMESTAMP WHERE id = ?', (user_id,))

    # Проверяем наличие параметра (ID товара)
    if context.args:
        lot_id = context.args[0]
        lot = db.fetchone("SELECT * FROM lots WHERE lot_id = ?", (lot_id,))
        if lot:
            # Увеличиваем счетчик просмотров
            db.execute('UPDATE lots SET views = views + 1 WHERE lot_id = ?', (lot_id,))
            db.execute('''
                INSERT OR REPLACE INTO views (user_id, lot_id, viewed_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            ''', (user_id, lot_id))
            await show_lot(update, context, lot_id)
            return

    # Приветственное сообщение
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


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
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
📞 @MaksutaMarce
⏰ 10:00 - 20:00 (МСК)
    """
    await update.message.reply_text(help_text, parse_mode='Markdown',
                                    reply_markup=main_menu_keyboard(update.effective_user.id))


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /menu"""
    await start(update, context)


# ========== ОСНОВНЫЕ ОБРАБОТЧИКИ ==========
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик callback-запросов"""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    logger.info(f"Callback: {data} от пользователя {user_id}")

    # Обработка основных действий
    if data == "main_menu":
        await edit_message_smart(query, "🏠 *Главное меню:*", reply_markup=main_menu_keyboard(user_id),
                                 parse_mode='Markdown')
    elif data == "shop":
        await edit_message_smart(query, "🛍️ *Выберите категорию:*", reply_markup=shop_keyboard(), parse_mode='Markdown')
    elif data == "admin_panel":
        if user_id in ADMIN_IDS:
            await edit_message_smart(query, "⚙️ *Админ-панель:*", reply_markup=admin_menu_keyboard(),
                                     parse_mode='Markdown')
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
        await edit_message_smart(
            query,
            "📸 *Отправьте скриншот оплаты:*\n\n"
            "❗ *Важно:*\n"
            "• Скриншот должен быть четким\n"
            "• Должна быть видна сумма 500₽\n"
            "• Должны быть видны реквизиты\n\n"
            "⏳ *Подтверждение менеджером в течение 24 часов*",
            reply_markup=cancel_keyboard(),
            parse_mode='Markdown'
        )
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
        pass  # Ничего не делаем для кнопки-заглушки
    else:
        # Если callback не распознан, показываем главное меню
        await edit_message_smart(
            query,
            "❌ *Неизвестная команда*",
            reply_markup=main_menu_keyboard(user_id),
            parse_mode='Markdown'
        )


async def show_category(update: Update, context: ContextTypes.DEFAULT_TYPE, category: str, page: int = 0):
    """Показать товары категории"""
    query = update.callback_query
    user_id = query.from_user.id

    # Получаем товары категории с пагинацией
    limit = 10
    offset = page * limit
    items = db.fetchall('''
        SELECT * FROM lots
        WHERE category = ? AND status = 'available'
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
    ''', (category, limit, offset))

    total_items_result = db.fetchone('''
        SELECT COUNT(*) as count FROM lots
        WHERE category = ? AND status = 'available'
    ''', (category,))
    total_items = total_items_result['count'] if total_items_result else 0
    total_pages = (total_items + limit - 1) // limit if total_items > 0 else 1

    if not items:
        category_name = CATEGORIES.get(category, category)
        await edit_message_smart(
            query,
            f"📭 *В категории '{category_name}' пока нет товаров*\n\n"
            f"✨ *Скоро появятся новые устройства!*",
            reply_markup=shop_keyboard(),
            parse_mode='Markdown'
        )
        return

    # Формируем список товаров
    keyboard = []
    for item in items:
        emoji = CATEGORY_EMOJIS.get(item['category'], '📦')
        keyboard.append([
            InlineKeyboardButton(
                f"{emoji} {item['name']} - {format_price(item['price'])}₽",
                callback_data=f"lot_{item['lot_id']}"
            )
        ])

    # Добавляем пагинацию
    keyboard.append(category_keyboard(category, page, total_pages).inline_keyboard[0])
    keyboard.append([
        InlineKeyboardButton("🛍️ Все категории", callback_data="shop"),
        InlineKeyboardButton("🏠 В меню", callback_data="main_menu")
    ])

    category_name = CATEGORIES.get(category, category)
    await edit_message_smart(
        query,
        f"🛍️ *{category_name}* (страница {page + 1} из {total_pages}):\n\n"
        f"📊 *Найдено товаров:* {total_items}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


async def show_lot(update: Update, context: ContextTypes.DEFAULT_TYPE, lot_id: str):
    """Показать подробную информацию о товаре"""
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

    # Обновляем счетчик просмотров
    db.execute('UPDATE lots SET views = views + 1 WHERE lot_id = ?', (lot_id,))
    db.execute('''
        INSERT OR REPLACE INTO views (user_id, lot_id, viewed_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    ''', (user_id, lot_id))

    # Формируем текст
    category_name = CATEGORIES.get(lot['category'], lot['category'])
    status_info = STATUSES.get(lot['status'], {'text': lot['status'], 'emoji': '📦'})
    photos = lot['photos'].split(',') if lot['photos'] else []
    balance = lot['price'] - DEPOSIT_FIXED

    text = f"""
{status_info['emoji']} *{lot['name']}*

📋 *Характеристики:*
• 📂 Категория: {category_name}
• 💰 Цена: {format_price(lot['price'])}₽
• 🆔 Артикул: `{lot['lot_id']}`
• 👀 Просмотров: {lot['views']}
• 📅 Добавлен: {lot['created_at'][:10]}

📝 *Описание:*
{lot['description']}

🔐 *Условия покупки:*
• 💳 Предоплата: {DEPOSIT_FIXED}₽ (фиксированная)
• 💰 Остаток при получении: {format_price(balance)}₽
• 🚚 Доставка: бесплатно по России
• ⏰ Бронь на: 24 часов

⚠️ *Важно:*
❌Имеется недостаток товара: невозможно установить и использовать RuStore

{status_info['emoji']} *Статус:* {status_info['text']}
    """

    reply_markup = lot_keyboard(lot_id, lot['status'], user_id)

    if photos:
        try:
            if query:
                await query.message.delete()

            message = await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=photos[0],
                caption=text,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )

            if len(photos) > 1:
                media_group = [InputMediaPhoto(media=photo) for photo in photos[1:]]
                await context.bot.send_media_group(
                    chat_id=update.effective_chat.id,
                    media=media_group
                )
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


async def start_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, lot_id: str):
    """Начать процесс оплаты"""
    query = update.callback_query
    lot = db.fetchone("SELECT * FROM lots WHERE lot_id = ?", (lot_id,))
    if not lot:
        await query.answer("❌ Товар не найден", show_alert=True)
        return

    if lot['status'] != 'available':
        await query.answer("❌ Товар недоступен для бронирования", show_alert=True)
        return

    user = update.effective_user
    balance = lot['price'] - DEPOSIT_FIXED

    payment_text = f"""
💳 *ОФОРМЛЕНИЕ ЗАКАЗА*

📱 *Товар:* {lot['name']}
🆔 *Артикул:* `{lot['lot_id']}`
💰 *Цена:* {format_price(lot['price'])}₽

🔐 *Условия оплаты:*
• 💵 Предоплата: {DEPOSIT_FIXED}₽
• 💰 Остаток: {format_price(balance)}₽
• 🚚 Доставка: бесплатно
• ⏰ Бронь на 24 часа

⚠️ *Важно:*
❌Имеется недостаток товара: невозможно установить и использовать RuStore

🏦 *Реквизиты для оплаты:*
• Банк: Сбербанк
• Карта: `2202 2068 3661 5885`
• Получатель: МАКСИМ М.

📋 *Инструкция:*
1. Переведите {DEPOSIT_FIXED}₽ на указанные реквизиты
2. Сделайте четкий скриншот перевода
3. Нажмите кнопку ниже и отправьте скриншот
4. Ожидайте подтверждения менеджера

⏳ *Подтверждение в течение 1 часа*
    """

    await edit_message_smart(
        query,
        payment_text,
        reply_markup=payment_keyboard(lot_id),
        parse_mode='Markdown'
    )


async def show_user_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать заказы пользователя"""
    query = update.callback_query
    user_id = update.effective_user.id

    orders = db.fetchall('''
        SELECT o.*, l.name as product_name, l.price, l.lot_id
        FROM orders o
        JOIN lots l ON o.lot_id = l.lot_id
        WHERE o.user_id = ?
        ORDER BY o.created_at DESC
    ''', (user_id,))

    if not orders:
        await edit_message_smart(
            query,
            "📭 *У вас пока нет заказов*\n\n"
            "🛍️ *Как сделать заказ:*\n"
            "1. Выберите товар в магазине\n"
            "2. Оплатите предоплату 500₽\n"
            "3. Отправьте скриншот оплаты\n"
            "4. Получите подтверждение менеджера",
            reply_markup=shop_keyboard(),
            parse_mode='Markdown'
        )
        return

    text = "📦 *Ваши заказы:*\n\n"
    for order in orders:
        status_info = STATUSES.get(order['status'], {'text': order['status'], 'emoji': '📦'})
        text += f"{status_info['emoji']} *Заказ #{order['id']}*\n"
        text += f"📱 {order['product_name']}\n"
        text += f"💰 {format_price(order['price'])}₽\n"
        text += f"📅 {order['created_at'][:10]}\n"
        text += f"📊 Статус: {status_info['text']}\n\n"

    await edit_message_smart(
        query,
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛍️ В магазин", callback_data="shop")],
            [InlineKeyboardButton("🏠 В меню", callback_data="main_menu")]
        ]),
        parse_mode='Markdown'
    )


async def toggle_favorite(update: Update, context: ContextTypes.DEFAULT_TYPE, lot_id: str):
    """Добавить/удалить товар из избранного"""
    query = update.callback_query
    user_id = query.from_user.id

    # Проверяем, есть ли уже в избранном
    fav = db.fetchone("SELECT * FROM favorites WHERE user_id = ? AND lot_id = ?", (user_id, lot_id))
    if fav:
        # Удаляем из избранного
        db.execute("DELETE FROM favorites WHERE user_id = ? AND lot_id = ?", (user_id, lot_id))
        await query.answer("❌ Удалено из избранного", show_alert=True)
    else:
        # Добавляем в избранное
        db.execute("INSERT INTO favorites (user_id, lot_id) VALUES (?, ?)", (user_id, lot_id))
        await query.answer("⭐ Добавлено в избранное", show_alert=True)

    # Обновляем сообщение с товаром
    await show_lot(update, context, lot_id)


async def show_favorites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать избранные товары"""
    query = update.callback_query
    user_id = query.from_user.id

    favorites = db.fetchall('''
        SELECT l.* FROM lots l
        JOIN favorites f ON l.lot_id = f.lot_id
        WHERE f.user_id = ? AND l.status = 'available'
        ORDER BY f.added_at DESC
    ''', (user_id,))

    if not favorites:
        await edit_message_smart(
            query,
            "⭐ *У вас пока нет избранных товаров*\n\n"
            "✨ *Как добавить в избранное:*\n"
            "1. Откройте товар в магазине\n"
            "2. Нажмите кнопку ⭐\n"
            "3. Товар появится здесь\n\n"
            "⭐ *Быстрый доступ к любимым товарам!*",
            reply_markup=shop_keyboard(),
            parse_mode='Markdown'
        )
        return

    text = "⭐ *Избранные товары:*\n\n"
    for item in favorites:
        emoji = CATEGORY_EMOJIS.get(item['category'], '📦')
        text += f"{emoji} *{item['name']}*\n"
        text += f"💰 {format_price(item['price'])}₽\n"
        text += f"📂 {CATEGORIES.get(item['category'], item['category'])}\n"
        text += f"🆔 `{item['lot_id']}`\n"
        text += "────────────────────\n\n"

    await edit_message_smart(
        query,
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛍️ В магазин", callback_data="shop")],
            [InlineKeyboardButton("🏠 В меню", callback_data="main_menu")]
        ]),
        parse_mode='Markdown'
    )


# ========== ПОИСК ТОВАРОВ ==========
async def start_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начать поиск товаров"""
    query = update.callback_query
    await edit_message_smart(
        query,
        "🔍 *ПОИСК ТОВАРОВ*\n\n"
        "Введите название или ключевые слова для поиска:\n\n"
        "✨ *Примеры:*\n"
        "• iPhone 14\n"
        "• MacBook Air\n"
        "• Apple Watch\n"
        "• 256GB\n"
        "• Pro Max",
        reply_markup=search_keyboard(),
        parse_mode='Markdown'
    )
    return SEARCH


async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка поискового запроса"""
    search_query = update.message.text.strip()
    if not search_query or len(search_query) < 2:
        await update.message.reply_text(
            "❌ *Слишком короткий запрос*\n\n"
            "Введите минимум 2 символа для поиска.",
            parse_mode='Markdown',
            reply_markup=search_keyboard()
        )
        return SEARCH

    # Выполняем поиск в базе данных
    items = db.fetchall('''
        SELECT * FROM lots 
        WHERE (name LIKE ? OR description LIKE ?) 
        AND status = 'available'
        ORDER BY created_at DESC
        LIMIT 20
    ''', (f'%{search_query}%', f'%{search_query}%'))

    if not items:
        await update.message.reply_text(
            f"🔍 *По запросу \"{search_query}\" ничего не найдено*\n\n"
            "Попробуйте изменить запрос или посмотрите все товары в магазине.",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🛍️ В магазин", callback_data="shop")],
                [InlineKeyboardButton("🔍 Новый поиск", callback_data="search")],
                [InlineKeyboardButton("🏠 В меню", callback_data="main_menu")]
            ])
        )
        return ConversationHandler.END

    # Формируем список найденных товаров
    text = f"🔍 *Результаты поиска по запросу \"{search_query}\":*\n\n"
    keyboard = []

    for item in items:
        emoji = CATEGORY_EMOJIS.get(item['category'], '📦')
        keyboard.append([
            InlineKeyboardButton(
                f"{emoji} {item['name']} - {format_price(item['price'])}₽",
                callback_data=f"lot_{item['lot_id']}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton("🛍️ В магазин", callback_data="shop"),
        InlineKeyboardButton("🔍 Новый поиск", callback_data="search")
    ])
    keyboard.append([InlineKeyboardButton("🏠 В меню", callback_data="main_menu")])

    await update.message.reply_text(
        text,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ConversationHandler.END


# ========== ПРОДАЖА УСТРОЙСТВА ==========
async def start_buyback_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начать диалог по продаже устройства"""
    query = update.callback_query
    context.user_data['buyback'] = {}
    context.user_data['buyback_photos'] = []

    await edit_message_smart(
        query,
        "💰 *ПРОДАЖА УСТРОЙСТВА APPLE*\n\n"
        "✨ *Мы предлагаем:*\n"
        "• 💎 Максимальную цену на рынке\n"
        "• ⚡ Мгновенную оплату\n"
        "• 🚗 Бесплатный выезд\n"
        "• 🔒 Безопасную сделку\n\n"
        "📱 *Шаг 1 из 6*\n"
        "*Введите модель вашего устройства:*\n\n"
        "✨ *Примеры:*\n"
        "• iPhone 14 Pro Max 256GB\n"
        "• MacBook Air M2 2022\n"
        "• Apple Watch Series 9",
        reply_markup=cancel_keyboard(),
        parse_mode='Markdown'
    )
    return DEVICE_MODEL


async def handle_device_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка модели устройства"""
    context.user_data['buyback']['model'] = update.message.text
    await update.message.reply_text(
        "📊 *Шаг 2 из 6*\n"
        "*Опишите состояние устройства:*\n\n"
        "✨ *Примеры:*\n"
        "• Новое (в коробке, не использовалось)\n"
        "• Отличное (минимальные следы использования)\n"
        "• Хорошее (есть небольшие потертости)\n"
        "• Удовлетворительное (есть повреждения)\n"
        "• Требует ремонта",
        parse_mode='Markdown',
        reply_markup=cancel_keyboard()
    )
    return DEVICE_CONDITION


async def handle_device_condition(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка состояния устройства"""
    context.user_data['buyback']['condition'] = update.message.text
    await update.message.reply_text(
        "📝 *Шаг 3 из 6*\n"
        "*Дополнительные детали:*\n\n"
        "✨ *Что можно указать:*\n"
        "• Комплектация (наличие коробки, зарядки)\n"
        "• Срок использования\n"
        "• Проводился ли ремонт\n"
        "• Косметические дефекты\n"
        "• Особенности работы",
        parse_mode='Markdown',
        reply_markup=cancel_keyboard()
    )
    return DEVICE_DESCRIPTION


async def handle_device_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка описания устройства"""
    context.user_data['buyback']['description'] = update.message.text
    await update.message.reply_text(
        "📍 *Шаг 4 из 6*\n"
        "*Ваше местоположение:*\n\n"
        "✨ *Пример:*\n"
        "• Москва, м. Пушкинская\n"
        "• Санкт-Петербург, центр\n"
        "• Казань, Вахитовский район\n\n"
        "🚗 *Мы приезжаем к вам бесплатно!*",
        parse_mode='Markdown',
        reply_markup=cancel_keyboard()
    )
    return DEVICE_LOCATION


async def handle_device_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка местоположения"""
    context.user_data['buyback']['location'] = update.message.text
    await update.message.reply_text(
        "📞 *Шаг 5 из 6*\n"
        "*Контактные данные:*\n\n"
        "✨ *Пример:*\n"
        "• Телефон: +7 676 767-67-67\n"
        "• Telegram: @epstein \n\n"
        "❗ *На эти данные мы свяжемся с вами*",
        parse_mode='Markdown',
        reply_markup=cancel_keyboard()
    )
    return DEVICE_CONTACT


async def handle_device_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка контактов"""
    context.user_data['buyback']['contact'] = update.message.text
    await update.message.reply_text(
        "📸 *Шаг 6 из 6 (опционально)*\n"
        "*Отправьте фото устройства:*\n\n"
        "✨ *Можно отправить несколько фото:*\n"
        "• Общий вид\n"
        "• Экрана/дисплея\n"
        "• Углов и граней\n"
        "• Дефекты (если есть)\n\n"
        "📎 *Отправьте фото или нажмите «Пропустить»*",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭️ Пропустить", callback_data="skip_photos")],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel")]
        ])
    )
    return DEVICE_PHOTOS


async def handle_device_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка фото устройства"""
    if 'buyback_photos' not in context.user_data:
        context.user_data['buyback_photos'] = []

    if update.message.photo:
        photo = update.message.photo[-1]
        if len(context.user_data['buyback_photos']) >= MAX_PHOTOS:
            await update.message.reply_text(
                f"❌ *Достигнут максимум {MAX_PHOTOS} фото*\n\n"
                f"✅ *Нажмите «Готово» чтобы завершить*",
                parse_mode='Markdown',
                reply_markup=photos_keyboard()
            )
            return DEVICE_PHOTOS

        context.user_data['buyback_photos'].append(photo.file_id)
        count = len(context.user_data['buyback_photos'])

        await update.message.reply_text(
            f"✅ *Фото добавлено!* ({count}/{MAX_PHOTOS})\n\n"
            f"📸 *Отправьте еще фото или нажмите «Готово»*",
            parse_mode='Markdown',
            reply_markup=photos_keyboard()
        )
    return DEVICE_PHOTOS


async def finish_buyback_with_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await finish_buyback(update, context, with_photos=True)


async def finish_buyback_without_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await finish_buyback(update, context, with_photos=False)


async def finish_buyback(update: Update, context: ContextTypes.DEFAULT_TYPE, with_photos: bool = True):
    query = update.callback_query
    user = update.effective_user
    buyback_data = context.user_data.get('buyback', {})

    if not buyback_data:
        await edit_message_smart(
            query,
            "❌ *Ошибка создания заявки*",
            reply_markup=main_menu_keyboard(user.id),
            parse_mode='Markdown'
        )
        return ConversationHandler.END

    required_fields = ['model', 'condition', 'contact']
    for field in required_fields:
        if field not in buyback_data or not buyback_data[field]:
            await edit_message_smart(
                query,
                f"❌ *Ошибка: поле '{field}' обязательно для заполнения*",
                reply_markup=main_menu_keyboard(user.id),
                parse_mode='Markdown'
            )
            return ConversationHandler.END

    photos = ','.join(context.user_data.get('buyback_photos', [])) if with_photos else ''

    try:
        cursor = db.execute('''
            INSERT INTO buyback_requests
            (user_id, user_name, user_username, device_model, device_condition,
             description, photos, location, contact_info, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        ''', (
            user.id,
            user.full_name,
            user.username,
            buyback_data['model'],
            buyback_data['condition'],
            buyback_data.get('description', ''),
            photos,
            buyback_data.get('location', ''),
            buyback_data['contact']
        ))
        request_id = cursor.lastrowid

        for admin_id in ADMIN_IDS:
            try:
                text = f"""
💰 *НОВАЯ ЗАЯВКА НА ВЫКУП #{request_id}!*
👤 *Пользователь:* {user.full_name}
🆔 *ID:* {user.id}
📱 *Модель:* {buyback_data['model']}
📊 *Состояние:* {buyback_data['condition']}
📍 *Локация:* {buyback_data.get('location', 'Не указано')}
📞 *Контакты:* {buyback_data['contact']}
📅 *Время:* {datetime.now().strftime('%d.%m.%Y %H:%M')}
                """
                if photos:
                    photo_list = photos.split(',')
                    if photo_list:
                        try:
                            await context.bot.send_photo(
                                chat_id=admin_id,
                                photo=photo_list[0],
                                caption=text,
                                parse_mode='Markdown'
                            )
                        except:
                            await context.bot.send_message(
                                chat_id=admin_id,
                                text=text,
                                parse_mode='Markdown'
                            )
                    else:
                        await context.bot.send_message(
                            chat_id=admin_id,
                            text=text,
                            parse_mode='Markdown'
                        )
                else:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=text,
                        parse_mode='Markdown'
                    )
            except Exception as e:
                logger.error(f"Ошибка отправки админу {admin_id}: {e}")

        keys_to_delete = [key for key in context.user_data if key.startswith('buyback')]
        for key in keys_to_delete:
            del context.user_data[key]

        await edit_message_smart(
            query,
            "✅ *Заявка успешно отправлена!*\n\n"
            "✨ *Что дальше:*\n"
            "• ⏳ Мы свяжемся в течение 1 часа\n"
            "• 💎 Получите максимальную оценку\n"
            "• 🤝 Согласуем удобное время\n"
            "• 💰 Мгновенная оплата наличными\n\n"
            "📞 *Контакты:* @mizirf ",
            reply_markup=main_menu_keyboard(user.id),
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Ошибка сохранения заявки: {e}")
        await edit_message_smart(
            query,
            "❌ *Ошибка при сохранении заявки. Попробуйте позже.*",
            reply_markup=main_menu_keyboard(user.id),
            parse_mode='Markdown'
        )
    return ConversationHandler.END


# ========== АДМИН ФУНКЦИИ ==========
async def start_add_lot_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начать добавление товара"""
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

    await edit_message_smart(
        query,
        "➕ *ДОБАВЛЕНИЕ ТОВАРА*\n\n"
        "📂 *Шаг 1 из 5*\n"
        "*Выберите категорию:*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return LOT_CATEGORY


async def handle_lot_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора категории"""
    query = update.callback_query
    category = query.data.split("_")[2]
    context.user_data['adding_lot']['category'] = category

    await edit_message_smart(
        query,
        "📝 *Шаг 2 из 5*\n"
        "*Введите название товара:*\n\n"
        "✨ *Пример:* iPhone 14 Pro Max 256GB Space Black",
        reply_markup=cancel_keyboard(),
        parse_mode='Markdown'
    )
    return LOT_NAME


async def handle_lot_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка названия товара"""
    context.user_data['adding_lot']['name'] = update.message.text
    await update.message.reply_text(
        "💰 *Шаг 3 из 5*\n"
        "*Введите цену товара в рублях:*\n\n"
        "✨ *Пример:* 89990",
        parse_mode='Markdown',
        reply_markup=cancel_keyboard()
    )
    return LOT_PRICE


async def handle_lot_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка цены товара"""
    price_str = update.message.text
    valid, price = validate_price(price_str)

    if not valid:
        await update.message.reply_text(
            "❌ *Неверный формат цены*\n\n"
            "💰 *Введите цену цифрами:*\n"
            "• Пример: 89990\n"
            "• Минимум: 100 рублей",
            parse_mode='Markdown',
            reply_markup=cancel_keyboard()
        )
        return LOT_PRICE

    context.user_data['adding_lot']['price'] = price
    await update.message.reply_text(
        "📝 *Шаг 4 из 5*\n"
        "*Введите описание товара:*\n\n"
        "✨ *Что можно указать:*\n"
        "• Технические характеристики\n"
        "• Состояние и комплектацию\n"
        "• Особенности и преимущества\n"
        "• Гарантию и условия",
        parse_mode='Markdown',
        reply_markup=cancel_keyboard()
    )
    return LOT_DESCRIPTION


async def handle_lot_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка описания товара"""
    context.user_data['adding_lot']['description'] = update.message.text
    await update.message.reply_text(
        "📸 *Шаг 5 из 5*\n"
        "*Отправьте фото товара:*\n\n"
        "✨ *Можно отправить до 10 фото:*\n"
        "• Общий вид\n"
        "• Детали\n"
        "• Упаковку\n"
        "• Документы\n\n"
        "📎 *Отправьте первое фото:*",
        parse_mode='Markdown',
        reply_markup=photos_keyboard()
    )
    return LOT_PHOTOS


async def handle_lot_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка фото товара"""
    if 'lot_photos' not in context.user_data:
        context.user_data['lot_photos'] = []

    if update.message.photo:
        photo = update.message.photo[-1]
        if len(context.user_data['lot_photos']) >= MAX_PHOTOS:
            await update.message.reply_text(
                f"📸 *Максимум {MAX_PHOTOS} фото достигнут*\n\n"
                f"✅ *Нажмите «Готово» чтобы завершить*",
                parse_mode='Markdown',
                reply_markup=photos_keyboard()
            )
            return LOT_PHOTOS

        context.user_data['lot_photos'].append(photo.file_id)
        count = len(context.user_data['lot_photos'])

        if count >= MAX_PHOTOS:
            await update.message.reply_text(
                f"📸 *Максимум {MAX_PHOTOS} фото достигнут*\n\n"
                f"✅ *Нажмите «Готово» чтобы завершить*",
                parse_mode='Markdown',
                reply_markup=photos_keyboard()
            )
        else:
            await update.message.reply_text(
                f"✅ *Фото добавлено!* ({count}/{MAX_PHOTOS})\n\n"
                f"📸 *Отправьте еще фото или нажмите «Готово»*",
                parse_mode='Markdown',
                reply_markup=photos_keyboard()
            )
    return LOT_PHOTOS


async def finish_add_lot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершение добавления товара"""
    query = update.callback_query
    lot_data = context.user_data.get('adding_lot', {})
    photos = context.user_data.get('lot_photos', [])

    if not lot_data or not photos:
        await edit_message_smart(
            query,
            "❌ *Ошибка: не все данные заполнены*",
            reply_markup=admin_menu_keyboard(),
            parse_mode='Markdown'
        )
        for key in ['adding_lot', 'lot_photos']:
            if key in context.user_data:
                del context.user_data[key]
        return ConversationHandler.END

    # Сохраняем товар в БД
    try:
        lot_id = generate_lot_id()
        photos_str = ','.join(photos)
        category_name = CATEGORIES.get(lot_data['category'], lot_data['category'])

        cursor = db.execute('''
            INSERT INTO lots (lot_id, name, description, price, photos, category, status)
            VALUES (?, ?, ?, ?, ?, ?, 'available')
        ''', (
            lot_id,
            lot_data['name'],
            lot_data['description'],
            lot_data['price'],
            photos_str,
            lot_data['category']
        ))

        # Публикуем в канал
        try:
            balance = lot_data['price'] - DEPOSIT_FIXED
            caption = f"""
🌟 *НОВЫЙ ТОВАР!* 🌟

📱 *{lot_data['name']}*
📂 Категория: {category_name}
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
            bot_link = f"https://t.me/{BOT_USERNAME}?start={lot_id}"
            message = await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=photos[0],
                caption=caption,
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🛒 КУПИТЬ", url=bot_link)
                ]])
            )
            # Сохраняем ID сообщения в канале
            db.execute("UPDATE lots SET channel_message_id = ? WHERE lot_id = ?", (message.message_id, lot_id))
        except Exception as e:
            logger.error(f"Ошибка публикации в канале: {e}")

        # Очищаем данные
        for key in ['adding_lot', 'lot_photos']:
            if key in context.user_data:
                del context.user_data[key]

        await edit_message_smart(
            query,
            f"✅ *Товар успешно добавлен!*\n\n"
            f"📱 *Название:* {lot_data['name']}\n"
            f"📂 *Категория:* {category_name}\n"
            f"💰 *Цена:* {format_price(lot_data['price'])}₽\n"
            f"🆔 *Артикул:* `{lot_id}`\n"
            f"📸 *Фото:* {len(photos)} шт.\n\n"
            f"✨ *Товар опубликован в канале*",
            reply_markup=admin_menu_keyboard(),
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Ошибка добавления товара: {e}")
        await edit_message_smart(
            query,
            "❌ *Ошибка при добавлении товара. Попробуйте позже.*",
            reply_markup=admin_menu_keyboard(),
            parse_mode='Markdown'
        )
    return ConversationHandler.END


async def manage_lots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Управление товарами"""
    query = update.callback_query
    lots = db.fetchall('''
        SELECT * FROM lots
        ORDER BY created_at DESC
        LIMIT 20
    ''')

    if not lots:
        await edit_message_smart(
            query,
            "📭 *Товаров пока нет*",
            reply_markup=admin_menu_keyboard(),
            parse_mode='Markdown'
        )
        return

    keyboard = []
    for lot in lots:
        status_info = STATUSES.get(lot['status'], {'text': lot['status'], 'emoji': '📦'})
        keyboard.append([
            InlineKeyboardButton(
                f"{status_info['emoji']} {lot['name'][:30]} - {format_price(lot['price'])}₽",
                callback_data=f"admin_lot_{lot['lot_id']}"
            )
        ])

    keyboard.append([InlineKeyboardButton("🏠 В меню", callback_data="admin_panel")])

    await edit_message_smart(
        query,
        "📊 *Управление товарами:*\n\n"
        "📈 *Последние 20 товаров*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


async def admin_show_lot(update: Update, context: ContextTypes.DEFAULT_TYPE, lot_id: str):
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


async def set_lot_status(update: Update, context: ContextTypes.DEFAULT_TYPE, lot_id: str, new_status: str):
    query = update.callback_query
    db.execute("UPDATE lots SET status = ? WHERE lot_id = ?", (new_status, lot_id))
    lot = db.fetchone("SELECT * FROM lots WHERE lot_id = ?", (lot_id,))
    await update_channel_caption(context, lot)
    await query.answer(f"Статус изменен на {STATUSES.get(new_status, {'text': new_status})['text']}")
    await admin_show_lot(update, context, lot_id)


async def view_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Просмотр заказов"""
    query = update.callback_query
    orders = db.fetchall('''
        SELECT o.*, l.name as product_name, l.price, l.lot_id
        FROM orders o
        JOIN lots l ON o.lot_id = l.lot_id
        ORDER BY o.created_at DESC
        LIMIT 20
    ''')

    if not orders:
        await edit_message_smart(
            query,
            "📭 *Нет заказов*",
            reply_markup=admin_menu_keyboard(),
            parse_mode='Markdown'
        )
        return

    text = "📋 *Заказы:*\n\n"
    keyboard = []
    for order in orders:
        status_info = STATUSES.get(order['status'], {'text': order['status'], 'emoji': '📦'})
        text += f"{status_info['emoji']} *Заказ #{order['id']}*\n"
        text += f"📱 {order['product_name']}\n"
        text += f"👤 {order['user_name']}\n"
        text += f"📅 {order['created_at'][:16]}\n\n"
        keyboard.append([
            InlineKeyboardButton(f"#{order['id']} {status_info['text']}", callback_data=f"admin_order_{order['id']}")
        ])

    keyboard.append([InlineKeyboardButton("🔄 Обновить", callback_data="view_orders")])
    keyboard.append([InlineKeyboardButton("🏠 В меню", callback_data="admin_panel")])

    await edit_message_smart(
        query,
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


async def admin_show_order(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int):
    query = update.callback_query
    order = db.fetchone('''
        SELECT o.*, l.name as product_name, l.lot_id
        FROM orders o
        JOIN lots l ON o.lot_id = l.lot_id
        WHERE o.id = ?
    ''', (order_id,))

    if not order:
        await edit_message_smart(query, "❌ Заказ не найден", reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("← Назад", callback_data="view_orders")]]), parse_mode='Markdown')
        return

    status_info = STATUSES.get(order['status'], {'text': order['status'], 'emoji': '📦'})
    text = f"📋 *Заказ #{order_id}*\n\n"
    text += f"📱 Товар: {order['product_name']}\n"
    text += f"🆔 Артикул: {order['lot_id']}\n"
    text += f"💰 Цена: {format_price(order['price'])}₽\n"
    text += f"👤 Пользователь: {order['user_name']} @{order['user_username'] or ''}\n"
    text += f"ID пользователя: {order['user_id']}\n"
    text += f"📅 Дата: {order['created_at']}\n"
    text += f"📊 Статус: {status_info['text']}\n"

    keyboard = []
    if order['status'] == 'pending':
        keyboard.append([
            InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve_order_{order_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_order_{order_id}")
        ])

    keyboard.append([InlineKeyboardButton("← Назад", callback_data="view_orders")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    if order['screenshot']:
        await query.message.delete()
        await context.bot.send_photo(
            chat_id=query.message.chat_id,
            photo=order['screenshot'],
            caption=text,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    else:
        await edit_message_smart(query, text, reply_markup=reply_markup, parse_mode='Markdown')


async def admin_show_order_details(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int):
    """Показать детали заказа администратору"""
    query = update.callback_query
    order = db.fetchone('''
        SELECT o.*, l.name as product_name, l.lot_id, l.price
        FROM orders o
        JOIN lots l ON o.lot_id = l.lot_id
        WHERE o.id = ?
    ''', (order_id,))

    if not order:
        await query.answer("❌ Заказ не найден", show_alert=True)
        return

    status_info = STATUSES.get(order['status'], {'text': order['status'], 'emoji': '📦'})
    text = f"""
📋 *Детали заказа #{order_id}*

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

    await edit_message_smart(
        query,
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("← Назад", callback_data=f"admin_order_{order_id}")]
        ]),
        parse_mode='Markdown'
    )


async def approve_order(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int):
    query = update.callback_query
    order = db.fetchone("SELECT * FROM orders WHERE id = ?", (order_id,))

    if order and order['status'] == 'pending':
        db.execute("UPDATE orders SET status = 'approved' WHERE id = ?", (order_id,))
        # Меняем статус товара на "забронирован" только после подтверждения менеджера
        db.execute("UPDATE lots SET status = 'reserved' WHERE lot_id = ?", (order['lot_id'],))
        lot = db.fetchone("SELECT * FROM lots WHERE lot_id = ?", (order['lot_id'],))
        await update_channel_caption(context, lot)

        try:
            await context.bot.send_message(
                order['user_id'],
                f"✅ *Ваш заказ #{order_id} подтвержден!*\n\n"
                f"Товар забронирован для вас.\n"
                f"Свяжитесь с @mizirf для деталей доставки и оплаты остатка.",
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Ошибка уведомления пользователя: {e}")

    await query.answer("Заказ подтвержден")
    await view_orders(update, context)


async def reject_order(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int):
    query = update.callback_query
    order = db.fetchone("SELECT * FROM orders WHERE id = ?", (order_id,))

    if order and order['status'] == 'pending':
        db.execute("UPDATE orders SET status = 'rejected' WHERE id = ?", (order_id,))
        # Не меняем статус товара - он остается доступным, так как бронь не подтверждена

        try:
            await context.bot.send_message(
                order['user_id'],
                f"❌ *Ваш заказ #{order_id} отклонен.*\n\n"
                f"Причина: возможно, неверный скриншот оплаты.\n"
                f"Товар снова доступен для покупки.\n"
                f"Если это ошибка, свяжитесь с @mizirf.",
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Ошибка уведомления пользователя: {e}")

    await query.answer("Заказ отклонен")
    await view_orders(update, context)


async def admin_approve_order(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int):
    """Подтвердить бронь через кнопку в уведомлении"""
    query = update.callback_query
    user_id = query.from_user.id

    if user_id not in ADMIN_IDS:
        await query.answer("⛔ У вас нет прав для этого действия", show_alert=True)
        return

    order = db.fetchone("SELECT * FROM orders WHERE id = ?", (order_id,))

    if not order:
        await query.answer("❌ Заказ не найден", show_alert=True)
        return

    if order['status'] != 'pending':
        await query.answer(f"❌ Заказ уже обработан (статус: {order['status']})", show_alert=True)
        return

    # Меняем статус заказа на подтвержденный
    db.execute("UPDATE orders SET status = 'approved' WHERE id = ?", (order_id,))

    # Меняем статус товара на "забронирован"
    db.execute("UPDATE lots SET status = 'reserved' WHERE lot_id = ?", (order['lot_id'],))
    lot = db.fetchone("SELECT * FROM lots WHERE lot_id = ?", (order['lot_id'],))
    await update_channel_caption(context, lot)

    # Уведомляем пользователя
    try:
        await context.bot.send_message(
            order['user_id'],
            f"✅ *Ваш заказ #{order_id} подтвержден!*\n\n"
            f"Товар забронирован для вас.\n"
            f"Свяжитесь с @mizirf для деталей доставки и оплаты остатка.",
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Ошибка уведомления пользователя: {e}")

    # Обновляем сообщение с уведомлением
    await query.edit_message_caption(
        caption=query.message.caption + f"\n\n✅ *Бронь подтверждена администратором*",
        parse_mode='Markdown',
        reply_markup=None
    )

    await query.answer("✅ Бронь успешно подтверждена")


async def admin_reject_order(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int):
    """Отклонить бронь через кнопку в уведомлении"""
    query = update.callback_query
    user_id = query.from_user.id

    if user_id not in ADMIN_IDS:
        await query.answer("⛔ У вас нет прав для этого действия", show_alert=True)
        return

    order = db.fetchone("SELECT * FROM orders WHERE id = ?", (order_id,))

    if not order:
        await query.answer("❌ Заказ не найден", show_alert=True)
        return

    if order['status'] != 'pending':
        await query.answer(f"❌ Заказ уже обработан (статус: {order['status']})", show_alert=True)
        return

    # Меняем статус заказа на отклоненный
    db.execute("UPDATE orders SET status = 'rejected' WHERE id = ?", (order_id,))

    # Товар остается доступным
    # Не меняем статус товара - он остается доступным

    # Уведомляем пользователя
    try:
        await context.bot.send_message(
            order['user_id'],
            f"❌ *Ваш заказ #{order_id} отклонен.*\n\n"
            f"Причина: неверный скриншот оплаты.\n"
            f"Товар снова доступен для покупки.\n"
            f"Если это ошибка, свяжитесь с @mizirf.",
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Ошибка уведомления пользователя: {e}")

    # Обновляем сообщение с уведомлением
    await query.edit_message_caption(
        caption=query.message.caption + f"\n\n❌ *Бронь отклонена администратором*",
        parse_mode='Markdown',
        reply_markup=None
    )

    await query.answer("❌ Бронь отклонена")


async def admin_contact_order(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int):
    """Связаться с покупателем через кнопку в уведомлении"""
    query = update.callback_query
    user_id = query.from_user.id

    if user_id not in ADMIN_IDS:
        await query.answer("⛔ У вас нет прав для этого действия", show_alert=True)
        return

    order = db.fetchone("SELECT * FROM orders WHERE id = ?", (order_id,))

    if not order:
        await query.answer("❌ Заказ не найден", show_alert=True)
        return

    # Показываем информацию для связи с покупателем
    contact_info = f"""
📞 *Контакты покупателя для заказа #{order_id}:*

👤 *Имя:* {order['user_name']}
🆔 *ID:* {order['user_id']}
📱 *Username:* @{order['user_username'] or 'не указан'}

💬 *Для связи:*
• Написать в ЛС: [Написать сообщение](tg://user?id={order['user_id']})
• Упомянуть в чате: @{order['user_username'] or 'пользователь'}

📱 *Товар:* {order['lot_id']}
💰 *Сумма заказа:* {format_price(order['price'])}₽
    """

    await edit_message_smart(
        query,
        contact_info,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✉️ Написать сообщение", url=f"tg://user?id={order['user_id']}")],
            [InlineKeyboardButton("← Назад к заказу", callback_data=f"admin_approve_{order_id}")]
        ]),
        parse_mode='Markdown'
    )


async def view_buyback_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Просмотр заявок на выкуп"""
    query = update.callback_query
    requests = db.fetchall('''
        SELECT * FROM buyback_requests
        WHERE status = 'pending'
        ORDER BY created_at DESC
        LIMIT 20
    ''')

    if not requests:
        await edit_message_smart(
            query,
            "📭 *Нет заявок на рассмотрении*",
            reply_markup=admin_menu_keyboard(),
            parse_mode='Markdown'
        )
        return

    text = "💰 *Заявки на выкуп:*\n\n"
    for req in requests:
        text += f"🆔 *#{req['id']}*\n"
        text += f"📱 {req['device_model']}\n"
        text += f"👤 {req['user_name']}\n"
        text += f"📍 {req['location'] or 'Не указано'}\n"
        text += f"📅 {req['created_at'][:16]}\n"
        text += "────────────────────\n\n"

    await edit_message_smart(
        query,
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Обновить", callback_data="view_buyback_requests")],
            [InlineKeyboardButton("🏠 В меню", callback_data="admin_panel")]
        ]),
        parse_mode='Markdown'
    )


async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    text += f"👥 Пользователей: {stats['users']}\n"
    text += f"📦 Товаров всего: {stats['lots']}\n"
    text += f"✅ В наличии: {stats['available']}\n"
    text += f"🔄 Забронировано: {stats['reserved']}\n"
    text += f"❌ Продано: {stats['sold']}\n"
    text += f"📋 Заказов всего: {stats['orders']}\n"
    text += f"⏳ Ожидающих: {stats['pending_orders']}\n"
    text += f"✅ Подтвержденных: {stats['approved_orders']}\n"
    text += f"❌ Отклоненных: {stats['rejected_orders']}\n"
    text += f"💰 Заявок на выкуп всего: {stats['buyback_requests']}\n"
    text += f"⏳ Ожидающих: {stats['pending_buyback']}\n"

    await edit_message_smart(query, text, reply_markup=admin_menu_keyboard(), parse_mode='Markdown')


async def view_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    users = db.fetchall("SELECT * FROM users ORDER BY last_active DESC LIMIT 50")

    if not users:
        await edit_message_smart(query, "👥 *Нет пользователей*", reply_markup=admin_menu_keyboard(),
                                 parse_mode='Markdown')
        return

    text = "👥 *Последние активные пользователи (50):*\n\n"
    for u in users:
        name = u['first_name']
        if u['last_name']:
            name += f" {u['last_name']}"
        username = f"@{u['username']}" if u['username'] else ""
        text += f"{name} {username}\n🆔 ID: {u['id']}\n⏰ Активен: {u['last_active'][:16]}\n\n"

    await edit_message_smart(
        query,
        text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 В меню", callback_data="admin_panel")]]),
        parse_mode='Markdown'
    )


# ========== ОБРАБОТЧИКИ СООБЩЕНИЙ ==========
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текстовых сообщений"""
    user_id = update.effective_user.id
    if 'awaiting_screenshot' in context.user_data:
        await handle_screenshot(update, context)
        return

    await update.message.reply_text(
        "👋 *Я не понимаю текст. Используйте кнопки меню!*\n\n"
        "✨ *Что вы можете сделать:*\n"
        "• /start - Главное меню\n"
        "• /help - Помощь\n"
        "• /menu - Вернуться в меню",
        parse_mode='Markdown',
        reply_markup=main_menu_keyboard(user_id)
    )


async def handle_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка скриншота оплаты"""
    if not update.message.photo:
        await update.message.reply_text(
            "❌ *Отправьте скриншот в виде фото!*\n\n"
            "📸 *Сделайте скриншот и отправьте его как фото*",
            parse_mode='Markdown',
            reply_markup=cancel_keyboard()
        )
        return

    lot_id = context.user_data['awaiting_screenshot']
    lot = db.fetchone("SELECT * FROM lots WHERE lot_id = ?", (lot_id,))
    if not lot:
        await update.message.reply_text(
            "❌ *Товар не найден*",
            parse_mode='Markdown',
            reply_markup=main_menu_keyboard(update.effective_user.id)
        )
        del context.user_data['awaiting_screenshot']
        return

    user = update.effective_user
    photo = update.message.photo[-1]

    # Сохраняем заказ
    try:
        cursor = db.execute('''
            INSERT INTO orders (lot_id, user_id, user_name, user_username, screenshot, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
        ''', (lot_id, user.id, user.full_name, user.username, photo.file_id))
        order_id = cursor.lastrowid

        # НЕ меняем статус товара на "забронирован" - только после подтверждения менеджера
        # Товар остается доступным до подтверждения заказа

        # Уведомляем администраторов с кнопками для быстрого подтверждения
        for admin_id in ADMIN_IDS:
            try:
                text = f"""
🛒 *НОВЫЙ ЗАКАЗ #{order_id}!*

📱 *Товар:* {lot['name']}
🆔 *Артикул:* `{lot_id}`
💰 *Цена:* {format_price(lot['price'])}₽
💵 *Предоплата:* {DEPOSIT_FIXED}₽

👤 *Покупатель:*
• Имя: {user.full_name}
• ID: {user.id}
• Username: @{user.username or 'не указан'}

⏰ *Время:* {datetime.now().strftime('%d.%m.%Y %H:%M')}
                """

                await context.bot.send_photo(
                    chat_id=admin_id,
                    photo=photo.file_id,
                    caption=text,
                    parse_mode='Markdown',
                    reply_markup=admin_order_action_keyboard(order_id)
                )
            except Exception as e:
                logger.error(f"Ошибка отправки админу {admin_id}: {e}")

        # Отправляем подтверждение пользователю
        balance = lot['price'] - DEPOSIT_FIXED
        await update.message.reply_text(
            f"✅ *Скриншот успешно отправлен!*\n\n"
            f"📋 *Детали заказа:*\n"
            f"🆔 Номер: #{order_id}\n"
            f"📱 Товар: {lot['name']}\n"
            f"💰 Стоимость: {format_price(lot['price'])}₽\n"
            f"💵 Внесено: {DEPOSIT_FIXED}₽\n"
            f"💳 Остаток: {format_price(balance)}₽\n\n"
            f"⏳ *Ожидайте подтверждения менеджера (в течение 1 часа)*\n"
            f"📞 @mizirf",
            parse_mode='Markdown',
            reply_markup=order_keyboard(order_id)
        )
    except Exception as e:
        logger.error(f"Ошибка сохранения заказа: {e}")
        await update.message.reply_text(
            "❌ *Ошибка при сохранении заказа. Попробуйте позже.*",
            parse_mode='Markdown',
            reply_markup=main_menu_keyboard(user.id)
        )

    del context.user_data['awaiting_screenshot']


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка фото"""
    if 'awaiting_screenshot' in context.user_data:
        await handle_screenshot(update, context)
        return

    user_id = update.effective_user.id
    await update.message.reply_text(
        "📸 *Фото получено!*\n\n"
        "✨ *Но я не знаю, что с ним делать.*\n"
        "Используйте кнопки меню для действий.",
        parse_mode='Markdown',
        reply_markup=main_menu_keyboard(user_id)
    )


# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
async def show_terms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать правила"""
    query = update.callback_query
    terms_text = """
📋 *ПОЛЬЗОВАТЕЛЬСКОЕ СОГЛАШЕНИЕ*

✨ *Добро пожаловать в MIZI MARKET!*

*1. Основные положения:*
• Мы продаем и выкупаем технику Apple
• Все товары проходят проверку
• Гарантия на все проданные устройства

*2. Процесс покупки:*
• Бронь по предоплате 500₽
• Остаток оплачивается при получении
• Доставка бесплатно по России
• Срок бронирования - 24 часа

*3. Процесс продажи:*
• Бесплатная оценка устройства
• Мгновенная оплата наличными
• Бесплатный выезд специалиста
• Безопасная сделка

*4. Гарантии:*
• Конфиденциальность данных
• Защита платежей
• Поддержка 24/7
• Гарантия от 3 месяцев

*5. Контакты:*
• Поддержка: @mizirf
• Время работы: 10:00-22:00 (МСК)

⚠️ *Важно:*
❌Имеется недостаток товара: невозможно установить и использовать RuStore

💎 *Спасибо, что выбираете MIZI MARKET!*
    """
    await edit_message_smart(
        query,
        terms_text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 В меню", callback_data="main_menu")]
        ]),
        parse_mode='Markdown'
    )


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена текущей операции"""
    query = update.callback_query if update.callback_query else None
    # Очищаем все данные пользователя
    keys_to_delete = [key for key in context.user_data if
                      key.startswith('buyback') or key.startswith('adding_lot') or key.startswith(
                          'lot_') or key == 'awaiting_screenshot']
    for key in keys_to_delete:
        del context.user_data[key]

    user_id = update.effective_user.id
    text = "❌ *Операция отменена*\n\n🏠 *Возврат в главное меню*"

    if query:
        await edit_message_smart(query, text, reply_markup=main_menu_keyboard(user_id), parse_mode='Markdown')
    else:
        await update.message.reply_text(text, parse_mode='Markdown', reply_markup=main_menu_keyboard(user_id))
    return ConversationHandler.END


async def show_all_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать все товары"""
    query = update.callback_query
    items = db.fetchall('''
        SELECT * FROM lots
        WHERE status = 'available'
        ORDER BY created_at DESC
        LIMIT 20
    ''')

    if not items:
        await edit_message_smart(
            query,
            "📭 *Товаров пока нет*\n\n"
            "✨ *Скоро появятся новые устройства!*",
            reply_markup=shop_keyboard(),
            parse_mode='Markdown'
        )
        return

    text = "🛍️ *Все товары:*\n\n"
    for item in items:
        emoji = CATEGORY_EMOJIS.get(item['category'], '📦')
        text += f"{emoji} *{item['name']}*\n"
        text += f"💰 {format_price(item['price'])}₽\n"
        text += f"📂 {CATEGORIES.get(item['category'], item['category'])}\n"
        text += f"🆔 `{item['lot_id']}`\n"
        text += "────────────────────\n\n"

    await edit_message_smart(
        query,
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛍️ В магазин", callback_data="shop")],
            [InlineKeyboardButton("🏠 В меню", callback_data="main_menu")]
        ]),
        parse_mode='Markdown'
    )


async def show_popular_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать популярные товары"""
    query = update.callback_query
    items = db.fetchall('''
        SELECT * FROM lots
        WHERE status = 'available'
        ORDER BY views DESC
        LIMIT 10
    ''')

    if not items:
        await edit_message_smart(
            query,
            "📭 *Товаров пока нет*",
            reply_markup=shop_keyboard(),
            parse_mode='Markdown'
        )
        return

    text = "⭐ *Популярные товары:*\n\n"
    for item in items:
        emoji = CATEGORY_EMOJIS.get(item['category'], '📦')
        text += f"{emoji} *{item['name']}*\n"
        text += f"💰 {format_price(item['price'])}₽\n"
        text += f"👀 Просмотров: {item['views']}\n"
        text += f"🆔 `{item['lot_id']}`\n"
        text += "────────────────────\n\n"

    await edit_message_smart(
        query,
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛍️ В магазин", callback_data="shop")],
            [InlineKeyboardButton("🏠 В меню", callback_data="main_menu")]
        ]),
        parse_mode='Markdown'
    )


async def show_new_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать новинки"""
    query = update.callback_query
    items = db.fetchall('''
        SELECT * FROM lots
        WHERE status = 'available'
        ORDER BY created_at DESC
        LIMIT 10
    ''')

    if not items:
        await edit_message_smart(
            query,
            "📭 *Товаров пока нет*",
            reply_markup=shop_keyboard(),
            parse_mode='Markdown'
        )
        return

    text = "💎 *Новинки:*\n\n"
    for item in items:
        emoji = CATEGORY_EMOJIS.get(item['category'], '📦')
        text += f"{emoji} *{item['name']}*\n"
        text += f"💰 {format_price(item['price'])}₽\n"
        text += f"📅 Добавлен: {item['created_at'][:10]}\n"
        text += f"🆔 `{item['lot_id']}`\n"
        text += "────────────────────\n\n"

    await edit_message_smart(
        query,
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛍️ В магазин", callback_data="shop")],
            [InlineKeyboardButton("🏠 В меню", callback_data="main_menu")]
        ]),
        parse_mode='Markdown'
    )


# ========== СОЗДАНИЕ ПРИЛОЖЕНИЯ ==========
def main():
    """Основная функция запуска бота"""
    try:
        # Создаем приложение
        application = Application.builder().token(TOKEN).build()

        # ========== HANDLERS ==========
        # Команды
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("menu", menu_command))

        # Conversation Handlers
        # Обработчик продажи устройства
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
                    CallbackQueryHandler(finish_buyback_with_photos, pattern='^finish_photos$')
                ],
            },
            fallbacks=[
                CallbackQueryHandler(cancel_conversation, pattern='^cancel$'),
                CommandHandler('cancel', cancel_conversation)
            ]
        )
        application.add_handler(sell_conversation)

        # Обработчик добавления товара (только для админов)
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
            fallbacks=[
                CallbackQueryHandler(cancel_conversation, pattern='^cancel$'),
                CommandHandler('cancel', cancel_conversation)
            ]
        )
        application.add_handler(add_lot_conversation)

        # Обработчик поиска товаров
        search_conversation = ConversationHandler(
            entry_points=[CallbackQueryHandler(start_search, pattern='^search$')],
            states={
                SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search)]
            },
            fallbacks=[
                CallbackQueryHandler(cancel_conversation, pattern='^cancel_search$'),
                CallbackQueryHandler(cancel_conversation, pattern='^cancel$'),
                CommandHandler('cancel', cancel_conversation)
            ]
        )
        application.add_handler(search_conversation)

        # Callback Query Handler
        application.add_handler(CallbackQueryHandler(handle_callback))

        # Message Handlers
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
        application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_photo))

        # ========== ЗАПУСК БОТА ==========
        logger.info("✨ Бот MIZI MARKET запущен!")
        print("=" * 50)
        print("🤖 Бот успешно запущен!")
        print(f"👤 Администраторы: {ADMIN_IDS}")
        print(f"📢 Канал: {CHANNEL_ID}")
        print("=" * 50)

        application.run_polling()
    except Exception as e:
        logger.error(f"Ошибка запуска бота: {e}", exc_info=True)
        print(f"❌ Ошибка запуска бота: {e}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
