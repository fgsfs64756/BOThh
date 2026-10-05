import telebot
from telebot import types
import time
import threading
import sqlite3
from datetime import datetime, timedelta
import logging
import warnings
import os
import random
import string
import re
import io
import requests

# Опционально: Excel поддержка
try:
    import openpyxl

    EXCEL_SUPPORT = True
except ImportError:
    EXCEL_SUPPORT = False

# Папка data всегда рядом с файлом бота
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

# ========== НАСТРОЙКИ ==========
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
telebot.logger.setLevel(logging.ERROR)
warnings.filterwarnings("ignore")

TOKEN = "8535360963:AAE4zzqRbpgQaO8o_U42f0zz7NcPna5lEbA"
bot = telebot.TeleBot(TOKEN)

# ========== ПРОВЕРКА ПОДПИСКИ НА КАНАЛ ==========
# Числовой ID приватного канала (узнать: добавить бота в канал как админа, отправить любое сообщение в канал, посмотреть update — там будет chat.id вида -100XXXXXXXXXX)
# ВАЖНО: замените значение ниже на реальный числовой ID вашего канала!
REQUIRED_CHANNEL_ID = -1003637846646  # <-- ЗАМЕНИТЕ НА РЕАЛЬНЫЙ ID КАНАЛА
REQUIRED_CHANNEL_INVITE = "https://t.me/+YonZv7YS55MzODIy"  # инвайт-ссылка для кнопки
REQUIRED_CHANNEL = REQUIRED_CHANNEL_INVITE  # оставлено для обратной совместимости
MAIN_ADMIN_ID = 7986575625
# ========== НАСТРОЙКИ API ==========
API_TOKEN = "6501852546:QkBPoZn0"  # ЗАМЕНИТЕ на ваш реальный токен от @LeakOSINT_bot (/api)
API_URL = "https://leakosintapi.com/"
API_ENABLED = True  # Включить/выключить API поиск
API_DEFAULT_LIMIT = 100
API_DEFAULT_LANG = "ru"
# BigBase (рядом с LeakOSINT — нужно до init_database для начального заполнения БД токенов)
BIGBASE_TOKEN = "HOdPZOYMor8PcCjLGEaQeQ95-CfYgX6m"
BIGBASE_URL = "https://bigbase.top/api/"
BIGBASE_ENABLED = True
# Список доступных прав
ALL_PERMISSIONS = {
    "admin_panel":     "👑 Админ-панель (/admin_panel)",
    "broadcast":       "📢 Рассылка (/broadcast)",
    "support":         "🆘 Поддержка (/reply, /supportlist, /endchat)",
    "users":           "👤 Управление пользователями (/moderation)",
    "stats":           "📊 Статистика (/stats)",
    "files":           "📁 Работа с файлами (/files)",
    "texts":           "📝 Редактирование текстов (/texts)",
    "secret":          "🔐 Засекречивание записей (/secret_list)",
    "block_query":     "🚫 Блокировка запросов (/block_query, /blocked_queries)",
    "api_manager":     "🌐 Менеджер API",
}

checking_subscription = {}

# ========== АНТИФЛУД СИСТЕМА ==========
user_messages = {}
user_warnings = {}
flood_settings = {"max_messages": 5, "period_seconds": 3, "warnings_before_ban": 3}

# ========== ФИЛЬТР УСТАРЕВШИХ СООБЩЕНИЙ ==========
# Время запуска бота — все сообщения с date раньше этого момента игнорируются
BOT_START_TIME = int(time.time())

def is_old_message(message) -> bool:
    """Возвращает True если сообщение было отправлено ДО запуска бота."""
    return message.date < BOT_START_TIME

# ========== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ==========
waiting_for_search = {}
user_api_choice = {}  # {user_id: "leakosint" | "bigbase" | "all"}
admin_broadcast = {}
admin_create_file = {}
support_mode = {}  # {admin_id: user_id}
user_support_request = {}  # {user_id: True}
live_chat = {}  # {user_id: admin_id} — двусторонний живой чат
admin_action = {}
notified_support_users = {}  # для антиспама
last_search_query = {}  # {user_id: query} — последний запрос поиска для логирования засекречивания
# Для хранения результатов поиска с навигацией
user_search_results = {}  # {user_id: {"results": [...], "current_page": 0, "query": "...", "total_pages": 0}}

# ========== РЕЖИМ ПРИЗРАКА ==========
# ghost_mode: {admin_id: target_user_id} — какой админ следит за каким юзером
ghost_mode = {}
# ghost_reverse: {target_user_id: admin_id} — обратный индекс
ghost_reverse = {}

# ── Monkey-patch bot.send_message для перехвата сообщений призраком ──────────
_original_send_message = bot.send_message

def _ghost_send_message(chat_id, text, *args, **kwargs):
    """Обёртка: если chat_id — цель призрака, дублируем сообщение боту → тому."""
    result = _original_send_message(chat_id, text, *args, **kwargs)
    try:
        uid = int(chat_id)
        if uid in ghost_reverse:
            admin_id = ghost_reverse[uid]
            # Не дублируем служебные сообщения самого призрака
            preview = (text or "")[:300]
            _original_send_message(
                admin_id,
                f"👻 *[ПРИЗРАК]* бот → `{uid}`:\n{preview}",
                parse_mode="Markdown"
            )
    except Exception:
        pass
    return result

bot.send_message = _ghost_send_message

# ========== БЛОКИРОВКА ЗАПРОСОВ ==========
# Состояние диалога добавления блокировки: {admin_id: {"step": ..., "pattern": ..., "mode": ...}}
admin_block_query_state = {}


def get_query_block(query: str):
    """Проверяет, заблокирован ли запрос. Возвращает (custom_text, block_id) или (None, None)."""
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute("SELECT id, query_pattern, custom_text, match_mode FROM query_blocks")
        rows = cursor.fetchall()
        conn.close()
        q_lower = query.strip().lower()
        for row_id, pattern, custom_text, mode in rows:
            p_lower = pattern.strip().lower()
            if mode == "exact":
                if q_lower == p_lower:
                    return custom_text, row_id
            else:  # contains
                if p_lower in q_lower:
                    return custom_text, row_id
        return None, None
    except Exception as e:
        print(f"[query_block] Ошибка проверки: {e}")
        return None, None


def add_query_block(pattern: str, custom_text: str, admin_id: int, mode: str = "contains"):
    """Добавляет блокировку запроса."""
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO query_blocks (query_pattern, custom_text, created_by, match_mode) VALUES (?,?,?,?)",
            (pattern.strip(), custom_text.strip(), admin_id, mode),
        )
        conn.commit()
        new_id = cursor.lastrowid
        conn.close()
        return new_id
    except Exception as e:
        print(f"[query_block] Ошибка добавления: {e}")
        return None


def delete_query_block(block_id: int):
    """Удаляет блокировку по ID."""
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute("DELETE FROM query_blocks WHERE id = ?", (block_id,))
        conn.commit()
        deleted = cursor.rowcount
        conn.close()
        return deleted > 0
    except Exception as e:
        print(f"[query_block] Ошибка удаления: {e}")
        return False


def list_query_blocks():
    """Возвращает все блокировки: список (id, pattern, custom_text, mode, created_at, created_by)."""
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, query_pattern, custom_text, match_mode, created_at, created_by FROM query_blocks ORDER BY id DESC"
        )
        rows = cursor.fetchall()
        conn.close()
        return rows
    except Exception as e:
        print(f"[query_block] Ошибка списка: {e}")
        return []

# ========== ФУНКЦИИ ДЛЯ РЕДАКТИРУЕМЫХ ТЕКСТОВ ==========
def get_default_start_text():
    return """╔════════════════════════════╗
║      👤 ЛИЧНЫЙ КАБИНЕТ     ║
╠════════════════════════════╣
║ 🆔 ID: {user_id}
║ 👋 Привет, {first_name}!
║ 📱 Username: @{username}
║ 📅 Регистрация: {reg_date}
╠════════════════════════════╣
║      💳 ПОДПИСКА           ║
╠════════════════════════════╣
║ {status_text}
╠════════════════════════════╣
║      🎫 ТРИПЫ              ║
╠════════════════════════════╣
║ 💰 Баланс: {trips} трипов
╠════════════════════════════╣
║      👥 РЕФЕРАЛЫ           ║
╠════════════════════════════╣
║ ├ Всего в боте: {total_users}
║ └ Ваши рефералы: 0
╚════════════════════════════╝

Выберите действие в меню ниже:"""


def get_default_help_text():
    return """📚 ДОСТУПНЫЕ КОМАНДЫ

👤 Для всех пользователей:
/start - запустить бота и показать меню
/help - показать это сообщение
/myhistory - показать историю поиска
/code XXXXX - активировать код доступа к файлам
/tripcode XXXXX - активировать код на трипы

🎫 Трипы — внутренняя валюта:
• 1 трип = 1 поиск по одному API
• 2 трипа = поиск по двум API сразу
• Трипы начисляются по коду (/tripcode)
• Без подписки и трипов — поиск недоступен

🔍 Поиск информации:
1. Нажми кнопку 🔍 Найти инфу
2. Введи номер телефона или ФИО
3. Получи информацию (если есть в базе)

💎 Подписка:
• Кнопка 💳 Оформить подписку - выбор тарифа"""


def get_default_history_text():
    return "📜 ТВОЯ ИСТОРИЯ ПОИСКА\n\n"


def get_default_custom_button_label():
    return "📌 Дополнительно"


def get_default_custom_button_text():
    return "ℹ️ Здесь будет текст вашей кнопки.\n\nОтредактируйте его через /texts → custom_button"


def get_bot_text(key, **kwargs):
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute("SELECT text, photo_id FROM bot_texts WHERE key = ?", (key,))
        result = cursor.fetchone()
        conn.close()

        if result:
            text = result[0] or ""
            if kwargs:
                try:
                    text = text.format(**kwargs)
                except:
                    pass
            return text
        return f"Текст для {key} не найден"
    except Exception as e:
        print(f"Ошибка получения текста {key}: {e}")
        return f"Ошибка загрузки текста"


def get_bot_text_full(key, **kwargs):
    """Возвращает (text, photo_id) — для отправки с возможным фото."""
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute("SELECT text, photo_id FROM bot_texts WHERE key = ?", (key,))
        result = cursor.fetchone()
        conn.close()
        if result:
            text = result[0] or ""
            photo_id = result[1]
            if kwargs:
                try:
                    text = text.format(**kwargs)
                except:
                    pass
            return text, photo_id
        return f"Текст для {key} не найден", None
    except Exception as e:
        print(f"Ошибка получения текста {key}: {e}")
        return "Ошибка загрузки текста", None


def send_bot_text(bot_instance, chat_id, key, reply_markup=None, parse_mode="HTML", **kwargs):
    """Отправляет текст из bot_texts, если есть фото — с фото."""
    text, photo_id = get_bot_text_full(key, **kwargs)
    if photo_id:
        bot_instance.send_photo(
            chat_id, photo_id,
            caption=text if text else None,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    else:
        bot_instance.send_message(
            chat_id, text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )


def update_bot_text(admin_id, key, new_text, photo_id=None):
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO bot_texts (key, text, photo_id, updated_by, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
            (key, new_text, photo_id, admin_id),
        )
        conn.commit()
        conn.close()
        return True, "Текст обновлен"
    except Exception as e:
        return False, str(e)


def list_bot_texts():
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute(
            "SELECT key, text, updated_by, updated_at, photo_id FROM bot_texts ORDER BY key"
        )
        texts = cursor.fetchall()
        conn.close()
        return texts
    except Exception as e:
        print(f"Ошибка получения списка текстов: {e}")
        return []


def md_escape(text: str) -> str:
    """Экранирует спецсимволы Markdown v1 для Telegram."""
    if not text:
        return text
    for ch in ['_', '*', '`', '[']:
        text = text.replace(ch, f'\\{ch}')
    return text


def init_database():
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            subscription_status TEXT DEFAULT 'inactive',
            subscription_type TEXT,
            subscription_start DATE,
            subscription_end DATE,
            banned INTEGER DEFAULT 0,
            ban_reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    try:
        cursor.execute("ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0")
    except:
        pass

    try:
        cursor.execute("ALTER TABLE users ADD COLUMN ban_reason TEXT")
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN banned_at TEXT")
        except:
            pass
    except:
        pass

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS search_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            query TEXT,
            found INTEGER,
            search_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            api_source TEXT DEFAULT NULL
        )
    """)
    try:
        cursor.execute("ALTER TABLE search_history ADD COLUMN api_source TEXT DEFAULT NULL")
    except:
        pass

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS admin_permissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            granted_by INTEGER,
            granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            is_active INTEGER DEFAULT 1
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS admin_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            created_by INTEGER,
            used_by INTEGER,
            used_at TIMESTAMP,
            duration TEXT DEFAULT 'day',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    try:
        cursor.execute('ALTER TABLE admin_codes ADD COLUMN duration TEXT DEFAULT "day"')
    except:
        pass
    try:
        cursor.execute(
            'ALTER TABLE admin_permissions ADD COLUMN permissions TEXT DEFAULT "all"'
        )
    except:
        pass
    try:
        cursor.execute(
            'ALTER TABLE admin_codes ADD COLUMN permissions TEXT DEFAULT "all"'
        )
    except:
        pass

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_texts (
            key TEXT PRIMARY KEY,
            text TEXT,
            photo_id TEXT,
            updated_by INTEGER,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Добавляем колонку photo_id если её ещё нет (миграция)
    try:
        cursor.execute("ALTER TABLE bot_texts ADD COLUMN photo_id TEXT")
    except:
        pass

    # Таблица для истории поддержки
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS support_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            admin_id INTEGER DEFAULT 0,
            message TEXT,
            is_from_user INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ========== ТАБЛИЦА ДЛЯ БЫСТРОГО ПОИСКА ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS search_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT NOT NULL,
            db_type TEXT DEFAULT 'default',
            raw_line TEXT NOT NULL,
            search_index TEXT NOT NULL,
            is_secret INTEGER DEFAULT 0,
            imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    try:
        cursor.execute("ALTER TABLE search_data ADD COLUMN is_secret INTEGER DEFAULT 0")
    except:
        pass
    try:
        cursor.execute("ALTER TABLE search_data ADD COLUMN secreted_by INTEGER DEFAULT NULL")
    except:
        pass
    try:
        cursor.execute("ALTER TABLE search_data ADD COLUMN secreted_query TEXT DEFAULT NULL")
    except:
        pass

    # Таблица для засекречивания API-результатов (LeakOSINT, BigBase и др.)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_secret_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_hash TEXT UNIQUE NOT NULL,
            api_source TEXT NOT NULL,
            secreted_by INTEGER,
            secreted_query TEXT,
            secreted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Индекс для быстрого поиска по search_index
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_search_data_index
        ON search_data (search_index)
    """)
    # Индекс для быстрой проверки наличия файла
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_search_data_source
        ON search_data (source_file)
    """)

    # ========== НАСТРОЙКИ БОТА (флаги инициализации) ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # ========== ВСТРОЕННЫЕ API: НЕСКОЛЬКО ТОКЕНОВ НА СЕРВИС (LeakOSINT / BigBase) ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS builtin_api_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_kind TEXT NOT NULL,
            token TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(api_kind, token)
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_builtin_api_tokens_kind
        ON builtin_api_tokens (api_kind)
    """)
    # Сеем дефолтные токены ТОЛЬКО при первом запуске.
    # После удаления токена через интерфейс они НЕ возвращаются при перезапуске.
    cursor.execute("SELECT value FROM bot_settings WHERE key = 'default_tokens_seeded'")
    _seeded = cursor.fetchone()
    if not _seeded:
        for _kind, _tok in (("leakosint", API_TOKEN), ("bigbase", BIGBASE_TOKEN)):
            _t = (_tok or "").strip()
            if not _t:
                continue
            cursor.execute(
                "INSERT OR IGNORE INTO builtin_api_tokens (api_kind, token) VALUES (?, ?)",
                (_kind, _t),
            )
        cursor.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('default_tokens_seeded', '1')"
        )

    # Документация к встроенным API (LeakOSINT / BigBase)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS builtin_api_docs (
            api_kind TEXT PRIMARY KEY,
            doc_text TEXT DEFAULT '',
            doc_file_id TEXT DEFAULT '',
            doc_filename TEXT DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ========== ТАБЛИЦА СОБЫТИЙ ПОЛЬЗОВАТЕЛЕЙ (приход / уход) ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            event_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_user_events_date
        ON user_events (event_at)
    """)

    # ========== ТАБЛИЦА ЗАБЛОКИРОВАННЫХ ЗАПРОСОВ ==========
    # match_mode: 'exact' — точное совпадение, 'contains' — запрос содержит паттерн
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS query_blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query_pattern TEXT NOT NULL,
            custom_text TEXT NOT NULL,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            match_mode TEXT DEFAULT 'contains'
        )
    """)

    # Тексты которые вставляются только если их ещё нет
    ignore_texts = [
        ("myhistory", get_default_history_text()),
        ("search_prompt", "📝 Напиши ФИО или номер телефона:"),
        ("subscribe_prompt", "💰 Выберите тариф подписки:"),
        ("custom_button_label", get_default_custom_button_label()),
        ("custom_button", get_default_custom_button_text()),
        ("admin_help", (
            "👑 АДМИН-ПАНЕЛЬ:\n"
            "{stats_line}{admin_panel_line}{mod_line}{texts_line}{files_line}"
            "{secret_line}{block_line}{broadcast_line}{support_line}{createtrip_line}"
            "------------------------------------\n"
            "/help - это сообщение"
        )),
    ]
    for key, default_text in ignore_texts:
        cursor.execute(
            "INSERT OR IGNORE INTO bot_texts (key, text) VALUES (?, ?)",
            (key, default_text),
        )

    # help и start: вставляются только если их ещё нет (не перезаписываем при рестарте)
    for key, new_text in [("help", get_default_help_text()), ("start", get_default_start_text())]:
        cursor.execute(
            "INSERT OR IGNORE INTO bot_texts (key, text, updated_by, updated_at) VALUES (?, ?, NULL, CURRENT_TIMESTAMP)",
            (key, new_text)
        )

    # ========== ТАБЛИЦА ТРИПОВ ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trips_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            trips_amount INTEGER NOT NULL,
            created_by INTEGER,
            used_by INTEGER DEFAULT NULL,
            used_at TIMESTAMP DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trips_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            reason TEXT,
            logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Колонка trips в users
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN trips INTEGER DEFAULT 0")
    except:
        pass

    # ========== АВТО-ДОБАВЛЕНИЕ CRYVEN API ==========
    # Добавляется один раз при первом запуске. При смене токена — удалить вручную через /api_manager.
    cursor.execute("SELECT value FROM bot_settings WHERE key = 'cryven_seeded'")
    _cryven_seeded = cursor.fetchone()
    if not _cryven_seeded:
        cursor.execute("""
            INSERT OR IGNORE INTO external_apis
                (name, emoji, url, api_key, method, auth_type, auth_key,
                 query_param, query_place, extra_body, result_path,
                 description, doc_text, doc_file_id, doc_filename, cost_trips, added_by, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "Cryven API",          # name
            "🔎",                  # emoji
            "https://cryven.info/api/search",  # url
            "@loveSophiaEgor:xyBkjyn2",        # api_key (токен)
            "POST",                # method
            "query_param",         # auth_type — key передаётся как параметр запроса
            "key",                 # auth_key — имя параметра
            "search",              # query_param — поле поиска
            "body",                # query_place — передаём в теле JSON
            '{"type": "auto", "method": "lite", "source": "auto"}',  # extra_body
            "",                    # result_path — пустой, парсер обрабатывает полный ответ сам
            "Поиск по phone, email, fio, telegram, vk, ip, auto, nick",  # description
            "Методы: lite, pro, full\nТипы: phone, email, fio, telegram, snils, inn, vk, ip, auto, nick\nЛимит: 3000 запросов/день, 3 Sherlock\nEndpoint: /api/search (GET или POST)\nSherlock: /api/telegram/search",  # doc_text
            "",                    # doc_file_id
            "",                    # doc_filename
            1,                     # cost_trips
            0,                     # added_by (система)
            1,                     # is_active
        ))
        cursor.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('cryven_seeded', '1')"
        )

    conn.commit()
    conn.close()


init_database()


# ========== МИГРАЦИЯ: сброс result_path для Cryven API ==========
def _migrate_cryven_result_path():
    """Если Cryven уже в БД с result_path='results' — исправляем на пустой."""
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE external_apis SET result_path = '' WHERE name = 'Cryven API' AND result_path = 'results'"
        )
        if cursor.rowcount:
            print("[migrate] Cryven API: result_path исправлен на ''")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[migrate] Cryven: {e}")

_migrate_cryven_result_path()


# ========== ВСТРОЕННЫЕ API: ТОКЕНЫ В SQLITE ==========

def get_builtin_tokens(kind: str) -> list:
    """Список (id, token) в порядке приоритета (по id). kind: leakosint | bigbase"""
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, token FROM builtin_api_tokens WHERE api_kind = ? ORDER BY id ASC",
            (kind,),
        )
        rows = cursor.fetchall()
        conn.close()
        return rows
    except Exception as e:
        print(f"[builtin_tokens] get {kind}: {e}")
        return []


def add_builtin_token(kind: str, token: str):
    """Добавляет токен. Возвращает (ok: bool, error_message: str)."""
    token = (token or "").strip()
    if not token:
        return False, "Пустой токен"
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO builtin_api_tokens (api_kind, token) VALUES (?, ?)",
            (kind, token),
        )
        conn.commit()
        conn.close()
        return True, ""
    except sqlite3.IntegrityError:
        return False, "Такой токен уже есть в списке."
    except Exception as e:
        return False, str(e)


def delete_builtin_token(token_id: int, kind: str) -> bool:
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM builtin_api_tokens WHERE id = ? AND api_kind = ?",
            (token_id, kind),
        )
        ok = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return ok
    except Exception as e:
        print(f"[builtin_tokens] delete: {e}")
        return False


def mask_builtin_token_preview(token: str) -> str:
    if not token:
        return "—"
    t = str(token).strip()
    if len(t) <= 12:
        return t[:4] + "…"
    return t[:8] + "…" + t[-3:]


def get_builtin_api_doc(kind: str) -> dict:
    """Документация встроенного API: doc_text, doc_file_id, doc_filename."""
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute(
            "SELECT doc_text, doc_file_id, doc_filename FROM builtin_api_docs WHERE api_kind = ?",
            (kind,),
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            return {"doc_text": "", "doc_file_id": "", "doc_filename": ""}
        return {
            "doc_text": row[0] or "",
            "doc_file_id": row[1] or "",
            "doc_filename": row[2] or "",
        }
    except Exception as e:
        print(f"[builtin_api_docs] get {kind}: {e}")
        return {"doc_text": "", "doc_file_id": "", "doc_filename": ""}


def builtin_api_doc_nonempty(kind: str) -> bool:
    d = get_builtin_api_doc(kind)
    return bool(d.get("doc_text") or d.get("doc_file_id"))


def update_builtin_api_documentation(
    kind: str, doc_text: str = "", doc_file_id: str = "", doc_filename: str = ""
) -> bool:
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM builtin_api_docs WHERE api_kind = ?", (kind,))
        if cursor.fetchone():
            cursor.execute(
                """
                UPDATE builtin_api_docs
                SET doc_text = ?, doc_file_id = ?, doc_filename = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE api_kind = ?
                """,
                (doc_text or "", doc_file_id or "", doc_filename or "", kind),
            )
        else:
            cursor.execute(
                """
                INSERT INTO builtin_api_docs (api_kind, doc_text, doc_file_id, doc_filename)
                VALUES (?, ?, ?, ?)
                """,
                (kind, doc_text or "", doc_file_id or "", doc_filename or ""),
            )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[builtin_api_docs] update {kind}: {e}")
        return False


def send_builtin_api_doc_to_chat(chat_id: int, kind: str) -> bool:
    """Отправляет пользователю сохранённую документацию встроенного API. True если что-то отправлено."""
    titles = {"leakosint": "🌍 LeakOSINT", "bigbase": "📊 BigBase"}
    if kind not in titles:
        return False
    d = get_builtin_api_doc(kind)
    doc_text = d.get("doc_text") or ""
    doc_file_id = d.get("doc_file_id") or ""
    doc_filename = d.get("doc_filename") or ""
    title = titles[kind]

    def _esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    if doc_text:
        bot.send_message(
            chat_id,
            f"📖 <b>{_esc(title)}</b> <i>(встроенный API)</i>\n\n{_esc(doc_text[:4000])}",
            parse_mode="HTML",
        )
        return True
    if doc_file_id:
        cap = f"📖 {title} (встроенный API)"
        if doc_filename:
            cap += f" — {doc_filename}"
        bot.send_document(chat_id, doc_file_id, caption=cap[:1024])
        return True
    bot.send_message(chat_id, f"📖 {title}: документация не добавлена.")
    return False


# ========== ФУНКЦИИ ТРИПОВ ==========

def get_user_trips(user_id: int) -> int:
    """Возвращает баланс трипов пользователя."""
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute("SELECT trips FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        conn.close()
        if row and row[0] is not None:
            return int(row[0])
        return 0
    except Exception as e:
        print(f"[trips] get_user_trips error: {e}")
        return 0


def add_trips(user_id: int, amount: int, reason: str = "code") -> int:
    """Добавляет трипы пользователю. Возвращает новый баланс."""
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        # Гарантируем что пользователь существует в таблице users,
        # иначе UPDATE ниже не запишет трипы (affected rows = 0)
        cursor.execute(
            "INSERT OR IGNORE INTO users (user_id, trips) VALUES (?, 0)",
            (user_id,)
        )
        cursor.execute(
            "UPDATE users SET trips = COALESCE(trips, 0) + ? WHERE user_id = ?",
            (amount, user_id)
        )
        cursor.execute(
            "INSERT INTO trips_log (user_id, amount, reason) VALUES (?, ?, ?)",
            (user_id, amount, reason)
        )
        conn.commit()
        cursor.execute("SELECT trips FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        conn.close()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception as e:
        print(f"[trips] add_trips error: {e}")
        return 0


def spend_trips(user_id: int, amount: int = 1) -> bool:
    """Списывает трипы. Возвращает True если успешно."""
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute("SELECT trips FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        balance = int(row[0]) if row and row[0] else 0
        if balance < amount:
            conn.close()
            return False
        cursor.execute(
            "UPDATE users SET trips = trips - ? WHERE user_id = ?",
            (amount, user_id)
        )
        cursor.execute(
            "INSERT INTO trips_log (user_id, amount, reason) VALUES (?, ?, ?)",
            (user_id, -amount, "search")
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[trips] spend_trips error: {e}")
        return False


def create_trips_code(admin_id: int, trips_amount: int) -> str:
    """Создаёт код на трипы. Возвращает код."""
    import random, string
    code = "T" + "".join(random.choices(string.ascii_uppercase + string.digits, k=7))
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO trips_codes (code, trips_amount, created_by) VALUES (?, ?, ?)",
            (code, trips_amount, admin_id)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[trips] create_trips_code error: {e}")
    return code


def activate_trips_code(code: str, user_id: int):
    """Активирует код на трипы. Возвращает (success, trips_amount_or_error)."""
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, trips_amount, used_by FROM trips_codes WHERE code = ?",
            (code.upper(),)
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return False, "Код не найден"
        code_id, trips_amount, used_by = row
        if used_by is not None:
            conn.close()
            return False, "Код уже использован"
        cursor.execute(
            "UPDATE trips_codes SET used_by = ?, used_at = ? WHERE id = ?",
            (user_id, datetime.now(), code_id)
        )
        conn.commit()
        conn.close()
        new_balance = add_trips(user_id, trips_amount, reason=f"code:{code.upper()}")
        return True, trips_amount
    except Exception as e:
        print(f"[trips] activate_trips_code error: {e}")
        return False, str(e)


def get_trips_top(limit: int = 10):
    """Топ пользователей по трипам."""
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.user_id, COALESCE(u.username, u.first_name, 'ID '||u.user_id), u.trips
            FROM users u
            WHERE u.trips > 0
            ORDER BY u.trips DESC
            LIMIT ?
        """, (limit,))
        rows = cursor.fetchall()
        conn.close()
        return rows
    except Exception as e:
        print(f"[trips] get_trips_top error: {e}")
        return []


def get_trips_daily_stats():
    """Статистика выдачи трипов по дням (для графика)."""
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute("""
            SELECT date(logged_at) as day, COUNT(DISTINCT user_id) as users, SUM(amount) as total
            FROM trips_log
            WHERE amount > 0
            GROUP BY day
            ORDER BY day DESC
            LIMIT 30
        """)
        rows = cursor.fetchall()
        conn.close()
        return rows
    except Exception as e:
        print(f"[trips] get_trips_daily_stats error: {e}")
        return []


# Состояние создания кода трипов
admin_trips_state = {}  # {user_id: {"step": ...}}


# ========== SQLite БЫСТРЫЙ ПОИСК: ИМПОРТ И ПОИСК ==========

def _sqlite_import_file(conn, file_path, source_name, db_type):
    """Импортирует один .txt файл в таблицу search_data. Пропускает уже загруженные."""
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM search_data WHERE source_file = ?", (source_name,))
    if cursor.fetchone()[0] > 0:
        return 0  # уже загружен

    try:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            with open(file_path, "r", encoding="cp1251") as f:
                content = f.read()
    except Exception as e:
        print(f"[sqlite_import] Ошибка чтения {file_path}: {e}")
        return 0

    lines = content.splitlines()
    semicolon_lines = sum(1 for l in lines if ";" in l)
    tab_lines = sum(1 for l in lines if "\t" in l)
    non_empty = [l for l in lines if l.strip()]
    is_csv = (semicolon_lines > len(non_empty) * 0.4 or tab_lines > len(non_empty) * 0.4)

    rows = []
    if is_csv:
        sep = ";" if semicolon_lines >= tab_lines else "\t"
        for line in non_empty:
            idx = line.lower()
            rows.append((source_name, db_type, line.strip(), idx))
    else:
        for block in content.strip().split("\n\n"):
            block = block.strip()
            if block:
                idx = block.lower()
                rows.append((source_name, db_type, block, idx))

    if rows:
        cursor.executemany(
            "INSERT INTO search_data (source_file, db_type, raw_line, search_index) VALUES (?,?,?,?)",
            rows,
        )
        conn.commit()
    return len(rows)


def import_all_txt_to_sqlite():
    """При старте бота импортирует все .txt из data/ в SQLite (пропускает уже загруженные)."""
    if not os.path.exists(DATA_DIR):
        return
    conn = sqlite3.connect("users.db")
    total = 0
    for root, dirs, files in os.walk(DATA_DIR):
        for filename in sorted(files):
            if not filename.endswith(".txt"):
                continue
            file_path = os.path.join(root, filename)
            db_type = detect_db_type_by_name(filename)
            n = _sqlite_import_file(conn, file_path, filename, db_type)
            if n:
                print(f"[sqlite_import] {filename}: +{n} записей")
                total += n
    conn.close()
    if total:
        print(f"[sqlite_import] Итого импортировано: {total} записей")


def sqlite_cleanup_dead_files():
    """Удаляет из SQLite записи файлов, которых больше нет в data/."""
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT source_file FROM search_data")
        indexed_files = [row[0] for row in cursor.fetchall()]
        removed = 0
        for source_file in indexed_files:
            real_path = os.path.join(DATA_DIR, source_file)
            if not os.path.exists(real_path):
                cursor.execute("DELETE FROM search_data WHERE source_file = ?", (source_file,))
                removed += cursor.rowcount
                print(f"[sqlite_cleanup] Удалены записи несуществующего файла: {source_file}")
        if removed:
            conn.commit()
            print(f"[sqlite_cleanup] Итого удалено устаревших записей: {removed}")
        conn.close()
    except Exception as e:
        print(f"[sqlite_cleanup] Ошибка очистки: {e}")


# ========== ЭКСПОРТ И АНАЛИТИКА ПОЛЬЗОВАТЕЛЕЙ ==========

def export_users_to_excel() -> io.BytesIO:
    """Собирает всю базу пользователей + историю поиска в один Excel-файл."""
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()

    wb = openpyxl.Workbook()

    # ── Лист 1: Пользователи ──
    ws_users = wb.active
    ws_users.title = "Пользователи"
    headers = ["ID", "Username", "Имя", "Фамилия", "Статус", "Тип подписки",
               "Начало подписки", "Конец подписки", "Забанен", "Причина бана", "Дата регистрации"]
    ws_users.append(headers)
    cursor.execute("""
        SELECT user_id, username, first_name, last_name, subscription_status,
               subscription_type, subscription_start, subscription_end,
               banned, ban_reason, created_at
        FROM users ORDER BY created_at DESC
    """)
    for row in cursor.fetchall():
        ws_users.append(list(row))

    # ── Лист 2: История поиска ──
    ws_hist = wb.create_sheet("История поиска")
    ws_hist.append(["ID записи", "User ID", "Username", "Имя", "Запрос", "Найдено", "Время поиска"])
    cursor.execute("""
        SELECT sh.id, sh.user_id, u.username, u.first_name, sh.query, sh.found, sh.search_time
        FROM search_history sh
        LEFT JOIN users u ON sh.user_id = u.user_id
        ORDER BY sh.search_time DESC
    """)
    for row in cursor.fetchall():
        ws_hist.append(list(row))

    # ── Лист 3: Топ запросов ──
    ws_top = wb.create_sheet("Топ запросов")
    ws_top.append(["Запрос", "Количество", "Найдено", "Не найдено"])
    cursor.execute("""
        SELECT query, COUNT(*) as cnt,
               SUM(CASE WHEN found=1 THEN 1 ELSE 0 END),
               SUM(CASE WHEN found=0 THEN 1 ELSE 0 END)
        FROM search_history
        GROUP BY lower(query)
        ORDER BY cnt DESC
        LIMIT 100
    """)
    for row in cursor.fetchall():
        ws_top.append(list(row))

    # ── Лист 4: Активность по дням ──
    ws_act = wb.create_sheet("Активность по дням")
    ws_act.append(["Дата", "Новых пользователей", "Поисков", "Уникальных искателей"])
    cursor.execute("""
        SELECT date(sh.search_time) as day,
               COUNT(DISTINCT u.user_id) FILTER (WHERE date(u.created_at) = date(sh.search_time)) as new_users,
               COUNT(sh.id) as searches,
               COUNT(DISTINCT sh.user_id) as unique_searchers
        FROM search_history sh
        LEFT JOIN users u ON sh.user_id = u.user_id
        GROUP BY day
        ORDER BY day DESC
        LIMIT 90
    """)
    for row in cursor.fetchall():
        ws_act.append(list(row))

    conn.close()

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    buf.name = f"users_export_{datetime.now().strftime('%d%m%Y_%H%M')}.xlsx"
    return buf


def export_users_to_txt() -> io.BytesIO:
    """Резервный экспорт в TXT если openpyxl недоступен."""
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    lines = [f"ЭКСПОРТ БАЗЫ ПОЛЬЗОВАТЕЛЕЙ | {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"]
    lines.append("=" * 60 + "\n")

    cursor.execute("""
        SELECT user_id, username, first_name, subscription_status,
               banned, created_at
        FROM users ORDER BY created_at DESC
    """)
    users = cursor.fetchall()
    lines.append(f"ВСЕГО ПОЛЬЗОВАТЕЛЕЙ: {len(users)}\n\n")
    for u in users:
        uid, uname, fname, status, banned, created = u
        lines.append(f"ID: {uid} | @{uname or '-'} | {fname or '-'} | {status} | {'🚫БАН' if banned else '✅ОК'} | {created}\n")

    lines.append("\n" + "=" * 60 + "\n")
    lines.append("ТОП-50 ЗАПРОСОВ:\n\n")
    cursor.execute("""
        SELECT query, COUNT(*) as cnt FROM search_history
        GROUP BY lower(query) ORDER BY cnt DESC LIMIT 50
    """)
    for i, (q, cnt) in enumerate(cursor.fetchall(), 1):
        lines.append(f"{i:3}. [{cnt:5}x] {q}\n")

    conn.close()
    buf = io.BytesIO("".join(lines).encode("utf-8"))
    buf.name = f"users_export_{datetime.now().strftime('%d%m%Y_%H%M')}.txt"
    return buf


def generate_stats_html_report() -> io.BytesIO:
    """Генерирует интерактивный HTML-отчёт со статистикой и графиками с фильтрами по времени."""
    import json as _json

    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()

    # --- Общая статистика (всё время) ---
    total_users    = cursor.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    active_subs    = cursor.execute("SELECT COUNT(*) FROM users WHERE subscription_status IN ('active','forever')").fetchone()[0]
    forever_subs   = cursor.execute("SELECT COUNT(*) FROM users WHERE subscription_status = 'forever'").fetchone()[0]
    banned_users   = cursor.execute("SELECT COUNT(*) FROM users WHERE banned = 1").fetchone()[0]
    total_searches = cursor.execute("SELECT COUNT(*) FROM search_history").fetchone()[0]
    found_count    = cursor.execute("SELECT COUNT(*) FROM search_history WHERE found = 1").fetchone()[0]
    not_found      = total_searches - found_count
    found_pct      = round(found_count / total_searches * 100) if total_searches else 0

    # --- Сегодня ---
    today = datetime.now().strftime('%Y-%m-%d')
    searches_today  = cursor.execute("SELECT COUNT(*) FROM search_history WHERE date(search_time)=?", (today,)).fetchone()[0]
    new_users_today = cursor.execute("SELECT COUNT(*) FROM users WHERE date(created_at)=?", (today,)).fetchone()[0]
    unique_today    = cursor.execute("SELECT COUNT(DISTINCT user_id) FROM search_history WHERE date(search_time)=?", (today,)).fetchone()[0]
    found_today     = cursor.execute("SELECT COUNT(*) FROM search_history WHERE date(search_time)=? AND found=1", (today,)).fetchone()[0]

    # --- Новые пользователи — строим данные за несколько периодов (1 год макс) ---
    from datetime import date, timedelta

    # Регистрации по дням — единственный источник правды
    cursor.execute("SELECT date(created_at) as d, COUNT(*) as cnt FROM users GROUP BY d ORDER BY d")
    _reg_by_day = {r[0]: r[1] for r in cursor.fetchall()}
    _first_reg = min(_reg_by_day.keys()) if _reg_by_day else datetime.now().strftime('%Y-%m-%d')

    def _build_newu(days):
        # Для года и больше — показываем весь рост с первого пользователя
        if days >= 365:
            start = _first_reg
        else:
            start = (datetime.now() - timedelta(days=days-1)).strftime('%Y-%m-%d')

        start_dt = datetime.strptime(start, '%Y-%m-%d')
        total_days = (datetime.now() - start_dt).days + 1
        dr = [(start_dt + timedelta(days=i)).strftime('%Y-%m-%d') for i in range(total_days)]

        # Уходы из user_events (если таблица есть)
        try:
            cursor.execute("""
                SELECT date(event_at) as d, COUNT(*) as cnt FROM user_events
                WHERE event_type='left' AND date(event_at)>=? GROUP BY d
            """, (start,))
            lmap = {r[0]: r[1] for r in cursor.fetchall()}
        except Exception:
            lmap = {}

        # Накопительный от 0: прибавляем регистрации каждый день
        # Накопительный от 0: прибавляем регистрации каждый день
        cumul, running, arrive, left_list = [], 0, [], []
        for d in dr:
            new_today = _reg_by_day.get(d, 0)
            running = max(0, running + new_today - lmap.get(d, 0))
            cumul.append(running)
            arrive.append(new_today)
            left_list.append(lmap.get(d, 0))

        # Добавляем нулевую точку за день до первой регистрации
        # чтобы график всегда начинался с 0
        if days >= 365 and dr:
            prev_day = (start_dt - timedelta(days=1)).strftime('%Y-%m-%d')
            dr        = [prev_day] + dr
            cumul     = [0] + cumul
            arrive    = [0] + arrive
            left_list = [0] + left_list

        return {"labels": dr, "arrive": arrive,
                "left": left_list, "cumulative": cumul}


    _np = {str(k): _build_newu(k) for k in (30, 90, 180, 365)}
    newu_periods_js    = _json.dumps(_np, ensure_ascii=False)
    date_range         = _np["365"]["labels"]
    newu_daily_labels  = date_range
    newu_daily_data    = _np["365"]["arrive"]
    newu_left_data     = _np["365"]["left"]
    cumulative         = _np["365"]["cumulative"]
    joined_map         = {d: v for d, v in zip(date_range, newu_daily_data)}
    left_map           = {d: v for d, v in zip(date_range, newu_left_data)}
    newu_labels_js     = _json.dumps(date_range)
    newu_arrive_js     = _json.dumps(newu_daily_data)
    newu_delta_js      = _json.dumps(newu_left_data)
    newu_cumulative_js = _json.dumps(cumulative)

    # --- Данные по периодам для интерактивных графиков ---
    periods = {"1": 1, "7": 7, "30": 30, "365": 365}
    period_data = {}

    for key, days in periods.items():
        interval = f'-{days} days'

        # Поиски по дням
        cursor.execute("""
            SELECT date(search_time) as d, COUNT(*) as cnt
            FROM search_history
            WHERE search_time >= datetime('now', ?)
            GROUP BY d ORDER BY d ASC
        """, (interval,))
        act_rows = cursor.fetchall()

        # Топ-10 запросов
        cursor.execute("""
            SELECT query, COUNT(*) as cnt
            FROM search_history
            WHERE search_time >= datetime('now', ?)
            GROUP BY lower(query)
            ORDER BY cnt DESC LIMIT 10
        """, (interval,))
        top_rows = cursor.fetchall()

        # Найдено vs Не найдено
        fc = cursor.execute("SELECT COUNT(*) FROM search_history WHERE search_time >= datetime('now',?) AND found=1", (interval,)).fetchone()[0]
        nf = cursor.execute("SELECT COUNT(*) FROM search_history WHERE search_time >= datetime('now',?) AND found=0", (interval,)).fetchone()[0]

        # Топ-10 активных пользователей
        cursor.execute("""
            SELECT sh.user_id, u.username, u.first_name, COUNT(*) as cnt
            FROM search_history sh
            LEFT JOIN users u ON sh.user_id = u.user_id
            WHERE sh.search_time >= datetime('now', ?)
            GROUP BY sh.user_id
            ORDER BY cnt DESC LIMIT 10
        """, (interval,))
        au_rows = cursor.fetchall()

        # Неудачные запросы по источнику API
        cursor.execute("""
            SELECT COALESCE(api_source, 'sqlite') as src, COUNT(*) as cnt
            FROM search_history
            WHERE search_time >= datetime('now', ?) AND found = 0
            GROUP BY src ORDER BY cnt DESC
        """, (interval,))
        fail_api_rows = cursor.fetchall()

        # Топ-5 пользователей с наибольшим числом неудач
        cursor.execute("""
            SELECT sh.user_id,
                   COALESCE(u.username, u.first_name, 'ID'||sh.user_id) as name,
                   COUNT(*) as cnt
            FROM search_history sh
            LEFT JOIN users u ON sh.user_id = u.user_id
            WHERE sh.search_time >= datetime('now', ?) AND sh.found = 0
            GROUP BY sh.user_id ORDER BY cnt DESC LIMIT 5
        """, (interval,))
        fail_user_rows = cursor.fetchall()

        # Последние 10 неудачных запросов
        cursor.execute("""
            SELECT sh.user_id,
                   COALESCE(u.username, u.first_name, 'ID'||sh.user_id) as name,
                   sh.query,
                   COALESCE(sh.api_source, 'sqlite') as src,
                   sh.search_time
            FROM search_history sh
            LEFT JOIN users u ON sh.user_id = u.user_id
            WHERE sh.search_time >= datetime('now', ?) AND sh.found = 0
            ORDER BY sh.search_time DESC LIMIT 10
        """, (interval,))
        last_fail_rows = cursor.fetchall()

        period_data[key] = {
            "act_labels": [r[0] for r in act_rows],
            "act_data":   [r[1] for r in act_rows],
            "top_labels": [r[0][:28] for r in top_rows],
            "top_data":   [r[1] for r in top_rows],
            "found":      fc,
            "not_found":  nf,
            "active_users": [
                {
                    "name": f"@{row[1]}" if row[1] else (row[2] or f"ID{row[0]}"),
                    "uid":  row[0],
                    "cnt":  row[3]
                }
                for row in au_rows
            ],
            "fail_by_api": [
                {"src": row[0].upper(), "cnt": row[1]}
                for row in fail_api_rows
            ],
            "fail_top_users": [
                {
                    "name": f"@{row[1]}" if not row[1].startswith("ID") else row[1],
                    "uid":  row[0],
                    "cnt":  row[2]
                }
                for row in fail_user_rows
            ],
            "last_fails": [
                {
                    "uid":  row[0],
                    "name": f"@{row[1]}" if not str(row[1]).startswith("ID") else row[1],
                    "query": row[2][:40] + ("…" if len(row[2]) > 40 else ""),
                    "src":  row[3].upper(),
                    "time": row[4][5:16] if row[4] else "—"
                }
                for row in last_fail_rows
            ],
        }

    # --- Последние 10 регистраций ---
    cursor.execute("""
        SELECT user_id, username, first_name, subscription_status, created_at
        FROM users ORDER BY created_at DESC LIMIT 10
    """)
    last_regs = cursor.fetchall()

    # --- ТРИПЫ: топ-10 и статистика по дням ---
    try:
        trips_top_rows = cursor.execute("""
            SELECT u.user_id, COALESCE(u.username, u.first_name, 'ID'||u.user_id), u.trips
            FROM users u
            WHERE u.trips > 0
            ORDER BY u.trips DESC
            LIMIT 10
        """).fetchall()
    except:
        trips_top_rows = []

    try:
        trips_daily_rows = cursor.execute("""
            SELECT date(logged_at) as day, COUNT(DISTINCT user_id) as users, SUM(amount) as total
            FROM trips_log
            WHERE amount > 0
            GROUP BY day
            ORDER BY day ASC
            LIMIT 30
        """).fetchall()
    except:
        trips_daily_rows = []

    # --- Трипы по периодам для графика ---
    trips_periods = {}
    for tkey, tdays in [("1", 1), ("7", 7), ("30", 30), ("365", 365)]:
        try:
            rows = cursor.execute("""
                SELECT date(logged_at) as day, COUNT(DISTINCT user_id) as users, SUM(amount) as total
                FROM trips_log
                WHERE amount > 0 AND logged_at >= datetime('now', ?)
                GROUP BY day ORDER BY day ASC
            """, (f"-{tdays} days",)).fetchall()
            trips_periods[tkey] = {
                "labels": [r[0] for r in rows],
                "users":  [r[1] for r in rows],
                "total":  [r[2] for r in rows],
            }
        except:
            trips_periods[tkey] = {"labels": [], "users": [], "total": []}

    # --- Топ-10 вечных подписок ---
    try:
        forever_subs_rows = cursor.execute("""
            SELECT user_id, COALESCE(username, first_name, 'ID'||user_id), subscription_start, subscription_type
            FROM users
            WHERE subscription_status = 'forever'
            ORDER BY subscription_start ASC
            LIMIT 10
        """).fetchall()
    except:
        forever_subs_rows = []

    # --- Топ-10 обычных подписок (от самых длинных по сроку) ---
    try:
        active_subs_rows = cursor.execute("""
            SELECT user_id, COALESCE(username, first_name, 'ID'||user_id),
                   subscription_start, subscription_end, subscription_type,
                   CAST((julianday(subscription_end) - julianday(subscription_start)) AS INTEGER) as duration_days
            FROM users
            WHERE subscription_status = 'active'
              AND subscription_end IS NOT NULL
              AND subscription_start IS NOT NULL
              AND datetime(subscription_end) > datetime('now')
            ORDER BY duration_days DESC
            LIMIT 10
        """).fetchall()
    except:
        active_subs_rows = []

    # --- Список заблокированных (от последнего) ---
    banned_rows = []
    try:
        # Проверяем есть ли колонка banned_at
        _cols = [r[1] for r in cursor.execute("PRAGMA table_info(users)").fetchall()]
        if 'banned_at' in _cols:
            banned_rows = cursor.execute("""
                SELECT user_id, COALESCE(username, first_name, 'ID'||user_id),
                       ban_reason, COALESCE(banned_at, '') as ban_dt
                FROM users
                WHERE banned = 1
                ORDER BY CASE WHEN banned_at IS NULL OR banned_at = '' THEN 1 ELSE 0 END ASC,
                         banned_at DESC
            """).fetchall()
        else:
            # Колонки banned_at ещё нет — берём без неё
            banned_rows = [
                (uid, name, reason, '')
                for uid, name, reason in cursor.execute("""
                    SELECT user_id, COALESCE(username, first_name, 'ID'||user_id), ban_reason
                    FROM users
                    WHERE banned = 1
                """).fetchall()
            ]
    except Exception as _e:
        banned_rows = []

    try:
        total_trips_issued = cursor.execute(
            "SELECT COALESCE(SUM(trips),0) FROM users"
        ).fetchone()[0] or 0
    except:
        total_trips_issued = 0

    conn.close()

    last_reg_html = ""
    for uid, uname, fname, status, created in last_regs:
        name  = f"@{uname}" if uname else (fname or "—")
        badge = {"active":"✅ active","forever":"💎 forever","inactive":"⬜ inactive"}.get(status, status)
        last_reg_html += f"<tr><td>{uid}</td><td>{name}</td><td>{badge}</td><td>{created}</td></tr>\n"

    # --- HTML блок: Топ трипов ---
    trips_top_html = ""
    for i, (uid, name, trips) in enumerate(trips_top_rows, 1):
        medal = ["🥇","🥈","🥉"][i-1] if i <= 3 else f"{i}."
        disp = f"@{name}" if not str(name).startswith("ID") and "@" not in str(name) else name
        trips_top_html += f"<tr><td>{medal}</td><td>{disp}</td><td>{uid}</td><td><b>{trips}</b></td></tr>\n"

    # --- JS данные для графика выдачи трипов по дням ---
    trips_day_labels = _json.dumps([r[0] for r in trips_daily_rows], ensure_ascii=False)
    trips_day_users  = _json.dumps([r[1] for r in trips_daily_rows], ensure_ascii=False)
    trips_day_total  = _json.dumps([r[2] for r in trips_daily_rows], ensure_ascii=False)
    trips_periods_js = _json.dumps(trips_periods, ensure_ascii=False)

    # --- HTML: вечные подписки ---
    forever_subs_html = ""
    for i, (uid, name, sub_start, sub_type) in enumerate(forever_subs_rows, 1):
        medal = ["🥇","🥈","🥉"][i-1] if i <= 3 else f"{i}."
        disp = f"@{name}" if not str(name).startswith("ID") and "@" not in str(name) else name
        since = str(sub_start)[:10] if sub_start else "—"
        stype = sub_type or "forever"
        forever_subs_html += f"<tr><td>{medal}</td><td>{disp}</td><td>{uid}</td><td>{stype}</td><td>{since}</td></tr>\n"

    # --- HTML: обычные подписки (топ по длительности) ---
    active_subs_html = ""
    for i, (uid, name, sub_start, sub_end, sub_type, dur) in enumerate(active_subs_rows, 1):
        disp = f"@{name}" if not str(name).startswith("ID") and "@" not in str(name) else name
        since = str(sub_start)[:10] if sub_start else "—"
        until = str(sub_end)[:10] if sub_end else "—"
        stype = sub_type or "active"
        dur_str = f"{dur} дн." if dur and dur > 0 else "—"
        active_subs_html += f"<tr><td>{i}.</td><td>{disp}</td><td>{uid}</td><td>{stype}</td><td>{since}</td><td>{until}</td><td><b>{dur_str}</b></td></tr>\n"

    # --- HTML: заблокированные пользователи ---
    banned_html = ""
    for uid, name, reason, ban_dt in banned_rows:
        disp = f"@{name}" if not str(name).startswith("ID") and "@" not in str(name) else name
        reason_str = reason or "—"
        dt_str = str(ban_dt)[:16] if ban_dt else "—"
        banned_html += f"<tr><td>{disp}</td><td>{uid}</td><td>{reason_str}</td><td>{dt_str}</td></tr>\n"

    now_str = datetime.now().strftime('%d.%m.%Y %H:%M')
    period_data_js = _json.dumps(period_data, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>📊 Статистика бота — {now_str}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {{
    --bg:#0f1117; --card:#1a1d27; --accent:#5b8dee;
    --green:#3ecf8e; --red:#f06292; --gold:#f9c74f;
    --text:#e8eaf0; --muted:#6c7a92; --border:#252836;
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:'Segoe UI',sans-serif;padding:24px}}
  h1{{font-size:1.7rem;margin-bottom:4px}}
  .subtitle{{color:var(--muted);font-size:.9rem;margin-bottom:28px}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:16px;margin-bottom:28px}}
  .kpi{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px 18px}}
  .kpi .val{{font-size:2rem;font-weight:700}}
  .kpi .lbl{{color:var(--muted);font-size:.8rem;margin-top:4px}}
  .kpi.blue .val{{color:var(--accent)}}
  .kpi.green .val{{color:var(--green)}}
  .kpi.red .val{{color:var(--red)}}
  .kpi.gold .val{{color:var(--gold)}}
  .section{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px;margin-bottom:20px}}
  .section h2{{font-size:1rem;margin-bottom:14px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}}
  .chart-card{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px;margin-bottom:20px}}
  .chart-card h2{{font-size:1rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:12px}}
  canvas{{max-height:260px}}
  table{{width:100%;border-collapse:collapse;font-size:.88rem}}
  th{{color:var(--muted);text-align:left;padding:6px 10px;border-bottom:1px solid var(--border)}}
  td{{padding:8px 10px;border-bottom:1px solid var(--border)}}
  tr:last-child td{{border-bottom:none}}
  tr:hover td{{background:rgba(91,141,238,.06)}}
  .today-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}
  .today-kpi{{background:rgba(91,141,238,.08);border:1px solid var(--border);border-radius:10px;padding:14px;text-align:center}}
  .today-kpi .v{{font-size:1.6rem;font-weight:700;color:var(--accent)}}
  .today-kpi .l{{font-size:.75rem;color:var(--muted);margin-top:4px}}
  footer{{text-align:center;color:var(--muted);font-size:.78rem;margin-top:32px}}

  /* Кнопки фильтра */
  .period-btns{{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}}
  .period-btn{{
    background:transparent;border:1px solid var(--border);
    color:var(--muted);border-radius:8px;padding:6px 14px;
    font-size:.82rem;cursor:pointer;transition:all .18s;
  }}
  .period-btn:hover{{border-color:var(--accent);color:var(--accent)}}
  .period-btn.active{{
    background:var(--accent);border-color:var(--accent);
    color:#fff;font-weight:600;
  }}
  .two-col{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
  @media(max-width:720px){{.two-col{{grid-template-columns:1fr}}.today-grid{{grid-template-columns:repeat(2,1fr)}}}}
</style>
</head>
<body>
<h1>📊 Статистика бота</h1>
<p class="subtitle">Сформировано: {now_str}</p>

<!-- KPI -->
<div class="grid">
  <div class="kpi blue"><div class="val">{total_users}</div><div class="lbl">👥 Всего пользователей</div></div>
  <div class="kpi green"><div class="val">{active_subs}</div><div class="lbl">✅ Активных подписок</div></div>
  <div class="kpi gold"><div class="val">{forever_subs}</div><div class="lbl">💎 Вечных подписок</div></div>
  <div class="kpi red"><div class="val">{banned_users}</div><div class="lbl">🚫 Забанено</div></div>
  <div class="kpi blue"><div class="val">{total_searches}</div><div class="lbl">🔍 Всего поисков</div></div>
  <div class="kpi green"><div class="val">{found_count} ({found_pct}%)</div><div class="lbl">✅ Найдено</div></div>
  <div class="kpi red"><div class="val">{not_found}</div><div class="lbl">❌ Не найдено</div></div>
  <div class="kpi gold"><div class="val">{total_trips_issued}</div><div class="lbl">🎫 Трипов в обороте</div></div>
</div>

<!-- Сегодня -->
<div class="section">
  <h2>📅 Активность сегодня ({today})</h2>
  <div class="today-grid">
    <div class="today-kpi"><div class="v">{searches_today}</div><div class="l">Поисков</div></div>
    <div class="today-kpi"><div class="v">{unique_today}</div><div class="l">Уник. пользователей</div></div>
    <div class="today-kpi"><div class="v">{new_users_today}</div><div class="l">Новых пользователей</div></div>
    <div class="today-kpi"><div class="v">{found_today}</div><div class="l">Найдено</div></div>
  </div>
</div>

<!-- ═══════════ НОВЫЕ ПОЛЬЗОВАТЕЛИ ═══════════ -->
<div class="chart-card">
  <h2>🆕 Пользователи (1 год)</h2>
  <div class="period-btns" id="newuModeBtns">
    <button class="period-btn active" onclick="setNewuMode('users',event)">👥 Накопительно</button>
    <button class="period-btn" onclick="setNewuMode('dynamics',event)">📊 Динамика</button>
  </div>
  <div class="period-btns" id="newuRangeBtns">
    <button class="period-btn" onclick="setNewuRange('30',event)">30 дней</button>
    <button class="period-btn" onclick="setNewuRange('90',event)">90 дней</button>
    <button class="period-btn" onclick="setNewuRange('180',event)">180 дней</button>
    <button class="period-btn active" onclick="setNewuRange('365',event)">1 год</button>
  </div>
  <canvas id="newuChart"></canvas>
</div>

<!-- ═══════════ ПОИСКИ с фильтром ═══════════ -->
<div class="chart-card">
  <h2>📈 Поиски по дням</h2>
  <div class="period-btns" id="actBtns">
    <button class="period-btn active" onclick="setActPeriod('1','actBtns')">1 день</button>
    <button class="period-btn" onclick="setActPeriod('7','actBtns')">7 дней</button>
    <button class="period-btn" onclick="setActPeriod('30','actBtns')">30 дней</button>
    <button class="period-btn" onclick="setActPeriod('365','actBtns')">1 год</button>
  </div>
  <canvas id="actChart"></canvas>
</div>

<!-- ═══════════ ТОП ЗАПРОСОВ + DONUT рядом ═══════════ -->
<div class="two-col">
  <div class="chart-card" style="margin-bottom:0">
    <h2>🏆 Топ-10 запросов</h2>
    <div class="period-btns" id="topBtns">
      <button class="period-btn active" onclick="setTopPeriod('1','topBtns')">1 день</button>
      <button class="period-btn" onclick="setTopPeriod('7','topBtns')">7 дней</button>
      <button class="period-btn" onclick="setTopPeriod('30','topBtns')">30 дней</button>
      <button class="period-btn" onclick="setTopPeriod('365','topBtns')">1 год</button>
    </div>
    <canvas id="topChart"></canvas>
  </div>
  <div class="chart-card" style="margin-bottom:0">
    <h2>🍩 Найдено vs Не найдено</h2>
    <div class="period-btns" id="doughBtns">
      <button class="period-btn active" onclick="setDoughPeriod('1','doughBtns')">1 день</button>
      <button class="period-btn" onclick="setDoughPeriod('7','doughBtns')">7 дней</button>
      <button class="period-btn" onclick="setDoughPeriod('30','doughBtns')">30 дней</button>
      <button class="period-btn" onclick="setDoughPeriod('365','doughBtns')">1 год</button>
    </div>
    <canvas id="doughChart"></canvas>
  </div>
</div>
<br>

<!-- ═══════════ ТОП АКТИВНЫХ с фильтром ═══════════ -->
<div class="section">
  <h2>🔥 Топ-10 активных пользователей</h2>
  <div class="period-btns" id="auBtns">
    <button class="period-btn active" onclick="setAuPeriod('1','auBtns')">1 день</button>
    <button class="period-btn" onclick="setAuPeriod('7','auBtns')">7 дней</button>
    <button class="period-btn" onclick="setAuPeriod('30','auBtns')">30 дней</button>
    <button class="period-btn" onclick="setAuPeriod('365','auBtns')">1 год</button>
  </div>
  <table>
    <thead><tr><th>#</th><th>Пользователь</th><th>ID</th><th>Запросов</th></tr></thead>
    <tbody id="auTableBody"></tbody>
  </table>
</div>

<!-- Последние регистрации -->
<div class="section">
  <h2>🆕 Последние 10 регистраций</h2>
  <table>
    <thead><tr><th>ID</th><th>Пользователь</th><th>Статус</th><th>Дата</th></tr></thead>
    <tbody>{last_reg_html}</tbody>
  </table>
</div>

<!-- ═══════════ НЕУДАЧНЫЕ ЗАПРОСЫ ═══════════ -->
<div class="chart-card">
  <h2>❌ Неудачные запросы по источнику</h2>
  <div class="period-btns" id="failApiPeriodBtns">
    <button class="period-btn active" onclick="setFailApiPeriod('1','failApiPeriodBtns')">1 день</button>
    <button class="period-btn" onclick="setFailApiPeriod('7','failApiPeriodBtns')">7 дней</button>
    <button class="period-btn" onclick="setFailApiPeriod('30','failApiPeriodBtns')">30 дней</button>
    <button class="period-btn" onclick="setFailApiPeriod('365','failApiPeriodBtns')">1 год</button>
  </div>
  <canvas id="failApiChart" style="max-height:220px"></canvas>
</div>

<div class="two-col">
  <div class="section" style="margin-bottom:0">
    <h2>🏴 Топ юзеров с провалами</h2>
    <div class="period-btns" id="failUserPeriodBtns">
      <button class="period-btn active" onclick="setFailUserPeriod('1','failUserPeriodBtns')">1 день</button>
      <button class="period-btn" onclick="setFailUserPeriod('7','failUserPeriodBtns')">7 дней</button>
      <button class="period-btn" onclick="setFailUserPeriod('30','failUserPeriodBtns')">30 дней</button>
      <button class="period-btn" onclick="setFailUserPeriod('365','failUserPeriodBtns')">1 год</button>
    </div>
    <table>
      <thead><tr><th>#</th><th>Пользователь</th><th>ID</th><th>Провалов</th></tr></thead>
      <tbody id="failUserTableBody"></tbody>
    </table>
  </div>
  <div class="section" style="margin-bottom:0">
    <h2>🕐 Последние неудачные запросы</h2>
    <div class="period-btns" id="lastFailPeriodBtns">
      <button class="period-btn active" onclick="setLastFailPeriod('1','lastFailPeriodBtns')">1 день</button>
      <button class="period-btn" onclick="setLastFailPeriod('7','lastFailPeriodBtns')">7 дней</button>
      <button class="period-btn" onclick="setLastFailPeriod('30','lastFailPeriodBtns')">30 дней</button>
      <button class="period-btn" onclick="setLastFailPeriod('365','lastFailPeriodBtns')">1 год</button>
    </div>
    <table>
      <thead><tr><th>Время</th><th>API</th><th>Юзер</th><th>Запрос</th></tr></thead>
      <tbody id="lastFailTableBody"></tbody>
    </table>
  </div>
</div>
<br>

<!-- ═══════════ ТРИПЫ ═══════════ -->
<div class="section">
  <h2>🎫 Топ-10 пользователей по трипам</h2>
  <table>
    <thead><tr><th>#</th><th>Пользователь</th><th>ID</th><th>Трипов</th></tr></thead>
    <tbody>{trips_top_html if trips_top_html else '<tr><td colspan="4" style="color:var(--muted);text-align:center">Нет данных</td></tr>'}</tbody>
  </table>
</div>

<div class="chart-card">
  <h2>📈 Выдача трипов по дням (сколько людей и сколько трипов)</h2>
  <div class="period-btns" id="tripsPeriodBtns">
    <button class="period-btn" onclick="setTripsPeriod('1',this)">День</button>
    <button class="period-btn active" onclick="setTripsPeriod('7',this)">Неделя</button>
    <button class="period-btn" onclick="setTripsPeriod('30',this)">Месяц</button>
    <button class="period-btn" onclick="setTripsPeriod('365',this)">Год</button>
  </div>
  <canvas id="tripsDailyChart" style="max-height:300px"></canvas>
</div>

<!-- ═══════════ ПОДПИСКИ ═══════════ -->
<div class="two-col">
<div class="section">
  <h2>💎 Топ-10 вечных подписок</h2>
  <table>
    <thead><tr><th>#</th><th>Пользователь</th><th>ID</th><th>Тип</th><th>С даты</th></tr></thead>
    <tbody>{forever_subs_html if forever_subs_html else '<tr><td colspan="5" style="color:var(--muted);text-align:center">Нет вечных подписок</td></tr>'}</tbody>
  </table>
</div>

<div class="section">
  <h2>✅ Топ-10 по длительности подписки</h2>
  <table>
    <thead><tr><th>#</th><th>Пользователь</th><th>ID</th><th>Тип</th><th>С</th><th>По</th><th>Срок</th></tr></thead>
    <tbody>{active_subs_html if active_subs_html else '<tr><td colspan="7" style="color:var(--muted);text-align:center">Нет активных подписок</td></tr>'}</tbody>
  </table>
</div>
</div>

<!-- ═══════════ ЗАБЛОКИРОВАННЫЕ ═══════════ -->
<div class="section">
  <h2>🚫 Заблокированные пользователи</h2>
  <table>
    <thead><tr><th>Пользователь</th><th>ID</th><th>Причина</th><th>Дата бана</th></tr></thead>
    <tbody>{banned_html if banned_html else '<tr><td colspan="4" style="color:var(--muted);text-align:center">Заблокированных нет</td></tr>'}</tbody>
  </table>
</div>

<footer>Telegram Bot Stats Report · {now_str}</footer>

<script>
// ══════════════ ДАННЫЕ ══════════════
const PD = {period_data_js};

const NEWU_PERIODS = {newu_periods_js};
const NEWU_LABELS  = {newu_labels_js};
const NEWU_ARRIVE  = {newu_arrive_js};
const NEWU_DELTA   = {newu_delta_js};

// ══════════════ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ══════════════
function setActiveBtns(groupId, clicked) {{
  document.querySelectorAll('#' + groupId + ' .period-btn').forEach(b => b.classList.remove('active'));
  clicked.classList.add('active');
}}

const scaleOpts = {{
  x: {{ ticks: {{ color:'#6c7a92' }}, grid: {{ color:'#252836' }} }},
  y: {{ ticks: {{ color:'#6c7a92' }}, grid: {{ color:'#252836' }}, beginAtZero:true }}
}};

// ══════════════ ГРАФИК: НОВЫЕ ПОЛЬЗОВАТЕЛИ ══════════════
const NEWU_CUMULATIVE = {newu_cumulative_js};

let _newuMode = 'users', _newuRange = '365';

function _makeDatasets(mode, range) {{
  const p = NEWU_PERIODS[range];
  if (mode === 'users') return [{{
    label: 'Всего пользователей', data: p.cumulative,
    borderColor:'#5b8dee', backgroundColor:'rgba(91,141,238,.13)',
    tension:.4, fill:true, pointRadius:1, pointHoverRadius:5, borderWidth:2.5
  }}];
  return [
    {{ label:'Прибыло', data:p.arrive,
       borderColor:'#3ecf8e', backgroundColor:'rgba(62,207,142,.12)',
       tension:.4, fill:true, pointRadius:1, pointHoverRadius:5, borderWidth:2.5 }},
    {{ label:'Ушло', data:p.left,
       borderColor:'#f06292', backgroundColor:'rgba(240,98,146,.07)',
       tension:.4, fill:true, pointRadius:1, pointHoverRadius:5, borderWidth:2 }}
  ];
}}

const newuCtx = document.getElementById('newuChart').getContext('2d');
const newuChart = new Chart(newuCtx, {{
  type: 'line',
  data: {{
    labels: NEWU_PERIODS['365'].labels,
    datasets: _makeDatasets('users', '365')
  }},
  options: {{
    responsive: true,
    interaction: {{ mode:'index', intersect:false }},
    plugins: {{ legend: {{ labels: {{ color:'#e8eaf0' }} }} }},
    scales: scaleOpts
  }}
}});

function setNewuMode(mode, ev) {{
  _newuMode = mode;
  setActiveBtns('newuModeBtns', ev.target);
  newuChart.data.labels   = NEWU_PERIODS[_newuRange].labels;
  newuChart.data.datasets = _makeDatasets(_newuMode, _newuRange);
  newuChart.update();
}}

function setNewuRange(range, ev) {{
  _newuRange = range;
  setActiveBtns('newuRangeBtns', ev.target);
  newuChart.data.labels   = NEWU_PERIODS[_newuRange].labels;
  newuChart.data.datasets = _makeDatasets(_newuMode, _newuRange);
  newuChart.update();
}}

// ══════════════ ГРАФИК: ПОИСКИ (фильтр) ══════════════
const actCtx = document.getElementById('actChart').getContext('2d');
const actChart = new Chart(actCtx, {{
  type: 'line',
  data: {{
    labels: PD['1'].act_labels,
    datasets: [{{ label:'Поисков', data: PD['1'].act_data,
      borderColor:'#5b8dee', backgroundColor:'rgba(91,141,238,.15)',
      tension:.35, fill:true, pointRadius:4, borderWidth:2.5 }}]
  }},
  options: {{
    responsive:true,
    plugins: {{ legend: {{ labels: {{ color:'#e8eaf0' }} }} }},
    scales: scaleOpts
  }}
}});

function setActPeriod(p, groupId) {{
  setActiveBtns(groupId, event.target);
  actChart.data.labels = PD[p].act_labels;
  actChart.data.datasets[0].data = PD[p].act_data;
  actChart.update();
}}

// ══════════════ ГРАФИК: ТОП ЗАПРОСОВ (фильтр) ══════════════
const topCtx = document.getElementById('topChart').getContext('2d');
const topChart = new Chart(topCtx, {{
  type: 'bar',
  data: {{
    labels: PD['1'].top_labels,
    datasets: [{{ label:'Раз', data: PD['1'].top_data,
      backgroundColor:'#f9c74f', borderRadius:6 }}]
  }},
  options: {{
    responsive:true,
    indexAxis:'y',
    plugins: {{ legend: {{ labels: {{ color:'#e8eaf0' }} }} }},
    scales: {{
      x: {{ ticks:{{color:'#6c7a92'}}, grid:{{color:'#252836'}}, beginAtZero:true }},
      y: {{ ticks:{{color:'#6c7a92', font:{{size:11}}}}, grid:{{color:'#252836'}} }}
    }}
  }}
}});

function setTopPeriod(p, groupId) {{
  setActiveBtns(groupId, event.target);
  topChart.data.labels = PD[p].top_labels;
  topChart.data.datasets[0].data = PD[p].top_data;
  topChart.update();
}}

// ══════════════ ГРАФИК: DONUT НАЙДЕНО/НЕ НАЙДЕНО (фильтр) ══════════════
const doughCtx = document.getElementById('doughChart').getContext('2d');
const doughChart = new Chart(doughCtx, {{
  type: 'doughnut',
  data: {{
    labels: ['Найдено ✅', 'Не найдено ❌'],
    datasets: [{{ data: [PD['1'].found, PD['1'].not_found],
      backgroundColor: ['#3ecf8e','#f06292'], borderWidth:0 }}]
  }},
  options: {{
    responsive:true,
    plugins: {{ legend: {{ labels: {{ color:'#e8eaf0' }} }} }}
  }}
}});

function setDoughPeriod(p, groupId) {{
  setActiveBtns(groupId, event.target);
  doughChart.data.datasets[0].data = [PD[p].found, PD[p].not_found];
  doughChart.update();
}}

// ══════════════ ТАБЛИЦА: АКТИВНЫЕ ПОЛЬЗОВАТЕЛИ (фильтр) ══════════════
function renderAuTable(p) {{
  const tbody = document.getElementById('auTableBody');
  const rows = PD[p].active_users;
  if (!rows || rows.length === 0) {{
    tbody.innerHTML = '<tr><td colspan="4" style="color:var(--muted);text-align:center">Нет данных за период</td></tr>';
    return;
  }}
  tbody.innerHTML = rows.map((r, i) =>
    `<tr><td>${{i+1}}</td><td>${{r.name}}</td><td>${{r.uid}}</td><td><b>${{r.cnt}}</b></td></tr>`
  ).join('');
}}

function setAuPeriod(p, groupId) {{
  setActiveBtns(groupId, event.target);
  renderAuTable(p);
}}

// Первичный рендер таблицы
renderAuTable('1');

// ══════════════ ГРАФИК: НЕУДАЧНЫЕ ПО API ══════════════
const API_COLORS = {{'ALL':'#a78bfa','LEAKOSINT':'#5b8dee','BIGBASE':'#f9c74f','SQLITE':'#6c7a92'}};
const API_COLORS_FALLBACK = '#f06292';

function buildFailApiChart(p) {{
  const rows = PD[p].fail_by_api || [];
  return {{
    labels: rows.map(r => r.src),
    datasets: [{{
      label: 'Неудачных запросов',
      data:  rows.map(r => r.cnt),
      backgroundColor: rows.map(r => API_COLORS[r.src] || API_COLORS_FALLBACK),
      borderRadius: 6
    }}]
  }};
}}

const failApiCtx = document.getElementById('failApiChart').getContext('2d');
const failApiChart = new Chart(failApiCtx, {{
  type: 'bar',
  data: buildFailApiChart('1'),
  options: {{
    responsive: true,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ ticks:{{color:'#6c7a92'}}, grid:{{color:'#252836'}} }},
      y: {{ ticks:{{color:'#6c7a92'}}, grid:{{color:'#252836'}}, beginAtZero:true }}
    }}
  }}
}});

function setFailApiPeriod(p, groupId) {{
  setActiveBtns(groupId, event.target);
  const d = buildFailApiChart(p);
  failApiChart.data.labels   = d.labels;
  failApiChart.data.datasets = d.datasets;
  failApiChart.update();
}}

// ══════════════ ТАБЛИЦА: ТОП ЮЗЕРОВ С ПРОВАЛАМИ ══════════════
function renderFailUserTable(p) {{
  const tbody = document.getElementById('failUserTableBody');
  const rows = PD[p].fail_top_users || [];
  if (!rows.length) {{
    tbody.innerHTML = '<tr><td colspan="4" style="color:var(--muted);text-align:center">Нет данных</td></tr>';
    return;
  }}
  tbody.innerHTML = rows.map((r, i) =>
    `<tr><td>${{i+1}}</td><td>${{r.name}}</td><td>${{r.uid}}</td><td style="color:var(--red)"><b>${{r.cnt}}</b></td></tr>`
  ).join('');
}}

function setFailUserPeriod(p, groupId) {{
  setActiveBtns(groupId, event.target);
  renderFailUserTable(p);
}}

// ══════════════ ТАБЛИЦА: ПОСЛЕДНИЕ НЕУДАЧИ ══════════════
const SRC_BADGE = {{
  'ALL':       '<span style="color:#a78bfa">🔀 ALL</span>',
  'LEAKOSINT': '<span style="color:#5b8dee">🌍 LEAK</span>',
  'BIGBASE':   '<span style="color:#f9c74f">📊 BB</span>',
  'SQLITE':    '<span style="color:#6c7a92">📁 DB</span>',
}};

function renderLastFailTable(p) {{
  const tbody = document.getElementById('lastFailTableBody');
  const rows = PD[p].last_fails || [];
  if (!rows.length) {{
    tbody.innerHTML = '<tr><td colspan="4" style="color:var(--muted);text-align:center">Нет данных</td></tr>';
    return;
  }}
  tbody.innerHTML = rows.map(r =>
    `<tr>
      <td style="white-space:nowrap;color:var(--muted);font-size:.8rem">${{r.time}}</td>
      <td>${{SRC_BADGE[r.src] || r.src}}</td>
      <td style="color:var(--accent)">${{r.name}}</td>
      <td style="font-size:.83rem;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${{r.query}}">${{r.query}}</td>
    </tr>`
  ).join('');
}}

function setLastFailPeriod(p, groupId) {{
  setActiveBtns(groupId, event.target);
  renderLastFailTable(p);
}}

// Первичный рендер
renderFailUserTable('1');
renderLastFailTable('1');

// ══════════════ ГРАФИК ТРИПОВ ══════════════
const TRIPS_PERIODS = {trips_periods_js};

(function() {{
  const ctx = document.getElementById('tripsDailyChart');
  if (!ctx) return;

  let _tripsUsers = TRIPS_PERIODS['7'] ? TRIPS_PERIODS['7'].users : [];

  function buildTripsData(p) {{
    const d = TRIPS_PERIODS[p] || {{labels:[], users:[], total:[]}};
    _tripsUsers = d.users;
    return {{
      labels: d.labels,
      datasets: [
        {{
          label: 'Трипов выдано',
          data: d.total,
          backgroundColor: 'rgba(62,207,142,0.55)',
          borderColor: '#3ecf8e',
          borderWidth: 1.5,
          borderRadius: 4,
        }}
      ]
    }};
  }}

  const tripsChart = new Chart(ctx.getContext('2d'), {{
    type: 'bar',
    data: buildTripsData('7'),
    options: {{
      responsive: true,
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{
        legend: {{ labels: {{ color: '#e8eaf0' }} }},
        tooltip: {{
          callbacks: {{
            title: function(items) {{
              return '📅 ' + items[0].label;
            }},
            label: function(item) {{
              const trips = item.raw;
              const users = _tripsUsers[item.dataIndex] || 0;
              const avg   = users > 0 ? (trips / users).toFixed(1) : '—';
              return [
                '🎫 Трипов выдано: ' + trips,
                '👤 Людей: ' + users,
                '⚡ Среднее: ' + avg + ' трипов/чел',
              ];
            }}
          }},
          backgroundColor: 'rgba(26,29,39,0.96)',
          borderColor: '#3ecf8e',
          borderWidth: 1,
          titleColor: '#3ecf8e',
          bodyColor: '#e8eaf0',
          padding: 12,
        }}
      }},
      scales: {{
        y: {{
          type: 'linear', position: 'left', beginAtZero: true,
          title: {{ display: true, text: 'Количество трипов', color: '#3ecf8e' }},
          grid: {{ color: 'rgba(255,255,255,.06)' }},
          ticks: {{ color: '#e8eaf0' }}
        }},
        x: {{
          title: {{ display: true, text: 'Дни', color: '#6c7a92' }},
          ticks: {{ color: '#6c7a92', maxRotation: 45 }},
          grid: {{ color: 'rgba(255,255,255,.04)' }}
        }}
      }}
    }}
  }});

  window.setTripsPeriod = function(p, btn) {{
    document.querySelectorAll('#tripsPeriodBtns .period-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const d = buildTripsData(p);  // обновляет _tripsUsers внутри
    tripsChart.data.labels   = d.labels;
    tripsChart.data.datasets = d.datasets;
    tripsChart.update();
  }};
}})();
</script>
</body>
</html>"""

    buf = io.BytesIO(html.encode("utf-8"))
    buf.name = f"stats_report_{datetime.now().strftime('%d%m%Y_%H%M')}.html"
    return buf


def get_top_queries(limit: int = 20) -> list:
    """Возвращает список (query, count) самых частых запросов."""
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT query, COUNT(*) as cnt
        FROM search_history
        GROUP BY lower(query)
        ORDER BY cnt DESC
        LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_most_active_users(limit: int = 10) -> list:
    """Возвращает (user_id, username, first_name, search_count) — самые активные."""
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sh.user_id, u.username, u.first_name, COUNT(*) as cnt
        FROM search_history sh
        LEFT JOIN users u ON sh.user_id = u.user_id
        GROUP BY sh.user_id
        ORDER BY cnt DESC
        LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_new_users_period(days: int = 7) -> list:
    """Возвращает пользователей зарегистрированных за последние N дней."""
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT user_id, username, first_name, subscription_status, created_at
        FROM users
        WHERE created_at >= datetime('now', ?)
        ORDER BY created_at DESC
    """, (f'-{days} days',))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_activity_today() -> dict:
    """Статистика за сегодня."""
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    today = datetime.now().strftime('%Y-%m-%d')
    searches_today = cursor.execute(
        "SELECT COUNT(*) FROM search_history WHERE date(search_time) = ?", (today,)
    ).fetchone()[0]
    unique_today = cursor.execute(
        "SELECT COUNT(DISTINCT user_id) FROM search_history WHERE date(search_time) = ?", (today,)
    ).fetchone()[0]
    new_users_today = cursor.execute(
        "SELECT COUNT(*) FROM users WHERE date(created_at) = ?", (today,)
    ).fetchone()[0]
    found_today = cursor.execute(
        "SELECT COUNT(*) FROM search_history WHERE date(search_time) = ? AND found = 1", (today,)
    ).fetchone()[0]
    conn.close()
    return {
        "searches": searches_today,
        "unique_users": unique_today,
        "new_users": new_users_today,
        "found": found_today,
    }


def search_user_by_query(query: str) -> list:
    """Ищет пользователей по ID, username или имени."""
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    q = query.strip().lstrip("@")
    try:
        uid = int(q)
        cursor.execute("""
            SELECT user_id, username, first_name, last_name,
                   subscription_status, banned, created_at
            FROM users WHERE user_id = ?
        """, (uid,))
    except ValueError:
        cursor.execute("""
            SELECT user_id, username, first_name, last_name,
                   subscription_status, banned, created_at
            FROM users WHERE lower(username) LIKE ? OR lower(first_name) LIKE ?
        """, (f"%{q.lower()}%", f"%{q.lower()}%"))
    rows = cursor.fetchall()
    conn.close()
    return rows


def sqlite_search(query: str, include_secret: bool = False):
    """
    Быстрый поиск по SQLite. Возвращает список
    (id, raw_line, source_file, db_type, is_secret).
    Поддерживает многословный запрос и нормализацию телефонов.
    Автоматически фильтрует результаты от удалённых/переименованных файлов.
    """
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    secret_filter = "" if include_secret else " AND is_secret = 0"

    def filter_existing(rows):
        dead_files = set()
        valid = []
        for row in rows:
            _, raw_line, source_file, db_type, _ = row
            real_path = os.path.join(DATA_DIR, source_file)
            if os.path.exists(real_path):
                valid.append(row)
            else:
                dead_files.add(source_file)
        if dead_files:
            for sf in dead_files:
                cursor.execute("DELETE FROM search_data WHERE source_file = ?", (sf,))
                print(f"[sqlite_search] Авточистка: удалены записи от {sf!r}")
            conn.commit()
        return valid

    # Нормализация телефона
    if RE_PHONE.search(query.strip()):
        digits = re.sub(r"\D", "", query.strip())
        core = digits[-10:] if len(digits) >= 10 else digits
        variants = [core, "7" + core, "8" + core] if len(core) == 10 else [core]
        results = []
        seen = set()
        for v in variants:
            cursor.execute(
                f"SELECT id, raw_line, source_file, db_type, is_secret "
                f"FROM search_data WHERE search_index LIKE ?{secret_filter} LIMIT 50",
                (f"%{v}%",),
            )
            for row in cursor.fetchall():
                if row[0] not in seen:
                    seen.add(row[0])
                    results.append(row)
        results = filter_existing(results)
        conn.close()
        return results

    # Обычный поиск — все слова должны присутствовать
    words = query.strip().lower().split()
    if not words:
        conn.close()
        return []

    conditions = " AND ".join(["search_index LIKE ?" for _ in words])
    params = [f"%{w}%" for w in words]
    cursor.execute(
        f"SELECT id, raw_line, source_file, db_type, is_secret "
        f"FROM search_data WHERE {conditions}{secret_filter} LIMIT 100",
        params,
    )
    results = filter_existing(cursor.fetchall())
    conn.close()
    return results


def set_search_data_secret(record_id: int, is_secret: bool, secreted_by: int = None, secreted_query: str = None):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    if is_secret:
        cursor.execute(
            "UPDATE search_data SET is_secret = ?, secreted_by = ?, secreted_query = ? WHERE id = ?",
            (1, secreted_by, secreted_query, record_id),
        )
    else:
        cursor.execute(
            "UPDATE search_data SET is_secret = 0, secreted_by = NULL, secreted_query = NULL WHERE id = ?",
            (record_id,),
        )
    conn.commit()
    updated = cursor.rowcount > 0
    conn.close()
    return updated


def get_search_data_secret(record_id: int):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT is_secret FROM search_data WHERE id = ?", (record_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return bool(row[0])


def get_secret_search_records(limit: int = 100):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, secreted_by, secreted_query
        FROM search_data
        WHERE is_secret = 1
        ORDER BY id DESC
        LIMIT ?
    """,
        (limit,),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows



# ========== ЗАСЕКРЕЧИВАНИЕ API-РЕЗУЛЬТАТОВ ==========
import hashlib

def _make_api_hash(text: str) -> str:
    """Создаёт уникальный хеш для API-результата по его тексту."""
    return hashlib.md5(text.encode("utf-8", errors="ignore")).hexdigest()


def is_api_result_secret(content_hash: str) -> bool:
    """Проверяет, засекречен ли API-результат по его хешу."""
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM api_secret_results WHERE content_hash = ?", (content_hash,)
        )
        exists = cursor.fetchone() is not None
        conn.close()
        return exists
    except Exception as e:
        print(f"[api_secret] Ошибка проверки: {e}")
        return False


def set_api_result_secret(content_hash: str, api_source: str, is_secret: bool,
                          secreted_by: int = None, secreted_query: str = None) -> bool:
    """Засекречивает или рассекречивает API-результат."""
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        if is_secret:
            cursor.execute(
                """
                INSERT OR IGNORE INTO api_secret_results
                    (content_hash, api_source, secreted_by, secreted_query)
                VALUES (?, ?, ?, ?)
                """,
                (content_hash, api_source, secreted_by, secreted_query),
            )
        else:
            cursor.execute(
                "DELETE FROM api_secret_results WHERE content_hash = ?",
                (content_hash,),
            )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[api_secret] Ошибка обновления: {e}")
        return False


def get_all_api_secrets(limit: int = 100):
    """Возвращает список засекреченных API-результатов."""
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT content_hash, api_source, secreted_by, secreted_query, secreted_at
            FROM api_secret_results
            ORDER BY secreted_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cursor.fetchall()
        conn.close()
        return rows
    except Exception as e:
        print(f"[api_secret] Ошибка получения списка: {e}")
        return []


def sqlite_add_records(source_name: str, records: list, db_type: str = "default"):
    """
    Добавляет записи в SQLite после загрузки файла через Telegram.
    records — список списков полей, например [['Иванов', '79161234567'], ...]
    """
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    # Удаляем старые записи из этого файла, чтобы не дублировать
    cursor.execute("DELETE FROM search_data WHERE source_file = ?", (source_name,))
    rows = []
    for fields in records:
        line = ";".join(str(f).strip() for f in fields if str(f).strip())
        if line:
            rows.append((source_name, db_type, line, line.lower()))
    if rows:
        cursor.executemany(
            "INSERT INTO search_data (source_file, db_type, raw_line, search_index) VALUES (?,?,?,?)",
            rows,
        )
    conn.commit()
    conn.close()
    return len(rows)

# ========== API ПОИСК (LeakOSINT: несколько токенов из БД, по очереди при сбое) ==========
def _leakosint_format_list(result: dict) -> list:
    formatted_results = []
    for db_name, db_data in result["List"].items():
        if db_name == "No results found" or not db_data.get("Data"):
            continue
        info_leak = db_data.get("InfoLeak", "")
        for record in db_data["Data"]:
            lines = [f"🌐 ═══ {db_name} ═══"]
            if info_leak:
                lines.append(f"📋 {info_leak}")
            lines.append("")
            for field_name, field_value in record.items():
                if field_value and str(field_value).strip():
                    ftype = detect_field_type(str(field_value))
                    icon = FIELD_ICONS.get(ftype, "▪️")
                    lines.append(f"{icon} {field_name}: {field_value}")
            formatted_results.append(("\n".join(lines), None, None, False))
    return formatted_results


def _leakosint_search_one_token(query: str, limit_value: int, lang: str, token: str):
    """
    Один запрос с конкретным токеном.
    Возвращает список результатов при успешном ответе API (в т.ч. пустой список при «нет данных»).
    Возвращает None — если нужно перейти к следующему токену.
    """
    try:
        data = {
            "token": token,
            "request": query,
            "limit": limit_value,
            "lang": lang,
        }
        response = requests.post(API_URL, json=data, timeout=30)
        raw = response.text.strip()
        print(f"[LEAKOSINT] HTTP {response.status_code}, ответ: {raw[:200]}")
        if response.status_code in (401, 403, 429):
            return None
        if response.status_code >= 500:
            return None
        if not raw:
            print("[LEAKOSINT] Пустой ответ — пробуем другой токен")
            return None
        try:
            result = response.json()
        except Exception as je:
            print(f"[LEAKOSINT] Невалидный JSON: {je} | тело: {raw[:300]}")
            return None
        if "Error code" in result:
            print(
                f"[LEAKOSINT] Ошибка API: {result['Error code']} — {result.get('Error text', '')}"
            )
            return None
        if "List" not in result:
            print(f"[LEAKOSINT] Нет поля List в ответе: {list(result.keys())}")
            return None
        out = _leakosint_format_list(result)
        print(f"[LEAKOSINT] Найдено: {len(out)}")
        return out
    except requests.exceptions.ConnectionError:
        print("[LEAKOSINT] Нет соединения — пробуем другой токен")
        return None
    except requests.exceptions.Timeout:
        print("[LEAKOSINT] Таймаут — пробуем другой токен")
        return None
    except Exception as e:
        print(f"[LEAKOSINT] Ошибка: {e}")
        return None


def api_search(query: str, limit: int = 100, lang: str = "ru") -> list:
    if not API_ENABLED:
        return []
    tokens = get_builtin_tokens("leakosint")
    if not tokens:
        return []
    limit_value = max(100, min(int(limit), 10000))
    for _tid, tok in tokens:
        res = _leakosint_search_one_token(query, limit_value, lang, tok)
        if res is not None:
            return res
    print("[LEAKOSINT] Все токены не дали ответа")
    return []


# ========== BIGBASE API ПОИСК ==========
def _bigbase_field_name_ru(field_key: str) -> str:
    """Переводит технические ключи BigBase в читаемые названия полей."""
    NAMES = {
        "fio": "ФИО", "phone": "Телефон", "email": "Email", "address": "Адрес",
        "birthday": "Дата рождения", "inn": "ИНН", "snils": "СНИЛС",
        "passport": "Паспорт", "gender": "Пол", "city": "Город",
        "region": "Регион", "company": "Организация", "position": "Должность",
        "login": "Логин", "password": "Пароль", "ip": "IP-адрес",
        "car_num": "Гос. номер", "car_model": "Марка авто", "vin": "VIN",
        "ogrn": "ОГРН", "bank": "Банк", "card": "Карта", "website": "Сайт",
    }
    return NAMES.get(field_key.lower(), field_key)


# Иконки для секций BigBase (ДТП, участники, авто и т.п.)
_BIGBASE_SECTION_ICONS = {
    # ДТП / участники
    "потерпевший":    "🫥",
    "нарушитель":     "⛔",
    "виновник":       "⛔",
    "водитель":       "🪪",
    "пассажир":       "👥",
    "пешеход":        "🚶",
    "страхователь":   "📄",
    "страховщик":     "🏦",
    "владелец":       "🪪",
    "собственник":    "🪪",
    # Авто
    "автомобиль":     "🛻",
    "транспортное":   "🛻",
    "тс":             "🛻",
    # Общие
    "место":          "🏠",
    "адрес":          "🏠",
    "дата":           "🗓️",
    "документ":       "📄",
    "полис":          "📄",
    "страховой":      "📄",
    "осаго":          "📄",
    "каско":          "📄",
}

# Поля которые пропускаем при выводе (служебные/дубли)
_BIGBASE_SKIP_FIELDS = {
    "дата иземнения", "дата изменения", "date_update", "updated_at",
    "created_at", "id", "record_id",
}

# Признаки "автомобильной" секции для определения иконки заголовка
_CAR_SECTION_HINTS = {"госномер", "vin", "марка", "модель", "выпуск", "кузов", "двигатель", "цвет"}


def _bigbase_format_field(name: str, value: str) -> str:
    """Форматирует одно поле с автоопределением иконки."""
    ftype = detect_field_type(value)
    if ftype == "other":
        name_lower = name.lower()
        # Расширенное сопоставление по имени поля
        NAME_TO_FTYPE = {
            "фамилия": "fio", "имя": "fio", "отчество": "fio", "фио": "fio",
            "телефон": "phone", "мобильный": "phone", "тел": "phone",
            "email": "email", "почта": "email", "e-mail": "email",
            "адрес": "address", "место": "address", "прописка": "address",
            "дата рождения": "birthday", "дата рожд": "birthday", "д.р": "birthday",
            "инн": "inn", "снилс": "snils",
            "паспорт": "passport", "серия": "passport", "номер паспорта": "passport",
            "госномер": "car_num", "гос номер": "car_num", "гос. номер": "car_num",
            "vin": "car_num", "марка": "car_model", "модель": "car_model",
            "логин": "login", "пароль": "password", "хэш": "password",
            "огрн": "ogrn", "организация": "company", "компания": "company",
            "должность": "position", "профессия": "position",
            "банк": "bank", "карта": "card",
            "сайт": "website", "url": "website",
            "ip": "ip", "ip-адрес": "ip",
        }
        for key_name, mapped in NAME_TO_FTYPE.items():
            if key_name in name_lower:
                ftype = mapped
                break
    icon = FIELD_ICONS.get(ftype, "▪️")
    return f"  {icon} {name}: {value}"


def _bigbase_section_icon(section_title: str, fields: list) -> str:
    """Определяет иконку для заголовка секции."""
    tl = section_title.lower()
    for keyword, icon in _BIGBASE_SECTION_ICONS.items():
        if keyword in tl:
            return icon
    # Если в полях есть автомобильные данные — ставим авто
    field_names_lower = {str(f[0]).lower() for f in fields if isinstance(f, (list, tuple)) and len(f) == 2}
    if field_names_lower & _CAR_SECTION_HINTS:
        return "🚗"
    return "📍"


def _bigbase_parse_pairs(raw) -> list:
    """
    Разворачивает произвольную вложенность пар [['Ключ','Знач'], ...].
    Поддерживает: [['K','V']], [[['K','V'],...]], [[[['K','V'],...]]]
    Возвращает плоский список пар [('Ключ', 'Значение'), ...].
    """
    result = []
    if not isinstance(raw, list):
        return result

    def _walk(node):
        if not isinstance(node, list):
            return
        # Это пара ['Ключ', 'Значение']?
        if len(node) == 2 and isinstance(node[0], str) and isinstance(node[1], str):
            result.append((node[0].strip(), node[1].strip()))
            return
        # Иначе итерируем вглубь
        for item in node:
            _walk(item)

    _walk(raw)
    return result


def _bigbase_format_section(title: str, raw_data) -> list:
    """
    Форматирует одну «именованную» секцию (например, ДТП-участник или авто).
    Возвращает список строк готовых к добавлению в сообщение.
    """
    pairs = _bigbase_parse_pairs(raw_data)
    if not pairs:
        return []

    section_icon = _bigbase_section_icon(title, pairs)
    # Не дублируем иконку если заголовок уже начинается с эмодзи
    first_char = title.strip()[:2] if title.strip() else ""
    has_emoji = any(ord(c) > 0x2000 for c in first_char)
    prefix = "" if has_emoji else f"{section_icon} "
    lines = [f"\n{prefix}{title}:"]

    for field_name, field_value in pairs:
        if not field_value or field_value == "None":
            continue
        if field_name.lower() in _BIGBASE_SKIP_FIELDS:
            continue
        lines.append(_bigbase_format_field(field_name, field_value))

    # Если только заголовок и нет данных — пропускаем
    if len(lines) <= 1:
        return []
    return lines


def _bigbase_build_formatted_results(data) -> list:
    """Разбор JSON BigBase в список форматированных блоков (как раньше в bigbase_search)."""
    _BIGBASE_META_KEYS = {
        "count_in_page",
        "free_request",
        "time",
        "success",
        "subscribe_queries",
        "queries",
        "referral_stats",
        "referral_url",
        "rate",
        "api_token",
        "user",
    }

    records = []
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        for key in ("results", "data", "items", "records"):
            if key in data and isinstance(data[key], list):
                records = data[key]
                break
        if not records and "error" not in data and "message" not in data:
            if not (_BIGBASE_META_KEYS & set(data.keys())):
                records = [data]

    SKIP_KEYS = {"record_id", "id", "base_info", "base_record", "connections"}

    formatted_results = []
    for rec in records:
        if not isinstance(rec, dict):
            continue

        record_id = str(rec.get("record_id", rec.get("id", "")))

        base_info = rec.get("base_info", {})
        if isinstance(base_info, dict):
            db_name = base_info.get("name", "BigBase")
            db_desc = base_info.get("description", "")
            db_year = base_info.get("date_relevance", "")
        else:
            db_name = "BigBase"
            db_desc = ""
            db_year = ""

        header = f"🌐 ═══ {db_name}" + (f" ({db_year})" if db_year else "") + " ═══"
        lines = [header]
        if db_desc:
            lines.append(f"📋 {db_desc}")
        lines.append("")

        base_record = rec.get("base_record", [])
        if isinstance(base_record, list):
            for item in base_record:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    fname, fval = str(item[0]).strip(), str(item[1]).strip()
                    if fval and fval != "None" and fname.lower() not in _BIGBASE_SKIP_FIELDS:
                        lines.append(_bigbase_format_field(fname, fval))

        for key, value in rec.items():
            if key in SKIP_KEYS:
                continue
            if not value:
                continue

            if isinstance(value, list):
                section_lines = _bigbase_format_section(key, value)
                lines.extend(section_lines)

            elif isinstance(value, (str, int, float)):
                val = str(value).strip()
                if val and val != "None":
                    human_name = _bigbase_field_name_ru(key)
                    lines.append(_bigbase_format_field(human_name, val))

            elif isinstance(value, dict):
                section_lines = [f"\n📎 {key}:"]
                for k2, v2 in value.items():
                    if v2 and str(v2).strip() not in ("None", ""):
                        section_lines.append(
                            _bigbase_format_field(
                                _bigbase_field_name_ru(k2), str(v2).strip()
                            )
                        )
                if len(section_lines) > 1:
                    lines.extend(section_lines)

        if len(lines) > 3:
            formatted_results.append(("\n".join(lines), None, record_id or None, False))

    return formatted_results


def _bigbase_search_one_token(query: str, token: str):
    """
    Один запрос BigBase. Возвращает список (в т.ч. пустой) при принятом ответе,
    None — перейти к следующему токену.
    """
    try:
        headers = {"Authorization": token, "Content-Type": "application/json"}
        payload = {"search": query, "page": 1}
        resp = requests.post(
            BIGBASE_URL + "search", json=payload, headers=headers, timeout=20
        )
        if resp.status_code in (401, 403, 429):
            print(f"[BIGBASE] HTTP {resp.status_code} — следующий токен")
            return None
        if resp.status_code >= 500:
            print(f"[BIGBASE] HTTP {resp.status_code} — следующий токен")
            return None
        try:
            data = resp.json()
        except Exception:
            print("[BIGBASE] Невалидный JSON — следующий токен")
            return None
        if isinstance(data, dict):
            if data.get("error") or data.get("errors"):
                print("[BIGBASE] Поле error в ответе — следующий токен")
                return None
        out = _bigbase_build_formatted_results(data)
        if out:
            print(f"[BIGBASE] Найдено: {len(out)}")
        return out
    except requests.exceptions.ConnectionError:
        print("[BIGBASE] Нет соединения — следующий токен")
        return None
    except requests.exceptions.Timeout:
        print("[BIGBASE] Таймаут — следующий токен")
        return None
    except Exception as e:
        print(f"[BIGBASE] Ошибка: {e}")
        return None


def bigbase_search(query: str) -> list:
    if not BIGBASE_ENABLED:
        return []
    tokens = get_builtin_tokens("bigbase")
    if not tokens:
        return []
    for _tid, tok in tokens:
        res = _bigbase_search_one_token(query, tok)
        if res is not None:
            return res
    print("[BIGBASE] Все токены не дали ответа")
    return []


# ========== УМНЫЙ ПАРСЕР И ФОРМАТТЕР БАЗ ==========

# Регулярки для автоопределения типа поля
RE_PHONE = re.compile(
    r"(\+?7|8)?[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"
)


def normalize_phone(phone: str) -> list:
    """
    Возвращает список вариантов номера для поиска.
    Например: +79161234567 → ['79161234567', '89161234567', '9161234567']
    """
    digits = re.sub(r"\D", "", phone)
    variants = set()
    if len(digits) == 11 and digits[0] in ("7", "8"):
        core = digits[1:]          # 10 цифр без кода
        variants.add("7" + core)
        variants.add("8" + core)
        variants.add(core)
    elif len(digits) == 10:
        variants.add("7" + digits)
        variants.add("8" + digits)
        variants.add(digits)
    else:
        variants.add(digits)
    return list(variants)
RE_CAR_NUM = re.compile(
    r"[АВЕКМНОРСТУХABEKMHOPCTYX]{1}\d{3}[АВЕКМНОРСТУХABEKMHOPCTYX]{2}\d{2,3}",
    re.IGNORECASE,
)
RE_DATE = re.compile(r"\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}")
RE_EMAIL = re.compile(r"[\w.\-]+@[\w.\-]+\.\w+")
RE_INN = re.compile(r"\b\d{10,12}\b")
RE_INDEX = re.compile(r"\b\d{6}\b")

FIELD_ICONS = {
    "phone":    "📞",
    "fio":      "🪪",
    "car_num":  "🔲",
    "car_model":"🛻",
    "car_color":"🖌️",
    "address":  "🏠",
    "city":     "🏙️",
    "date":     "🗓️",
    "email":    "✉️",
    "inn":      "🔢",
    "index":    "📮",
    "tariff":   "💼",
    "price":    "💰",
    "passport": "📕",
    "document": "📄",
    "password": "🔒",
    "login":    "👤",
    "ip":       "📡",
    "bank":     "🏦",
    "card":     "🏦",
    "snils":    "🟢",
    "ogrn":     "📋",
    "birthday": "🗓️",
    "gender":   "⚥",
    "website":  "🔗",
    "company":  "💼",
    "position": "👔",
    "other":    "▫️",
}

CITY_WORDS = {
    "москва",
    "питер",
    "санкт-петербург",
    "спб",
    "новосибирск",
    "екатеринбург",
    "краснодар",
    "казань",
    "нижний новгород",
    "челябинск",
    "омск",
    "самара",
    "ростов",
    "уфа",
    "воронеж",
    "пермь",
    "волгоград",
    "красноярск",
    "саратов",
}

CAR_MODELS = {
    "toyota",
    "hyundai",
    "kia",
    "lada",
    "vaz",
    "bmw",
    "mercedes",
    "audi",
    "nissan",
    "ford",
    "renault",
    "volkswagen",
    "skoda",
    "mazda",
    "mitsubishi",
    "honda",
    "lexus",
    "volvo",
    "peugeot",
    "citroen",
    "opel",
    "chevrolet",
    "jeep",
    "subaru",
    "suzuki",
    "land rover",
    "infiniti",
    "porsche",
    "haval",
    "geely",
    "chery",
    "lifan",
}

CAR_COLORS = {
    "белый",
    "черный",
    "серый",
    "серебристый",
    "красный",
    "синий",
    "зеленый",
    "желтый",
    "оранжевый",
    "коричневый",
    "бежевый",
    "голубой",
    "фиолетовый",
    "золотой",
}

TARIFF_WORDS = {
    "эконом",
    "комфорт",
    "бизнес",
    "премиум",
    "standart",
    "economy",
    "comfort",
    "business",
    "premium",
    "xl",
    "детский",
    "грузовой",
}

ADDRESS_WORDS = {
    "ул.",
    "улица",
    "пр.",
    "проспект",
    "пер.",
    "переулок",
    "бульвар",
    "шоссе",
    "набережная",
    "площадь",
    "д.",
    "дом",
    "кв.",
    "квартира",
    "офис",
    "корп.",
}


def detect_field_type(value: str) -> str:
    """Определяет тип поля по его значению."""
    v = value.strip()
    vl = v.lower()

    if RE_PHONE.search(v):
        return "phone"
    if RE_CAR_NUM.fullmatch(v.replace(" ", "")):
        return "car_num"
    if RE_EMAIL.search(v):
        return "email"
    if RE_DATE.search(v) and len(v) < 20:
        return "date"
    
    # Паспорт РФ (4 цифры пробел 6 цифр или 10 цифр подряд)
    if re.match(r"^\d{4}\s?\d{6}$", v.replace(" ", "")):
        return "passport"
    
    # ИНН (10 или 12 цифр)
    if re.match(r"^\d{10}$|^\d{12}$", v.replace(" ", "")):
        return "inn"
    
    # СНИЛС (XXX-XXX-XXX XX или 11 цифр)
    if re.match(r"^\d{3}[-\s]?\d{3}[-\s]?\d{3}[-\s]?\d{2}$", v.replace(" ", "")):
        return "snils"
    
    # IP адрес
    if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", v):
        return "ip"
    
    # Банковская карта (16 цифр)
    if re.match(r"^\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}$", v.replace(" ", "")):
        return "card"
    
    # Сайт/URL
    if re.match(r"^https?://|^www\.|\.(ru|com|org|net|рф)", vl):
        return "website"
    
    # Пароль (содержит спецсимволы или md5 хеш)
    if re.search(r"[*#!@$%^&]", v) or re.match(r"^[a-f0-9]{32}$", vl):
        return "password"
    
    # Логин (короткое слово без пробелов)
    if len(v) <= 30 and " " not in v and re.match(r"^[a-zA-Z0-9_\-\.]+$", v):
        return "login"
    
    if any(w in vl for w in TARIFF_WORDS):
        return "tariff"
    if any(w in vl for w in CAR_COLORS) and len(v.split()) <= 2:
        return "car_color"
    if any(m in vl for m in CAR_MODELS):
        return "car_model"
    
    # --- УЛУЧШЕННОЕ ОПРЕДЕЛЕНИЕ АДРЕСА ---
    # Проверяем наличие ключевых слов, характерных для адреса
    address_keywords = [
        "ул.", "улица", "пр.", "проспект", "пер.", "переулок", "бульвар", "шоссе", 
        "набережная", "площадь", "д.", "дом", "кв.", "квартира", "офис", "корп.",
        "г.", "город", "респ.", "республика", "край", "обл.", "область", "р-н", "район",
        "пос.", "поселок", "с.", "село", "дер.", "деревня"
    ]
    # Если есть хотя бы одно ключевое слово адреса
    if any(kw in vl for kw in address_keywords):
        return "address"
    
    # Дополнительная проверка для адресов в формате "город, улица, дом"
    # Если есть запятые и длина строки больше 10 символов - возможно, это адрес
    if ',' in v and len(v) > 15:
        # Проверяем, не похоже ли это на ФИО (ФИО обычно короче и без цифр)
        if not any(c.isdigit() for c in v) and len(v.split()) <= 3:
            return "fio"
        return "address"
        
    if any(w in vl for w in CITY_WORDS):
        return "city"
    # --- КОНЕЦ УЛУЧШЕНИЙ АДРЕСА ---

    # ОГРН/ОГРНИП (13 или 15 цифр)
    if re.match(r"^\d{13}$|^\d{15}$", v.replace(" ", "")):
        return "ogrn"
    
    # День рождения
    if re.search(r"\d{1,2}[./-]\d{1,2}[./-](19|20)\d{2}", v):
        return "birthday"
    
    # Пол
    if vl in ["муж", "мужской", "мужчина", "м", "male", "m"]:
        return "gender"
    if vl in ["жен", "женский", "женщина", "ж", "female", "f"]:
        return "gender"
    
    # Компания (ООО, ИП, ЗАО и т.д.)
    if re.search(r"\b(ООО|ИП|ЗАО|ОАО|ПАО|АО|НКО)\b", v.upper()):
        return "company"
    
    # ФИО: 2-3 слова, только буквы
    words = v.split()
    if 2 <= len(words) <= 3 and all(
        re.match(r"^[А-ЯЁа-яёA-Za-z\-]+$", w) for w in words
    ):
        return "fio"
    
    if any(w in vl for w in {"руб", "₽", "usd", "$", "eur", "€"}):
        return "price"
    
    # Документ
    if any(w in vl for w in ["документ", "паспорт", "удостоверение", "свидетельство", "сертификат"]):
        return "document"
    
    return "other"

def smart_format_record(
    parts: list, db_type: str = "default", filename: str = ""
) -> str:
    """
    Принимает список строк (поля записи) и возвращает
    красиво отформатированный текст с иконками.
    Автоматически определяет тип каждого поля.
    """
    parts = [p.strip() for p in parts if p.strip()]
    if not parts:
        return ""

    if filename:
        # Убираем расширение .txt и показываем имя файла как заголовок
        display_name = os.path.splitext(filename)[0]
        header_map = {
            "avto": f"🚗 ═══ {display_name} ═══",
            "imush": f"🏠 ═══ {display_name} ═══",
            "work": f"💼 ═══ {display_name} ═══",
            "people": f"👤 ═══ {display_name} ═══",
            "default": f"📋 ═══ {display_name} ═══",
        }
    else:
        header_map = {
            "avto": "🚗 ═══ АВТО ═══",
            "imush": "🏠 ═══ ИМУЩЕСТВО ═══",
            "work": "💼 ═══ СОТРУДНИК ═══",
            "people": "👤 ═══ ДАННЫЕ ═══",
            "default": "📋 ═══ РЕЗУЛЬТАТ ═══",
        }
    header = header_map.get(db_type, header_map["default"])

    lines = [header]
    for part in parts:
        ftype = detect_field_type(part)
        icon = FIELD_ICONS.get(ftype, "▪️")
        lines.append(f"{icon} {part}")

    return "\n".join(lines)


def get_file_headers(filepath: str) -> list:
    """Читает заголовки: сначала из .headers файла, потом из первой строки txt"""
    # 1. Проверяем .headers файл
    headers_path = filepath.replace(".txt", ".headers")
    if os.path.exists(headers_path):
        try:
            with open(headers_path, "r", encoding="utf-8") as hf:
                line = hf.readline().strip()
                if line:
                    return [h.strip() for h in line.split(";")]
        except:
            pass

    # 2. Читаем первую строку txt — проверяем что это заголовок
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            first_line = f.readline().strip()
        if not first_line:
            return []
        parts = [p.strip() for p in first_line.split(";")]
        has_phone = any(c.isdigit() for p in parts for c in p if len([x for x in p if x.isdigit()]) > 6)
        has_date  = any("." in p and p.replace(".", "").replace(" ", "").replace(":", "").isdigit() for p in parts)
        has_email = any("@" in p for p in parts)
        is_header = not has_email and not has_date and all(len(p) < 50 for p in parts)
        if is_header:
            return parts
    except:
        pass
    return []

def smart_format_with_headers(parts: list, headers: list, db_type: str = "default", filename: str = "") -> str:
    """
    Форматирует запись используя заголовки из первой строки файла.
    Например: id;email;phone → ID: 123 / Email: test@mail.ru / Телефон: +7...
    """
    parts = [p.strip() for p in parts if p.strip()]
    if not parts:
        return ""

    display_name = os.path.splitext(filename)[0] if filename else "РЕЗУЛЬТАТ"
    
    # Иконки для типичных названий колонок
    header_icons = {
        "id": "🆔", "yauid": "🆔", "uid": "🆔",
        "username": "👤", "login": "🔑", "логин": "🔑", "user": "👤",
        "email": "📧", "e-mail": "📧", "почта": "📧", "mail": "📧",
        "phone": "📱", "phone_number": "📱", "телефон": "📱", "тел": "📱",
        "first_name": "👤", "last_name": "👤", "name": "👤",
        "имя": "👤", "фамилия": "👤", "отчество": "👤", "фио": "👤",
        "address": "🏠", "адрес": "🏠",
        "date": "📅", "дата": "📅", "birthday": "📅",
        "city": "🏙", "город": "🏙",
        "password": "🔒", "пароль": "🔒", "pass": "🔒", "pin": "🔒",
        "активность": "✅", "activity": "✅",
        "inn": "🏢", "инн": "🏢",
        "default": "▪️"
    }

    type_header_map = {
        "avto": f"🚗 ═══ {display_name} ═══",
        "imush": f"🏠 ═══ {display_name} ═══",
        "work": f"💼 ═══ {display_name} ═══",
        "people": f"👤 ═══ {display_name} ═══",
        "default": f"📋 ═══ {display_name} ═══",
    }
    header_line = type_header_map.get(db_type, type_header_map["default"])
    lines = [header_line]

    for i, part in enumerate(parts):
        if headers and i < len(headers):
            col_name = headers[i]
            icon = header_icons.get(col_name.lower(), "▪️")
            lines.append(f"{icon} {col_name}: {part}")
        else:
            # Нет заголовка — определяем тип автоматически
            from_type = detect_field_type(part)
            icon = FIELD_ICONS.get(from_type, "▪️")
            lines.append(f"{icon} {part}")

    return "\n".join(lines)


def detect_db_type_by_name(filename: str) -> str:
    """Определяет тип базы по имени файла."""
    fn = filename.lower()
    if any(
        k in fn
        for k in ["avto", "авто", "auto", "car", "машин", "такси", "taxi", "vehicle"]
    ):
        return "avto"
    if any(
        k in fn
        for k in ["imush", "имущ", "недвиж", "nedvig", "realty", "квартир", "property"]
    ):
        return "imush"
    if any(k in fn for k in ["work", "работ", "сотруд", "employee", "staff", "worker"]):
        return "work"
    if any(
        k in fn
        for k in [
            "fio",
            "фио",
            "люди",
            "people",
            "person",
            "клиент",
            "client",
            "phone",
            "телефон",
        ]
    ):
        return "people"
    return "default"


def detect_db_type_by_content(lines: list) -> str:
    """Определяет тип базы по содержимому — анализирует первые 20 строк."""
    sample = " ".join(lines[:20]).lower()
    car_score = sum(
        [
            bool(RE_CAR_NUM.search(sample)),
            any(m in sample for m in CAR_MODELS),
            any(c in sample for c in CAR_COLORS),
            any(t in sample for t in TARIFF_WORDS),
        ]
    )
    imush_score = sum(
        [
            any(
                w in sample
                for w in ["кв.", "квартира", "комнат", "этаж", "площадь", "кадастр"]
            ),
        ]
    )
    work_score = sum(
        [
            any(
                w in sample
                for w in ["должность", "отдел", "компания", "организация", "зарплата"]
            ),
        ]
    )
    scores = {"avto": car_score, "imush": imush_score, "work": work_score}
    best = max(scores, key=scores.get)
    if scores[best] > 0:
        return best
    return "people"  # по умолчанию — люди/контакты


def parse_uploaded_file(raw_bytes: bytes, filename: str) -> tuple[list, str, str]:
    """
    Разбирает загруженный файл (txt/csv/xlsx).
    Возвращает: (список_записей, разделитель, тип_базы)
    Каждая запись — список строк (полей).
    """
    ext = os.path.splitext(filename)[1].lower()
    records = []
    lines_all = []

    # ===== EXCEL =====
    if ext in (".xlsx", ".xls") and EXCEL_SUPPORT:
        try:
            wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            # Первая строка — заголовки (если есть текст)
            if rows:
                for row in rows[1:]:  # пропускаем заголовок
                    fields = [
                        str(c).strip() for c in row if c is not None and str(c).strip()
                    ]
                    if fields:
                        records.append(fields)
                        lines_all.extend(fields)
        except Exception as e:
            pass
        db_type = detect_db_type_by_name(filename)
        if db_type == "default":
            db_type = detect_db_type_by_content(lines_all)
        return records, "xlsx", db_type

    # ===== TXT / CSV =====
    for enc in ("utf-8", "cp1251", "utf-8-sig"):
        try:
            text = raw_bytes.decode(enc)
            break
        except:
            continue
    else:
        text = raw_bytes.decode("utf-8", errors="replace")

    lines = text.splitlines()
    lines_all = [l.strip() for l in lines if l.strip()]

    # Определяем разделитель
    semicolon_count = sum(1 for l in lines_all if ";" in l)
    comma_count = sum(1 for l in lines_all if "," in l)
    tab_count = sum(1 for l in lines_all if "\t" in l)

    if semicolon_count > len(lines_all) * 0.4:
        sep = ";"
    elif tab_count > len(lines_all) * 0.4:
        sep = "\t"
    elif comma_count > len(lines_all) * 0.4:
        sep = ","
    else:
        sep = None  # блоки разделённые пустой строкой

    if sep:
        for line in lines_all:
            parts = [p.strip() for p in line.split(sep) if p.strip()]
            if parts:
                records.append(parts)
    else:
        # Блочный формат
        for block in text.strip().split("\n\n"):
            block_lines = [l.strip() for l in block.splitlines() if l.strip()]
            if block_lines:
                records.append(block_lines)

    db_type = detect_db_type_by_name(filename)
    if db_type == "default":
        db_type = detect_db_type_by_content(lines_all)

    return records, sep or "blocks", db_type


# ========== ФУНКЦИЯ ПРОВЕРКИ ПОДПИСКИ (С КЭШЕМ) ==========
subscription_check_cache = {}
last_cache_cleanup = time.time()


def check_subscription(user_id):
    global subscription_check_cache, last_cache_cleanup

    if user_id == MAIN_ADMIN_ID:
        return True

    # Очистка кэша раз в 10 минут
    if time.time() - last_cache_cleanup > 600:
        subscription_check_cache.clear()
        last_cache_cleanup = time.time()

    # Проверяем кэш (кэш на 5 минут)
    if user_id in subscription_check_cache:
        cache_time, status = subscription_check_cache[user_id]
        if time.time() - cache_time < 300:
            return status

    try:
        member = bot.get_chat_member(REQUIRED_CHANNEL_ID, user_id)
        status = member.status in ["member", "administrator", "creator"]

        # Сохраняем в кэш
        subscription_check_cache[user_id] = (time.time(), status)
        return status
    except Exception as e:
        logging.error(f"Ошибка проверки подписки: {e}")
        return False


# ========== ФУНКЦИИ РАБОТЫ С БАЗОЙ ==========
def get_user_subscription(user_id):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute(
        "SELECT subscription_status, subscription_end FROM users WHERE user_id = ?",
        (user_id,),
    )
    result = cursor.fetchone()
    conn.close()

    if result:
        status, end_date = result
        if status == "active" and end_date:
            try:
                if (
                    datetime.strptime(end_date, "%Y-%m-%d").date()
                    < datetime.now().date()
                ):
                    update_subscription_status(user_id, "expired")
                    return "expired"
            except:
                pass
        return status
    return None


def save_search_history(user_id, query, found, api_source=None):
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO search_history (user_id, query, found, search_time, api_source)
            VALUES (?, ?, ?, ?, ?)
        """,
            (
                user_id,
                query,
                1 if found else 0,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                api_source,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Ошибка сохранения истории: {e}")


def get_user_history_with_first(cursor, user_id, limit=10, include_first=False):
    """
    Возвращает последние `limit` запросов пользователя.
    include_first=True — добавляет самый первый запрос в конец списка (только для админов).
    """
    cursor.execute(
        """
        SELECT id, query, found, search_time
        FROM search_history
        WHERE user_id = ?
        ORDER BY datetime(search_time) DESC
        LIMIT ?
    """,
        (user_id, limit),
    )
    recent_history = cursor.fetchall()

    if include_first:
        cursor.execute(
            """
            SELECT id, query, found, search_time
            FROM search_history
            WHERE user_id = ?
            ORDER BY datetime(search_time) ASC
            LIMIT 1
        """,
            (user_id,),
        )
        first_record = cursor.fetchone()
        if first_record and all(row[0] != first_record[0] for row in recent_history):
            recent_history.append(first_record)

    return recent_history


def update_subscription_status(user_id, status, sub_type=None, days=0):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    now = datetime.now()
    start_date = now.date()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user_exists = cursor.fetchone()

    if status == "active" and days > 0:
        end_date = start_date + timedelta(days=days)
        if user_exists:
            cursor.execute(
                "UPDATE users SET subscription_status=?, subscription_type=?, subscription_start=?, subscription_end=? WHERE user_id=?",
                (status, sub_type, start_date, end_date, user_id),
            )
        else:
            cursor.execute(
                "INSERT INTO users (user_id, subscription_status, subscription_type, subscription_start, subscription_end, created_at) VALUES (?,?,?,?,?,?)",
                (user_id, status, sub_type, start_date, end_date, now),
            )
    elif status == "forever":
        if user_exists:
            cursor.execute(
                "UPDATE users SET subscription_status=?, subscription_type=?, subscription_end=NULL WHERE user_id=?",
                ("forever", "forever", user_id),
            )
        else:
            cursor.execute(
                "INSERT INTO users (user_id, subscription_status, subscription_type, created_at) VALUES (?,?,?,?)",
                (user_id, "forever", "forever", now),
            )
    else:
        # inactive / expired — сбрасываем подписку
        if user_exists:
            cursor.execute(
                "UPDATE users SET subscription_status='inactive', subscription_type=NULL, subscription_end=NULL WHERE user_id=?",
                (user_id,),
            )
    conn.commit()
    conn.close()
    # Сбрасываем кэш подписки
    global subscription_check_cache
    if user_id in subscription_check_cache:
        del subscription_check_cache[user_id]


def add_or_update_user(message):
    user_id = message.from_user.id
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    is_new = not cursor.fetchone()
    if is_new:
        cursor.execute(
            "INSERT INTO users (user_id, username, first_name, last_name, created_at) VALUES (?,?,?,?,?)",
            (
                user_id,
                message.from_user.username,
                message.from_user.first_name,
                message.from_user.last_name,
                datetime.now(),
            ),
        )
        # Логируем событие прихода нового пользователя
        cursor.execute(
            "INSERT INTO user_events (user_id, event_type, event_at) VALUES (?, 'joined', ?)",
            (user_id, datetime.now()),
        )
    conn.commit()
    conn.close()


# ========== ФУНКЦИИ ДЛЯ РАБОТЫ С КОДАМИ ==========


def create_admin_code(admin_id, custom_code=None, duration="day", permissions="all"):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()

    if custom_code:
        cursor.execute(
            "SELECT id FROM admin_codes WHERE code = ? AND used_by IS NULL",
            (custom_code,),
        )
        if cursor.fetchone():
            conn.close()
            return None, "Код уже существует"
        code = custom_code
    else:
        while True:
            code = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
            cursor.execute(
                "SELECT id FROM admin_codes WHERE code = ? AND used_by IS NULL", (code,)
            )
            if not cursor.fetchone():
                break

    cursor.execute(
        """
        INSERT INTO admin_codes (code, created_by, duration, permissions)
        VALUES (?, ?, ?, ?)
    """,
        (code, admin_id, duration, permissions),
    )

    conn.commit()
    conn.close()
    return code, None


def get_duration_text(duration):
    durations = {"hour": "1 час", "day": "1 день", "forever": "навсегда"}
    return durations.get(duration, "1 день")


def check_admin_code(code, user_id):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM admin_codes WHERE code = ? AND used_by IS NULL", (code,)
    )
    code_data = cursor.fetchone()

    if code_data:
        cursor.execute(
            """
            UPDATE admin_codes SET used_by = ?, used_at = ?
            WHERE code = ?
        """,
            (user_id, datetime.now(), code),
        )
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False


def get_user_file_permission(user_id):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT duration, used_at FROM admin_codes
        WHERE used_by = ?
    """,
        (user_id,),
    )
    result = cursor.fetchone()
    conn.close()

    if not result:
        return False

    duration, used_at = result

    if duration == "forever":
        return True

    if used_at:
        try:
            if "." in used_at:
                used_time = datetime.strptime(used_at, "%Y-%m-%d %H:%M:%S.%f")
            else:
                used_time = datetime.strptime(used_at, "%Y-%m-%d %H:%M:%S")

            if duration == "hour" and datetime.now() - used_time < timedelta(hours=1):
                return True
            elif duration == "day" and datetime.now() - used_time < timedelta(days=1):
                return True
        except:
            return False
    return False


def cleanup_old_codes():
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM admin_codes
            WHERE created_at < datetime('now', '-30 days')
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Ошибка очистки кодов: {e}")


cleanup_old_codes()


# ========== ФУНКЦИИ АНТИФЛУДА ==========
def check_flood(user_id):
    current_time = time.time()

    if user_id in user_messages:
        user_messages[user_id] = [
            ts
            for ts in user_messages[user_id]
            if current_time - ts < flood_settings["period_seconds"]
        ]
    else:
        user_messages[user_id] = []

    user_messages[user_id].append(current_time)

    if len(user_messages[user_id]) > flood_settings["max_messages"]:
        return False
    return True


def add_warning(user_id):
    if user_id == MAIN_ADMIN_ID:
        return False, 0

    if user_id not in user_warnings:
        user_warnings[user_id] = 1
    else:
        user_warnings[user_id] += 1

    warnings = user_warnings[user_id]

    if warnings >= flood_settings["warnings_before_ban"]:
        ban_user(MAIN_ADMIN_ID, user_id, f"Автобан за флуд")
        if user_id in user_messages:
            del user_messages[user_id]
        if user_id in user_warnings:
            del user_warnings[user_id]
        return True, warnings

    return False, warnings


def reset_flood_counter(user_id):
    if user_id in user_messages:
        del user_messages[user_id]


# ========== ФУНКЦИИ ДЛЯ БАНА ==========
def check_banned(user_id):
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute(
            "SELECT banned, ban_reason FROM users WHERE user_id = ?", (user_id,)
        )
        result = cursor.fetchone()
        conn.close()

        if result and result[0] == 1:
            return True, result[1]
        return False, None
    except Exception as e:
        print(f"Ошибка проверки бана: {e}")
        return False, None


def ban_user(admin_id, user_id, reason=None):
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()

        if user_id == MAIN_ADMIN_ID:
            conn.close()
            return False, "Нельзя забанить главного админа!"

        # Запрещаем банить действующих администраторов
        cursor.execute(
            "SELECT 1 FROM admin_permissions WHERE user_id = ? AND is_active = 1",
            (user_id,),
        )
        if cursor.fetchone():
            conn.close()
            return False, "Нельзя забанить администратора! Сначала отзовите его права через 'Управление админами'."

        cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        if not cursor.fetchone():
            conn.close()
            return False, f"Пользователь {user_id} не найден в базе"

        # Добавляем колонку banned_at если ещё нет
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN banned_at TEXT")
            conn.commit()
        except Exception:
            pass  # уже существует

        cursor.execute(
            "UPDATE users SET banned = 1, ban_reason = ?, banned_at = datetime('now') WHERE user_id = ?",
            (reason, user_id),
        )
        conn.commit()
        conn.close()

        return True, f"Пользователь {user_id} забанен"
    except Exception as e:
        return False, str(e)


def unban_user(admin_id, user_id):
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET banned = 0, ban_reason = NULL WHERE user_id = ?",
            (user_id,),
        )
        affected = cursor.rowcount
        conn.commit()
        conn.close()

        if affected > 0:
            return True, f"Пользователь {user_id} разбанен"
        else:
            return False, f"Пользователь {user_id} не найден"
    except Exception as e:
        return False, str(e)


def get_banned_users():
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, username, first_name, ban_reason, created_at
            FROM users
            WHERE banned = 1
            ORDER BY created_at DESC
        """)
        users = cursor.fetchall()
        conn.close()
        return users
    except Exception as e:
        print(f"Ошибка получения списка: {e}")
        return []


# ========== ФУНКЦИИ ДЛЯ УПРАВЛЕНИЯ АДМИН-ПРАВАМИ ==========
def grant_admin_permission(admin_id, target_user_id, days=30):
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        expires_at = datetime.now() + timedelta(days=days)
        cursor.execute(
            "SELECT id FROM admin_permissions WHERE user_id = ?", (target_user_id,)
        )
        existing = cursor.fetchone()

        if existing:
            cursor.execute(
                """
                UPDATE admin_permissions
                SET granted_by = ?, expires_at = ?, is_active = 1, granted_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
            """,
                (admin_id, expires_at, target_user_id),
            )
        else:
            cursor.execute(
                """
                INSERT INTO admin_permissions (user_id, granted_by, expires_at)
                VALUES (?, ?, ?)
            """,
                (target_user_id, admin_id, expires_at),
            )

        conn.commit()
        conn.close()
        return True, expires_at.strftime("%d.%m.%Y %H:%M")
    except Exception as e:
        return False, str(e)


def revoke_admin_permission(admin_id, target_user_id):
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()

        # Отзываем права из admin_permissions
        cursor.execute(
            """
            UPDATE admin_permissions
            SET is_active = 0
            WHERE user_id = ? AND is_active = 1
        """,
            (target_user_id,),
        )
        affected_perms = cursor.rowcount

        # Также обнуляем используемые коды, чтобы права через коды тоже пропали
        cursor.execute(
            """
            UPDATE admin_codes
            SET used_by = NULL, used_at = NULL
            WHERE used_by = ?
        """,
            (target_user_id,),
        )
        affected_codes = cursor.rowcount

        conn.commit()
        conn.close()

        affected = affected_perms + affected_codes
        return (
            affected > 0,
            "Права отозваны" if affected > 0 else "Пользователь не найден в базе прав",
        )
    except Exception as e:
        return False, str(e)


def get_admin_permissions_list():
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                ap.user_id,
                u.username,
                u.first_name,
                ap.granted_at,
                ap.expires_at,
                ap.is_active
            FROM admin_permissions ap
            LEFT JOIN users u ON ap.user_id = u.user_id
            WHERE ap.is_active = 1
            ORDER BY ap.expires_at DESC
        """)
        permissions = cursor.fetchall()
        conn.close()
        return permissions
    except Exception as e:
        print(f"Ошибка получения списка: {e}")
        return []


def check_admin_permission(user_id):
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT expires_at, permissions FROM admin_permissions
            WHERE user_id = ? AND is_active = 1
        """,
            (user_id,),
        )
        result = cursor.fetchone()

        if result:
            expires_str, perms_str = result
            perms_list = perms_str.split(",") if perms_str else []
            try:
                if "." in expires_str:
                    expires = datetime.strptime(expires_str, "%Y-%m-%d %H:%M:%S.%f")
                else:
                    expires = datetime.strptime(expires_str, "%Y-%m-%d %H:%M:%S")

                if expires > datetime.now():
                    conn.close()
                    return True, expires, perms_list
                else:
                    cursor.execute(
                        "UPDATE admin_permissions SET is_active = 0 WHERE user_id = ?",
                        (user_id,),
                    )
                    conn.commit()
                    conn.close()
                    return False, None, []
            except Exception as e:
                print(f"Ошибка парсинга expires_at: {e}")
                conn.close()
                return False, None, []

        cursor.execute(
            """
            SELECT duration, used_at, permissions FROM admin_codes
            WHERE used_by = ?
        """,
            (user_id,),
        )
        code_result = cursor.fetchone()

        if code_result:
            duration, used_at, code_perms_str = code_result
            code_perms_list = (
                code_perms_str.split(",")
                if code_perms_str and code_perms_str != "all"
                else list(ALL_PERMISSIONS.keys())
            )

            if duration == "forever":
                conn.close()
                return True, datetime.now() + timedelta(days=3650), code_perms_list

            if used_at:
                try:
                    if "." in used_at:
                        used_time = datetime.strptime(used_at, "%Y-%m-%d %H:%M:%S.%f")
                    else:
                        used_time = datetime.strptime(used_at, "%Y-%m-%d %H:%M:%S")

                    if duration == "hour" and datetime.now() - used_time < timedelta(
                        hours=1
                    ):
                        expires = used_time + timedelta(hours=1)
                        conn.close()
                        return True, expires, code_perms_list

                    elif duration == "day" and datetime.now() - used_time < timedelta(
                        days=1
                    ):
                        expires = used_time + timedelta(days=1)
                        conn.close()
                        return True, expires, code_perms_list
                except Exception as e:
                    print(f"Ошибка парсинга used_at: {e}")
                    conn.close()
                    return False, None, []

        conn.close()
        return False, None, []
    except Exception as e:
        print(f"Ошибка проверки прав: {e}")
        return False, None, []


def has_perm(user_id, perm_key):
    """Проверяет, есть ли у пользователя конкретное право.
    Главный владелец (MAIN_ADMIN_ID) имеет все права всегда."""
    if user_id == MAIN_ADMIN_ID:
        return True
    active, _, perms_list = check_admin_permission(user_id)
    if not active:
        return False
    # Если список пустой — считаем "все права" (совместимость со старыми записями)
    if not perms_list:
        return True
    return perm_key in perms_list


# ========== ФУНКЦИИ ПОДДЕРЖКИ ==========
def save_support_message(user_id, message_text, is_from_user=True, admin_id=0):
    """Сохраняет сообщение поддержки в БД"""
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO support_history (user_id, admin_id, message, is_from_user, created_at)
            VALUES (?, ?, ?, ?, ?)
        """,
            (user_id, admin_id, message_text, 1 if is_from_user else 0, datetime.now()),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logging.error(f"Ошибка сохранения сообщения: {e}")
        return False


def get_waiting_users():
    """Возвращает список пользователей, у которых последнее сообщение — это вопрос без ответа"""
    waiting = []
    try:
        conn = sqlite3.connect("users.db")
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Выбираем последние сообщения для каждого пользователя
        # Если статус последнего сообщения is_from_user = 1, значит пользователь ждет ответа
        cursor.execute("""
            SELECT h.user_id, h.message, h.created_at, u.first_name, u.username
            FROM support_history h
            JOIN users u ON h.user_id = u.user_id
            WHERE h.id IN (
                SELECT MAX(id) FROM support_history GROUP BY user_id
            )
            AND h.is_from_user = 1
            ORDER BY h.created_at DESC
        """)

        rows = cursor.fetchall()
        for row in rows:
            name = row["first_name"] or row["username"] or f"ID: {row['user_id']}"
            last_msg = (
                row["message"][:50] + "..."
                if len(row["message"]) > 50
                else row["message"]
            )

            waiting.append(
                {
                    "id": row["user_id"],
                    "name": name,
                    "last_message": last_msg,
                    "last_time": row["created_at"],
                }
            )
        conn.close()
    except Exception as e:
        logging.error(f"Ошибка в get_waiting_users: {e}")

    return waiting


def notify_admins_support(user_id, user_name, message_text=None):
    """Уведомляет всех админов о новом обращении"""
    global notified_support_users

    current_time = time.time()
    if user_id in notified_support_users:
        if current_time - notified_support_users[user_id] < 300:  # 5 минут
            return
    notified_support_users[user_id] = current_time

    admins = get_admin_permissions_list()
    admin_ids = [a[0] for a in admins]
    if MAIN_ADMIN_ID not in admin_ids:
        admin_ids.append(MAIN_ADMIN_ID)

    preview = ""
    if message_text:
        if len(message_text) > 100:
            preview = f"\n💬 **Сообщение:** {message_text[:100]}..."
        else:
            preview = f"\n💬 **Сообщение:** {message_text}"

    for admin_id in admin_ids:
        if admin_id == user_id:
            continue
        try:
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton(
                    "💬 Ответить", callback_data=f"start_livechat_{user_id}"
                )
            )
            bot.send_message(
                admin_id,
                f"🆘 **НОВОЕ ОБРАЩЕНИЕ В ПОДДЕРЖКУ!**\n\n"
                f"👤 **Пользователь:** {user_name}\n"
                f"🆔 **ID:** `{user_id}`{preview}\n\n"
                f"👇 Нажмите кнопку чтобы начать диалог:",
                parse_mode="Markdown",
                reply_markup=markup,
            )
        except:
            pass


# ========== КРАСИВОЕ МЕНЮ ==========
def main_menu():
    """Нижняя кнопка '📋 Меню' — всегда видна на главном экране."""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1, one_time_keyboard=False)
    markup.add(types.KeyboardButton("📋 Меню"))
    return markup


def back_menu():
    """Нижняя кнопка '🔙 Назад' — показывается внутри любого раздела."""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1, one_time_keyboard=False)
    markup.add(types.KeyboardButton("🔙 Назад"))
    return markup


def _apply_keyboard(chat_id, reply_markup, text="​"):
    """Применяет keyboard markup тихим сообщением."""
    try:
        bot.send_message(chat_id, text, reply_markup=reply_markup, disable_notification=True)
    except:
        pass


def show_bottom_menu(chat_id):
    _apply_keyboard(chat_id, main_menu())


def show_back_button(chat_id):
    _apply_keyboard(chat_id, back_menu())


def clear_bottom_menu(chat_id):
    _apply_keyboard(chat_id, types.ReplyKeyboardRemove())


def _back_markup():
    """Inline-клавиатура с одной кнопкой Назад."""
    m = types.InlineKeyboardMarkup()
    m.add(types.InlineKeyboardButton("🔙 Назад", callback_data="menu_profile"))
    return m


def _edit_to_content(call, text, extra_markup=None, parse_mode="Markdown"):
    """
    Редактирует сообщение меню: текст заменяется на content, снизу кнопка Назад.
    extra_markup — InlineKeyboardMarkup с доп. кнопками (будут над Назад).
    """
    markup = types.InlineKeyboardMarkup()
    if extra_markup:
        for row in extra_markup.keyboard:
            markup.row(*[btn for btn in row])
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="menu_profile"))
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode=parse_mode,
        )
    except Exception as e:
        print(f"[_edit_to_content] {e}")


def _edit_menu_add_back(call):
    """Оставлено для совместимости — теперь просто меняет разметку."""
    try:
        bot.edit_message_reply_markup(
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_back_markup(),
        )
    except:
        pass


def show_main_menu(chat_id, menu_text, user_id=None, photo_id=None):
    if user_id is not None and user_id in waiting_for_search:
        waiting_for_search[user_id] = False
    if photo_id:
        bot.send_photo(chat_id, photo_id, caption=menu_text, reply_markup=main_menu())
    else:
        bot.send_message(chat_id, menu_text, reply_markup=main_menu())
    bot.send_message(chat_id, "👇 Выберите действие:", reply_markup=inline_main_menu())


def inline_main_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🔍 Найти инфу", callback_data="menu_search"),
        types.InlineKeyboardButton("💳 Подписка", callback_data="menu_subscribe"),
    )
    markup.add(
        types.InlineKeyboardButton("📜 История поиска", callback_data="menu_history"),
        types.InlineKeyboardButton("ℹ️ Возможности", callback_data="menu_help"),
    )
    markup.add(types.InlineKeyboardButton("🆘 Поддержка", callback_data="menu_support"))
    # ── Кастомная кнопка (название редактируется через /texts → custom_button_label) ──
    custom_label = get_bot_text("custom_button_label")
    if custom_label and custom_label.strip():
        markup.add(types.InlineKeyboardButton(custom_label.strip(), callback_data="menu_custom"))
    return markup


def inline_subscribe_menu():
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("💎 100 ₽ - 1 день", callback_data="sub_1day"))
    markup.add(types.InlineKeyboardButton("💎 500 ₽ - 7 дней", callback_data="sub_7days"))
    markup.add(types.InlineKeyboardButton("💎 1000 ₽ - 30 дней", callback_data="sub_30days"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="menu_profile"))
    return markup


# ========== АНИМАЦИЯ ПОИСКА ==========
def show_search_result_page(chat_id, user_id, page, message_id=None):
    """Показывает страницу с результатом поиска и кнопками навигации"""
    if user_id not in user_search_results:
        return
    
    data = user_search_results[user_id]
    results = data["results"]
    total = len(results)

    # Защита от пустого списка (может случиться если поиск ещё идёт или результаты сброшены)
    if total == 0:
        try:
            if message_id:
                bot.edit_message_text("⏳ Результаты ещё загружаются...", chat_id, message_id)
            else:
                bot.send_message(chat_id, "⏳ Результаты ещё загружаются...")
        except:
            pass
        return
    
    if page < 0:
        page = 0
    if page >= total:
        page = total - 1
    
    data["current_page"] = page
    text_info, photo_path, rec_id, is_secret, db_source = results[page]
    
    # Создаем клавиатуру с навигацией
    markup = types.InlineKeyboardMarkup(row_width=5)
    
    nav_buttons = []
    if page > 0:
        nav_buttons.append(types.InlineKeyboardButton("⏪", callback_data=f"search_first_{user_id}"))
        nav_buttons.append(types.InlineKeyboardButton("◀️", callback_data=f"search_prev_{user_id}"))
    
    # Индикатор страницы
    nav_buttons.append(types.InlineKeyboardButton(f"{page + 1}/{total}", callback_data="none"))
    
    if page < total - 1:
        nav_buttons.append(types.InlineKeyboardButton("▶️", callback_data=f"search_next_{user_id}"))
        nav_buttons.append(types.InlineKeyboardButton("⏩", callback_data=f"search_last_{user_id}"))
    
    markup.add(*nav_buttons)
    markup.add(types.InlineKeyboardButton("❌ Закрыть", callback_data=f"search_close_{user_id}"))
    
    # Кнопки для админов
    is_admin_user = user_id == MAIN_ADMIN_ID or check_admin_permission(user_id)[0]
    if is_admin_user:
        # Лейбл "все" — если все результаты с rec_id уже засекречены → показываем "Рассекретить всё"
        all_secret = bool(results) and all(r[3] for r in results if r[2] is not None)
        all_label = "🔓🔓 Рассекретить всё" if all_secret else "🔒🔒 Скрыть по всем API"

        # Определяем текущий источник для отображения в кнопке
        current_source = db_source or "запись"
        # Укорачиваем название источника для кнопки
        short_source = current_source[:12] if len(current_source) > 12 else current_source

        if rec_id is not None:
            if isinstance(rec_id, str) and rec_id.startswith("api:"):
                api_hash = rec_id[4:]
                if is_secret:
                    one_label = f"🔓 [{short_source}]"
                else:
                    one_label = f"🔒 [{short_source}]"
                markup.add(
                    types.InlineKeyboardButton(one_label, callback_data=f"secret_api_{api_hash}"),
                    types.InlineKeyboardButton(all_label, callback_data=f"secret_all_{user_id}"),
                )
            else:
                if is_secret:
                    one_label = f"🔓 [{short_source}]"
                else:
                    one_label = f"🔒 [{short_source}]"
                markup.add(
                    types.InlineKeyboardButton(one_label, callback_data=f"secret_toggle_{rec_id}"),
                    types.InlineKeyboardButton(all_label, callback_data=f"secret_all_{user_id}"),
                )
        else:
            markup.add(types.InlineKeyboardButton(all_label, callback_data=f"secret_all_{user_id}"))

    # Кнопки внизу
    markup.add(
        types.InlineKeyboardButton("📥 Скачать отчёт", callback_data=f"download_report_{user_id}"),
        types.InlineKeyboardButton("🗂 Сводка", callback_data=f"smart_summary_{user_id}"),
    )
    
    # ЕСЛИ ЕСТЬ message_id - РЕДАКТИРУЕМ
    if message_id:
        try:
            if photo_path:
                # Фото нельзя редактировать - удаляем и отправляем новое
                try:
                    bot.delete_message(chat_id, message_id)
                except:
                    pass
                with open(photo_path, "rb") as photo:
                    msg = bot.send_photo(
                        chat_id,
                        photo,
                        caption=text_info[:1024] if text_info else None,
                        reply_markup=markup,
                    )
                data["message_id"] = msg.message_id
            else:
                # Текст - редактируем на месте
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text_info[:4096],
                    reply_markup=markup
                )
                # message_id не меняется — сообщение осталось тем же
        except Exception as e:
            print(f"[edit error] {e}")
            # Если редактирование не удалось — удаляем старое и шлём новое
            try:
                bot.delete_message(chat_id, message_id)
            except:
                pass
            if photo_path:
                with open(photo_path, "rb") as photo:
                    msg = bot.send_photo(chat_id, photo, caption=text_info[:1024], reply_markup=markup)
            else:
                msg = bot.send_message(chat_id, text_info[:4096], reply_markup=markup)
            data["message_id"] = msg.message_id
    else:
        # ПЕРВЫЙ ПОКАЗ - отправляем новое
        if photo_path:
            with open(photo_path, "rb") as photo:
                msg = bot.send_photo(chat_id, photo, caption=text_info[:1024], reply_markup=markup)
        else:
            msg = bot.send_message(chat_id, text_info[:4096], reply_markup=markup)
        data["message_id"] = msg.message_id
        
def show_api_choice_menu(chat_id, user_id=None):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("🌍 LeakOSINT (−1 трип)",    callback_data="search_api_leakosint"),
        types.InlineKeyboardButton("📊 BigBase (−1 трип)",       callback_data="search_api_bigbase"),
        types.InlineKeyboardButton("🔍 По всем встроенным (−2 трипа)", callback_data="search_api_all"),
    )
    ext_apis = get_active_external_apis()
    if ext_apis:
        for ext in ext_apis:
            cost  = ext.get("cost_trips", 1)
            ename = ext.get("name", "API")
            eemoji = ext.get("emoji") or "🌐"
            desc  = ext.get("description") or ""
            label = f"{eemoji} {ename}"
            if desc:
                label += f" — {desc[:30]}"
            label += f" (−{cost} трип{'а' if 2 <= cost <= 4 else 'ов' if cost >= 5 else ''})"
            markup.add(types.InlineKeyboardButton(label, callback_data=f"search_api_ext_{ext['id']}"))
        markup.add(types.InlineKeyboardButton("🔀 По всем API (включая внешние)", callback_data="search_api_ext_all"))
    has_ext_doc = any(
        (e.get("doc_text") or e.get("doc_file_id")) for e in ext_apis
    )
    has_builtin_doc = builtin_api_doc_nonempty("leakosint") or builtin_api_doc_nonempty("bigbase")
    if has_ext_doc or has_builtin_doc:
        markup.add(types.InlineKeyboardButton("📖 Описание API", callback_data="api_docs_menu"))
    bot.send_message(chat_id, "🤖 Выберите по какому API искать:", reply_markup=markup)

def send_search_animation(chat_id, user_id, query):
    # ОБЯЗАТЕЛЬНАЯ ПРОВЕРКА ПОДПИСКИ
    if not check_subscription(user_id) and user_id != MAIN_ADMIN_ID:
        bot.send_message(
            chat_id,
            "🚫 Для поиска нужно подписаться на канал!",
            reply_markup=main_menu(),
        )
        waiting_for_search[user_id] = False
        return

    # ПРОВЕРКА ТРИПОВ (только для не-администраторов)
    is_admin_check = user_id == MAIN_ADMIN_ID or check_admin_permission(user_id)[0]
    if not is_admin_check:
        has_sub = get_user_subscription(user_id) in ("active", "forever")
        trips_balance = get_user_trips(user_id)
        # Стоимость: "all"/"ext_all" = 2, внешний API = его стоимость, иначе 1
        _choice = user_api_choice.get(user_id, "all")
        if _choice in ("all", "ext_all"):
            trips_needed = 2
        elif str(_choice).startswith("ext_"):
            try:
                _ext_id = int(str(_choice).replace("ext_", ""))
                _ext_conn = sqlite3.connect("users.db")
                _ext_cur = _ext_conn.cursor()
                _ext_cur.execute("SELECT cost_trips FROM external_apis WHERE id = ?", (_ext_id,))
                _ext_row = _ext_cur.fetchone()
                _ext_conn.close()
                trips_needed = int(_ext_row[0]) if _ext_row and _ext_row[0] else 1
            except Exception:
                trips_needed = 1
        else:
            trips_needed = 1
        if not has_sub and trips_balance < trips_needed:
            bot.send_message(
                chat_id,
                "💸 На вашем балансе недостаточно средств.\n\n"
                f"💰 Ваш баланс: {trips_balance} трипов\n"
                f"🔍 Необходимо: {trips_needed} трип(ов)\n\n"
                "Активируйте код через /tripcode XXXXX или оформите подписку.",
                reply_markup=main_menu(),
            )
            waiting_for_search[user_id] = False
            return

    try:
        # ── Спиннер: крутится пока идёт поиск ──
        spinner_frames = ["⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"]
        search_done = threading.Event()   # сигнал «поиск завершён»

        msg = bot.send_message(chat_id, f"{spinner_frames[0]} Ищу информацию...")

        def _spin():
            idx = 1
            while not search_done.is_set():
                frame = spinner_frames[idx % len(spinner_frames)]
                try:
                    bot.edit_message_text(
                        f"{frame} Ищу информацию...", chat_id, msg.message_id
                    )
                except:
                    pass
                idx += 1
                search_done.wait(timeout=0.3)   # ждём 0.3 с или пока не выставят флаг

        spin_thread = threading.Thread(target=_spin, daemon=True)
        spin_thread.start()

        found = False
        results = []  # список: (текст, фото_путь, rec_id, is_secret, db_source)

        # ========== ПРОВЕРКА БЛОКИРОВКИ ЗАПРОСА ==========
        is_admin_user_early = user_id == MAIN_ADMIN_ID or check_admin_permission(user_id)[0]
        if not is_admin_user_early:
            block_text, block_id = get_query_block(query)
            if block_text:
                search_done.set()
                spin_thread.join(timeout=1)
                try:
                    bot.edit_message_text(block_text, chat_id, msg.message_id, reply_markup=main_menu())
                except:
                    try:
                        bot.delete_message(chat_id, msg.message_id)
                    except:
                        pass
                    bot.send_message(chat_id, block_text, reply_markup=main_menu())
                save_search_history(user_id, query, False)
                waiting_for_search[user_id] = False
                return

        # ========== ПОИСК В БАЗЕ WORKERS ==========
        workers_db = os.path.join(BASE_DIR, "workers.db")
        if os.path.exists(workers_db):
            try:
                query_lower = query.strip().lower()
                wconn = sqlite3.connect(workers_db)
                wcursor = wconn.cursor()
                wcursor.execute(
                    """
                    SELECT fio, phone, car_number, car_color, car_model, city, tariff
                    FROM workers
                    WHERE lower(fio) LIKE ? OR lower(phone) LIKE ?
                       OR lower(car_number) LIKE ? OR lower(car_model) LIKE ?
                       OR lower(city) LIKE ? OR lower(tariff) LIKE ?
                """,
                    (f"%{query_lower}%",) * 6,
                )
                rows = wcursor.fetchall()
                wconn.close()
                for row in rows:
                    fio, phone, car_number, car_color, car_model, city, tariff = row
                    card = (
                        f"🚗 ═══ WORKERS ═══\n"
                        f"👤 ФИО: {fio}\n"
                        f"📱 Телефон: {phone}\n"
                        f"🚙 Авто: {car_model}\n"
                        f"🎨 Цвет: {car_color}\n"
                        f"🔢 Номер: {car_number}\n"
                        f"📍 Город: {city}\n"
                        f"💼 Тариф: {tariff}"
                    )
                    results.append((card, None, None, False, "WORKERS"))
                    found = True
            except Exception as e:
                print(f"Ошибка поиска в workers.db: {e}")

        # ========== ВЫБОР API ==========
        _api_choice = user_api_choice.get(user_id, "all")

        # ========== ПОИСК ЧЕРЕЗ LEAKOSINT ==========
        api_results = []
        if API_ENABLED and get_builtin_tokens("leakosint") and _api_choice in ("leakosint", "all", "ext_all"):
            try:
                api_results = api_search(query, limit=50, lang=API_DEFAULT_LANG)
                if api_results:
                    print(f"[LEAKOSINT] Найдено: {len(api_results)}")
            except Exception as e:
                print(f"[LEAKOSINT] Ошибка: {e}")

        # ========== ПОИСК ЧЕРЕЗ BIGBASE ==========
        bigbase_results = []
        if _api_choice in ("bigbase", "all", "ext_all"):
            try:
                bigbase_results = bigbase_search(query)
            except Exception as e:
                print(f"[BIGBASE] Ошибка вызова: {e}")

        # ========== ПОИСК ЧЕРЕЗ ВНЕШНИЕ API ==========
        ext_api_results = []
        if str(_api_choice).startswith("ext_") or _api_choice == "ext_all":
            try:
                ext_api_results = search_via_all_external_apis(query, _api_choice)
                if ext_api_results:
                    print(f"[EXT_API] Найдено: {len(ext_api_results)}")
            except Exception as e:
                print(f"[EXT_API] Ошибка: {e}")

        # ========== БЫСТРЫЙ ПОИСК ЧЕРЕЗ SQLite ==========
        is_admin_user = user_id == MAIN_ADMIN_ID or check_admin_permission(user_id)[0]
        last_search_query[user_id] = query
        sqlite_hits = sqlite_search(query, include_secret=is_admin_user)
        
        if sqlite_hits:
            for rec_id, raw_line, source_file, db_type, is_secret in sqlite_hits:
                # Ищем фото
                photo_path = None
                base = os.path.splitext(source_file)[0]
                for ext in ["jpg", "jpeg", "png", "webp"]:
                    candidate = os.path.join(DATA_DIR, f"{base}.{ext}")
                    if os.path.exists(candidate):
                        photo_path = candidate
                        break

                file_path_for_headers = os.path.join(DATA_DIR, source_file)
                file_headers = get_file_headers(file_path_for_headers) if os.path.exists(file_path_for_headers) else []

                # Форматирование
                if ";" in raw_line or "\t" in raw_line:
                    sep = ";" if ";" in raw_line else "\t"
                    parts = [p.strip() for p in raw_line.split(sep) if p.strip()]
                    if file_headers:
                        formatted = smart_format_with_headers(parts, file_headers, db_type, source_file)
                    else:
                        formatted = smart_format_record(parts, db_type, source_file)
                else:
                    block_lines = [l.strip() for l in raw_line.splitlines() if l.strip()]
                    formatted = smart_format_record(block_lines, db_type, source_file)

                db_source = os.path.splitext(source_file)[0].upper()
                formatted = f"📁 ═══ {db_source} ═══\n{formatted.replace('═══', '───', 1)}" if '═══' in formatted else f"📁 ═══ {db_source} ═══\n{formatted}"
                
                results.append((formatted, photo_path, rec_id, is_secret, db_source))
                found = True

        # Добавляем результаты API (LeakOSINT)
        if api_results:
            for api_text, photo, rec_id, is_secret in api_results:
                api_hash = _make_api_hash(api_text)
                if not is_admin_user and is_api_result_secret(api_hash):
                    continue  # скрыть от обычных пользователей
                secret_flag = is_api_result_secret(api_hash)
                results.append((api_text, photo, f"api:{api_hash}", secret_flag, "LEAKOSINT"))
            found = found or len(api_results) > 0

        # Добавляем результаты BIGBASE
        if bigbase_results:
            for bb_text, photo, rec_id, is_secret in bigbase_results:
                bb_hash = _make_api_hash(bb_text)
                if not is_admin_user and is_api_result_secret(bb_hash):
                    continue  # скрыть от обычных пользователей
                secret_flag = is_api_result_secret(bb_hash)
                results.append((bb_text, photo, f"api:{bb_hash}", secret_flag, "BIGBASE"))
            found = found or len(bigbase_results) > 0

        # Добавляем результаты ВНЕШНИХ API
        if ext_api_results:
            for ext_text, photo, rec_id, is_secret in ext_api_results:
                ext_hash = _make_api_hash(ext_text)
                if not is_admin_user and is_api_result_secret(ext_hash):
                    continue
                secret_flag = is_api_result_secret(ext_hash)
                results.append((ext_text, photo, f"api:{ext_hash}", secret_flag, "EXT_API"))
            found = found or len(ext_api_results) > 0

        # ========== FALLBACK: ПОИСК В TXT ФАЙЛАХ ==========
        if not found and os.path.exists(DATA_DIR):
            query_lower = query.strip().lower()

            if RE_PHONE.search(query.strip()):
                digits_only = re.sub(r"\D", "", query.strip())
                core10 = digits_only[-10:] if len(digits_only) >= 10 else digits_only
                query_lower = core10

            query_words = query_lower.split()

            try:
                _conn = sqlite3.connect("users.db")
                _cur = _conn.cursor()
                _cur.execute("SELECT DISTINCT source_file FROM search_data")
                already_imported = {r[0] for r in _cur.fetchall()}
                _conn.close()
            except Exception:
                already_imported = set()

            for root, dirs, files in os.walk(DATA_DIR):
                for filename in sorted(files):
                    if not filename.endswith(".txt"):
                        continue
                    if filename in already_imported:
                        continue

                    file_path = os.path.join(root, filename)
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            content = f.read()
                    except Exception:
                        try:
                            with open(file_path, "r", encoding="cp1251") as f:
                                content = f.read()
                        except Exception:
                            continue

                    photo_path = None
                    base = os.path.splitext(filename)[0]
                    for ext in ["jpg", "jpeg", "png", "webp"]:
                        candidate = os.path.join(root, f"{base}.{ext}")
                        if os.path.exists(candidate):
                            photo_path = candidate
                            break

                    db_type_by_name = detect_db_type_by_name(filename)
                    lines_for_detect = [l.strip() for l in content.splitlines() if l.strip()]
                    db_type = (
                        db_type_by_name
                        if db_type_by_name != "default"
                        else detect_db_type_by_content(lines_for_detect)
                    )

                    lines = content.splitlines()
                    semicolon_lines = sum(1 for l in lines if ";" in l)
                    tab_lines = sum(1 for l in lines if "\t" in l)
                    non_empty = [l for l in lines if l.strip()]
                    is_csv_format = (
                        semicolon_lines > len(non_empty) * 0.4
                        or tab_lines > len(non_empty) * 0.4
                    )

                    file_headers = get_file_headers(file_path)

                    if is_csv_format:
                        sep = ";" if semicolon_lines >= tab_lines else "\t"
                        for line in lines:
                            line = line.strip()
                            if not line:
                                continue
                            line_lower = line.lower()
                            if all(word in line_lower for word in query_words):
                                parts = [p.strip() for p in line.split(sep) if p.strip()]
                                if file_headers:
                                    formatted = smart_format_with_headers(parts, file_headers, db_type, filename)
                                else:
                                    formatted = smart_format_record(parts, db_type, filename)
                                db_source = os.path.splitext(filename)[0].upper()
                                formatted = f"📁 ═══ {db_source} ═══\n{formatted.replace('═══', '───', 1)}"
                                results.append((formatted, photo_path, None, False, db_source))
                                found = True
                    else:
                        raw_blocks = content.strip().split("\n\n")
                        for block in raw_blocks:
                            block = block.strip()
                            if not block:
                                continue
                            block_lower = block.lower()
                            if all(word in block_lower for word in query_words):
                                block_lines = [l.strip() for l in block.splitlines() if l.strip()]
                                formatted = smart_format_record(block_lines, db_type, filename)
                                db_source = os.path.splitext(filename)[0].upper()
                                formatted = f"📁 ═══ {db_source} ═══\n{formatted.replace('═══', '───', 1)}"
                                results.append((formatted, photo_path, None, False, db_source))
                                found = True

        if not found:
            secret_match_exists = False
            if not is_admin_user:
                try:
                    hidden_hits = sqlite_search(query, include_secret=True)
                    secret_match_exists = any(row[4] for row in hidden_hits)
                except Exception as e:
                    print(f"[secret check] Ошибка проверки скрытых данных: {e}")

                # Также проверяем API-результаты в api_secret_results по запросу
                if not secret_match_exists:
                    try:
                        conn_sc = sqlite3.connect("users.db")
                        cur_sc = conn_sc.cursor()
                        cur_sc.execute(
                            "SELECT 1 FROM api_secret_results WHERE secreted_query = ? LIMIT 1",
                            (query,)
                        )
                        if cur_sc.fetchone():
                            secret_match_exists = True
                        conn_sc.close()
                    except Exception as e:
                        print(f"[secret check api] Ошибка: {e}")

            save_search_history(user_id, query, False, api_source=_api_choice)
            # Трипы НЕ списываются если ничего не найдено или была ошибка
            not_found_text = (
                "🔒 Данные засекречены."
                if secret_match_exists
                else "❌ Информация не найдена. Попробуйте другой запрос."
            )
            search_done.set()
            spin_thread.join(timeout=1)
            try:
                bot.edit_message_text(
                    not_found_text,
                    chat_id,
                    msg.message_id,
                    reply_markup=main_menu(),
                )
            except:
                try:
                    bot.delete_message(chat_id, msg.message_id)
                except:
                    pass
                bot.send_message(
                    chat_id,
                    not_found_text,
                    reply_markup=main_menu(),
                )
        else:
            save_search_history(user_id, query, True, api_source=_api_choice)

            # Списываем трипы (если не подписка и не админ)
            if not is_admin_check:
                has_sub = get_user_subscription(user_id) in ("active", "forever")
                if not has_sub:
                    _choice_now = user_api_choice.get(user_id, "all")
                    trips_to_spend = 2 if _choice_now == "all" else 1
                    spend_trips(user_id, trips_to_spend)
            
            # Сохраняем результаты для навигации
            user_search_results[user_id] = {
                "results": results,
                "current_page": 0,
                "query": query,
                "total_pages": len(results)
            }

            # Останавливаем спиннер
            search_done.set()
            spin_thread.join(timeout=1)

            # Показываем количество
            try:
                bot.edit_message_text(
                    f"✅ Найдено результатов: {len(results)}",
                    chat_id,
                    msg.message_id,
                )
            except:
                try:
                    bot.delete_message(chat_id, msg.message_id)
                except:
                    pass
                bot.send_message(chat_id, f"✅ Найдено результатов: {len(results)}")

            # Показываем первый результат
            show_search_result_page(chat_id, user_id, 0)

        waiting_for_search[user_id] = False
    except Exception as e:
        import traceback
        print(f"Ошибка в анимации: {e}")
        traceback.print_exc()
        try:
            search_done.set()
        except:
            pass
        try:
            bot.send_message(chat_id, "❌ Произошла ошибка", reply_markup=main_menu())
        except:
            pass


# ========== ФУНКЦИЯ РАССЫЛКИ ==========
def send_broadcast(admin_id, text):
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users")
        users = cursor.fetchall()
        conn.close()

        bot.send_message(admin_id, f"📨 Начинаю рассылку {len(users)} пользователям...")

        success = 0
        failed = 0

        for user in users:
            user_id = user[0]
            try:
                bot.send_message(user_id, text)
                success += 1
                time.sleep(0.05)
            except:
                failed += 1

        bot.send_message(
            admin_id,
            f"✅ Рассылка завершена!\n✓ Успешно: {success}\n✗ Не удалось: {failed}",
        )

    except Exception as e:
        bot.send_message(admin_id, f"❌ Ошибка при рассылке: {e}")


# ========== КОМАНДЫ ДЛЯ ВСЕХ ПОЛЬЗОВАТЕЛЕЙ ==========
@bot.message_handler(commands=["start"])
def send_welcome(message):
    user_id = message.from_user.id

    # Игнорируем сообщения, отправленные пока бот был оффлайн
    if is_old_message(message):
        return

    # Проверка бана
    is_banned, ban_reason = check_banned(user_id)
    if is_banned:
        bot.send_message(
            message.chat.id,
            f"🚫 ДОСТУП ЗАПРЕЩЕН!\n\nВы были забанены.\nПричина: {ban_reason}",
        )
        return

    # ОБЯЗАТЕЛЬНАЯ ПРОВЕРКА ПОДПИСКИ
    if not check_subscription(user_id):
        markup = types.InlineKeyboardMarkup()
        btn_channel = types.InlineKeyboardButton(
            "📢 ПОДПИСАТЬСЯ", url=REQUIRED_CHANNEL_INVITE
        )
        btn_check = types.InlineKeyboardButton(
            "✅ Я ПОДПИСАЛСЯ", callback_data="check_sub"
        )
        markup.add(btn_channel)
        markup.add(btn_check)

        checking_subscription[user_id] = True
        bot.send_message(
            message.chat.id,
            f"🚫 ДОСТУП ЗАПРЕЩЕН!\n\n"
            f"Для использования бота необходимо подписаться на канал.\n\n"
            f"👉 Подпишись и нажми кнопку ниже 👇",
            reply_markup=markup,
        )
        return

    add_or_update_user(message)

    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute(
        "SELECT username, first_name, created_at FROM users WHERE user_id = ?",
        (user_id,),
    )
    user_data = cursor.fetchone()

    if user_data:
        username, first_name, created_at = user_data
        reg_date = (
            datetime.strptime(str(created_at).split()[0], "%Y-%m-%d").strftime(
                "%d.%m.%Y"
            )
            if created_at
            else "неизвестно"
        )
    else:
        username = "нет"
        first_name = "пользователь"
        reg_date = "неизвестно"

    status = get_user_subscription(user_id)
    if status == "forever":
        status_text = "🔴 ВЕЧНАЯ"
    elif status == "active":
        cursor.execute(
            "SELECT subscription_end FROM users WHERE user_id = ?", (user_id,)
        )
        end_date = cursor.fetchone()
        if end_date and end_date[0]:
            end_date_str = datetime.strptime(end_date[0], "%Y-%m-%d").strftime(
                "%d.%m.%Y"
            )
            status_text = f"🟢 активна до {end_date_str}"
        else:
            status_text = "🟢 активна"
    elif status == "expired":
        status_text = "⚫ истекла"
    else:
        status_text = "⚪ нет подписки"

    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    conn.close()

    trips = get_user_trips(user_id)

    menu_text, menu_photo = get_bot_text_full(
        "start",
        user_id=user_id,
        first_name=first_name or "пользователь",
        username=username or "нет",
        reg_date=reg_date,
        status_text=status_text,
        total_users=total_users,
        trips=trips,
    )

    # Один раз закрепляем Reply-кнопку "📋 Меню" у пользователя
    if menu_photo:
        bot.send_photo(message.chat.id, menu_photo, caption=menu_text, reply_markup=main_menu())
    else:
        bot.send_message(message.chat.id, menu_text, reply_markup=main_menu())
    bot.send_message(message.chat.id, "👇 Выберите действие:", reply_markup=inline_main_menu())
    if user_id in waiting_for_search:
        waiting_for_search[user_id] = False


@bot.message_handler(commands=["help"])
def show_help(message):
    user_id = message.from_user.id

    if is_old_message(message):
        return

    help_text = get_bot_text("help")

    if user_id == MAIN_ADMIN_ID or check_admin_permission(user_id)[0]:
        admin_panel_line = "/admin_panel - управление админами 👑\n" if has_perm(user_id, "admin_panel") else ""
        stats_line       = "/stats - общая статистика 📊\n"          if has_perm(user_id, "stats") else ""
        mod_line         = "/moderation - модерация 🛡️\n"            if has_perm(user_id, "users") else ""
        texts_line       = "/texts - управление текстами 📝\n"       if has_perm(user_id, "texts") else ""
        files_line       = "/files - работа с файлами 📁\n"          if has_perm(user_id, "files") else ""
        secret_line      = "/secret_list - засекреченные записи 🔐\n" if has_perm(user_id, "secret") else ""
        block_line       = "/block_query - заблокировать запрос 🚫\n/blocked_queries - список блокировок\n" if has_perm(user_id, "block_query") else ""
        broadcast_line   = "/broadcast - рассылка 📢\n"              if has_perm(user_id, "broadcast") else ""
        support_line     = "/supportlist - обращения в поддержку 💬\n" if has_perm(user_id, "support") else ""
        createtrip_line  = "/createtrip N - создать код на N трипов 🎫\n"

        # Берём шаблон из БД (редактируется через /texts → Редактировать АДМИН-ПАНЕЛЬ)
        admin_help_template = get_bot_text("admin_help")
        admin_block = "\n\n" + admin_help_template.format(
            stats_line=stats_line,
            admin_panel_line=admin_panel_line,
            mod_line=mod_line,
            texts_line=texts_line,
            files_line=files_line,
            secret_line=secret_line,
            block_line=block_line,
            broadcast_line=broadcast_line,
            support_line=support_line,
            createtrip_line=createtrip_line,
        )
        help_text += admin_block

    bot.send_message(message.chat.id, help_text)


# ========== АДМИН-КОМАНДЫ ==========
@bot.message_handler(commands=["admin_panel"])
def admin_panel_menu(message):
    user_id = message.from_user.id

    if not has_perm(user_id, "admin_panel"):
        bot.send_message(message.chat.id, "⛔ Только админы!")
        return

    markup = types.InlineKeyboardMarkup(row_width=2)
    btn1 = types.InlineKeyboardButton(
        "🔑 Создать код", callback_data="admin_createcode"
    )
    btn2 = types.InlineKeyboardButton("👑 Выдать права", callback_data="admin_grant")
    btn3 = types.InlineKeyboardButton("❌ Отозвать права", callback_data="admin_revoke")
    btn4 = types.InlineKeyboardButton("📋 Список админов", callback_data="admin_list")
    btn5 = types.InlineKeyboardButton("◀️ Назад", callback_data="admin_back")
    markup.add(btn1, btn2, btn3, btn4, btn5)

    bot.send_message(
        message.chat.id,
        "👑 УПРАВЛЕНИЕ АДМИНАМИ\n\nВыберите действие:",
        reply_markup=markup,
    )


@bot.message_handler(commands=["stats"])
def stats_menu(message):
    user_id = message.from_user.id

    if not has_perm(user_id, "stats"):
        bot.send_message(message.chat.id, "⛔ Нет прав для просмотра статистики!")
        return

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📊 Общая статистика",   callback_data="stats_general"),
        types.InlineKeyboardButton("🗃️ База данных",        callback_data="stats_showdb"),
    )
    markup.add(
        types.InlineKeyboardButton("🔍 Вся история",        callback_data="stats_allhistory"),
        types.InlineKeyboardButton("📌 История юзера",      callback_data="stats_userhistory"),
    )
    markup.add(
        types.InlineKeyboardButton("🏆 Топ запросов",       callback_data="stats_topqueries"),
        types.InlineKeyboardButton("🔥 Активные юзеры",     callback_data="stats_activeusers"),
    )
    markup.add(
        types.InlineKeyboardButton("🆕 Новые пользователи", callback_data="stats_newusers"),
        types.InlineKeyboardButton("📅 Активность сегодня", callback_data="stats_today"),
    )
    markup.add(
        types.InlineKeyboardButton("🔎 Найти пользователя", callback_data="stats_searchuser"),
        types.InlineKeyboardButton("📥 Экспорт базы",       callback_data="stats_export"),
    )
    markup.add(
        types.InlineKeyboardButton("📈 Полный отчёт (HTML)", callback_data="stats_fullreport"),
    )
    markup.add(
        types.InlineKeyboardButton("🗑️ Очистить всю БД пользователей", callback_data="stats_cleardb_ask"),
    )
    markup.add(types.InlineKeyboardButton("◀️ Назад", callback_data="stats_back"))

    bot.send_message(
        message.chat.id,
        "📊 **СТАТИСТИКА БОТА**\n\nВыберите раздел:",
        reply_markup=markup,
        parse_mode="Markdown",
    )


@bot.message_handler(commands=["secret_list"])
def secret_list(message):
    user_id = message.from_user.id
    is_admin_user = user_id == MAIN_ADMIN_ID or check_admin_permission(user_id)[0]
    if not is_admin_user or not has_perm(user_id, "secret"):
        bot.send_message(message.chat.id, "⛔ Только админы!")
        return

    rows = get_secret_search_records(limit=100)
    api_rows = get_all_api_secrets(limit=100)

    if not rows and not api_rows:
        bot.send_message(message.chat.id, "🔐 Засекреченных записей пока нет.")
        return

    lines = [f"🔐 Засекреченные записи: {len(rows) + len(api_rows)}\n"]

    if rows:
        lines.append("📁 Из локальной базы:")
        for i, (rec_id, secreted_by, secreted_query) in enumerate(rows, 1):
            who = f"ID {secreted_by}" if secreted_by else "неизвестно"
            what = secreted_query or "запрос не сохранён"
            lines.append(f"{i}. 👤 Кто: {who}\n   🔍 Запрос: {what}")

    if api_rows:
        lines.append("\n🌐 Из внешних API:")
        for i, (content_hash, api_source, secreted_by, secreted_query, secreted_at) in enumerate(api_rows, 1):
            who = f"ID {secreted_by}" if secreted_by else "неизвестно"
            what = secreted_query or "запрос не сохранён"
            lines.append(f"{i}. [{api_source}] 👤 Кто: {who}\n   🔍 Запрос: {what}\n   🕐 {secreted_at}")

    text = "\n\n".join(lines)
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks[:-1]:
        bot.send_message(message.chat.id, chunk)

    # Последний чанк — с кнопкой "Убрать все скрытое"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(
        "🗑️ Убрать все скрытое", callback_data="secret_clear_all_ask"
    ))
    bot.send_message(message.chat.id, chunks[-1], reply_markup=markup)


@bot.message_handler(commands=["moderation"])
def moderation_menu(message):
    user_id = message.from_user.id

    if not has_perm(user_id, "users"):
        bot.send_message(message.chat.id, "⛔ Нет прав для модерации!")
        return

    markup = types.InlineKeyboardMarkup(row_width=2)
    btn1 = types.InlineKeyboardButton("🚫 Забанить", callback_data="mod_ban")
    btn2 = types.InlineKeyboardButton("✅ Разбанить", callback_data="mod_unban")
    btn3 = types.InlineKeyboardButton("📋 Список банов", callback_data="mod_banlist")
    btn4 = types.InlineKeyboardButton(
        "🛡️ Настройки флуда", callback_data="mod_floodsettings"
    )
    btn5 = types.InlineKeyboardButton(
        "🧹 Сбросить флуд", callback_data="mod_clearflood"
    )
    btn6 = types.InlineKeyboardButton("◀️ Назад", callback_data="mod_back")
    markup.add(btn1, btn2, btn3, btn4, btn5, btn6)

    bot.send_message(
        message.chat.id, "🛡️ МОДЕРАЦИЯ\n\nВыберите действие:", reply_markup=markup
    )


@bot.message_handler(commands=["texts"])
def texts_menu(message):
    user_id = message.from_user.id

    if not has_perm(user_id, "texts"):
        bot.send_message(message.chat.id, "⛔ Нет прав для управления текстами!")
        return

    texts = list_bot_texts()

    if not texts:
        bot.send_message(message.chat.id, "📭 Нет текстов в базе")
        return

    markup = types.InlineKeyboardMarkup(row_width=2)
    for key, _, _, _, *__ in texts[:8]:
        btn = types.InlineKeyboardButton(f"📝 {key}", callback_data=f"edittext_{key}")
        markup.add(btn)

    btn_adminhelp = types.InlineKeyboardButton("👑 Текст АДМИН-ПАНЕЛИ", callback_data="edittext_admin_help")
    btn_custlabel = types.InlineKeyboardButton("🔤 Название кнопки", callback_data="edittext_custom_button_label")
    btn_custtext  = types.InlineKeyboardButton("📌 Текст кнопки", callback_data="edittext_custom_button")
    btn_view = types.InlineKeyboardButton("📄 Все тексты", callback_data="view_all_texts")
    btn_back = types.InlineKeyboardButton("◀️ Назад", callback_data="texts_back")
    markup.add(btn_adminhelp)
    markup.add(btn_custlabel, btn_custtext)
    markup.add(btn_view, btn_back)

    bot.send_message(
        message.chat.id,
        "📝 УПРАВЛЕНИЕ ТЕКСТАМИ\n\nВыберите текст для редактирования:",
        reply_markup=markup,
    )


@bot.message_handler(commands=["files"])
def files_menu(message):
    user_id = message.from_user.id

    if not has_perm(user_id, "files"):
        bot.send_message(message.chat.id, "⛔ У вас нет прав для работы с файлами!")
        return

    markup = types.InlineKeyboardMarkup(row_width=2)
    btn1 = types.InlineKeyboardButton("📝 Создать файл", callback_data="file_create")
    btn2 = types.InlineKeyboardButton("✏️ Редактировать", callback_data="file_edit")
    btn3 = types.InlineKeyboardButton("📋 Переименовать", callback_data="file_rename")
    btn4 = types.InlineKeyboardButton("📁 Список файлов", callback_data="file_list")
    btn5 = types.InlineKeyboardButton("🗑️ Удалить файл", callback_data="file_delete")
    btn6 = types.InlineKeyboardButton("◀️ Назад", callback_data="file_back")
    markup.add(btn1, btn2, btn3, btn4, btn5, btn6)

    bot.send_message(
        message.chat.id,
        "📁 РАБОТА С ФАЙЛАМИ\n\nВыберите действие:",
        reply_markup=markup,
    )


@bot.message_handler(commands=["myhistory"])
def show_history(message):
    user_id = message.from_user.id

    if is_old_message(message):
        return

    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    history = get_user_history_with_first(cursor, user_id)
    conn.close()

    if not history:
        bot.send_message(message.chat.id, "📭 У вас пока нет истории поиска")
        return

    text = get_bot_text("myhistory")

    for i, (_, query, found, search_time) in enumerate(history, 1):
        status = "✅ Найдено" if found else "❌ Не найдено"
        text += f"{i}. 🔍 {query}\n   {status} - {search_time}\n\n"

        if len(text) > 3500:
            bot.send_message(message.chat.id, text)
            text = get_bot_text("myhistory")

    if text != get_bot_text("myhistory"):
        bot.send_message(message.chat.id, text)


@bot.message_handler(commands=["code"])
def activate_code(message):
    user_id = message.from_user.id

    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(
            message.chat.id, "❌ Использование: /code XXXXX\nПример: /code admin123"
        )
        return

    code = parts[1].strip().lower()

    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, duration, created_by, permissions FROM admin_codes WHERE code = ? AND used_by IS NULL",
        (code,),
    )
    code_data = cursor.fetchone()
    conn.close()

    if not code_data:
        bot.send_message(message.chat.id, "❌ Неверный или уже использованный код!")
        return

    code_id, duration, created_by, code_permissions = code_data

    if check_admin_code(code, user_id):
        duration_text = get_duration_text(duration)

        if duration == "forever":
            days = 3650
        elif duration == "day":
            days = 1
        elif duration == "hour":
            days = 1  # grant_admin_permission работает в днях; доступ на час проверяется через admin_codes
        else:
            days = 1

        success, expires = grant_admin_permission(created_by, user_id, days)

        # Сохраняем права из кода в admin_permissions
        if success and code_permissions and code_permissions != "all":
            try:
                conn2 = sqlite3.connect("users.db")
                cur2 = conn2.cursor()
                cur2.execute(
                    "UPDATE admin_permissions SET permissions = ? WHERE user_id = ? AND is_active = 1",
                    (code_permissions, user_id),
                )
                conn2.commit()
                conn2.close()
            except Exception as e:
                print(f"Ошибка сохранения прав: {e}")

        if duration == "hour":
            expires_display = "(1 час)"
        else:
            expires_display = expires

        if success:
            # Показываем /admin_panel только если право admin_panel включено в код
            perms_list = (
                code_permissions.split(",")
                if code_permissions and code_permissions != "all"
                else list(ALL_PERMISSIONS.keys())
            )
            admin_panel_hint = (
                "\n📋 Используйте /admin_panel для управления"
                if "admin_panel" in perms_list
                else ""
            )
            bot.send_message(
                message.chat.id,
                f"✅ Код активирован!\n\n"
                f"⏰ Доступ действует: {duration_text}\n"
                f"👑 Вам выданы права администратора до: {expires_display}"
                f"{admin_panel_hint}",
            )
        else:
            bot.send_message(
                message.chat.id,
                f"✅ Код активирован!\n\n"
                f"⏰ Доступ действует: {duration_text}\n\n"
                f"⚠️ Но не удалось добавить в список админов: {expires}",
            )
    else:
        bot.send_message(message.chat.id, "❌ Неверный или уже использованный код!")


# ========== ТРИПЫ: ПОЛЬЗОВАТЕЛЬСКАЯ КОМАНДА ==========
@bot.message_handler(commands=["tripcode"])
def activate_trip_code(message):
    user_id = message.from_user.id
    if is_old_message(message):
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(
            message.chat.id,
            "❌ Использование: /tripcode XXXXX\nПример: /tripcode T1234567"
        )
        return
    code = parts[1].strip()
    ok, result = activate_trips_code(code, user_id)
    if ok:
        new_balance = get_user_trips(user_id)
        bot.send_message(
            message.chat.id,
            f"✅ Код активирован!\n\n"
            f"🎫 Начислено трипов: +{result}\n"
            f"💰 Ваш баланс: {new_balance} трипов\n\n"
            f"1 трип = 1 запрос по 1 API (2 трипа = по двум API)",
        )
    else:
        bot.send_message(message.chat.id, f"❌ Ошибка: {result}")


# ========== ТРИПЫ: СОЗДАНИЕ КОДА АДМИНИСТРАТОРОМ ==========
@bot.message_handler(commands=["createtrip"])
def create_trip_code_cmd(message):
    user_id = message.from_user.id
    if not (user_id == MAIN_ADMIN_ID or check_admin_permission(user_id)[0]):
        bot.send_message(message.chat.id, "⛔ Нет прав!")
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(
            message.chat.id,
            "❌ Использование: /createtrip КОЛИЧЕСТВО\nПример: /createtrip 50\n\nСоздаст код на 50 трипов."
        )
        return
    try:
        amount = int(parts[1])
        if amount <= 0:
            raise ValueError
    except ValueError:
        bot.send_message(message.chat.id, "❌ Количество должно быть положительным числом!")
        return
    code = create_trips_code(user_id, amount)
    bot.send_message(
        message.chat.id,
        f"✅ Код на трипы создан!\n\n"
        f"🎫 Код: <code>{code}</code>\n"
        f"💰 Трипов: {amount}\n\n"
        f"Пользователь активирует через: /tripcode {code}",
        parse_mode="HTML"
    )


@bot.message_handler(commands=["broadcast"])
def broadcast_start(message):
    user_id = message.from_user.id

    if not has_perm(user_id, "broadcast"):
        bot.send_message(message.chat.id, "⛔ Нет прав для рассылки!")
        return

    admin_broadcast[user_id] = True
    bot.send_message(
        message.chat.id,
        "📝 Отправь сообщение для рассылки всем пользователям:\n"
        "(или отправь /cancel для отмены)",
    )


# ========== КОМАНДЫ ПОДДЕРЖКИ ==========
@bot.message_handler(commands=["support"])
def support_start(message):
    user_id = message.from_user.id

    is_banned, ban_reason = check_banned(user_id)
    if is_banned:
        bot.send_message(
            message.chat.id, "🚫 Вы забанены и не можете писать в поддержку!"
        )
        return

    # Проверка подписки УЖЕ ЕСТЬ в начале handle_buttons, здесь не нужна

    user_support_request[user_id] = True
    bot.send_message(
        message.chat.id,
        "📝 СЛУЖБА ПОДДЕРЖКИ\n\n"
        "Напишите ваш вопрос. Администратор ответит в ближайшее время.\n"
        "Отправьте /cancel для отмены.",
    )

    notify_admins_support(user_id, message.from_user.first_name or "Пользователь")


@bot.message_handler(commands=["reply"])
def reply_to_user(message):
    admin_id = message.from_user.id

    # Проверка прав админа
    if not has_perm(admin_id, "support"):
        bot.send_message(message.chat.id, "⛔ Нет прав для ответа в поддержку!")
        return

    # Разбираем команду
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(
            message.chat.id,
            "❌ Использование: /reply USER_ID\nПример: /reply 123456789",
        )
        return

    try:
        target_user_id = int(parts[1])

        # Проверяем, существует ли пользователь
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute(
            "SELECT user_id, banned FROM users WHERE user_id = ?", (target_user_id,)
        )
        user_data = cursor.fetchone()
        conn.close()

        if not user_data:
            bot.send_message(
                message.chat.id,
                f"❌ Пользователь с ID {target_user_id} не найден в базе!",
            )
            return

        is_banned = user_data[1] == 1
        if is_banned:
            bot.send_message(message.chat.id, "❌ Пользователь забанен!")
            return

        # Устанавливаем режим ответа
        support_mode[admin_id] = target_user_id
        live_chat[target_user_id] = admin_id

        # Удаляем пользователя из ожидающих, если он там есть
        if target_user_id in user_support_request:
            del user_support_request[target_user_id]

        bot.send_message(
            message.chat.id,
            f"✅ Живой чат открыт с пользователем `{target_user_id}`\n"
            f"Пишите сообщения — они будут пересылаться напрямую.\n"
            f"Для завершения диалога отправьте /endchat",
            parse_mode="Markdown",
        )
        try:
            bot.send_message(
                target_user_id,
                "✅ Администратор подключился. Можете общаться!\n"
                "Для завершения диалога отправьте /endchat",
            )
        except:
            pass

    except ValueError:
        bot.send_message(message.chat.id, "❌ ID должен быть числом!")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}")


@bot.message_handler(commands=["stopreply", "endchat"])
def stop_reply(message):
    uid = message.from_user.id

    # Если это админ
    if uid in support_mode:
        target_id = support_mode.pop(uid)
        if live_chat.get(target_id) == uid:
            del live_chat[target_id]
        bot.send_message(message.chat.id, "✅ Диалог завершён.")
        try:
            bot.send_message(target_id, "Диалог с поддержкой завершён.")
        except:
            pass
        return

    # Если это пользователь в живом чате
    if uid in live_chat:
        admin_id = live_chat.pop(uid)
        if support_mode.get(admin_id) == uid:
            del support_mode[admin_id]
        bot.send_message(message.chat.id, "✅ Диалог завершён.")
        try:
            bot.send_message(admin_id, "✅ Пользователь завершил диалог.")
        except:
            pass
        return

    bot.send_message(message.chat.id, "❌ Нет активного диалога.")


# ========== БЛОКИРОВКА ЗАПРОСОВ ==========

@bot.message_handler(commands=["block_query"])
def cmd_block_query(message):
    """Начинает диалог добавления блокировки запроса."""
    uid = message.from_user.id
    if not has_perm(uid, "block_query"):
        bot.send_message(message.chat.id, "⛔ Недостаточно прав.")
        return
    admin_block_query_state[uid] = {"step": "pattern"}
    bot.send_message(
        message.chat.id,
        "🔒 <b>Добавление блокировки запроса</b>\n\n"
        "Шаг 1/3: Введите <b>паттерн</b> (фразу или слово) которое нужно блокировать.\n\n"
        "<i>Пример: Лекомцев Антон</i>\n\n"
        "Отправьте /cancel для отмены.",
        parse_mode="HTML",
    )


@bot.message_handler(commands=["blocked_queries"])
def cmd_blocked_queries(message):
    """Показывает список всех заблокированных запросов."""
    uid = message.from_user.id
    if not has_perm(uid, "block_query"):
        bot.send_message(message.chat.id, "⛔ Недостаточно прав.")
        return
    blocks = list_query_blocks()
    if not blocks:
        bot.send_message(message.chat.id, "📋 Список заблокированных запросов пуст.")
        return

    # Отправляем по одному блоку с кнопкой удаления
    bot.send_message(message.chat.id, f"📋 <b>Заблокированных запросов: {len(blocks)}</b>", parse_mode="HTML")
    for row_id, pattern, custom_text, mode, created_at, created_by in blocks:
        mode_label = "точное совпадение" if mode == "exact" else "содержит"
        text = (
            f"🔒 <b>#{row_id}</b> | Режим: {mode_label}\n"
            f"📝 <b>Паттерн:</b> <code>{pattern}</code>\n"
            f"💬 <b>Ответ пользователю:</b>\n{custom_text}\n"
            f"📅 Создан: {created_at[:16] if created_at else '—'}"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(f"🗑 Удалить #{row_id}", callback_data=f"del_qblock_{row_id}"))
        bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=markup)


@bot.message_handler(commands=["supportlist"])
def support_list(message):
    admin_id = message.from_user.id

    if not has_perm(admin_id, "support"):
        bot.send_message(message.chat.id, "⛔ Нет прав для просмотра обращений!")
        return

    waiting = get_waiting_users()

    if not waiting:
        bot.send_message(
            message.chat.id,
            "✅ **Все обращения обработаны!**\nОжидающих сообщений нет.",
            parse_mode="Markdown",
        )
        return

    markup = types.InlineKeyboardMarkup(row_width=1)

    text = "📋 **СПИСОК ОБРАЩЕНИЙ В ПОДДЕРЖКУ:**\n\n"
    for i, user in enumerate(waiting, 1):
        text += f"{i}. 👤 **{user['name']}** (`{user['id']}`)\n"
        text += f"   💬 {user['last_message']}\n"
        text += f"   🕐 _{user['last_time']}_\n\n"

        markup.add(
            types.InlineKeyboardButton(
                text=f"Ответить {user['name']}", callback_data=f"reply_to_{user['id']}"
            )
        )

    markup.add(
        types.InlineKeyboardButton("❌ Закрыть", callback_data="close_support_list")
    )

    bot.send_message(admin_id, text, reply_markup=markup, parse_mode="Markdown")


@bot.message_handler(commands=["cancel"])
def cancel_action(message):
    user_id = message.from_user.id
    if user_id in api_add_state:
        del api_add_state[user_id]
        bot.send_message(message.chat.id, "❌ Добавление API отменено.", reply_markup=main_menu())
    elif user_id in admin_block_query_state:
        del admin_block_query_state[user_id]
        bot.send_message(message.chat.id, "❌ Блокировка запроса отменена.", reply_markup=main_menu())
    elif user_id in admin_broadcast:
        del admin_broadcast[user_id]
        bot.send_message(
            message.chat.id, "❌ Рассылка отменена", reply_markup=main_menu()
        )
    elif user_id in admin_create_file:
        del admin_create_file[user_id]
        bot.send_message(
            message.chat.id, "❌ Действие отменено", reply_markup=main_menu()
        )
    elif user_id in admin_action:
        del admin_action[user_id]
        bot.send_message(
            message.chat.id, "❌ Действие отменено", reply_markup=main_menu()
        )
    elif user_id in user_support_request:
        del user_support_request[user_id]
        bot.send_message(
            message.chat.id,
            "❌ Обращение в поддержку отменено",
            reply_markup=main_menu(),
        )
    elif user_id in live_chat:
        admin_id = live_chat.pop(user_id)
        if support_mode.get(admin_id) == user_id:
            del support_mode[admin_id]
        bot.send_message(message.chat.id, "✅ Диалог завершён.", reply_markup=main_menu())
        try:
            bot.send_message(admin_id, "✅ Пользователь завершил диалог.")
        except:
            pass
    else:
        bot.send_message(
            message.chat.id,
            "❌ Нет активных действий для отмены",
            reply_markup=main_menu(),
        )


# ========== API МЕНЕДЖЕР ==========

def db_init_external_apis():
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS external_apis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            emoji TEXT DEFAULT '🌐',
            url TEXT NOT NULL,
            api_key TEXT DEFAULT '',
            method TEXT DEFAULT 'POST',
            auth_type TEXT DEFAULT 'body',
            auth_key TEXT DEFAULT 'token',
            query_param TEXT DEFAULT 'request',
            query_place TEXT DEFAULT 'body',
            extra_body TEXT DEFAULT '{}',
            result_path TEXT DEFAULT '',
            token_param TEXT DEFAULT 'token',
            description TEXT DEFAULT '',
            doc_text TEXT DEFAULT '',
            doc_file_id TEXT DEFAULT '',
            doc_filename TEXT DEFAULT '',
            cost_trips INTEGER DEFAULT 1,
            is_active INTEGER DEFAULT 1,
            added_by INTEGER,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Миграция: добавляем новые колонки если их нет (для уже существующих БД)
    migrations = [
        ("emoji",       "TEXT DEFAULT '🌐'"),
        ("auth_type",   "TEXT DEFAULT 'body'"),
        ("auth_key",    "TEXT DEFAULT 'token'"),
        ("query_place", "TEXT DEFAULT 'body'"),
        ("extra_body",  "TEXT DEFAULT '{}'"),
        ("result_path", "TEXT DEFAULT ''"),
        ("description", "TEXT DEFAULT ''"),
        ("cost_trips",  "INTEGER DEFAULT 1"),
    ]
    for col, coldef in migrations:
        try:
            cursor.execute(f"ALTER TABLE external_apis ADD COLUMN {col} {coldef}")
        except Exception:
            pass
    conn.commit()
    conn.close()

db_init_external_apis()


# ========== ФУНКЦИИ ВЫПОЛНЕНИЯ ЗАПРОСОВ К ВНЕШНИМ API ==========

def get_active_external_apis():
    """Возвращает список активных внешних API из БД."""
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, name, emoji, url, api_key, method, auth_type, auth_key,
               query_param, query_place, extra_body, result_path,
               description, doc_text, doc_file_id, cost_trips
        FROM external_apis WHERE is_active = 1 ORDER BY id
    """)
    cols = [d[0] for d in cursor.description]
    rows = [dict(zip(cols, r)) for r in cursor.fetchall()]
    conn.close()
    return rows


def get_all_external_apis():
    """Возвращает все внешние API (включая неактивные)."""
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, name, emoji, url, api_key, method, auth_type, auth_key,
               query_param, query_place, extra_body, result_path,
               description, doc_text, doc_filename, doc_file_id, cost_trips, is_active
        FROM external_apis ORDER BY id
    """)
    cols = [d[0] for d in cursor.description]
    rows = [dict(zip(cols, r)) for r in cursor.fetchall()]
    conn.close()
    return rows


def toggle_external_api(api_id: int) -> bool:
    """Включает/выключает внешний API. Возвращает новое состояние."""
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT is_active FROM external_apis WHERE id = ?", (api_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False
    new_val = 0 if row[0] else 1
    cursor.execute("UPDATE external_apis SET is_active = ? WHERE id = ?", (new_val, api_id))
    conn.commit()
    conn.close()
    return bool(new_val)


def update_external_api_doc(api_id: int, doc_text: str) -> bool:
    """Обновляет только текст документации внешнего API."""
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE external_apis SET doc_text = ? WHERE id = ?", (doc_text, api_id))
    conn.commit()
    ok = cursor.rowcount > 0
    conn.close()
    return ok


def update_external_api_documentation(
    api_id: int,
    doc_text: str = "",
    doc_file_id: str = "",
    doc_filename: str = "",
) -> bool:
    """Полная замена полей документации (текст и/или файл Telegram)."""
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE external_apis
            SET doc_text = ?, doc_file_id = ?, doc_filename = ?
            WHERE id = ?
            """,
            (doc_text or "", doc_file_id or "", doc_filename or "", api_id),
        )
        conn.commit()
        ok = cursor.rowcount > 0
        conn.close()
        return ok
    except Exception as e:
        print(f"[external_apis] update doc full: {e}")
        return False


def execute_external_api_search(api_rec: dict, query: str) -> list:
    """
    Выполняет поиск через внешний API, сохранённый в БД.
    Возвращает список (текст_карточки, None, None, False).
    """
    import json as _json
    name     = api_rec.get("name", "API")
    emoji    = api_rec.get("emoji", "🌐")
    url      = api_rec.get("url", "")
    api_key  = api_rec.get("api_key", "")
    method   = (api_rec.get("method") or "POST").upper()
    auth_type= api_rec.get("auth_type", "body")
    auth_key = api_rec.get("auth_key", "token")
    qparam   = api_rec.get("query_param", "request")
    qplace   = api_rec.get("query_place", "body")
    result_path = api_rec.get("result_path", "")

    try:
        extra_body = _json.loads(api_rec.get("extra_body") or "{}")
    except Exception:
        extra_body = {}

    headers = {"Content-Type": "application/json"}
    body    = {}
    params  = {}

    # Авторизация
    if auth_type == "bearer":
        headers["Authorization"] = f"Bearer {api_key}" if not api_key.startswith("Bearer") else api_key
    elif auth_type == "header":
        headers[auth_key] = api_key
    elif auth_type == "query_param":
        params[auth_key] = api_key
    else:  # body (default)
        if api_key:
            body[auth_key] = api_key

    # Поисковый запрос
    if qplace == "query_param":
        params[qparam] = query
        params.update(extra_body)
    else:
        body[qparam] = query
        body.update(extra_body)

    try:
        if method == "GET":
            resp = requests.get(url, params=params, headers=headers, timeout=25)
        else:
            resp = requests.post(url, json=body, params=params, headers=headers, timeout=25)

        print(f"[EXT_API:{name}] HTTP {resp.status_code}")
        print(f"[EXT_API:{name}] ТЕЛО ОТВЕТА: {resp.text[:500]}")
        if resp.status_code != 200:
            print(f"[EXT_API:{name}] Ответ: {resp.text[:200]}")
            return []

        data = resp.json()
    except requests.exceptions.Timeout:
        print(f"[EXT_API:{name}] Таймаут")
        return []
    except requests.exceptions.ConnectionError:
        print(f"[EXT_API:{name}] Нет соединения")
        return []
    except Exception as e:
        print(f"[EXT_API:{name}] Ошибка: {e}")
        return []

    # ========== CRYVEN: передаём сырой ответ напрямую в форматтер ==========
    if name == "Cryven API":
        return _format_ext_api_results([data], name, emoji)

    # Извлечение результатов по result_path (для всех остальных API)
    records = _extract_ext_api_results(data, result_path)
    if not records:
        return []

    return _format_ext_api_results(records, name, emoji)


def _extract_ext_api_results(data, result_path: str) -> list:
    """Извлекает массив записей из ответа API."""
    if result_path:
        keys = result_path.split(".")
        node = data
        for k in keys:
            if isinstance(node, dict) and k in node:
                node = node[k]
            else:
                return []
        # LeakOSINT-подобный формат: {"DbName": {"Data": [...]}}
        if isinstance(node, dict):
            records = []
            for db_name, db_data in node.items():
                if db_name == "No results found":
                    continue
                if isinstance(db_data, dict) and "Data" in db_data:
                    for rec in db_data["Data"]:
                        rec["__db_name__"] = db_name
                        rec["__info_leak__"] = db_data.get("InfoLeak", "")
                        records.append(rec)
                elif isinstance(db_data, list):
                    for rec in db_data:
                        if isinstance(rec, dict):
                            rec["__db_name__"] = db_name
                        records.append(rec)
            return records
        return node if isinstance(node, list) else []

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("results", "data", "items", "records", "List"):
            if key in data and isinstance(data[key], list):
                return data[key]
        # Словарь как одна запись (только если не мета-ответ)
        META_KEYS = {"count_in_page", "free_request", "time", "success", "queries", "rate"}
        if not (META_KEYS & set(data.keys())) and "error" not in data:
            return [data]
    return []


def _format_ext_api_results(records: list, api_name: str, emoji: str) -> list:
    """Форматирует записи внешнего API в текстовые карточки."""
    formatted = []
    
    # ========== СПЕЦИАЛЬНЫЙ ПАРСЕР ДЛЯ CRYVEN API ==========
    if api_name == "Cryven API":
        # records может приходить в двух вариантах:
        # 1) [полный ответ] = [{"success":true,"results":{"full-result":{...}}}]  (result_path="")
        # 2) [{"full-result":{...}}]  — если result_path="results" (система уже извлекла поле)
        raw = records[0] if isinstance(records, list) and records else records

        if not isinstance(raw, dict):
            return formatted

        # Вариант 1 — полный ответ API
        if "success" in raw:
            if not raw.get("success"):
                return formatted
            detected_type   = raw.get("detected_type", "?")
            detected_source = raw.get("detected_source", "?")
            results_section = raw.get("results", {})
        else:
            # Вариант 2 — система уже вырезала поле results
            detected_type   = "?"
            detected_source = "?"
            results_section = raw

        full_result = results_section.get("full-result", {}) if isinstance(results_section, dict) else {}
        fast_result = full_result.get("fast_result", full_result.get("fast-result", {}))
        db_list     = full_result.get("Базы Данных", [])

        FIELD_LABELS = {
            "birthday":  "🎂 Дата рождения",
            "email":     "✉️ Email",
            "fullname":  "🪪 ФИО",
            "phone":     "📞 Телефон",
            "region":    "📍 Регион/Адрес",
            "country":   "🌍 Страна",
            "address":   "🏠 Адрес",
            "social":    "🌐 Соцсети",
            "vk":        "💙 ВКонтакте",
            "name":      "👤 Имя",
            "inn":       "🏦 ИНН",
            "snils":     "📋 СНИЛС",
            "passport":  "🪪 Паспорт",
            "ip":        "🌐 IP",
        }

        lines = [f"{emoji} <b>Cryven API</b>"]
        lines.append(f"🔍 Тип: {data.get('detected_type','?')} | Источник: {data.get('detected_source','?')}")
        lines.append("")

        # fast_result: {field: [[value, weight], ...]}
        if fast_result:
            for field_name, field_data in fast_result.items():
                if not field_data:
                    continue
                values = []
                for item in field_data:
                    if isinstance(item, list) and len(item) >= 1:
                        val = str(item[0]).strip()
                        if val and val not in values:
                            values.append(val)
                if values:
                    label = FIELD_LABELS.get(field_name, f"📌 {field_name}")
                    lines.append(f"{label}:")
                    for v in values[:10]:
                        lines.append(f"  • {v}")
                    lines.append("")

        # Базы Данных (только при method=all)
        if isinstance(db_list, list) and db_list:
            lines.append("📂 <b>Найдено в базах:</b>")
            for db_entry in db_list[:20]:
                if isinstance(db_entry, dict):
                    db_name = db_entry.get("name") or db_entry.get("db") or "База"
                    lines.append(f"  • {db_name}")
            lines.append("")

        if len(lines) > 3:
            formatted.append(("\n".join(lines), None, None, False))
        return formatted
    # ========== КОНЕЦ СПЕЦИАЛЬНОГО ПАРСЕРА ==========
    
    # Обычный парсер для всех остальных API
    for rec in records:
        if not isinstance(rec, dict):
            continue
        db_name   = rec.pop("__db_name__", api_name)
        info_leak = rec.pop("__info_leak__", "")

        lines = [f"{emoji} ═══ {db_name} ═══"]
        if info_leak:
            lines.append(f"📋 {info_leak}")
        lines.append("")

        for key, value in rec.items():
            if not value or str(value).strip() in ("", "None", "null"):
                continue
            ftype = detect_field_type(str(value))
            icon  = FIELD_ICONS.get(ftype, "▪️")
            lines.append(f"{icon} {key}: {value}")

        if len(lines) > 2:
            formatted.append(("\n".join(lines), None, None, False))

    return formatted

def search_via_all_external_apis(query: str, api_choice) -> list:
    """
    Запрашивает внешние API в зависимости от выбора пользователя.
    api_choice: "ext_all" | "ext_N" (N — id API) | другое (пропускаем)
    Возвращает список (текст, None, None, False).
    """
    results = []
    if not str(api_choice).startswith("ext_"):
        return results

    apis = get_active_external_apis()
    if not apis:
        return results

    if api_choice == "ext_all":
        targets = apis
    else:
        try:
            tid = int(str(api_choice).replace("ext_", ""))
            targets = [a for a in apis if a["id"] == tid]
        except Exception:
            targets = []

    for api_rec in targets:
        try:
            r = execute_external_api_search(api_rec, query)
            results.extend(r)
        except Exception as e:
            print(f"[EXT_API:{api_rec.get('name')}] Ошибка: {e}")

    return results

api_add_state = {}  # {user_id: {"step": ..., "data": {...}}}
# Ожидание новой документации для уже существующего внешнего API: user_id -> api_id
api_doc_edit_wait = {}
# Ожидание документации для встроенного API: user_id -> leakosint | bigbase
builtin_doc_edit_wait = {}

def api_manager_menu(chat_id, message_id=None):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("🔒 Встроенные API (LeakOSINT / BigBase)", callback_data="apimgr_builtin"),
        types.InlineKeyboardButton("🌐 Внешние API", callback_data="apimgr_ext_menu"),
    )
    markup.add(
        types.InlineKeyboardButton("➕ Добавить встроенный API", callback_data="apimgr_add_builtin_choose"),
        types.InlineKeyboardButton("➕ Добавить внешний API", callback_data="apimgr_add"),
    )
    lo_status = "✅" if API_ENABLED else "❌"
    bb_status = "✅" if BIGBASE_ENABLED else "❌"
    ext_count = len(get_active_external_apis())
    text = (
        f"🌐 <b>Менеджер API</b>\n"
        f"<i>Команда:</i> <code>/api_manager</code>\n\n"
        f"🔒 <b>Встроенные:</b>\n"
        f"  {lo_status} LeakOSINT\n"
        f"  {bb_status} BigBase\n\n"
        f"🌐 <b>Внешних активных:</b> <b>{ext_count}</b>\n\n"
        f"Выберите группу для управления или добавьте новый API:"
    )
    if message_id:
        try:
            bot.edit_message_text(
                text, chat_id, message_id, parse_mode="HTML", reply_markup=markup
            )
        except Exception:
            bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
    else:
        bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)

def api_documentation_list_screen(chat_id, message_id=None):
    """Сводка всех документаций (встроенные + внешние) с кнопками просмотра и редактирования."""

    def _esc(s):
        return (
            str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    lines = ["📚 <b>Список документаций</b>\n", "<b>Встроенные API</b>"]
    for kind, em, title in (
        ("leakosint", "🌍", "LeakOSINT"),
        ("bigbase", "📊", "BigBase"),
    ):
        d = get_builtin_api_doc(kind)
        if builtin_api_doc_nonempty(kind):
            parts = []
            if d.get("doc_text"):
                parts.append(f"текст ~{len(d['doc_text'])} симв.")
            if d.get("doc_file_id"):
                fn = d.get("doc_filename") or "файл"
                parts.append(f"файл: {_esc(fn)[:80]}")
            lines.append(f"• {em} <b>{title}</b> — {'; '.join(parts)}")
        else:
            lines.append(f"• {em} <b>{title}</b> — <i>нет</i>")
    lines.append("")
    lines.append("<b>Внешние API</b>")
    ext_rows = get_all_external_apis()
    if not ext_rows:
        lines.append("<i>Список внешних API пуст.</i>")
    else:
        for src in ext_rows:
            nm = _esc(str(src.get("name") or "?"))
            active = "✅" if src.get("is_active") else "❌"
            if src.get("doc_text") or src.get("doc_file_id"):
                parts = []
                if src.get("doc_text"):
                    parts.append(f"текст ~{len(src['doc_text'])} с.")
                if src.get("doc_file_id"):
                    fn = src.get("doc_filename") or "файл"
                    parts.append(f"файл: {_esc(str(fn))[:60]}")
                lines.append(f"• {active} <b>{nm}</b> — {'; '.join(parts)}")
            else:
                lines.append(f"• {active} <b>{nm}</b> — <i>нет документации</i>")
    text = "\n".join(lines)

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("🌍 📖 LO", callback_data="apimgr_builtin_docshow_leakosint"))
    markup.add(types.InlineKeyboardButton("🌍 ✏️ LO", callback_data="apimgr_builtin_editdoc_leakosint"))
    markup.add(types.InlineKeyboardButton("📊 📖 BB", callback_data="apimgr_builtin_docshow_bigbase"))
    markup.add(types.InlineKeyboardButton("📊 ✏️ BB", callback_data="apimgr_builtin_editdoc_bigbase"))
    for src in ext_rows:
        api_id = src["id"]
        raw_name = str(src.get("name") or "?")
        label = raw_name if len(raw_name) <= 20 else raw_name[:17] + "…"
        row = [
            types.InlineKeyboardButton(f"📖 {label}", callback_data=f"apimgr_showdoc_{api_id}"),
            types.InlineKeyboardButton(f"✏️ {label}", callback_data=f"apimgr_editdoc_{api_id}"),
        ]
        markup.add(*row)
    markup.add(types.InlineKeyboardButton("◀️ Назад в менеджер API", callback_data="apimgr_doclist_back"))

    if message_id:
        try:
            bot.edit_message_text(
                text, chat_id, message_id, parse_mode="HTML", reply_markup=markup
            )
        except Exception as e:
            print(f"[api_documentation_list] edit: {e}")
            bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
    else:
        bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)


def api_ext_menu(chat_id, message_id=None):
    """Меню управления внешними API."""
    ext_count = len(get_active_external_apis())
    all_ext = get_all_external_apis()
    text = (
        f"🌐 <b>Внешние API</b>\n\n"
        f"Активных: <b>{ext_count}</b> / Всего: <b>{len(all_ext)}</b>\n\n"
        f"Выберите действие:"
    )
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("📋 Список внешних API", callback_data="apimgr_list"),
        types.InlineKeyboardButton("➕ Добавить внешний API", callback_data="apimgr_add"),
        types.InlineKeyboardButton("📚 Список документаций", callback_data="apimgr_docs_list"),
        types.InlineKeyboardButton("◀️ Назад", callback_data="apimgr_back"),
    )
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, parse_mode="HTML", reply_markup=markup)
        except Exception:
            bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
    else:
        bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)


def api_builtin_menu(chat_id, message_id=None):
    """Панель управления встроенными API (LeakOSINT и BigBase)."""
    lo_status = "✅ Включён" if API_ENABLED else "❌ Выключен"
    bb_status = "✅ Включён" if BIGBASE_ENABLED else "❌ Выключен"
    lo_toggle = "🔴 Выключить LeakOSINT" if API_ENABLED else "🟢 Включить LeakOSINT"
    bb_toggle = "🔴 Выключить BigBase" if BIGBASE_ENABLED else "🟢 Включить BigBase"

    lo_rows = get_builtin_tokens("leakosint")
    bb_rows = get_builtin_tokens("bigbase")

    text = (
        f"🔒 <b>Встроенные API</b>\n\n"
        f"🌍 <b>LeakOSINT</b>\n"
        f"   Статус: {lo_status}\n"
        f"   URL: <code>https://leakosintapi.com/</code>\n"
        f"   Токенов в базе: <b>{len(lo_rows)}</b>\n\n"
        f"📊 <b>BigBase</b>\n"
        f"   Статус: {bb_status}\n"
        f"   URL: <code>https://bigbase.top/api/</code>\n"
        f"   Токенов в базе: <b>{len(bb_rows)}</b>"
    )

    markup = types.InlineKeyboardMarkup(row_width=1)

    # --- Вкл / Выкл ---
    markup.add(types.InlineKeyboardButton(lo_toggle, callback_data="apimgr_builtin_toggle_leakosint"))
    markup.add(types.InlineKeyboardButton(bb_toggle, callback_data="apimgr_builtin_toggle_bigbase"))

    # --- Документации ---
    markup.add(types.InlineKeyboardButton("📚 Список документаций", callback_data="apimgr_docs_list"))
    markup.add(
        types.InlineKeyboardButton("✏️ Изменить документацию LeakOSINT", callback_data="apimgr_builtin_editdoc_leakosint"),
        types.InlineKeyboardButton("✏️ Изменить документацию BigBase", callback_data="apimgr_builtin_editdoc_bigbase"),
    )

    # --- Токены ---
    markup.add(
        types.InlineKeyboardButton("📜 Токены LeakOSINT", callback_data="apimgr_builtin_tokens_leakosint"),
        types.InlineKeyboardButton("📜 Токены BigBase", callback_data="apimgr_builtin_tokens_bigbase"),
    )
    markup.add(
        types.InlineKeyboardButton("➕ Добавить токен LeakOSINT", callback_data="apimgr_builtin_addtoken_leakosint"),
        types.InlineKeyboardButton("➕ Добавить токен BigBase", callback_data="apimgr_builtin_addtoken_bigbase"),
    )

    markup.add(types.InlineKeyboardButton("◀️ Назад", callback_data="apimgr_back"))

    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, parse_mode="HTML", reply_markup=markup)
        except:
            bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
    else:
        bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)

def api_builtin_tokens_list_screen(chat_id, message_id, kind: str):
    """Экран списка токенов LeakOSINT или BigBase с кнопками удаления."""
    title = "LeakOSINT" if kind == "leakosint" else "BigBase"
    rows = get_builtin_tokens(kind)
    if not rows:
        text = (
            f"📜 <b>Токены {title}</b>\n\n"
            f"Список пуст. Нажмите «➕ Токен {title}» в меню встроенных API."
        )
    else:
        lines = [
            f"📜 <b>Токены {title}</b> — всего <b>{len(rows)}</b>\n",
            "Порядок сверху вниз = порядок перебора при поиске.\n",
        ]
        for i, (tid, tok) in enumerate(rows, 1):
            lines.append(f"{i}. <code>{mask_builtin_token_preview(tok)}</code>  <i>(id {tid})</i>")
        text = "\n".join(lines)
    markup = types.InlineKeyboardMarkup(row_width=1)
    for tid, tok in rows:
        preview = mask_builtin_token_preview(tok)
        if len(preview) > 28:
            preview = preview[:25] + "…"
        markup.add(
            types.InlineKeyboardButton(
                f"🗑 Удалить: {preview}",
                callback_data=f"apimgr_tokdel_{kind}_{tid}",
            )
        )
    markup.add(types.InlineKeyboardButton("◀️ К встроенным API", callback_data="apimgr_builtin"))
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, parse_mode="HTML", reply_markup=markup)
        except Exception as e:
            print(f"[api_builtin_tokens_list] edit: {e}")
            bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
    else:
        bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)


# Состояние ожидания нового токена встроенного API (добавление в БД)
_builtin_token_wait = {}  # {user_id: "add_leakosint" | "add_bigbase"}
# Состояние пошагового добавления встроенного токена с документацией
# {user_id: {"kind": "leakosint"|"bigbase", "step": "token"|"doc", "token": str}}
_builtin_add_state = {}


@bot.message_handler(commands=["api_manager"])
def cmd_api_manager(message):
    user_id = message.from_user.id
    if user_id != MAIN_ADMIN_ID and not check_admin_permission(user_id)[0]:
        bot.send_message(message.chat.id, "⛔ Нет прав для этой команды.")
        return
    api_manager_menu(message.chat.id)


def api_add_start(call):
    user_id = call.from_user.id
    api_add_state[user_id] = {"step": "name", "data": {}}
    bot.edit_message_text(
        "➕ <b>Добавление нового API</b>\n\n"
        "Шаг 1/8 — Введите <b>название</b> API (например: MySearchAPI):\n\n"
        "/cancel — отмена",
        call.message.chat.id, call.message.message_id,
        parse_mode="HTML",
    )

def api_list_show(chat_id, message_id=None):
    rows = get_all_external_apis()

    if not rows:
        text = "📋 <b>Список внешних API пуст.</b>\n\nДобавьте первый через кнопку ➕."
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("➕ Добавить", callback_data="apimgr_add"))
        markup.add(
            types.InlineKeyboardButton(
                "📚 Список документаций", callback_data="apimgr_docs_list"
            )
        )
        markup.add(types.InlineKeyboardButton("◀️ Назад", callback_data="apimgr_back"))
        if message_id:
            try:
                bot.edit_message_text(text, chat_id, message_id, parse_mode="HTML", reply_markup=markup)
            except:
                bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
        else:
            bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
        return

    def _esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    markup = types.InlineKeyboardMarkup(row_width=2)
    lines = ["📋 <b>Внешние API:</b>\n"]
    for src in rows:
        api_id  = src["id"]
        name    = _esc(src["name"])
        emoji   = src.get("emoji") or "🌐"
        url     = _esc(src["url"])
        active  = src["is_active"]
        doc_fn  = _esc(src.get("doc_filename") or "")
        desc    = _esc(src.get("description") or "")
        status  = "✅" if active else "❌"
        has_doc = bool(src.get("doc_text") or src.get("doc_file_id"))
        doc_info = f"  📄 {doc_fn}" if doc_fn else ("  📝 есть документация" if has_doc else "  📄 без документации")
        desc_line = f"\n  💬 {desc}" if desc else ""
        lines.append(f"{status} {emoji} <b>{name}</b>\n  🔗 <code>{url}</code>{desc_line}\n{doc_info}")
        toggle_label = "🔴 Выкл" if active else "🟢 Вкл"
        row_btns = [
            types.InlineKeyboardButton(toggle_label, callback_data=f"apimgr_toggle_{api_id}"),
        ]
        if has_doc:
            row_btns.append(
                types.InlineKeyboardButton("📖 Док", callback_data=f"apimgr_showdoc_{api_id}")
            )
        row_btns.append(
            types.InlineKeyboardButton("✏️ Док", callback_data=f"apimgr_editdoc_{api_id}")
        )
        row_btns.append(types.InlineKeyboardButton("🗑 Удалить", callback_data=f"apimgr_del_{api_id}"))
        markup.add(*row_btns)

    markup.add(types.InlineKeyboardButton("➕ Добавить ещё", callback_data="apimgr_add"))
    markup.add(
        types.InlineKeyboardButton("📚 Список документаций", callback_data="apimgr_docs_list")
    )
    markup.add(types.InlineKeyboardButton("◀️ Назад", callback_data="apimgr_back"))

    text = "\n\n".join(lines)
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, parse_mode="HTML", reply_markup=markup)
        except:
            bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
    else:
        bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)

def handle_api_add_steps(message):
    user_id = message.from_user.id
    state = api_add_state[user_id]
    step = state["step"]
    text = message.text.strip() if message.text else ""

    if text == "/cancel":
        del api_add_state[user_id]
        bot.send_message(message.chat.id, "❌ Добавление API отменено.")
        api_manager_menu(message.chat.id)
        return

    skip = text.lower() in ("-", "пропустить", "skip")

    if step == "name":
        if len(text) < 2:
            bot.send_message(message.chat.id, "⚠️ Слишком короткое название, попробуйте снова:")
            return
        state["data"]["name"] = text
        state["step"] = "emoji"
        bot.send_message(
            message.chat.id,
            "Шаг 2/8 — Введите <b>иконку</b> (один эмодзи, например: 🔍 🌐 📡).\n"
            "Или отправьте <code>-</code> чтобы использовать 🌐\n\n/cancel — отмена",
            parse_mode="HTML",
        )

    elif step == "emoji":
        state["data"]["emoji"] = "🌐" if skip else text[:2]
        state["step"] = "description"
        bot.send_message(
            message.chat.id,
            "Шаг 3/8 — Введите краткое <b>описание</b> для пользователей (до 60 символов).\n"
            "Или отправьте <code>-</code> чтобы пропустить.\n\n/cancel — отмена",
            parse_mode="HTML",
        )

    elif step == "description":
        state["data"]["description"] = "" if skip else text[:60]
        state["step"] = "url"
        bot.send_message(
            message.chat.id,
            "Шаг 4/8 — Введите <b>URL</b> API эндпоинта:\n"
            "Например: <code>https://api.example.com/search</code>\n\n/cancel — отмена",
            parse_mode="HTML",
        )

    elif step == "url":
        if not text.startswith("http"):
            bot.send_message(message.chat.id, "⚠️ URL должен начинаться с http:// или https://. Попробуйте снова:")
            return
        state["data"]["url"] = text
        state["step"] = "auth"
        bot.send_message(
            message.chat.id,
            "Шаг 5/8 — <b>Тип авторизации</b>:\n\n"
            "• <code>body</code> — токен в теле JSON (самый частый)\n"
            "• <code>bearer</code> — Bearer-токен в заголовке Authorization\n"
            "• <code>header</code> — кастомный заголовок\n"
            "• <code>query_param</code> — параметр в URL\n"
            "• <code>none</code> — без авторизации\n\n"
            "Введите один из вариантов:\n/cancel — отмена",
            parse_mode="HTML",
        )

    elif step == "auth":
        valid = {"body", "bearer", "header", "query_param", "none"}
        if text.lower() not in valid:
            bot.send_message(message.chat.id, f"⚠️ Введите один из: {', '.join(valid)}")
            return
        state["data"]["auth_type"] = text.lower()
        if text.lower() == "none":
            state["data"]["api_key"]  = ""
            state["data"]["auth_key"] = ""
            state["step"] = "query_param"
            bot.send_message(
                message.chat.id,
                "Шаг 6/8 — Как называется поле поискового запроса?\n"
                "Например: <code>query</code>, <code>search</code>, <code>request</code>\n\n/cancel — отмена",
                parse_mode="HTML",
            )
        elif text.lower() == "bearer":
            state["data"]["auth_key"] = "Authorization"
            state["step"] = "key"
            bot.send_message(
                message.chat.id,
                "Введите <b>Bearer-токен</b>:\n\n/cancel — отмена",
                parse_mode="HTML",
            )
        else:
            state["step"] = "auth_key"
            bot.send_message(
                message.chat.id,
                "Как называется параметр авторизации?\n"
                "Например: <code>token</code>, <code>api_key</code>, <code>X-API-Key</code>\n\n/cancel — отмена",
                parse_mode="HTML",
            )

    elif step == "auth_key":
        state["data"]["auth_key"] = text
        state["step"] = "key"
        bot.send_message(
            message.chat.id,
            "Введите <b>токен / API-ключ</b>.\n"
            "Если ключ не нужен — отправьте <code>-</code>\n\n/cancel — отмена",
            parse_mode="HTML",
        )

    elif step == "key":
        state["data"]["api_key"] = "" if skip else text
        state["step"] = "query_param"
        bot.send_message(
            message.chat.id,
            "Шаг 6/8 — Как называется поле поискового запроса?\n"
            "Например: <code>query</code>, <code>search</code>, <code>request</code>\n\n/cancel — отмена",
            parse_mode="HTML",
        )

    elif step == "query_param":
        state["data"]["query_param"] = text if text else "query"
        state["step"] = "query_place"
        bot.send_message(
            message.chat.id,
            "Шаг 7/8 — Где передавать поисковый запрос?\n\n"
            "• <code>body</code> — в теле JSON\n"
            "• <code>query_param</code> — в URL (?key=value)\n\n/cancel — отмена",
            parse_mode="HTML",
        )

    elif step == "query_place":
        if text.lower() not in ("body", "query_param"):
            bot.send_message(message.chat.id, "⚠️ Введите body или query_param:")
            return
        state["data"]["query_place"] = text.lower()
        state["step"] = "extra"
        bot.send_message(
            message.chat.id,
            "Шаг 7.5/8 — Дополнительные поля тела (JSON) или <code>-</code>:\n"
            "Например: <code>{\"limit\": 100, \"lang\": \"ru\"}</code>\n\n/cancel — отмена",
            parse_mode="HTML",
        )

    elif step == "extra":
        import json as _j
        if skip:
            state["data"]["extra_body"] = "{}"
        else:
            try:
                _j.loads(text)
                state["data"]["extra_body"] = text
            except Exception:
                bot.send_message(message.chat.id, "⚠️ Неверный JSON. Попробуйте снова или отправьте <code>-</code>:", parse_mode="HTML")
                return
        state["step"] = "result_path"
        bot.send_message(
            message.chat.id,
            "Шаг 7.8/8 — Путь к данным в ответе.\n"
            "Например: <code>data</code>, <code>results</code>, <code>List</code>\n"
            "Если данные в корне — отправьте <code>-</code>\n\n/cancel — отмена",
            parse_mode="HTML",
        )

    elif step == "result_path":
        state["data"]["result_path"] = "" if skip else text
        state["step"] = "doc"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("⏭ Пропустить", callback_data="apimgr_skipdoc"))
        bot.send_message(
            message.chat.id,
            "Шаг 8/8 — Прикрепите <b>документацию</b> (файл .txt / .pdf / .json)\n"
            "или введите текст документации,\n"
            "или нажмите «Пропустить».\n\n/cancel — отмена",
            parse_mode="HTML",
            reply_markup=markup,
        )

    elif step in ("doc", "doc_text"):
        state["data"]["doc_text"] = "" if skip else text
        state["data"]["doc_file_id"] = ""
        state["data"]["doc_filename"] = ""
        _api_save(message.chat.id, user_id)




def _api_save(chat_id, user_id):
    data = api_add_state[user_id]["data"]
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO external_apis
                (name, emoji, url, api_key, method, auth_type, auth_key,
                 query_param, query_place, extra_body, result_path,
                 description, doc_text, doc_file_id, doc_filename, cost_trips, added_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data.get("name", ""),
            data.get("emoji", "🌐"),
            data.get("url", ""),
            data.get("api_key", ""),
            data.get("method", "POST"),
            data.get("auth_type", "body"),
            data.get("auth_key", "token"),
            data.get("query_param", "request"),
            data.get("query_place", "body"),
            data.get("extra_body", "{}"),
            data.get("result_path", ""),
            data.get("description", ""),
            data.get("doc_text", ""),
            data.get("doc_file_id", ""),
            data.get("doc_filename", ""),
            data.get("cost_trips", 1),
            user_id,
        ))
        conn.commit()
        conn.close()
        bot.send_message(
            chat_id,
            f"✅ API <b>{data['name']}</b> успешно добавлен!\n"
            f"Пользователи уже могут выбрать его при поиске.",
            parse_mode="HTML",
        )
    except Exception as e:
        bot.send_message(chat_id, f"❌ Ошибка сохранения: {e}")
    finally:
        if user_id in api_add_state:
            del api_add_state[user_id]
    api_manager_menu(chat_id)

@bot.message_handler(
    content_types=["document"],
    func=lambda m: m.from_user.id in _builtin_add_state
    and _builtin_add_state[m.from_user.id]["step"] == "doc",
)
def handle_builtin_api_doc_upload(message):
    user_id = message.from_user.id
    st = _builtin_add_state.get(user_id)
    if not st:
        return
    kind = st["kind"]
    title = "LeakOSINT" if kind == "leakosint" else "BigBase"
    doc = message.document
    filename = doc.file_name or "document"
    doc_text = ""
    ext = os.path.splitext(filename)[1].lower()
    if ext in (".txt", ".json"):
        try:
            file_info = bot.get_file(doc.file_id)
            raw = bot.download_file(file_info.file_path)
            doc_text = raw.decode("utf-8", errors="ignore")[:10000]
        except Exception:
            pass
    _builtin_add_state.pop(user_id, None)
    ok, err = add_builtin_token(kind, st["token"])
    if ok:
        update_builtin_api_documentation(kind, doc_text, doc.file_id, filename)
        bot.send_message(
            message.chat.id,
            f"✅ Токен <b>{title}</b> добавлен! Документация (файл: <code>{filename}</code>) сохранена.",
            parse_mode="HTML",
        )
    else:
        bot.send_message(message.chat.id, f"❌ {err}")
    api_builtin_menu(message.chat.id)


@bot.message_handler(
    content_types=["document"],
    func=lambda m: m.from_user.id in api_add_state
    and api_add_state[m.from_user.id]["step"] == "doc",
)
def handle_api_doc_upload(message):
    user_id = message.from_user.id
    state = api_add_state[user_id]
    doc = message.document
    filename = doc.file_name or "document"
    doc_text = ""
    ext = os.path.splitext(filename)[1].lower()
    if ext in (".txt", ".json"):
        try:
            file_info = bot.get_file(doc.file_id)
            raw = bot.download_file(file_info.file_path)
            doc_text = raw.decode("utf-8", errors="ignore")[:10000]
        except:
            pass
    state["data"]["doc_file_id"] = doc.file_id
    state["data"]["doc_filename"] = filename
    state["data"]["doc_text"] = doc_text
    _api_save(message.chat.id, user_id)


def _api_doc_edit_priority(message):
    """Документ для режима редактирования документации внешнего API."""
    uid = message.from_user.id
    if uid in builtin_doc_edit_wait:
        return False
    if uid not in api_doc_edit_wait:
        return False
    if uid in api_add_state and api_add_state[uid].get("step") == "doc":
        return False
    return True


@bot.message_handler(content_types=["document"], func=_api_doc_edit_priority)
def handle_api_doc_edit_document(message):
    user_id = message.from_user.id
    api_id = api_doc_edit_wait.pop(user_id)
    doc = message.document
    filename = doc.file_name or "document"
    ext = os.path.splitext(filename)[1].lower()
    doc_text = ""
    if ext in (".txt", ".json"):
        try:
            file_info = bot.get_file(doc.file_id)
            raw = bot.download_file(file_info.file_path)
            doc_text = raw.decode("utf-8", errors="ignore")[:10000]
        except Exception:
            pass
    if update_external_api_documentation(api_id, doc_text, doc.file_id, filename):
        fn_esc = (
            str(filename)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        bot.send_message(
            message.chat.id,
            f"✅ Документация обновлена и привязана к API.\n📎 Файл: <code>{fn_esc}</code>",
            parse_mode="HTML",
        )
    else:
        bot.send_message(message.chat.id, "❌ Не удалось сохранить.")
    api_list_show(message.chat.id)


def _builtin_doc_edit_priority(message):
    """Документ для режима редактирования документации встроенного API."""
    uid = message.from_user.id
    if uid not in builtin_doc_edit_wait:
        return False
    if uid in api_doc_edit_wait:
        return False
    if uid in api_add_state and api_add_state[uid].get("step") == "doc":
        return False
    return True


@bot.message_handler(content_types=["document"], func=_builtin_doc_edit_priority)
def handle_builtin_doc_edit_document(message):
    user_id = message.from_user.id
    kind = builtin_doc_edit_wait.pop(user_id)
    doc = message.document
    filename = doc.file_name or "document"
    ext = os.path.splitext(filename)[1].lower()
    doc_text = ""
    if ext in (".txt", ".json"):
        try:
            file_info = bot.get_file(doc.file_id)
            raw = bot.download_file(file_info.file_path)
            doc_text = raw.decode("utf-8", errors="ignore")[:10000]
        except Exception:
            pass
    if update_builtin_api_documentation(kind, doc_text, doc.file_id, filename):
        fn_esc = (
            str(filename)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        bot.send_message(
            message.chat.id,
            f"✅ Документация встроенного API сохранена.\n📎 Файл: <code>{fn_esc}</code>",
            parse_mode="HTML",
        )
    else:
        bot.send_message(message.chat.id, "❌ Не удалось сохранить.")
    api_builtin_menu(message.chat.id)


# ========== ВАЖНО: Обработчик ответов админов ==========
@bot.message_handler(
    func=lambda message: (
        message.from_user.id in support_mode and not message.text.startswith("/")
    )
)
def handle_admin_reply(message):
    admin_id = message.from_user.id
    user_id = support_mode[admin_id]

    try:
        is_banned, _ = check_banned(user_id)
        if is_banned:
            bot.send_message(admin_id, "❌ Нельзя отправить сообщение забаненному пользователю!")
            return

        # Чистая пересылка без системных надписей
        bot.send_message(user_id, message.text)
        save_support_message(user_id, message.text, is_from_user=False, admin_id=admin_id)

    except Exception as e:
        bot.send_message(admin_id, f"❌ Не удалось отправить: {e}")


# ========== ОБРАБОТЧИК СООБЩЕНИЙ ADMIN-ПРИЗРАКА → ПОЛЬЗОВАТЕЛЮ ==========
@bot.message_handler(
    func=lambda message: (
        message.from_user.id in ghost_mode
        and message.from_user.id not in support_mode
        and message.text
        and not message.text.startswith("/")
    )
)
def handle_ghost_admin_message(message):
    """Когда admin в режиме призрака пишет — сообщение уходит целевому пользователю."""
    admin_id = message.from_user.id
    target_id = ghost_mode[admin_id]
    try:
        _original_send_message(target_id, message.text)
        _original_send_message(
            admin_id,
            f"✅ *[ПРИЗРАК]* Отправлено → `{target_id}`:\n{message.text}",
            parse_mode="Markdown"
        )
    except Exception as e:
        _original_send_message(admin_id, f"❌ Не удалось отправить призраком: {e}")


# ========== ОБРАБОТЧИК ФОТО (для /texts редактирования) ==========
@bot.message_handler(content_types=["photo"])
def handle_photo_message(message):
    user_id = message.from_user.id

    # Проверяем — ожидаем ли фото для редактирования текста
    if user_id in admin_create_file:
        state = admin_create_file[user_id]
        mode = state.get("mode", "")
        if mode.startswith("editing_text_"):
            # Передаём в основной обработчик через content_type="photo"
            handle_buttons(message)
            return

    # Вне режима редактирования — фото игнорируем (или можно добавить другую логику)


# ========== ОБРАБОТЧИК КНОПОК И СООБЩЕНИЙ ==========
@bot.message_handler(func=lambda message: True)
def handle_buttons(message):

    if message.content_type == "text" and message.text and message.text.startswith("/"):
        return

    user_id = message.from_user.id
    text = message.text or ""

    # ── Ранний перехват фото для редактирования текстов ──────────────────────
    if message.content_type == "photo" and user_id in admin_create_file:
        mode = admin_create_file[user_id].get("mode", "")
        if mode.startswith("editing_text_"):
            key = mode.replace("editing_text_", "")
            state_data = admin_create_file[user_id].get("data", {})
            current_photo = state_data.get("current_photo")

            new_photo = message.photo[-1].file_id
            if message.caption:
                new_text = message.caption
            else:
                new_text = get_bot_text(key)  # текст не меняем

            success, result = update_bot_text(user_id, key, new_text, photo_id=new_photo)
            if success:
                bot.send_message(
                    message.chat.id,
                    f"✅ Текст <b>{key}</b> обновлён! 📷 Фото сохранено.",
                    parse_mode="HTML",
                    reply_markup=main_menu(),
                )
            else:
                bot.send_message(message.chat.id, f"❌ Ошибка: {result}", reply_markup=main_menu())
            del admin_create_file[user_id]
            return
        return  # фото вне режима редактирования — игнорируем
    elif message.content_type == "photo":
        return  # фото без контекста — игнорируем

    is_banned, ban_reason = check_banned(user_id)
    if is_banned:
        bot.send_message(
            message.chat.id,
            f"🚫 ДОСТУП ЗАПРЕЩЕН!\n\nВы были забанены.\nПричина: {ban_reason}",
        )
        return

    # ========== ДИАЛОГ ДОБАВЛЕНИЯ БЛОКИРОВКИ ЗАПРОСА ==========
    if user_id in admin_block_query_state:
        state = admin_block_query_state[user_id]
        step = state.get("step")

        if step == "pattern":
            state["pattern"] = text.strip()
            state["step"] = "text"
            bot.send_message(
                message.chat.id,
                "✅ Паттерн сохранён.\n\n"
                "Шаг 2/3: Введите <b>текст ответа</b> который увидит пользователь при этом запросе.\n\n"
                "<i>Пример: 🔒 Информация по данному лицу засекречена по решению администрации.</i>\n\n"
                "/cancel — отмена",
                parse_mode="HTML",
            )
            return

        elif step == "text":
            state["custom_text"] = text.strip()
            state["step"] = "mode"
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("🔍 Содержит (рекомендуется)", callback_data="qblock_mode_contains"),
                types.InlineKeyboardButton("🎯 Точное совпадение", callback_data="qblock_mode_exact"),
            )
            bot.send_message(
                message.chat.id,
                "✅ Текст ответа сохранён.\n\n"
                "Шаг 3/3: Выберите <b>режим совпадения</b>:\n\n"
                "• <b>Содержит</b> — блокирует если запрос <i>содержит</i> паттерн (подходит для имён)\n"
                "• <b>Точное совпадение</b> — только если запрос полностью совпадает с паттерном",
                parse_mode="HTML",
                reply_markup=markup,
            )
            return
        return  # игнорируем текст на шаге mode (ждём кнопки)

    # ОБЯЗАТЕЛЬНАЯ ПРОВЕРКА ПОДПИСКИ ДЛЯ ЛЮБОГО ДЕЙСТВИЯ
    if not check_subscription(user_id) and user_id != MAIN_ADMIN_ID:
        markup = types.InlineKeyboardMarkup()
        btn_channel = types.InlineKeyboardButton(
            "📢 ПОДПИСАТЬСЯ", url=REQUIRED_CHANNEL_INVITE
        )
        btn_check = types.InlineKeyboardButton(
            "✅ Я ПОДПИСАЛСЯ", callback_data="check_sub"
        )
        markup.add(btn_channel)
        markup.add(btn_check)

        bot.send_message(
            message.chat.id,
            f"🚫 ДОСТУП ЗАПРЕЩЕН!\n\n"
            f"Для использования бота необходимо подписаться на канал.\n\n"
            f"👉 Подпишись и нажми кнопку ниже 👇",
            reply_markup=markup,
        )
        return

    if not check_flood(user_id):
        is_banned, warnings = add_warning(user_id)
        if is_banned:
            bot.send_message(
                message.chat.id,
                f"🚫 ВЫ ЗАБАНЕНЫ!\n\nПричина: Автоматический бан за флуд.",
            )
            return
        else:
            bot.send_message(
                message.chat.id,
                f"⚠️ НЕ ФЛУДИ!\n\n"
                f"Предупреждение {warnings}/{flood_settings['warnings_before_ban']}",
            )
            return

    if user_id in admin_broadcast:
        threading.Thread(target=send_broadcast, args=(user_id, text)).start()
        del admin_broadcast[user_id]
        return

    if user_id in live_chat:
        admin_id = live_chat[user_id]
        try:
            bot.send_message(admin_id, message.text)
            save_support_message(user_id, text, is_from_user=True, admin_id=admin_id)
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ Ошибка пересылки: {e}")
        return

    # ── Режим ПРИЗРАКА: дублируем сообщение админу ───────────────────────────
    if user_id in ghost_reverse:
        admin_id = ghost_reverse[user_id]
        try:
            bot.send_message(
                admin_id,
                f"👻 *[ПРИЗРАК]* `{user_id}` → боту:\n{text}",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        # НЕ делаем return — сообщение продолжает обрабатываться как обычно

    if user_id in user_support_request:
        # Сохраняем сообщение в историю
        save_support_message(user_id, text)

        # Отправляем подтверждение пользователю
        bot.send_message(
            user_id, "✅ Ваше сообщение отправлено в поддержку. Ожидайте ответа."
        )

        # Уведомляем админов
        notify_admins_support(
            user_id, message.from_user.first_name or "Пользователь", text
        )

        # Удаляем пользователя из ожидающих
        del user_support_request[user_id]
        return

    if user_id in admin_create_file:
        mode = admin_create_file[user_id]["mode"]

        if mode == "upload_rename":
            new_name = "".join(c for c in text.strip() if c.isalnum() or c in " .-_")
            if user_id in pending_uploads:
                pending_uploads[user_id]["filename"] = new_name
                info = pending_uploads[user_id]
                markup = types.InlineKeyboardMarkup(row_width=1)
                markup.add(
                    types.InlineKeyboardButton(
                        "✅ Сохранить в базу", callback_data=f"upload_confirm_{user_id}"
                    ),
                    types.InlineKeyboardButton(
                        "❌ Отмена", callback_data=f"upload_cancel_{user_id}"
                    ),
                )
                bot.send_message(
                    message.chat.id,
                    f"✅ Название: {new_name}.txt\n📝 Записей: {len(info['records'])}\n\nПодтвердите сохранение:",
                    reply_markup=markup,
                )
            else:
                bot.send_message(
                    message.chat.id,
                    "❌ Нет данных для сохранения.",
                    reply_markup=main_menu(),
                )
            del admin_create_file[user_id]
            return

        elif mode == "waiting_code_name":
            code_text = text.strip()
            duration = admin_create_file[user_id]["data"]["duration"]
            permissions = admin_create_file[user_id]["data"].get(
                "permissions_str", "all"
            )

            if code_text.lower() == "random":
                code, error = create_admin_code(user_id, None, duration, permissions)
                if code:
                    bot.send_message(
                        message.chat.id,
                        f"✅ Код успешно создан!\n\n"
                        f"🔑 Код: {code}\n"
                        f"⏰ Действует: {get_duration_text(duration)}\n\n"
                        f"📌 После активации код станет недействительным.",
                        reply_markup=main_menu(),
                    )
                else:
                    bot.send_message(
                        message.chat.id, f"❌ Ошибка: {error}", reply_markup=main_menu()
                    )
            else:
                code, error = create_admin_code(
                    user_id, code_text, duration, permissions
                )
                if code:
                    bot.send_message(
                        message.chat.id,
                        f"✅ Код успешно создан!\n\n"
                        f"🔑 Код: {code}\n"
                        f"⏰ Действует: {get_duration_text(duration)}\n\n"
                        f"📌 После активации код станет недействительным.",
                        reply_markup=main_menu(),
                    )
                else:
                    bot.send_message(
                        message.chat.id, f"❌ Ошибка: {error}", reply_markup=main_menu()
                    )

            del admin_create_file[user_id]
            return

        elif mode == "waiting_name":
            filename = text.strip()
            filename = "".join(c for c in filename if c.isalnum() or c in " .-_).:,;")

            admin_create_file[user_id]["data"]["filename"] = filename
            admin_create_file[user_id]["mode"] = "waiting_content"

            bot.send_message(
                message.chat.id,
                f"✅ Имя файла: {filename}.txt\n\n"
                f"📝 Теперь отправь содержимое файла:\n\n"
                f"Отправь /cancel для отмены",
            )
            return

        elif mode == "waiting_content":
            filename = admin_create_file[user_id]["data"]["filename"]
            content = text

            try:
                if not os.path.exists(DATA_DIR):
                    os.makedirs(DATA_DIR)

                filepath = os.path.join(DATA_DIR, f"{filename}.txt")
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)

                bot.send_message(
                    message.chat.id,
                    f"✅ Файл успешно создан!\n\n"
                    f"📁 Путь: {filepath}\n"
                    f"🔍 Теперь если пользователь введет {filename}, он увидит этот текст.",
                    reply_markup=main_menu(),
                )

            except Exception as e:
                bot.send_message(
                    message.chat.id, f"❌ Ошибка: {e}", reply_markup=main_menu()
                )

            del admin_create_file[user_id]
            return

        elif mode == "waiting_delete":
            filename = text.strip()
            filepath = os.path.join(DATA_DIR, filename)

            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
                    bot.send_message(
                        message.chat.id,
                        f"✅ Файл {filename} удален!",
                        reply_markup=main_menu(),
                    )
                else:
                    txt_path = os.path.join(
                        DATA_DIR,
                        f"{filename}.txt"
                        if not filename.endswith(".txt")
                        else filename,
                    )
                    if os.path.exists(txt_path):
                        os.remove(txt_path)
                        bot.send_message(
                            message.chat.id,
                            f"✅ Файл {os.path.basename(txt_path)} удален!",
                            reply_markup=main_menu(),
                        )
                    else:
                        bot.send_message(
                            message.chat.id,
                            f"❌ Файл {filename} не найден!",
                            reply_markup=main_menu(),
                        )
            except Exception as e:
                bot.send_message(
                    message.chat.id, f"❌ Ошибка: {e}", reply_markup=main_menu()
                )

            del admin_create_file[user_id]
            return

        elif mode == "editing":
            filename = admin_create_file[user_id]["data"]["filename"]
            new_content = text

            try:
                filepath = os.path.join(DATA_DIR, f"{filename}.txt")
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(new_content)

                # ===== ОБНОВЛЯЕМ SQLite ИНДЕКС =====
                try:
                    records = [
                        line.split(";")
                        for line in new_content.splitlines()
                        if line.strip()
                    ]
                    db_type = detect_db_type_by_name(f"{filename}.txt")
                    sqlite_add_records(f"{filename}.txt", records, db_type)
                    print(f"[sqlite] Переиндексирован файл после редактирования: {filename}.txt")
                except Exception as e:
                    print(f"[sqlite] Ошибка переиндексации при редактировании: {e}")
                # ===================================

                bot.send_message(
                    message.chat.id,
                    f"✅ Файл отредактирован!\n\n📁 Файл: {filename}.txt",
                    reply_markup=main_menu(),
                )

            except Exception as e:
                bot.send_message(
                    message.chat.id, f"❌ Ошибка: {e}", reply_markup=main_menu()
                )

            del admin_create_file[user_id]
            return

        elif mode == "renaming":
            old_filename = admin_create_file[user_id]["data"]["old_filename"]
            new_name = text.strip()
            new_name = "".join(c for c in new_name if c.isalnum() or c in " .-_).:,;")

            try:
                old_path = os.path.join(DATA_DIR, f"{old_filename}.txt")
                new_path = os.path.join(DATA_DIR, f"{new_name}.txt")

                if os.path.exists(old_path):
                    os.rename(old_path, new_path)

                    # ===== ОБНОВЛЯЕМ SQLite ИНДЕКС =====
                    try:
                        conn_r = sqlite3.connect("users.db")
                        cursor_r = conn_r.cursor()
                        cursor_r.execute(
                            "DELETE FROM search_data WHERE source_file = ?",
                            (f"{old_filename}.txt",),
                        )
                        conn_r.commit()
                        db_type_r = detect_db_type_by_name(f"{new_name}.txt")
                        _sqlite_import_file(conn_r, new_path, f"{new_name}.txt", db_type_r)
                        conn_r.close()
                        print(f"[sqlite] Переиндексирован: {old_filename}.txt -> {new_name}.txt")
                    except Exception as e:
                        print(f"[sqlite] Ошибка переиндексации при переименовании: {e}")
                    # ===================================

                    bot.send_message(
                        message.chat.id,
                        f"✅ Файл переименован!\n\n"
                        f"📁 Было: {old_filename}.txt\n"
                        f"📁 Стало: {new_name}.txt",
                        reply_markup=main_menu(),
                    )
                else:
                    bot.send_message(
                        message.chat.id, "❌ Файл не найден!", reply_markup=main_menu()
                    )

            except Exception as e:
                bot.send_message(
                    message.chat.id, f"❌ Ошибка: {e}", reply_markup=main_menu()
                )

            del admin_create_file[user_id]
            return

        elif mode.startswith("editing_text_"):
            key = mode.replace("editing_text_", "")
            state_data = admin_create_file[user_id].get("data", {})
            current_photo = state_data.get("current_photo")

            new_text  = None
            new_photo = None

            if message.content_type == "photo":
                # Фото (с подписью или без)
                new_photo = message.photo[-1].file_id
                if message.caption:
                    new_text = message.caption
                else:
                    # Только фото — текст не меняем
                    new_text = get_bot_text(key)  # сохраняем старый
            elif message.content_type == "text":
                new_text = message.text
                if new_text.strip() == "-":
                    # Сброс фото
                    new_photo = None
                    new_text = get_bot_text(key)
                else:
                    # Только текст — фото не меняем
                    new_photo = current_photo

            if new_text is None:
                bot.send_message(message.chat.id, "❌ Отправь текст или фото (можно с подписью).")
                return

            success, result = update_bot_text(user_id, key, new_text, photo_id=new_photo)

            if success:
                photo_note = "🖼 Фото сброшено" if new_photo is None and current_photo else ("📷 Фото обновлено" if new_photo else "")
                bot.send_message(
                    message.chat.id,
                    f"✅ Текст <b>{key}</b> обновлён! {photo_note}",
                    parse_mode="HTML",
                    reply_markup=main_menu(),
                )
            else:
                bot.send_message(
                    message.chat.id, f"❌ Ошибка: {result}", reply_markup=main_menu()
                )

            del admin_create_file[user_id]
            return

        return

    # ========== ВСТРОЕННЫЕ API: ПОШАГОВОЕ ДОБАВЛЕНИЕ ТОКЕНА + ДОКУМЕНТАЦИИ ==========
    if user_id in _builtin_add_state:
        st = _builtin_add_state[user_id]
        kind = st["kind"]
        title = "LeakOSINT" if kind == "leakosint" else "BigBase"
        text_in = (message.text or "").strip()

        if text_in == "/cancel":
            _builtin_add_state.pop(user_id, None)
            bot.send_message(message.chat.id, "❌ Отменено.")
            api_builtin_menu(message.chat.id)
            return

        if st["step"] == "token":
            if len(text_in) < 4:
                bot.send_message(message.chat.id, "⚠️ Токен слишком короткий. Попробуйте ещё раз:")
                return
            st["token"] = text_in
            st["step"] = "doc"
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("⏭️ Пропустить", callback_data="apimgr_builtin_skipdoc"))
            bot.send_message(
                message.chat.id,
                f"🔒 <b>Добавление токена {title}</b>\n\n"
                f"Шаг 2/2 — Опционально: прикрепите <b>документацию</b>\n"
                f"(файл .txt/.pdf/.json или текст сообщением)\n\n"
                f"Документация не обязательна — нажмите «Пропустить» чтобы добавить только токен.\n\n"
                "/cancel — отмена",
                parse_mode="HTML",
                reply_markup=markup,
            )
            return

        if st["step"] == "doc":
            # Got text as documentation
            doc_text = "" if text_in in ("-", "пропустить", "skip") else text_in
            _builtin_add_state.pop(user_id, None)
            ok, err = add_builtin_token(kind, st["token"])
            if ok:
                if doc_text:
                    update_builtin_api_documentation(kind, doc_text, "", "")
                bot.send_message(
                    message.chat.id,
                    f"✅ Токен <b>{title}</b> добавлен!" +
                    (" Документация сохранена." if doc_text else " Без документации."),
                    parse_mode="HTML",
                )
            else:
                bot.send_message(message.chat.id, f"❌ {err}")
            api_builtin_menu(message.chat.id)
            return

    # ========== ВСТРОЕННЫЕ API: ВВОД НОВОГО ТОКЕНА (сохранение в БД) ==========
    if user_id in _builtin_token_wait:
        which = _builtin_token_wait.pop(user_id)
        if text == "/cancel":
            bot.send_message(message.chat.id, "❌ Отменено.")
            api_manager_menu(message.chat.id)
            return
        if which in ("add_leakosint", "add_bigbase"):
            kind = "leakosint" if which == "add_leakosint" else "bigbase"
            title = "LeakOSINT" if kind == "leakosint" else "BigBase"
            ok, err = add_builtin_token(kind, text)
            if ok:
                bot.send_message(
                    message.chat.id,
                    f"✅ Токен <b>{title}</b> добавлен и сохранён в базе бота.\n"
                    f"<code>{mask_builtin_token_preview(text)}</code>",
                    parse_mode="HTML",
                )
            else:
                bot.send_message(message.chat.id, f"❌ {err}")
            api_builtin_menu(message.chat.id)
            return

    # ========== РЕДАКТИРОВАНИЕ ДОКУМЕНТАЦИИ ВСТРОЕННОГО API (только текст) ==========
    if user_id in builtin_doc_edit_wait:
        if message.content_type != "text":
            return
        t = (message.text or "").strip()
        if t == "/cancel":
            del builtin_doc_edit_wait[user_id]
            if user_id in api_doc_edit_wait:
                del api_doc_edit_wait[user_id]
            bot.send_message(message.chat.id, "❌ Отменено.")
            api_builtin_menu(message.chat.id)
            return
        kind = builtin_doc_edit_wait.pop(user_id)
        if t == "-":
            if update_builtin_api_documentation(kind, "", "", ""):
                bot.send_message(message.chat.id, "✅ Документация встроенного API удалена.")
            else:
                bot.send_message(message.chat.id, "❌ Не удалось обновить запись.")
        else:
            if update_builtin_api_documentation(kind, t, "", ""):
                bot.send_message(
                    message.chat.id,
                    "✅ Текст документации сохранён для встроенного API.",
                )
            else:
                bot.send_message(message.chat.id, "❌ Не удалось сохранить.")
        api_builtin_menu(message.chat.id)
        return

    # ========== РЕДАКТИРОВАНИЕ ДОКУМЕНТАЦИИ ВНЕШНЕГО API (только текст) ==========
    if user_id in api_doc_edit_wait:
        if message.content_type != "text":
            return
        t = (message.text or "").strip()
        if t == "/cancel":
            del api_doc_edit_wait[user_id]
            if user_id in builtin_doc_edit_wait:
                del builtin_doc_edit_wait[user_id]
            bot.send_message(message.chat.id, "❌ Отменено.")
            api_list_show(message.chat.id)
            return
        api_id = api_doc_edit_wait.pop(user_id)
        if t == "-":
            if update_external_api_documentation(api_id, "", "", ""):
                bot.send_message(message.chat.id, "✅ Документация удалена.")
            else:
                bot.send_message(message.chat.id, "❌ Не удалось обновить запись.")
        else:
            if update_external_api_documentation(api_id, t, "", ""):
                bot.send_message(
                    message.chat.id,
                    "✅ Текст документации сохранён и привязан к этому API.",
                )
            else:
                bot.send_message(message.chat.id, "❌ Не удалось сохранить.")
        api_list_show(message.chat.id)
        return

    # ========== API МЕНЕДЖЕР: ПОШАГОВЫЙ ВВОД ==========
    if user_id in api_add_state:
        handle_api_add_steps(message)
        return

    if user_id in admin_action:
        action = admin_action[user_id]["action"]

        if action == "grant_admin":
            try:
                target_id = int(text)
                success, result = grant_admin_permission(user_id, target_id, 30)
                if success:
                    bot.send_message(message.chat.id, f"✅ Права выданы до {result}")
                else:
                    bot.send_message(message.chat.id, f"❌ {result}")
            except:
                bot.send_message(message.chat.id, "❌ ID должен быть числом!")
            del admin_action[user_id]
            return

        elif action == "revoke_admin":
            try:
                target_id = int(text)
                success, msg = revoke_admin_permission(user_id, target_id)
                if success:
                    bot.send_message(message.chat.id, f"✅ {msg}")
                else:
                    bot.send_message(message.chat.id, f"❌ {msg}")
            except:
                bot.send_message(message.chat.id, "❌ ID должен быть числом!")
            del admin_action[user_id]
            return

        elif action == "ban_user":
            parts = text.split(maxsplit=1)
            try:
                target_id = int(parts[0])
                reason = parts[1] if len(parts) > 1 else "Без причины"
                success, result = ban_user(user_id, target_id, reason)
                if success:
                    bot.send_message(message.chat.id, f"✅ {result}")
                    try:
                        bot.send_message(
                            target_id, f"🚫 ВЫ ЗАБАНЕНЫ!\n\nПричина: {reason}"
                        )
                    except:
                        pass
                else:
                    bot.send_message(message.chat.id, f"❌ {result}")
            except:
                bot.send_message(message.chat.id, "❌ Используйте: ID [причина]")
            del admin_action[user_id]
            return

        elif action == "unban_user":
            try:
                target_id = int(text)
                success, result = unban_user(user_id, target_id)
                if success:
                    bot.send_message(message.chat.id, f"✅ {result}")
                else:
                    bot.send_message(message.chat.id, f"❌ {result}")
            except:
                bot.send_message(message.chat.id, "❌ ID должен быть числом!")
            del admin_action[user_id]
            return

        elif action == "clearflood":
            try:
                target_id = int(text)
                reset_flood_counter(target_id)
                bot.send_message(
                    message.chat.id, f"✅ Счетчик флуда для {target_id} сброшен"
                )
            except:
                bot.send_message(message.chat.id, "❌ ID должен быть числом!")
            del admin_action[user_id]
            return

        elif action == "floodsettings":
            parts = text.split()
            if len(parts) == 2:
                try:
                    param = parts[0]
                    value = int(parts[1])
                    if param in flood_settings:
                        flood_settings[param] = value
                        bot.send_message(message.chat.id, f"✅ {param} = {value}")
                    else:
                        bot.send_message(
                            message.chat.id,
                            f"❌ Неверный параметр. Доступно: {', '.join(flood_settings.keys())}",
                        )
                except:
                    bot.send_message(message.chat.id, "❌ Значение должно быть числом!")
            else:
                text = f"""📊 ТЕКУЩИЕ НАСТРОЙКИ:
max_messages: {flood_settings["max_messages"]}
period_seconds: {flood_settings["period_seconds"]}
warnings_before_ban: {flood_settings["warnings_before_ban"]}

Введите: параметр значение
Например: max_messages 10"""
                bot.send_message(message.chat.id, text)
            del admin_action[user_id]
            return

        elif action == "stats_userhistory_input":
            try:
                target_id = int(text)

                conn = sqlite3.connect("users.db")
                cursor = conn.cursor()

                cursor.execute(
                    """
                    SELECT username, first_name, last_name, subscription_status, created_at
                    FROM users WHERE user_id = ?
                """,
                    (target_id,),
                )
                user_info = cursor.fetchone()

                if not user_info:
                    bot.send_message(
                        message.chat.id,
                        f"❌ Пользователь с ID {target_id} не найден в базе",
                    )
                    conn.close()
                    del admin_action[user_id]
                    return

                username, first_name, last_name, sub_status, reg_date = user_info

                total_queries = cursor.execute(
                    "SELECT COUNT(*) FROM search_history WHERE user_id = ?",
                    (target_id,),
                ).fetchone()[0]
                found_queries = cursor.execute(
                    "SELECT COUNT(*) FROM search_history WHERE user_id = ? AND found = 1",
                    (target_id,),
                ).fetchone()[0]
                not_found_queries = cursor.execute(
                    "SELECT COUNT(*) FROM search_history WHERE user_id = ? AND found = 0",
                    (target_id,),
                ).fetchone()[0]

                username_safe   = md_escape(username or "Нет")
                first_name_safe = md_escape(first_name or "Нет")
                last_name_safe  = md_escape(last_name or "")
                sub_status_safe = md_escape(sub_status or "inactive")
                reg_date_safe   = md_escape(str(reg_date or "Неизвестно"))

                user_text = f"""👤 **ИНФОРМАЦИЯ О ПОЛЬЗОВАТЕЛЕ**
╔════════════════════════════╗
║ 🆔 ID: {target_id}
║ 👤 Username: @{username_safe}
║ 📝 Имя: {first_name_safe} {last_name_safe}
║ 📌 Статус подписки: {sub_status_safe}
║ 📅 Дата регистрации: {reg_date_safe}
╚════════════════════════════╝

📊 **СТАТИСТИКА ЗАПРОСОВ**
╔════════════════════════════╗
║ Всего запросов: {total_queries}
║ ✅ Найдено: {found_queries}
║ ❌ Не найдено: {not_found_queries}
╚════════════════════════════╝
"""
                if total_queries > 0:
                    history = get_user_history_with_first(cursor, target_id, include_first=True)
                    history_text = "\n📜 **ИСТОРИЯ ЗАПРОСОВ ПОЛЬЗОВАТЕЛЯ**\n\n"
                    for i, (_, query, found, search_time) in enumerate(history, 1):
                        status = "✅ НАЙДЕНО" if found else "❌ НЕ НАЙДЕНО"
                        query_safe = md_escape(str(query))
                        history_text += f"{i}. 🔍 `{query_safe}`\n   {status} - {search_time}\n\n"
                    user_text += history_text

                conn.close()

                # Трипы
                trips_balance = 0
                is_target_admin = False
                try:
                    conn2 = __import__('sqlite3').connect("users.db")
                    c2 = conn2.cursor()
                    row = c2.execute("SELECT COALESCE(trips,0), banned, ban_reason FROM users WHERE user_id=?", (target_id,)).fetchone()
                    # Проверяем является ли target администратором
                    adm_row = c2.execute(
                        "SELECT 1 FROM admin_permissions WHERE user_id = ? AND is_active = 1",
                        (target_id,)
                    ).fetchone()
                    is_target_admin = bool(adm_row) or (target_id == MAIN_ADMIN_ID)
                    conn2.close()
                    if row:
                        trips_balance = row[0]
                        is_banned = row[1]
                        ban_reason_val = row[2]
                    else:
                        is_banned = 0
                        ban_reason_val = None
                except:
                    is_banned = 0
                    ban_reason_val = None
                    is_target_admin = False

                ban_reason_safe = md_escape(str(ban_reason_val or 'да'))
                ban_line = f"\n║ 🚫 БАН: {ban_reason_safe}" if is_banned else ""
                user_text_patched = user_text.replace(
                    f"║ 📌 Статус подписки: {sub_status_safe}",
                    f"║ 📌 Статус подписки: {sub_status_safe}\n║ 🎫 Трипов: {trips_balance}{ban_line}"
                )

                markup = types.InlineKeyboardMarkup(row_width=1)
                markup.add(
                    types.InlineKeyboardButton("🎫 Управление трипами",    callback_data=f"uh_trips_{target_id}"),
                    types.InlineKeyboardButton("💳 Управление подпиской",  callback_data=f"uh_sub_{target_id}"),
                )
                if is_target_admin:
                    # Администратора нельзя банить — показываем заблокированную кнопку
                    markup.add(
                        types.InlineKeyboardButton(
                            "🛡 Забанить нельзя (это администратор)",
                            callback_data="none"
                        )
                    )
                else:
                    markup.add(
                        types.InlineKeyboardButton(
                            "🚫 Заблокировать" if not is_banned else "✅ Разблокировать",
                            callback_data=f"uh_ban_{target_id}" if not is_banned else f"uh_unban_{target_id}"
                        )
                    )
                markup.add(
                    types.InlineKeyboardButton(
                        "👻 Призрак: вкл" if user_id not in ghost_mode or ghost_mode[user_id] != target_id else "👻 Призрак: выкл",
                        callback_data=f"uh_ghost_{target_id}"
                    ),
                )
                markup.add(
                    types.InlineKeyboardButton("🗑️ Удалить пользователя из БД", callback_data=f"uh_deleteuser_ask_{target_id}"),
                )
                markup.add(
                    types.InlineKeyboardButton("◀️ Назад", callback_data="stats_back"),
                )
                bot.send_message(message.chat.id, user_text_patched[:4096], parse_mode="Markdown", reply_markup=markup)

            except ValueError:
                bot.send_message(message.chat.id, "❌ ID должен быть числом!")
            except Exception as e:
                bot.send_message(message.chat.id, f"❌ Ошибка: {e}")

            del admin_action[user_id]
            return

        elif action == "stats_searchuser_input":
            results_found = search_user_by_query(text)
            if not results_found:
                bot.send_message(message.chat.id, f"🔎 Пользователь не найден по запросу: «{text}»")
            else:
                out = f"🔎 НАЙДЕНО ПОЛЬЗОВАТЕЛЕЙ: {len(results_found)}\n\n"
                for uid2, uname, fname, lname, status, banned, created in results_found[:10]:
                    name = f"@{uname}" if uname else (fname or f"ID{uid2}")
                    ban_mark = " 🚫БАН" if banned else ""
                    out += (
                        f"🆔 {uid2} | {name}{ban_mark}\n"
                        f"   {fname or ''} {lname or ''}\n"
                        f"   Статус: {status} | Регистрация: {created}\n\n"
                    )
                if len(results_found) > 10:
                    out += f"... и ещё {len(results_found)-10}. Уточните запрос."
                # Кнопка для просмотра полной истории первого найденного
                markup = types.InlineKeyboardMarkup()
                if results_found:
                    markup.add(
                        types.InlineKeyboardButton(
                            f"📜 История запросов ID {results_found[0][0]}",
                            callback_data=f"stats_viewuserhistory_{results_found[0][0]}"
                        )
                    )
                markup.add(types.InlineKeyboardButton("◀️ Назад", callback_data="stats_back"))
                bot.send_message(message.chat.id, out, reply_markup=markup)
            del admin_action[user_id]
            return

        return

    # Обработка кнопки поддержки
    if text == "🆘 Поддержка":
        is_banned, ban_reason = check_banned(user_id)
        if is_banned:
            bot.send_message(
                message.chat.id, "🚫 Вы забанены и не можете писать в поддержку!"
            )
            return

        user_support_request[user_id] = True
        bot.send_message(
            message.chat.id,
            "📝 **СЛУЖБА ПОДДЕРЖКИ**\n\n"
            "Напишите ваш вопрос. Администратор ответит в ближайшее время.\n"
            "Отправьте /cancel для отмены.",
            parse_mode="Markdown",
        )

        notify_admins_support(user_id, message.from_user.first_name or "Пользователь")
        return

    subscription_status = get_user_subscription(user_id)

    if text == "ррр":
        update_subscription_status(user_id, "forever", "forever")
        bot.send_message(
            message.chat.id, "✅ Подписка навсегда оформлена!", reply_markup=main_menu()
        )

    elif text == "🔍 Найти инфу":
        if subscription_status in ["active", "forever"]:
            show_api_choice_menu(message.chat.id)
        elif subscription_status == "expired":
            bot.send_message(
                message.chat.id,
                "⏰ Срок подписки истек!\nНажмите '💳 Оформить подписку' для продления.",
                reply_markup=main_menu(),
            )
        else:
            bot.send_message(
                message.chat.id,
                "❌ У вас нет активной подписки!\nНажмите '💳 Оформить подписку' для доступа к поиску.",
                reply_markup=main_menu(),
            )

    elif text == "💳 Оформить подписку":
        # Подписка выбирается инлайн-кнопками, снизу оставляем только "📋 Меню"
        show_bottom_menu(message.chat.id)
        bot.send_message(
            message.chat.id,
            get_bot_text("subscribe_prompt"),
            reply_markup=inline_subscribe_menu(),
        )

    elif text == "📋 Меню" or text == "🔙 Назад":
        # ========== СБРОС ВСЕХ АКТИВНЫХ СОСТОЯНИЙ ==========
        if user_id in waiting_for_search:
            waiting_for_search[user_id] = False
        if user_id in user_support_request:
            del user_support_request[user_id]
        if user_id in admin_create_file:
            del admin_create_file[user_id]
        if user_id in admin_broadcast:
            del admin_broadcast[user_id]
        # ========== АВТОРЕГИСТРАЦИЯ (если пользователь был удалён из базы) ==========
        add_or_update_user(message)
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute(
            "SELECT username, first_name, created_at FROM users WHERE user_id = ?",
            (user_id,),
        )
        user_data = cursor.fetchone()
        if user_data:
            username, first_name, created_at = user_data
            reg_date = (
                datetime.strptime(str(created_at).split()[0], "%Y-%m-%d").strftime("%d.%m.%Y")
                if created_at else "неизвестно"
            )
        else:
            username = "нет"
            first_name = "пользователь"
            reg_date = "неизвестно"
        status = get_user_subscription(user_id)
        if status == "forever":
            status_text = "🔴 ВЕЧНАЯ"
        elif status == "active":
            cursor.execute("SELECT subscription_end FROM users WHERE user_id = ?", (user_id,))
            end_date = cursor.fetchone()
            if end_date and end_date[0]:
                status_text = f"🟢 активна до {datetime.strptime(end_date[0], '%Y-%m-%d').strftime('%d.%m.%Y')}"
            else:
                status_text = "🟢 активна"
        elif status == "expired":
            status_text = "⚫ истекла"
        else:
            status_text = "⚪ нет подписки"
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]
        conn.close()
        trips = get_user_trips(user_id)
        menu_text, menu_photo = get_bot_text_full(
            "start",
            user_id=user_id,
            first_name=first_name or "пользователь",
            username=username or "нет",
            reg_date=reg_date,
            status_text=status_text,
            total_users=total_users,
            trips=trips,
        )
        if menu_photo:
            bot.send_photo(message.chat.id, menu_photo, caption=menu_text, reply_markup=main_menu())
        else:
            bot.send_message(message.chat.id, menu_text, reply_markup=main_menu())
        bot.send_message(message.chat.id, "👇 Выберите действие:", reply_markup=inline_main_menu())

    elif text in ["💎 100 ₽ - 1 день", "100 ₽ - 1 день"]:
        # Проверка подписки уже есть в начале функции
        update_subscription_status(user_id, "active", "1day", 1)
        bot.send_message(
            message.chat.id,
            "✅ Подписка на 1 день активирована!",
            reply_markup=main_menu(),
        )

    elif text in ["💎 500 ₽ - 7 дней", "500 ₽ - 7 дней"]:
        update_subscription_status(user_id, "active", "7days", 7)
        bot.send_message(
            message.chat.id,
            "✅ Подписка на 7 дней активирована!",
            reply_markup=main_menu(),
        )

    elif text in ["💎 1000 ₽ - 30 дней", "1000 ₽ - 30 дней"]:
        update_subscription_status(user_id, "active", "30days", 30)
        bot.send_message(
            message.chat.id,
            "✅ Подписка на 30 дней активирована!",
            reply_markup=main_menu(),
        )

    elif user_id in waiting_for_search and waiting_for_search[user_id]:
        query = text.strip()
        animation_thread = threading.Thread(
            target=send_search_animation, args=(message.chat.id, user_id, query)
        )
        animation_thread.start()


# ========== ОБРАБОТЧИК INLINE КНОПОК ==========
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    user_id = call.from_user.id

    # ========== СБРОС АКТИВНЫХ СОСТОЯНИЙ ПРИ ЛЮБОМ ДЕЙСТВИИ ==========
    # Если пользователь нажал любую кнопку — сбрасываем ожидание ввода,
    # кроме кнопок навигации по результатам поиска и кнопок загрузки файла
    _skip_reset = (
        call.data.startswith("search_")
        or call.data.startswith("upload_")
        or call.data.startswith("qblock_mode_")
        or call.data.startswith("apimgr_")
        or call.data == "none"
    )
    if not _skip_reset:
        # Сбрасываем режим ожидания поиска
        if user_id in waiting_for_search:
            waiting_for_search[user_id] = False
        # Сбрасываем ожидание обращения в поддержку
        if user_id in user_support_request:
            del user_support_request[user_id]
        # Сбрасываем состояние создания файла (если не в процессе загрузки)
        if user_id in admin_create_file and call.data not in ("none",):
            # Сбрасываем только если пользователь ушёл в другой раздел меню
            if call.data.startswith("menu_") or call.data.startswith("sub_"):
                del admin_create_file[user_id]

    # ========== БЛОКИРОВКА ЗАПРОСОВ: КНОПКИ ==========
    if call.data.startswith("qblock_mode_"):
        if user_id not in admin_block_query_state or admin_block_query_state[user_id].get("step") != "mode":
            bot.answer_callback_query(call.id, "❌ Сначала начните с /block_query")
            return
        mode = "contains" if call.data == "qblock_mode_contains" else "exact"
        state = admin_block_query_state[user_id]
        pattern = state.get("pattern", "")
        custom_text = state.get("custom_text", "")
        new_id = add_query_block(pattern, custom_text, user_id, mode)
        del admin_block_query_state[user_id]
        if new_id:
            mode_label = "содержит" if mode == "contains" else "точное совпадение"
            bot.answer_callback_query(call.id, "✅ Блокировка добавлена!")
            bot.edit_message_text(
                f"✅ <b>Блокировка #{new_id} добавлена!</b>\n\n"
                f"📝 <b>Паттерн:</b> <code>{pattern}</code>\n"
                f"🔍 <b>Режим:</b> {mode_label}\n"
                f"💬 <b>Ответ пользователю:</b>\n{custom_text}\n\n"
                f"Используйте /blocked_queries для управления блокировками.",
                call.message.chat.id, call.message.message_id,
                parse_mode="HTML",
            )
        else:
            bot.answer_callback_query(call.id, "❌ Ошибка сохранения")
        return

    if call.data.startswith("del_qblock_"):
        if not has_perm(user_id, "block_query"):
            bot.answer_callback_query(call.id, "⛔ Нет прав!")
            return
        try:
            block_id = int(call.data.replace("del_qblock_", ""))
        except ValueError:
            bot.answer_callback_query(call.id, "❌ Ошибка ID")
            return
        ok = delete_query_block(block_id)
        if ok:
            bot.answer_callback_query(call.id, f"✅ Блокировка #{block_id} удалена!")
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
                bot.edit_message_text(
                    call.message.text + f"\n\n🗑 <i>Удалено</i>",
                    call.message.chat.id, call.message.message_id,
                    parse_mode="HTML",
                )
            except:
                pass
        else:
            bot.answer_callback_query(call.id, "❌ Не найдено или уже удалено")
        return

    # Роутинг загрузки файлов
    if call.data.startswith("upload_"):
        handle_upload_callbacks(call)
        return
    if call.data == "none":
        bot.answer_callback_query(call.id)
        return
    # ========== API МЕНЕДЖЕР: КНОПКИ ==========
    if call.data.startswith("apimgr_"):
        user_id = call.from_user.id
        if not has_perm(user_id, "api_manager") and user_id != MAIN_ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔ Нет прав!")
            return

        if call.data == "apimgr_add":
            bot.answer_callback_query(call.id)
            api_add_start(call)
            return

        if call.data == "apimgr_add_builtin_choose":
            bot.answer_callback_query(call.id)
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(
                types.InlineKeyboardButton("🌍 Добавить токен LeakOSINT", callback_data="apimgr_builtin_addtoken_leakosint"),
                types.InlineKeyboardButton("📊 Добавить токен BigBase", callback_data="apimgr_builtin_addtoken_bigbase"),
                types.InlineKeyboardButton("◀️ Назад", callback_data="apimgr_back"),
            )
            try:
                bot.edit_message_text(
                    "➕ <b>Добавление встроенного API</b>\n\n"
                    "Выберите, к какому API добавить токен:\n\n"
                    "• <b>LeakOSINT</b> — получить: @LeakOSINT_bot → /api\n"
                    "• <b>BigBase</b> — получить на bigbase.top в личном кабинете",
                    call.message.chat.id, call.message.message_id,
                    parse_mode="HTML", reply_markup=markup,
                )
            except Exception:
                bot.send_message(
                    call.message.chat.id,
                    "➕ <b>Добавление встроенного API</b>\n\nВыберите тип:",
                    parse_mode="HTML", reply_markup=markup,
                )
            return

        if call.data == "apimgr_builtin":
            bot.answer_callback_query(call.id)
            api_builtin_menu(call.message.chat.id, call.message.message_id)
            return

        if call.data == "apimgr_ext_menu":
            bot.answer_callback_query(call.id)
            api_ext_menu(call.message.chat.id, call.message.message_id)
            return

        if call.data == "apimgr_builtin_toggle_leakosint":
            global API_ENABLED
            API_ENABLED = not API_ENABLED
            bot.answer_callback_query(call.id, "✅ Включён" if API_ENABLED else "❌ Выключен")
            api_builtin_menu(call.message.chat.id, call.message.message_id)
            return

        if call.data == "apimgr_builtin_toggle_bigbase":
            global BIGBASE_ENABLED
            BIGBASE_ENABLED = not BIGBASE_ENABLED
            bot.answer_callback_query(call.id, "✅ Включён" if BIGBASE_ENABLED else "❌ Выключен")
            api_builtin_menu(call.message.chat.id, call.message.message_id)
            return

        if call.data == "apimgr_builtin_addtoken_leakosint":
            bot.answer_callback_query(call.id)
            _builtin_add_state[user_id] = {"kind": "leakosint", "step": "token", "token": ""}
            bot.send_message(
                call.message.chat.id,
                "🔒 <b>Добавление токена LeakOSINT</b>\n\n"
                "Шаг 1/2 — Введите <b>токен</b>:\n"
                "(получить: @LeakOSINT_bot → /api)\n\n"
                "/cancel — отмена",
                parse_mode="HTML",
            )
            return

        if call.data == "apimgr_builtin_addtoken_bigbase":
            bot.answer_callback_query(call.id)
            _builtin_add_state[user_id] = {"kind": "bigbase", "step": "token", "token": ""}
            bot.send_message(
                call.message.chat.id,
                "🔒 <b>Добавление токена BigBase</b>\n\n"
                "Шаг 1/2 — Введите <b>токен</b>:\n"
                "(получить в личном кабинете bigbase.top)\n\n"
                "/cancel — отмена",
                parse_mode="HTML",
            )
            return

        if call.data == "apimgr_builtin_tokens_leakosint":
            bot.answer_callback_query(call.id)
            api_builtin_tokens_list_screen(
                call.message.chat.id, call.message.message_id, "leakosint"
            )
            return

        if call.data == "apimgr_builtin_tokens_bigbase":
            bot.answer_callback_query(call.id)
            api_builtin_tokens_list_screen(
                call.message.chat.id, call.message.message_id, "bigbase"
            )
            return

        if call.data == "apimgr_builtin_docshow_leakosint":
            bot.answer_callback_query(call.id)
            send_builtin_api_doc_to_chat(call.message.chat.id, "leakosint")
            return

        if call.data == "apimgr_builtin_docshow_bigbase":
            bot.answer_callback_query(call.id)
            send_builtin_api_doc_to_chat(call.message.chat.id, "bigbase")
            return

        if call.data == "apimgr_builtin_editdoc_leakosint":
            bot.answer_callback_query(call.id)
            if user_id in api_doc_edit_wait:
                del api_doc_edit_wait[user_id]
            builtin_doc_edit_wait[user_id] = "leakosint"
            bot.send_message(
                call.message.chat.id,
                "✏️ <b>Документация: LeakOSINT</b> (встроенный API)\n\n"
                "Отправьте <b>текст</b> одним сообщением или <b>файл-документ</b> "
                "(для .txt и .json текст также сохранится в поле описания).\n\n"
                "Отправьте <code>-</code>, чтобы удалить документацию.\n"
                "/cancel — отмена",
                parse_mode="HTML",
            )
            return

        if call.data == "apimgr_builtin_editdoc_bigbase":
            bot.answer_callback_query(call.id)
            if user_id in api_doc_edit_wait:
                del api_doc_edit_wait[user_id]
            builtin_doc_edit_wait[user_id] = "bigbase"
            bot.send_message(
                call.message.chat.id,
                "✏️ <b>Документация: BigBase</b> (встроенный API)\n\n"
                "Отправьте <b>текст</b> одним сообщением или <b>файл-документ</b> "
                "(для .txt и .json текст также сохранится в поле описания).\n\n"
                "Отправьте <code>-</code>, чтобы удалить документацию.\n"
                "/cancel — отмена",
                parse_mode="HTML",
            )
            return

        if call.data.startswith("apimgr_tokdel_"):
            rest = call.data[len("apimgr_tokdel_"):]
            if rest.startswith("leakosint_"):
                kind = "leakosint"
                tid_s = rest[len("leakosint_"):]
            elif rest.startswith("bigbase_"):
                kind = "bigbase"
                tid_s = rest[len("bigbase_"):]
            else:
                bot.answer_callback_query(call.id, "❌ Ошибка")
                return
            try:
                tid = int(tid_s)
            except ValueError:
                bot.answer_callback_query(call.id, "❌ Ошибка ID")
                return
            ok = delete_builtin_token(tid, kind)
            bot.answer_callback_query(call.id, "🗑 Удалено" if ok else "⚠️ Не найдено")
            api_builtin_tokens_list_screen(
                call.message.chat.id, call.message.message_id, kind
            )
            return

        if call.data == "apimgr_docs_list":
            bot.answer_callback_query(call.id)
            api_documentation_list_screen(
                call.message.chat.id, call.message.message_id
            )
            return

        if call.data == "apimgr_doclist_back":
            bot.answer_callback_query(call.id)
            api_manager_menu(call.message.chat.id, call.message.message_id)
            return

        if call.data == "apimgr_list":
            bot.answer_callback_query(call.id)
            api_list_show(call.message.chat.id, call.message.message_id)
            return

        if call.data == "apimgr_back":
            bot.answer_callback_query(call.id)
            api_manager_menu(call.message.chat.id, call.message.message_id)
            return

        if call.data == "apimgr_builtin_skipdoc":
            bot.answer_callback_query(call.id)
            if user_id in _builtin_add_state:
                st = _builtin_add_state.pop(user_id)
                kind = st["kind"]
                title = "LeakOSINT" if kind == "leakosint" else "BigBase"
                ok, err = add_builtin_token(kind, st["token"])
                if ok:
                    bot.send_message(
                        call.message.chat.id,
                        f"✅ Токен <b>{title}</b> добавлен без документации.",
                        parse_mode="HTML",
                    )
                else:
                    bot.send_message(call.message.chat.id, f"❌ {err}")
                api_builtin_menu(call.message.chat.id)
            return

        if call.data == "apimgr_skipdoc":
            bot.answer_callback_query(call.id)
            if user_id in api_add_state:
                api_add_state[user_id]["data"]["doc_text"] = ""
                api_add_state[user_id]["data"]["doc_file_id"] = ""
                api_add_state[user_id]["data"]["doc_filename"] = ""
                _api_save(call.message.chat.id, user_id)
            return

        if call.data.startswith("apimgr_del_"):
            bot.answer_callback_query(call.id)
            try:
                api_id = int(call.data.split("_")[2])
            except:
                return
            conn = sqlite3.connect("users.db")
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM external_apis WHERE id = ?", (api_id,))
            row = cursor.fetchone()
            conn.close()
            if not row:
                bot.send_message(call.message.chat.id, "❌ API не найден.")
                return
            name = row[0]
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(
                types.InlineKeyboardButton("✅ Да, удалить", callback_data=f"apimgr_delconfirm_{api_id}"),
                types.InlineKeyboardButton("❌ Отмена", callback_data="apimgr_list"),
            )
            try:
                bot.edit_message_text(
                    f"🗑 Удалить API <b>{name}</b>?\n\nДокументация тоже будет удалена.",
                    call.message.chat.id, call.message.message_id,
                    parse_mode="HTML", reply_markup=markup,
                )
            except:
                bot.send_message(
                    call.message.chat.id,
                    f"🗑 Удалить API <b>{name}</b>?",
                    parse_mode="HTML", reply_markup=markup,
                )
            return

        if call.data.startswith("apimgr_toggle_"):
            try:
                api_id = int(call.data.split("_")[2])
            except:
                bot.answer_callback_query(call.id, "❌ Ошибка ID")
                return
            new_state = toggle_external_api(api_id)
            bot.answer_callback_query(call.id, "✅ Включён" if new_state else "❌ Отключён")
            api_list_show(call.message.chat.id, call.message.message_id)
            return

        if call.data.startswith("apimgr_editdoc_"):
            try:
                api_id = int(call.data.replace("apimgr_editdoc_", ""))
            except ValueError:
                bot.answer_callback_query(call.id, "❌ Ошибка ID")
                return
            conn = sqlite3.connect("users.db")
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM external_apis WHERE id = ?", (api_id,))
            row = cursor.fetchone()
            conn.close()
            if not row:
                bot.answer_callback_query(call.id, "❌ API не найден")
                return
            bot.answer_callback_query(call.id)
            name = row[0]
            if user_id in builtin_doc_edit_wait:
                del builtin_doc_edit_wait[user_id]
            api_doc_edit_wait[user_id] = api_id
            ne = (
                str(name)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            bot.send_message(
                call.message.chat.id,
                f"✏️ <b>Документация: «{ne}»</b>\n\n"
                "Отправьте <b>текст</b> одним сообщением или <b>файл-документ</b> "
                "(для .txt и .json текст также сохранится в поле описания).\n\n"
                "Отправьте <code>-</code>, чтобы удалить всю документацию у этого API.\n"
                "/cancel — отмена",
                parse_mode="HTML",
            )
            return

        if call.data.startswith("apimgr_showdoc_"):
            try:
                api_id = int(call.data.split("_")[2])
            except:
                bot.answer_callback_query(call.id)
                return
            bot.answer_callback_query(call.id)
            conn = sqlite3.connect("users.db")
            cursor = conn.cursor()
            cursor.execute("SELECT name, doc_text, doc_file_id, doc_filename FROM external_apis WHERE id = ?", (api_id,))
            row = cursor.fetchone()
            conn.close()
            if not row:
                bot.send_message(call.message.chat.id, "❌ API не найден.")
                return
            name, doc_text, doc_file_id, doc_filename = row
            def _esc(s): return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
            if doc_text:
                bot.send_message(call.message.chat.id, f"📖 <b>{_esc(name)}</b>\n\n{_esc(doc_text[:4000])}", parse_mode="HTML")
            elif doc_file_id:
                bot.send_document(call.message.chat.id, doc_file_id, caption=f"📖 Документация {name}")
            else:
                bot.send_message(call.message.chat.id, f"📖 {name}: документация не добавлена.")
            return

        if call.data.startswith("apimgr_delconfirm_"):
            bot.answer_callback_query(call.id)
            try:
                api_id = int(call.data.split("_")[2])
            except:
                return
            conn = sqlite3.connect("users.db")
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM external_apis WHERE id = ?", (api_id,))
            row = cursor.fetchone()
            if row:
                name = row[0]
                cursor.execute("DELETE FROM external_apis WHERE id = ?", (api_id,))
                conn.commit()
                conn.close()
                try:
                    bot.edit_message_text(
                        f"✅ API <b>{name}</b> удалён.",
                        call.message.chat.id, call.message.message_id, parse_mode="HTML",
                    )
                except:
                    bot.send_message(call.message.chat.id, f"✅ API <b>{name}</b> удалён.", parse_mode="HTML")
                import time as _time; _time.sleep(0.5)
                api_list_show(call.message.chat.id)
            else:
                conn.close()
                bot.send_message(call.message.chat.id, "❌ API не найден.")
            return

        return  # unknown apimgr_ callback

    # ========== ВЫБОР API ДЛЯ ПОИСКА ==========
    if call.data.startswith("search_api_"):
        bot.answer_callback_query(call.id)
        choice = call.data.replace("search_api_", "")  # leakosint | bigbase | all | ext_N | ext_all
        user_api_choice[user_id] = choice
        waiting_for_search[user_id] = True

        # Определяем метку выбранного API
        static_labels = {
            "leakosint": "🌍 LeakOSINT",
            "bigbase":   "📊 BigBase",
            "all":       "🔍 все встроенные API",
            "ext_all":   "🔀 все API (включая внешние)",
        }
        if choice in static_labels:
            label = static_labels[choice]
        elif choice.startswith("ext_"):
            try:
                ext_id = int(choice.replace("ext_", ""))
                conn_lbl = sqlite3.connect("users.db")
                cur_lbl = conn_lbl.cursor()
                cur_lbl.execute("SELECT name, emoji FROM external_apis WHERE id = ?", (ext_id,))
                row_lbl = cur_lbl.fetchone()
                conn_lbl.close()
                label = f"{row_lbl[1] or '🌐'} {row_lbl[0]}" if row_lbl else f"API #{ext_id}"
            except Exception:
                label = choice
        else:
            label = choice

        prompt_text, prompt_photo = get_bot_text_full("search_prompt")
        full_text = f"✅ Выбрано: {label}\n\n{prompt_text}"
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        if prompt_photo:
            bot.send_photo(call.message.chat.id, prompt_photo, caption=full_text, parse_mode=None)
        else:
            bot.send_message(call.message.chat.id, full_text, parse_mode=None)
        return

    # ========== МЕНЮ ДОКУМЕНТАЦИИ API ==========
    if call.data == "api_docs_menu":
        bot.answer_callback_query(call.id)
        ext_apis = get_active_external_apis()
        ext_with_doc = [
            e for e in ext_apis if e.get("doc_text") or e.get("doc_file_id")
        ]
        builtin_items = []
        for bk, em, title in (
            ("leakosint", "🌍", "LeakOSINT"),
            ("bigbase", "📊", "BigBase"),
        ):
            if builtin_api_doc_nonempty(bk):
                builtin_items.append((bk, em, title))
        if not ext_with_doc and not builtin_items:
            bot.send_message(
                call.message.chat.id,
                "📖 Документация по API пока не добавлена.",
            )
            return
        markup = types.InlineKeyboardMarkup(row_width=1)
        for bk, em, title in builtin_items:
            markup.add(
                types.InlineKeyboardButton(
                    f"{em} {title} (встроенный)",
                    callback_data=f"api_showbd_{bk}",
                )
            )
        for e in ext_with_doc:
            markup.add(
                types.InlineKeyboardButton(
                    f"{e.get('emoji', '🌐')} {e['name']}",
                    callback_data=f"api_showdoc_{e['id']}",
                )
            )
        bot.send_message(
            call.message.chat.id,
            "📚 Выберите API для просмотра документации:",
            reply_markup=markup,
        )
        return

    if call.data.startswith("api_showbd_"):
        bot.answer_callback_query(call.id)
        kind = call.data.replace("api_showbd_", "")
        if kind not in ("leakosint", "bigbase"):
            return
        send_builtin_api_doc_to_chat(call.message.chat.id, kind)
        return

    if call.data.startswith("api_showdoc_"):
        bot.answer_callback_query(call.id)
        try:
            api_id = int(call.data.replace("api_showdoc_", ""))
        except Exception:
            return
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name, doc_text, doc_file_id, doc_filename FROM external_apis WHERE id = ?",
            (api_id,),
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            bot.send_message(call.message.chat.id, "❌ API не найден.")
            return
        name, doc_text, doc_file_id, doc_filename = row

        def _esc(s):
            return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        if doc_text:
            bot.send_message(
                call.message.chat.id,
                f"📖 <b>{_esc(name)}</b>\n\n{_esc(doc_text[:4000])}",
                parse_mode="HTML",
            )
        elif doc_file_id:
            cap = f"📖 Документация: {name}"
            if doc_filename:
                cap += f" — {doc_filename}"
            bot.send_document(call.message.chat.id, doc_file_id, caption=cap[:1024])
        else:
            bot.send_message(
                call.message.chat.id,
                "📖 Документация для этого API не добавлена.",
            )
        return


    if call.data == "check_sub":
        # Сбрасываем кэш - иначе бот не увидит свежую подписку
        if user_id in subscription_check_cache:
            del subscription_check_cache[user_id]
        if check_subscription(user_id):
            bot.answer_callback_query(call.id, "✅ Подписка подтверждена!")
            bot.delete_message(call.message.chat.id, call.message.message_id)

            add_or_update_user(call.message)

            conn = sqlite3.connect("users.db")
            cursor = conn.cursor()
            cursor.execute(
                "SELECT username, first_name, created_at FROM users WHERE user_id = ?",
                (user_id,),
            )
            user_data = cursor.fetchone()

            if user_data:
                username, first_name, created_at = user_data
                reg_date = (
                    datetime.strptime(str(created_at).split()[0], "%Y-%m-%d").strftime(
                        "%d.%m.%Y"
                    )
                    if created_at
                    else "неизвестно"
                )
            else:
                username = "нет"
                first_name = "пользователь"
                reg_date = "неизвестно"

            status = get_user_subscription(user_id)
            if status == "forever":
                status_text = "🔴 ВЕЧНАЯ"
            elif status == "active":
                cursor.execute(
                    "SELECT subscription_end FROM users WHERE user_id = ?", (user_id,)
                )
                end_date = cursor.fetchone()
                if end_date and end_date[0]:
                    end_date_str = datetime.strptime(end_date[0], "%Y-%m-%d").strftime(
                        "%d.%m.%Y"
                    )
                    status_text = f"🟢 активна до {end_date_str}"
                else:
                    status_text = "🟢 активна"
            elif status == "expired":
                status_text = "⚫ истекла"
            else:
                status_text = "⚪ нет подписки"

            cursor.execute("SELECT COUNT(*) FROM users")
            total_users = cursor.fetchone()[0]
            conn.close()

            trips = get_user_trips(user_id)
            menu_text, menu_photo = get_bot_text_full(
                "start",
                user_id=user_id,
                first_name=first_name or "пользователь",
                username=username or "нет",
                reg_date=reg_date,
                status_text=status_text,
                total_users=total_users,
                trips=trips,
            )

            show_main_menu(call.message.chat.id, menu_text, user_id=user_id, photo_id=menu_photo)
        else:
            bot.answer_callback_query(
                call.id, "❌ Ты ещё не подписался!", show_alert=True
            )
        return

    if call.data == "menu_search":
        bot.answer_callback_query(call.id)
        if not check_subscription(user_id) and user_id != MAIN_ADMIN_ID:
            bot.send_message(
                call.message.chat.id,
                "🚫 Для поиска нужно подписаться на канал!",
                reply_markup=main_menu(),
            )
            return
        # Предупреждение о трипах (показываем баланс)
        _is_admin_ms = user_id == MAIN_ADMIN_ID or check_admin_permission(user_id)[0]
        if not _is_admin_ms:
            _has_sub_ms = get_user_subscription(user_id) in ("active", "forever")
            if not _has_sub_ms:
                _trips_ms = get_user_trips(user_id)
                if _trips_ms == 0:
                    _edit_to_content(
                        call,
                        "💸 На вашем балансе недостаточно средств.\n\n"
                        "💰 Ваш баланс: 0 трипов\n\n"
                        "Активируйте код через /tripcode XXXXX или оформите подписку.",
                        parse_mode=None
                    )
                    return
        show_api_choice_menu(call.message.chat.id, user_id)
        return

    if call.data == "menu_subscribe":
        bot.answer_callback_query(call.id)
        sub_markup = types.InlineKeyboardMarkup(row_width=1)
        sub_markup.add(
            types.InlineKeyboardButton("💎 100 ₽ - 1 день",   callback_data="sub_1day"),
            types.InlineKeyboardButton("💎 500 ₽ - 7 дней",   callback_data="sub_7days"),
            types.InlineKeyboardButton("💎 1000 ₽ - 30 дней", callback_data="sub_30days"),
        )
        _trips_bal = get_user_trips(user_id)
        _sub_text = get_bot_text("subscribe_prompt") + f"\n\n🎫 Ваш баланс трипов: {_trips_bal}"
        _edit_to_content(call, _sub_text, extra_markup=sub_markup, parse_mode=None)
        return

    if call.data == "menu_history":
        bot.answer_callback_query(call.id)
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        history = get_user_history_with_first(cursor, user_id)
        conn.close()

        if not history:
            _edit_to_content(call, "📭 У вас пока нет истории поиска", parse_mode=None)
            return

        text = get_bot_text("myhistory")
        for i, (_, query, found, search_time) in enumerate(history, 1):
            status = "✅ Найдено" if found else "❌ Не найдено"
            text += f"{i}. 🔍 {query}\n   {status} - {search_time}\n\n"

        _edit_to_content(call, text[:4000], parse_mode=None)
        return

    if call.data == "menu_help":
        bot.answer_callback_query(call.id)
        help_text = get_bot_text("help")
        if user_id == MAIN_ADMIN_ID or check_admin_permission(user_id)[0]:
            admin_panel_line = "/admin_panel - управление админами 👑\n" if has_perm(user_id, "admin_panel") else ""
            stats_line       = "/stats - общая статистика 📊\n"          if has_perm(user_id, "stats") else ""
            mod_line         = "/moderation - модерация 🛡️\n"            if has_perm(user_id, "users") else ""
            texts_line       = "/texts - управление текстами 📝\n"       if has_perm(user_id, "texts") else ""
            files_line       = "/files - работа с файлами 📁\n"          if has_perm(user_id, "files") else ""
            secret_line      = "/secret_list - засекреченные записи 🔐\n" if has_perm(user_id, "secret") else ""
            block_line       = "/block_query - заблокировать запрос 🚫\n/blocked_queries - список блокировок\n" if has_perm(user_id, "block_query") else ""
            broadcast_line   = "/broadcast - рассылка 📢\n"              if has_perm(user_id, "broadcast") else ""
            support_line     = "/supportlist - обращения в поддержку 💬\n" if has_perm(user_id, "support") else ""
            createtrip_line  = "/createtrip N - создать код на N трипов 🎫\n"
            admin_help_template = get_bot_text("admin_help")
            help_text += "\n\n" + admin_help_template.format(
                stats_line=stats_line,
                admin_panel_line=admin_panel_line,
                mod_line=mod_line,
                texts_line=texts_line,
                files_line=files_line,
                secret_line=secret_line,
                block_line=block_line,
                broadcast_line=broadcast_line,
                support_line=support_line,
                createtrip_line=createtrip_line,
            )
        _edit_to_content(call, help_text, parse_mode=None)
        return
    if call.data.startswith("search_"):
        parts = call.data.split("_")
        action = parts[1]
        
        if len(parts) >= 3:
            target_uid = int(parts[2])
        else:
            target_uid = user_id
        
        if target_uid not in user_search_results:
            bot.answer_callback_query(call.id, "❌ Результаты поиска устарели")
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except:
                pass
            return
        
        search_data = user_search_results[target_uid]
        current = search_data["current_page"]
        total = search_data["total_pages"]
        # Всегда редактируем то сообщение, на кнопку которого нажали
        message_id = call.message.message_id
        search_data["message_id"] = message_id
        
        if action == "next":
            new_page = current + 1
        elif action == "prev":
            new_page = current - 1
        elif action == "first":
            new_page = 0
        elif action == "last":
            new_page = total - 1
        elif action == "close":
            del user_search_results[target_uid]
            bot.answer_callback_query(call.id, "✅ Просмотр завершен")
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except:
                pass
            bot.send_message(call.message.chat.id, "👇 Выберите действие:", reply_markup=main_menu())
            return
        else:
            return
        
        # Передаём message_id — редактируем то же сообщение, не отправляем новое
        show_search_result_page(call.message.chat.id, target_uid, new_page, message_id)
        bot.answer_callback_query(call.id)
        return

    if call.data.startswith("download_report_"):
        bot.answer_callback_query(call.id, "⏳ Формирую отчёт...")
        try:
            target_uid = int(call.data.split("_")[2])
            if target_uid not in user_search_results:
                bot.send_message(call.message.chat.id, "❌ Результаты поиска устарели.")
                return

            search_data = user_search_results[target_uid]
            results = search_data["results"]
            query = search_data.get("query", "—")
            now_str = datetime.now().strftime("%d.%m.%Y %H:%M")

            def _esc(s):
                return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"','&quot;')

            # Группируем результаты по базам
            from collections import OrderedDict
            groups = OrderedDict()
            for result in results:
                text_info = result[0] if result[0] else ""
                source = str(result[2]) if len(result) > 2 and result[2] else "Без источника"
                db_t   = result[3] if len(result) > 3 and result[3] else "default"
                if source not in groups:
                    groups[source] = {"db_type": db_t, "records": []}
                groups[source]["records"].append(text_info)

            db_type_names = {
                "avto": "Автомобили", "imush": "Имущество",
                "work": "Работа", "people": "Люди", "default": "База данных",
            }
            db_type_icons = {
                "avto": "🚗", "imush": "🏠", "work": "💼",
                "people": "👤", "default": "📋",
            }
            db_colors = {
                "avto": "#34d399", "imush": "#fb923c",
                "work": "#a78bfa", "people": "#60a5fa", "default": "#94a3b8",
            }

            FIELD_ICONS_MAP = {
                "телефон": "📞", "phone": "📞", "моб": "📞",
                "фио": "🪪", "имя": "🪪", "фамилия": "🪪", "fullname": "🪪",
                "email": "✉️", "почта": "✉️", "mail": "✉️",
                "адрес": "🏠", "address": "🏠", "прописка": "🏠", "регистрация": "🏠",
                "дата рожд": "🗓️", "bday": "🗓️", "birthday": "🗓️", "д.р.": "🗓️",
                "инн": "🔢", "inn": "🔢",
                "снилс": "🟢", "snils": "🟢",
                "паспорт": "📕", "passport": "📕",
                "госномер": "🔲", "номер авто": "🔲",
                "марка": "🛻", "модель": "🛻", "авто": "🛻",
                "логин": "👤", "login": "👤", "username": "👤",
                "пароль": "🔒", "password": "🔒", "pass": "🔒",
                "ip": "📡",
                "карта": "🏦", "card": "🏦",
                "компания": "💼", "company": "💼", "работодатель": "💼",
                "город": "🏙️", "city": "🏙️",
            }

            def get_field_icon(fname):
                fl = fname.lower()
                for kw, icon in FIELD_ICONS_MAP.items():
                    if kw in fl:
                        return icon
                return "▪️"

            blocks_html = ""
            for src, gdata in groups.items():
                db_t = gdata["db_type"]
                color = db_colors.get(db_t, "#94a3b8")
                type_label = db_type_names.get(db_t, "База данных")
                type_icon  = db_type_icons.get(db_t, "📋")
                raw_src = str(src)
                # Для API-источников (api:hash) убираем hash — показываем просто "API"
                if raw_src.startswith("api:"):
                    src_name = "API"
                else:
                    src_name = os.path.splitext(os.path.basename(raw_src))[0]

                records_html = ""
                valid_count = 0
                # Собираем название и описание базы из первой записи
                block_db_name = ""   # из строки ═══ Название ═══
                block_desc = ""      # описательный текст (инфо об утечке и т.п.)

                for i, rec_text in enumerate(gdata["records"], 1):
                    lines = rec_text.splitlines()
                    cells_html = ""
                    rec_db_name = ""
                    for ln in lines:
                        s = ln.strip()
                        if not s:
                            continue
                        if "═══" in s:
                            name_candidate = s.strip("═ 🌐").strip()
                            if name_candidate:
                                rec_db_name = name_candidate
                                if not block_db_name:
                                    block_db_name = name_candidate
                            continue
                        # Описание базы (длинный текст без двоеточия в начале)
                        if len(s) > 60 and ": " not in s[:40] and not block_desc:
                            block_desc = s
                            continue
                        if ": " in s:
                            fname, fval = s.split(": ", 1)
                            fname_clean = fname.strip().lstrip("📱👤📍✉️🔢🏢🏦📕🟢📡🔒👤🌐▪️🎂🚗🔲🛻🖌️💼🫂⚠️📋📅 ")
                            fval = fval.strip()
                            if not fval or fval in ("—", "-", ""):
                                continue
                            icon = get_field_icon(fname_clean)
                            cells_html += f'''<div class="cell">
                                <div class="cell-icon">{icon}</div>
                                <div class="cell-body">
                                    <div class="cell-label">{_esc(fname_clean)}</div>
                                    <div class="cell-value">{_esc(fval)}</div>
                                </div>
                            </div>'''
                        elif s and "═══" not in s and "───" not in s and len(s) <= 60:
                            cells_html += f'<div class="cell-plain">{_esc(s)}</div>'

                    if not cells_html:
                        continue
                    valid_count += 1
                    records_html += f'''<div class="record">
                        <div class="rec-header">
                            <span class="rec-num">{valid_count}</span>
                        </div>
                        <div class="cells-grid">{cells_html}</div>
                    </div>'''

                if not records_html:
                    continue

                # Заголовок блока: название базы из данных или имя файла
                display_name = block_db_name or src_name
                desc_html = f'<div class="db-desc">{_esc(block_desc)}</div>' if block_desc else ""

                blocks_html += f'''<div class="db-block" style="--bc:{color}">
                    <div class="db-header">
                        <div class="db-left">
                            <div class="db-title">{_esc(display_name)}</div>
                            {desc_html}
                        </div>
                        <div class="db-count-pill">{valid_count} зап.</div>
                    </div>
                    <div class="db-body">{records_html}</div>
                </div>'''

            total_records = sum(len(g["records"]) for g in groups.values())

            html = f'''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Отчёт: {_esc(query)}</title>
<style>
/* Times New Roman — системный шрифт */
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#080a0e;--s1:#0f1117;--s2:#161a23;--s3:#1c2130;
  --border:#1e2535;--text:#dde4f0;--muted:#4a5568;--dim:#2d3748;
}}
body{{
  background:var(--bg);color:var(--text);
  font-family:'JetBrains Mono',monospace;
  min-height:100vh;padding:2rem 1rem;
  background-image:
    radial-gradient(ellipse 70% 45% at 50% -8%,rgba(59,130,246,.13) 0%,transparent 65%),
    repeating-linear-gradient(0deg,transparent,transparent 39px,rgba(255,255,255,.014) 39px,rgba(255,255,255,.014) 40px),
    repeating-linear-gradient(90deg,transparent,transparent 39px,rgba(255,255,255,.014) 39px,rgba(255,255,255,.014) 40px);
}}
.wrap{{max-width:960px;margin:0 auto}}

/* HEADER */
.header{{
  background:var(--s1);border:1px solid var(--border);border-radius:18px;
  padding:2rem 2.4rem;margin-bottom:2rem;position:relative;overflow:hidden;
}}
.header::before{{
  content:'';position:absolute;inset:0;
  background:linear-gradient(135deg,rgba(59,130,246,.1) 0%,transparent 55%);
  pointer-events:none;
}}
.h-eyebrow{{
  font-family:'Unbounded',sans-serif;font-size:.58rem;font-weight:700;
  letter-spacing:.22em;text-transform:uppercase;color:#3b82f6;margin-bottom:.5rem;
}}
.h-query{{
  font-family:'Unbounded',sans-serif;font-size:1.7rem;font-weight:900;
  color:#fff;word-break:break-all;line-height:1.2;margin-bottom:1rem;
}}
.h-stats{{display:flex;flex-wrap:wrap;gap:.6rem}}
.stat-pill{{
  display:flex;align-items:center;gap:.45rem;
  background:var(--s2);border:1px solid var(--border);
  border-radius:8px;padding:.32rem .8rem;font-size:.72rem;color:var(--muted);
}}
.stat-pill strong{{color:var(--text)}}

/* DB BLOCK */
.db-block{{
  background:var(--s1);border:1px solid var(--border);
  border-radius:16px;margin-bottom:1.4rem;overflow:hidden;
  animation:fadeUp .4s ease both;
}}
@keyframes fadeUp{{from{{opacity:0;transform:translateY(14px)}}to{{opacity:1;transform:translateY(0)}}}}
.db-block:nth-child(1){{animation-delay:.05s}}.db-block:nth-child(2){{animation-delay:.1s}}
.db-block:nth-child(3){{animation-delay:.15s}}.db-block:nth-child(4){{animation-delay:.2s}}
.db-block:nth-child(n+5){{animation-delay:.25s}}
.db-block:hover{{box-shadow:0 6px 28px rgba(0,0,0,.35),0 0 0 1px var(--bc,#3b82f6)}}
transition:box-shadow .2s;

.db-header{{
  display:flex;align-items:center;justify-content:space-between;
  padding:1rem 1.4rem;border-bottom:1px solid var(--border);
  background:linear-gradient(90deg,color-mix(in srgb,var(--bc,#3b82f6) 14%,transparent) 0%,transparent 60%);
}}
.db-left{{display:flex;flex-direction:column;gap:.3rem}}
.db-title{{
  font-family:'Unbounded',sans-serif;
  font-size:.95rem;font-weight:900;
  color:var(--bc,#3b82f6);
  text-shadow:0 0 18px color-mix(in srgb,var(--bc,#3b82f6) 40%,transparent);
  letter-spacing:.02em;
}}
.db-desc{{
  font-size:.7rem;line-height:1.5;
  color:#a0aec0;
  max-width:680px;
}}
.db-count-pill{{
  background:color-mix(in srgb,var(--bc,#3b82f6) 18%,transparent);
  color:var(--bc,#3b82f6);border-radius:20px;
  padding:.22rem .85rem;font-size:.68rem;font-weight:700;white-space:nowrap;
}}

/* RECORDS */
.db-body{{padding:1rem 1.2rem;display:flex;flex-direction:column;gap:1rem}}
.record{{
  background:var(--s2);border-radius:12px;overflow:hidden;
  border:1px solid var(--border);
}}
.rec-header{{
  display:flex;align-items:center;gap:.6rem;
  padding:.55rem .9rem;background:var(--s3);
  border-bottom:1px solid var(--border);
}}
.rec-num{{
  width:22px;height:22px;border-radius:50%;font-size:.65rem;font-weight:700;
  display:flex;align-items:center;justify-content:center;
  background:color-mix(in srgb,var(--bc,#3b82f6) 22%,transparent);
  color:var(--bc,#3b82f6);flex-shrink:0;
}}
.rec-title{{font-size:.7rem;color:var(--dim);font-weight:600}}

/* CELLS GRID */
.cells-grid{{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(200px,1fr));
  gap:1px;background:var(--border);
}}
.cell{{
  display:flex;align-items:flex-start;gap:.55rem;
  padding:.65rem .85rem;background:var(--s2);
  transition:background .15s;
}}
.cell:hover{{background:var(--s3)}}
.cell-icon{{font-size:.95rem;flex-shrink:0;margin-top:.05rem}}
.cell-body{{display:flex;flex-direction:column;gap:.15rem;min-width:0}}
.cell-label{{font-size:.62rem;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}}
.cell-value{{font-size:.78rem;color:var(--text);word-break:break-all;line-height:1.4}}
.cell-plain{{
  padding:.5rem .85rem;background:var(--s2);
  font-size:.73rem;color:var(--dim);
  border-top:1px solid var(--border);
  grid-column:1/-1;
}}

.footer{{
  text-align:center;font-size:.65rem;color:var(--muted);
  margin-top:2.5rem;padding-top:1rem;border-top:1px solid var(--border);
}}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="h-eyebrow">📥 Полный отчёт · Детализация по базам</div>
    <div class="h-query">{_esc(query)}</div>
    <div class="h-stats">
      <div class="stat-pill">🕐 <strong>{now_str}</strong></div>
      <div class="stat-pill">🗄️ Баз: <strong>{len(groups)}</strong></div>
      <div class="stat-pill">📝 Записей: <strong>{total_records}</strong></div>
    </div>
  </div>
  {blocks_html}
  <div class="footer">Сгенерировано автоматически · {now_str}</div>
</div>
</body>
</html>'''

            safe_query = re.sub(r"[^\w\-]", "_", query)[:30]
            fname = f"report_{safe_query}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
            report_bytes = io.BytesIO(html.encode("utf-8"))
            report_bytes.name = fname
            bot.send_document(
                call.message.chat.id,
                report_bytes,
                caption=f"📥 Отчёт по запросу: <b>{_esc(query)}</b>\n📊 Записей: {total_records} из {len(groups)} баз\n\n🌐 Откройте файл в браузере",
                parse_mode="HTML",
            )
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"[download_report] Ошибка: {e}")
            bot.send_message(call.message.chat.id, "❌ Не удалось сформировать отчёт.")
        return

    if call.data.startswith("smart_summary_"):
        bot.answer_callback_query(call.id, "⏳ Собираю сводку...")
        try:
            target_uid = int(call.data.split("_")[2])
            if target_uid not in user_search_results:
                bot.send_message(call.message.chat.id, "❌ Результаты поиска устарели.")
                return

            search_data = user_search_results[target_uid]
            results = search_data["results"]
            query = search_data.get("query", "—")

            # Собираем все значения по типам
            collected = {
                "phone":      set(),
                "fio":        set(),
                "email":      set(),
                "address":    set(),
                "birthday":   set(),
                "car_num":    set(),
                "car_model":  set(),
                "car_color":  set(),
                "inn":        set(),
                "snils":      set(),
                "passport":   set(),
                "city":       set(),
                "login":      set(),
                "password":   set(),
                "ip":         set(),
                "card":       set(),
                "company":    set(),
                "relatives":  set(),
                "incidents":  set(),   # ДТП / BigBase инциденты
                "violations": set(),   # ГИБДД / штрафы / ФССП / судимости
                "other":      set(),
            }

            # Буфер для склейки серии+номера паспорта из отдельных полей
            _passport_buf = {}   # {result_idx: {"series": ..., "number": ...}}

            # Хранилище пар ФИО → ДР (из одного блока результата)
            fio_with_bday = {}  # {фио_строка: дата_рождения_строка}

            # Маппинг: поле → значение → множество источников (баз)
            # Используется для tooltip в HTML-сводке
            value_sources = {k: {} for k in collected}  # {field_type: {value: {src_name, ...}}}

            def _register_value_source(field_type, value, src_name):
                """Регистрирует связь значение → база для tooltip в сводке."""
                if field_type not in value_sources:
                    return
                if value not in value_sources[field_type]:
                    value_sources[field_type][value] = set()
                value_sources[field_type][value].add(str(src_name))

            for _res_idx, result in enumerate(results):
                text_info = result[0] if result[0] else ""
                _res_db_source = str(result[4]) if len(result) > 4 and result[4] else "Без источника"
                lines = text_info.splitlines()
                block_fios = []   # список ФИО из этого блока
                block_bdays = []  # список ДР из этого блока
                for _line in lines:
                    _s = _line.strip()
                    _sl = _s.lower()
                    if not _s or "═══" in _s or "───" in _s:
                        continue
                    # ФИО
                    if any(kw in _sl for kw in ["fio", "фио", "👤 фио", "имя", "name", "фамилия"]):
                        if ": " in _s:
                            _v = _s.split(": ", 1)[1].strip()
                            if _v:
                                block_fios.append(_v)
                    # ДР — строка вида "Дата рождения: ...", "BDay: ...", "ДР: ..."
                    if any(kw in _sl for kw in ["bday", "birthday", "дата рожд", "д.р.", "д р ", "др:"]):
                        if ": " in _s:
                            _v = _s.split(": ", 1)[1].strip()
                            if _v:
                                block_bdays.append(_v)
                        else:
                            # попробуем вытащить дату из строки без двоеточия
                            _m = re.search(r'(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})', _s)
                            if _m:
                                block_bdays.append(_m.group(1))

                # Если в блоке ровно одно ФИО и одно ДР — привязываем
                if len(block_fios) == 1 and len(block_bdays) == 1:
                    fio_with_bday[block_fios[0]] = block_bdays[0]
                # Если несколько — пробуем привязать позиционно
                elif len(block_fios) > 1 and len(block_bdays) > 0:
                    for _i, _fio in enumerate(block_fios):
                        if _i < len(block_bdays):
                            fio_with_bday[_fio] = block_bdays[_i]
                        else:
                            fio_with_bday.setdefault(_fio, None)

                # ── Парсинг секций ДТП / инцидентов (BigBase) ──────────────────
                # Реальный формат строк из BigBase:
                #   "  📍 ПОТЕРПЕВШИЙ - ВЛАДЕЛЕЦ ТС: [[['Фамилия', 'ИВАНОВ'], ...]]"
                #   "  📱 Страховой полис: [[['Номер', 'ЕЕЕ 038...'], ...]]]"
                # Также общие поля инцидента:
                #   "  📅 Дата инцидента: 2017-03-07 00:00:00"
                #   "  ▪️ Тип инцидента: ДТП"

                _INCIDENT_ROLE_KW = (
                    "потерпевш", "нарушитель", "виновник", "пострадавш",
                    "водитель тс", "владелец тс", "пешеход", "страхователь",
                )
                _INCIDENT_VEHICLE_KW = (
                    "госномер", "vin", "марка", "модель", "выпуск",
                )
                _INCIDENT_DOC_KW = ("страховой полис", "осаго", "каско", "полис")

                # Собираем общие поля инцидента и роли/авто отдельно
                _inc_date = None
                _inc_type = None
                _inc_roles = []    # [(роль, {поле: значение})]
                _inc_autos = []    # [(марка, {поле: значение})]
                _inc_docs  = []    # [(название, {поле: значение})]

                def _parse_nested_pairs(raw_str):
                    """Вытаскивает пары из строки вида [[['К','В'],['К2','В2']]]"""
                    result = {}
                    for m in re.finditer(r"\['([^']+)',\s*'([^']+)'\]", raw_str):
                        k, v = m.group(1).strip(), m.group(2).strip()
                        k_low = k.lower()
                        if k_low not in ("дата иземнения", "дата изменения", "date_update"):
                            result[k] = v
                    return result

                for _ln in lines:
                    _s = _ln.strip()
                    if not _s or "═══" in _s:
                        continue

                    _sl = _s.lower()

                    # Общие поля инцидента (без вложенных списков)
                    if "[[" not in _s and ": " in _s:
                        _fn_raw, _fv_raw = _s.split(": ", 1)
                        _fn_low = _fn_raw.lower().strip(" 📅▪️📋📱📍👤🚗🚙🔑🏢")
                        _fv_raw = _fv_raw.strip()
                        if "дата инцидент" in _fn_low or "date" in _fn_low and "инцид" in _sl:
                            _inc_date = _fv_raw.split(" ")[0]  # только дата, без времени
                        elif "тип инцидент" in _fn_low or ("тип" in _fn_low and "инцид" in _sl):
                            _inc_type = _fv_raw

                    # Роль участника (человек)
                    elif "[[" in _s and any(kw in _sl for kw in _INCIDENT_ROLE_KW):
                        if ": " in _s:
                            _role_title, _nested = _s.split(": ", 1)
                            _role_title = _role_title.strip().lstrip("📍📱▪️🎂👤🔑  ")
                            _pairs = _parse_nested_pairs(_nested)
                            if _pairs:
                                _inc_roles.append((_role_title, _pairs))

                    # Автомобиль
                    elif "[[" in _s and not any(kw in _sl for kw in _INCIDENT_ROLE_KW):
                        _nested_part = _s
                        if ": " in _s:
                            _title_part, _nested_part = _s.split(": ", 1)
                        else:
                            _title_part = _s
                        _pairs = _parse_nested_pairs(_nested_part)
                        if _pairs and any(kw in str(_pairs).lower() for kw in _INCIDENT_VEHICLE_KW):
                            _auto_title = _title_part.strip().lstrip("🚙🚗📍▪️  ")
                            _inc_autos.append((_auto_title, _pairs))
                        elif _pairs and any(kw in _sl for kw in _INCIDENT_DOC_KW):
                            _doc_title = _title_part.strip().lstrip("📋📱▪️  ") if ": " in _s else "Документ"
                            _inc_docs.append((_doc_title, _pairs))

                # Если нашли хоть что-то по инциденту — формируем запись
                if _inc_roles or _inc_autos:
                    _inc_header_parts = []
                    if _inc_date:
                        _inc_header_parts.append(_inc_date)
                    if _inc_type:
                        _inc_header_parts.append(_inc_type)
                    _inc_header = " | ".join(_inc_header_parts) if _inc_header_parts else "Инцидент"

                    _inc_entry_lines = [f"📅 {_inc_header}"]

                    for _role, _fields in _inc_roles:
                        _name_parts = [_fields.get("Фамилия",""), _fields.get("Имя",""), _fields.get("Отчество","")]
                        _full_name = " ".join(p for p in _name_parts if p).strip()
                        _bday = _fields.get("Дата рождения","")
                        _person_str = _full_name
                        if _bday:
                            _bday_short = _bday.split(" ")[0]
                            _person_str += f" ({_bday_short})"
                        if _person_str.strip():
                            _inc_entry_lines.append(f"  🪪 {_role}: {_person_str}")

                    for _auto_title, _fields in _inc_autos:
                        _gos = _fields.get("Госномер","")
                        _vin = _fields.get("VIN","")
                        _marka = _fields.get("Марка", _auto_title)
                        _auto_str = _marka
                        if _gos: _auto_str += f" | {_gos}"
                        if _vin:  _auto_str += f" | VIN: {_vin}"
                        _inc_entry_lines.append(f"  🛻 {_auto_str}")

                    for _doc_title, _fields in _inc_docs:
                        _num = _fields.get("Номер","")
                        _sk = _fields.get("Страховая компания","")
                        _doc_str = _doc_title
                        if _num: _doc_str += f" №{_num}"
                        if _sk:  _doc_str += f" ({_sk})"
                        _inc_entry_lines.append(f"  📄 {_doc_str}")

                    collected["incidents"].add("\n".join(_inc_entry_lines))

                # Переменные для отслеживания контекста (построчный проход ниже)
                current_fio = None
                current_bday = None
                in_relatives_section = False

                # ── Паспорт: буфер для склейки серии+номера из отдельных полей ──
                _pp_buf = _passport_buf.setdefault(_res_idx, {})

                # ── Violations-парсер: ГИБДД, штрафы, ФССП, нарушения ──
                _VIOLATION_HEADER_KW = (
                    "гибдд", "нарушени", "штраф", "протокол", "постановление",
                    "фссп", "исполнительн", "судимост", "уголовн", "административн",
                    "налоговая задолженност", "долг", "банкротств",
                    "лишение прав", "арест", "запрет", "розыск",
                )
                _VIOLATION_FIELD_KW = (
                    "дата нарушени", "дата штрафа", "сумма штрафа", "сумма долга",
                    "статья", "кодекс", "постановление №", "протокол №",
                    "место нарушени", "тип нарушени", "вид нарушени",
                    "исполнительное производство", "номер дела",
                    "суд", "решение суда", "приговор",
                )

                _viol_date = None
                _viol_type = None
                _viol_fields = {}

                def _flush_violation():
                    """Добавляет текущее нарушение в collected если есть данные."""
                    if not (_viol_type or _viol_date or _viol_fields):
                        return
                    parts_v = []
                    if _viol_date:
                        parts_v.append(f"📅 {_viol_date}")
                    if _viol_type:
                        parts_v.append(f"📋 {_viol_type}")
                    for k, v in _viol_fields.items():
                        parts_v.append(f"  {k}: {v}")
                    collected["violations"].add("\n".join(parts_v))

                for _vln in lines:
                    _vs = _vln.strip()
                    if not _vs or "═══" in _vs:
                        continue
                    _vsl = _vs.lower()

                    # Заголовок нового нарушения/происшествия
                    if any(kw in _vsl for kw in _VIOLATION_HEADER_KW) and "[[" not in _vs:
                        if ": " in _vs:
                            _vtitle, _vval = _vs.split(": ", 1)
                            _vtitle_clean = _vtitle.strip().lstrip("📅📋▪️⚠️🚔🚨🔴 ")
                            _vval = _vval.strip()
                            # Это поле нарушения, не новый заголовок
                            if any(kw in _vtitle_clean.lower() for kw in ("дата", "сумма", "статья", "номер", "тип", "вид")):
                                if "дата" in _vtitle_clean.lower():
                                    _viol_date = _vval.split(" ")[0]
                                elif "тип" in _vtitle_clean.lower() or "вид" in _vtitle_clean.lower():
                                    _viol_type = _vval
                                else:
                                    _viol_fields[_vtitle_clean] = _vval
                            else:
                                # Новый тип нарушения — flush предыдущего
                                _flush_violation()
                                _viol_date = None
                                _viol_type = _vval or _vtitle_clean
                                _viol_fields = {}
                        else:
                            # Строка-заголовок без значения
                            _flush_violation()
                            _viol_date = None
                            _viol_type = _vs.strip().lstrip("📋⚠️🚔🚨🔴▪️ ")
                            _viol_fields = {}
                    elif any(kw in _vsl for kw in _VIOLATION_FIELD_KW) and ": " in _vs:
                        _vfn, _vfv = _vs.split(": ", 1)
                        _vfn = _vfn.strip().lstrip("📅📋▪️ ")
                        _vfv = _vfv.strip()
                        if "дата" in _vfn.lower():
                            _viol_date = _vfv.split(" ")[0]
                        elif "сумма" in _vfn.lower():
                            _viol_fields["Сумма"] = _vfv
                        elif "статья" in _vfn.lower():
                            _viol_fields["Статья"] = _vfv
                        elif "номер дела" in _vfn.lower() or "постановление" in _vfn.lower() or "протокол" in _vfn.lower():
                            _viol_fields["№"] = _vfv
                        else:
                            _viol_fields[_vfn] = _vfv
                _flush_violation()

                for line in lines:
                    # Убираем заголовки типа "═══"
                    if "═══" in line or "───" in line:
                        continue
                    
                    stripped = line.strip()
                    if not stripped:
                        continue

                    # Пропускаем строки с вложенными BigBase-списками [[...]] — они уже
                    # обработаны парсером инцидентов выше
                    if "[[" in stripped and "]]" in stripped:
                        continue

                    # --- ОПРЕДЕЛЕНИЕ СЕКЦИИ РОДСТВЕННИКОВ ---
                    lower_stripped = stripped.lower()
                    if any(kw in lower_stripped for kw in ["relatives", "родственник", "связи", "related"]):
                        in_relatives_section = True
                        # Если после двоеточия сразу идут данные
                        if ":" in stripped:
                            relatives_part = stripped.split(":", 1)[1].strip()
                            if relatives_part:
                                # Парсим список родственников (может быть через запятую)
                                for rel in relatives_part.split(","):
                                    rel = rel.strip()
                                    if rel:
                                        # Ищем дату рождения в скобках или через пробел
                                        bday_match = re.search(r'(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})', rel)
                                        if bday_match:
                                            bday = bday_match.group(1)
                                            name = rel.replace(bday, "").strip(" ()")
                                            collected["relatives"].add(f"{name} ({bday})" if name else f"Дата: {bday}")
                                        else:
                                            collected["relatives"].add(rel)
                        continue
                    
                    # Если мы в секции родственников и строка не пустая
                    if in_relatives_section:
                        # Проверяем, не началась ли новая секция
                        if ":" in stripped and not any(kw in lower_stripped for kw in ["relatives", "родственник"]):
                            in_relatives_section = False
                        else:
                            # Это строка с родственником
                            # Ищем дату рождения
                            bday_match = re.search(r'(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})', stripped)
                            if bday_match:
                                bday = bday_match.group(1)
                                name = re.sub(r'\d{1,2}[./-]\d{1,2}[./-]\d{2,4}', '', stripped).strip(" ,()")
                                collected["relatives"].add(f"{name} ({bday})" if name else f"Дата: {bday}")
                            else:
                                collected["relatives"].add(stripped)
                            continue

                    # --- РАННЯЯ ОБРАБОТКА АДРЕСНЫХ ПОЛЕЙ (до фильтра длины!) ---
                    # Поля типа "🏠 Адрес 21.05.2022: 427000,..." длиннее 80 символов,
                    # но должны попасть в адреса — проверяем ДО фильтра
                    if ": " in stripped:
                        _early_fn = stripped.split(": ", 1)[0].strip().lower()
                        _early_val = stripped.split(": ", 1)[1].strip()
                        _EARLY_ADDR_KEYS = ("адрес", "address", "прописка", "регистрация",
                                            "место жительства", "местонахождение", "pointaddress")
                        if any(kw in _early_fn for kw in _EARLY_ADDR_KEYS) and _early_val:
                            collected["address"].add(_early_val)
                            continue

                    # --- ФИЛЬТР ОПИСАТЕЛЬНЫХ ПРЕДЛОЖЕНИЙ ---
                    if len(stripped) > 80:
                        continue
                    words_in_line = stripped.split()
                    if len(words_in_line) > 6 and ": " not in stripped:
                        continue
                    if stripped.endswith((".","…","года","году")):
                        continue

                    # --- ОБРАБОТКА BDay (ДАТЫ РОЖДЕНИЯ) ---
                    # Ищем паттерн "BDay:12.12.2000" или "Дата рождения: 12.12.2000"
                    if "bday" in lower_stripped or "birthday" in lower_stripped or "дата рождения" in lower_stripped:
                        if ":" in stripped:
                            bday_value = stripped.split(":", 1)[1].strip()
                            if bday_value:
                                # Сохраняем ДР для связки с ФИО
                                current_bday = bday_value
                                collected["birthday"].add(bday_value)
                        continue

                    # Пробуем вытащить значение после ": "
                    if ": " in stripped:
                        field_name = stripped.split(": ", 1)[0].strip().lower()
                        value = stripped.split(": ", 1)[1].strip()

                        # Обработка поля с именем (ФИО)
                        # ВАЖНО: "👤" убран — этот эмодзи стоит у РАЗНЫХ полей (House, Amount, Region...)
                        # Точный список ключей для ФИО, без "имя"/"name" как подстроки (иначе захватит "amountdebt" и др.)
                        _FIO_FIELD_KEYS = {"fio", "фио", "fullname", "full_name", "фамилия"}
                        _clean_fn = field_name.strip().lstrip("👤📋🪪📍 ")
                        if _clean_fn in _FIO_FIELD_KEYS or any(kw in _clean_fn for kw in ("fio", "фио", "fullname", "фамилия")):
                            current_fio = value
                            collected["fio"].add(value)
                            continue

                        # Обработка поля с родственниками (если не сработало выше)
                        if any(kw in field_name for kw in ["relatives", "родственник", "связи"]):
                            for rel in value.split(","):
                                rel = rel.strip()
                                if rel:
                                    bday_match = re.search(r'(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})', rel)
                                    if bday_match:
                                        bday = bday_match.group(1)
                                        name = rel.replace(bday, "").strip(" ()")
                                        collected["relatives"].add(f"{name} ({bday})" if name else f"Дата: {bday}")
                                    else:
                                        collected["relatives"].add(rel)
                            continue

                        # Если имя поля явно указывает на адрес — принудительно address
                        # (до detect_field_type, чтобы индекс в начале не сбивал тип)
                        _ADDRESS_FIELD_NAMES = (
                            "адрес", "address", "прописка", "регистрация",
                            "место жительства", "место рождения", "местонахождение",
                        )
                        if any(kw in field_name for kw in _ADDRESS_FIELD_NAMES):
                            collected["address"].add(value)
                            continue

                        # Если имя поля явно указывает на ИНН — принудительно inn
                        _INN_FIELD_NAMES = ("инн", "inn", "itin", "tax_id", "taxid", "taxpayer")
                        if any(kw in field_name.lower() for kw in _INN_FIELD_NAMES):
                            collected["inn"].add(value)
                            continue

                        # ── Паспорт: серия и номер из отдельных полей ──
                        _fn_clean = field_name.strip().lstrip("📕📋▪️ ").lower()
                        _PASSPORT_SERIES_KW = ("passportseries", "серия паспорта", "серия", "doc_series", "passseries", "passportserie")
                        _PASSPORT_NUMBER_KW = ("passportnumber", "номер паспорта", "номерпаспорта", "doc_number", "passnumber", "passportnumber", "паспорт номер")
                        if any(kw in _fn_clean for kw in _PASSPORT_SERIES_KW):
                            _pp_buf["series"] = value
                            if "number" in _pp_buf:
                                collected["passport"].add(f"{_pp_buf['series']} {_pp_buf['number']}")
                            continue
                        if any(kw in _fn_clean for kw in _PASSPORT_NUMBER_KW):
                            _pp_buf["number"] = value
                            if "series" in _pp_buf:
                                collected["passport"].add(f"{_pp_buf['series']} {_pp_buf['number']}")
                            continue

                        # ── Violations из полей (ГИБДД, штрафы, ФССП) ──
                        _VIOL_FIELD_KW_INLINE = (
                            "штраф", "нарушени", "протокол", "постановлени",
                            "фссп", "исполнительн", "судимост", "арест",
                            "задолженност", "долг", "лишение прав",
                        )
                        if any(kw in _fn_clean for kw in _VIOL_FIELD_KW_INLINE) and value:
                            _label = field_name.strip().lstrip("📋⚠️🚔🚨▪️ ")
                            collected["violations"].add(f"📋 {_label}: {value}")
                            continue
                    else:
                        # Убираем первый "символ" (иконку) если строка начинается не с буквы
                        parts_l = stripped.split(" ", 1)
                        value = parts_l[1].strip() if len(parts_l) > 1 else stripped
                        field_name = ""

                    if not value or len(value) < 2:
                        continue

                    # Дополнительный фильтр значения
                    # Для потенциальных адресов поднимаем лимит длины
                    # Паттерн: индекс + пробел ИЛИ запятая (427000,РОССИЯ или 427000 Россия)
                    _looks_like_address = re.match(r'^\d{5,6}[\s,]', value) or any(
                        kw in value.lower() for kw in [
                            "ул.", "улица", "пр.", "проспект", "пер.", "россия",
                            "респ", "обл.", "район", "г.", "город", "р-н", "ул,"
                        ]
                    )
                    if len(value) > 150:
                        continue
                    if len(value) > 80 and not _looks_like_address:
                        continue
                    value_words = value.split()
                    if len(value_words) > 7 and not any(c.isdigit() for c in value):
                        continue

                    # --- ОПРЕДЕЛЕНИЕ ТИПА ЗНАЧЕНИЯ ---
                    ftype = detect_field_type(value)

                    # Принудительная проверка для адресов — ДО фильтров мусора
                    _address_keywords = [
                        "ул.", "улица", "пр.", "проспект", "пер.", "переулок",
                        "бульвар", "шоссе", "набережная", "площадь", "д.", "дом",
                        "кв.", "квартира", "офис", "корп.", "г.", "город",
                        "респ.", "республика", "край", "обл.", "область",
                        "р-н", "район", "пос.", "поселок", "с.", "село",
                        "дер.", "деревня", "индекс", "россия", "рф",
                    ]
                    if ftype in ("other", "phone"):
                        sl = value.lower()
                        # Почтовый индекс в начале строки (5-6 цифр затем пробел/буква)
                        has_postal = bool(re.match(r'^\d{5,6}[\s,]', value))
                        if has_postal or any(kw in sl for kw in _address_keywords) or (',' in value and len(value) > 20):
                            ftype = "address"

                    # --- ФИЛЬТРЫ МУСОРА ---
                    digits_only = re.sub(r"\D", "", value)
                    # Телефон: только 10-11 цифр, отклоняем VIN / длинные числа / карточные номера
                    if ftype == "phone":
                        if len(digits_only) < 10 or len(digits_only) > 11:
                            ftype = "other"
                        # Отклоняем если содержит буквы (VIN и т.п.)
                        elif re.search(r"[A-Za-zА-Яа-я]", value):
                            ftype = "other"
                    # Логин: не должен быть просто числом, коротким словом, VIN, статусом
                    if ftype == "login":
                        _JUNK_LOGIN = {
                            "active", "good", "male", "female", "yes", "no", "да", "нет",
                            "true", "false", "null", "none",
                        }
                        if value.lower() in _JUNK_LOGIN:
                            ftype = "other"
                        elif re.fullmatch(r"[\d.,]+", value):   # просто число/дробь
                            ftype = "other"
                        elif re.fullmatch(r"[A-Z0-9]{10,17}", value):  # VIN-подобное
                            ftype = "other"
                        elif len(value) <= 2:
                            ftype = "other"
                    # Адрес: не должен содержать [[, быть именем базы, или быть одним словом без цифр
                    if ftype == "address":
                        if "[[" in value or len(value.split()) == 1:
                            ftype = "other"
                        # Пропускаем названия баз данных (формат "РФ. Что-то")
                        if re.match(r"^РФ\.", value):
                            continue
                    # "Прочее": пропускаем даты-времени (мусор из служебных полей),
                    # одиночные слова всем капсом, украинские/казахские названия без контекста
                    if ftype == "other":
                        # Пропускаем datetime-строки
                        if re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}", value):
                            continue
                        # Пропускаем одиночные слова без цифр короче 4 символов
                        if re.fullmatch(r"[А-ЯA-ZЁ]{1,3}", value):
                            continue
                        # Пропускаем строки-описания из базы (длинные, нет двоеточия)
                        if len(value) > 60 and ":" not in value:
                            continue
                    
                    # Если это ФИО и у нас есть сохраненная дата рождения — объединяем
                    if ftype == "fio":
                        current_fio = value
                        if current_bday:
                            fio_with_bday[value] = current_bday
                            current_bday = None  # сбрасываем, чтобы не привязать к следующему
                    
                    if ftype in collected:
                        collected[ftype].add(value)
                    else:
                        sl = stripped.lower()
                        fn_sl = field_name.lower() if field_name else ""
                        if any(k in sl for k in ["телефон", "phone", "📱"]):
                            collected["phone"].add(value)
                        elif any(k in sl for k in ["фио", "имя", "фамил", "👤"]):
                            collected["fio"].add(value)
                        elif any(k in sl for k in ["адрес", "address", "📍"]):
                            collected["address"].add(value)
                        elif any(k in sl for k in ["email", "почта", "📧"]):
                            collected["email"].add(value)
                        elif any(k in sl for k in ["родственник", "relatives", "связи"]):
                            collected["relatives"].add(value)
                        elif any(k in sl for k in ["снилс", "snils", "📇"]):
                            collected["snils"].add(value)
                        elif any(k in sl for k in ["паспорт", "passport", "🛂"]):
                            collected["passport"].add(value)
                        elif any(k in fn_sl for k in ["инн", "inn"]) or re.match(r"^\d{10}$|^\d{12}$", value.replace(" ", "")):
                            collected["inn"].add(value)
                        else:
                            collected["other"].add(value)

            # Объединяем ФИО с датами рождения
            for fio, bday in fio_with_bday.items():
                if fio in collected["fio"]:
                    # Заменяем простое ФИО на ФИО с датой рождения
                    collected["fio"].discard(fio)
                    _combined = f"{fio} ({bday})"
                    collected["fio"].add(_combined)
                    # Перенос источников на новый ключ с датой
                    if "fio" in value_sources and fio in value_sources["fio"]:
                        value_sources["fio"][_combined] = value_sources["fio"].pop(fio)

            # Функция сортировки телефонов
            def sort_phones(phones_set):
                phones_list = list(phones_set)
                def get_priority(phone):
                    digits = ''.join(c for c in phone if c.isdigit())
                    if len(digits) == 11 and digits[0] in ('7', '8'):
                        return (0, phone)
                    elif len(digits) == 10 and digits[0] == '9':
                        return (1, phone)
                    elif len(digits) >= 11 and digits.startswith('7'):
                        return (2, phone)
                    elif any(x in phone for x in ['+7', '8 (', '8(']):
                        return (3, phone)
                    else:
                        return (9, phone)
                return sorted(phones_list, key=lambda x: (get_priority(x)[0], x))

            # Заголовки секций
            section_titles = {
                "phone":      "📞 Телефоны",
                "fio":        "🪪 ФИО",
                "email":      "✉️ Email",
                "address":    "🏠 Адреса",
                "birthday":   "🗓️ Дата рождения",
                "car_num":    "🔲 Номера авто",
                "car_model":  "🛻 Марки авто",
                "car_color":  "🖌️ Цвета авто",
                "inn":        "🔢 ИНН",
                "snils":      "🟢 СНИЛС",
                "passport":   "📕 Паспорт",
                "city":       "🏙️ Города",
                "login":      "👤 Логины",
                "password":   "🔒 Пароли",
                "ip":         "📡 IP адреса",
                "card":       "🏦 Банк. карты",
                "company":    "💼 Компании",
                "relatives":  "🫂 Родственники",
                "incidents":  "⚠️ ДТП / Инциденты",
                "violations": "🚔 ГИБДД / Нарушения / ФССП",
                "other":      "▫️ Прочее",
            }

            _now_str = datetime.now().strftime('%d.%m.%Y %H:%M')
            summary_lines = [
                f"🔷 <b>СВОДКА</b>  ╱  <code>{query}</code>",
                f"<i>🗄 Источников: <b>{len(results)}</b>   ·   🕐 {_now_str}</i>",
                "▔" * 28,
            ]

            # Иконки секций для яркого вида в Telegram
            _section_icons = {
                "phone":     "📞",
                "fio":       "🪪",
                "email":     "✉️",
                "address":   "🏠",
                "birthday":  "🗓",
                "car_num":   "🔲",
                "car_model": "🚗",
                "car_color": "🎨",
                "inn":       "🏦",
                "snils":     "📋",
                "passport":  "📕",
                "city":      "🏙",
                "login":     "👤",
                "password":  "🔒",
                "ip":        "📡",
                "card":      "💳",
                "company":   "💼",
                "relatives": "👥",
                "incidents": "🚨",
                "violations":"⚖️",
                "other":     "▫️",
            }

            has_data = False
            for key, title in section_titles.items():
                if key == "phone":
                    items = sort_phones(collected[key])
                else:
                    items = sorted(collected[key])

                if not items:
                    continue
                has_data = True
                icon = _section_icons.get(key, "▫️")
                summary_lines.append(f"\n{icon} <b>{title}</b>")

                if key == "incidents":
                    for item in items:
                        if "  →  " in item:
                            inc_title, inc_fields = item.split("  →  ", 1)
                            summary_lines.append(f"  ┌ <b>{inc_title}</b>")
                            for field_part in inc_fields.split("  |  "):
                                summary_lines.append(f"  │  {field_part.strip()}")
                            summary_lines.append(f"  └{'─' * 28}")
                        else:
                            for vline in item.splitlines():
                                summary_lines.append(f"  {vline}" if vline.startswith("  ") else f"  • {vline}")
                elif key == "violations":
                    for item in items:
                        summary_lines.append(f"  ┌{'─' * 28}")
                        for vline in item.splitlines():
                            summary_lines.append(f"  │ {vline.strip()}")
                        summary_lines.append(f"  └{'─' * 28}")
                elif key == "fio":
                    fio_lines = []
                    for fio_item in sorted(collected["fio"]):
                        if re.search(r'\(\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\)', fio_item):
                            fio_lines.append(f"  ◆ {fio_item}")
                        else:
                            bday = fio_with_bday.get(fio_item)
                            if bday:
                                fio_lines.append(f"  ◆ <b>{fio_item}</b>  <i>({bday})</i>")
                            else:
                                fio_lines.append(f"  ◆ {fio_item}")
                    summary_lines.extend(fio_lines)
                else:
                    for item in items:
                        summary_lines.append(f"  ◆ {item}")

            if not has_data:
                summary_lines.append("\n❌ Не удалось структурировать данные.")

            summary_lines.append(f"\n▁" * 28)
            summary_text = "\n".join(summary_lines)

            # ========== ГЕНЕРАЦИЯ HTML ОТЧЁТА (ЕДИНЫЙ ФАЙЛ: СВОДКА + ДЕТАЛИЗАЦИЯ ПО БАЗАМ) ==========
            # Собираем groups так же как в download_report для нижней части
            from collections import OrderedDict
            groups = OrderedDict()
            for result in results:
                text_info = result[0] if result[0] else ""
                source = str(result[2]) if len(result) > 2 and result[2] else "Без источника"
                db_t   = result[3] if len(result) > 3 and result[3] else "default"
                if source not in groups:
                    groups[source] = {"db_type": db_t, "records": []}
                groups[source]["records"].append(text_info)

            db_type_names_map = {
                "avto": "Автомобили", "imush": "Имущество",
                "work": "Работа", "people": "Люди", "default": "База данных",
            }
            db_colors_map = {
                "avto": "#34d399", "imush": "#fb923c",
                "work": "#a78bfa", "people": "#60a5fa", "default": "#94a3b8",
            }
            FIELD_ICONS_MAP2 = {
                "телефон": "📞", "phone": "📞", "моб": "📞",
                "фио": "🪪", "имя": "🪪", "фамилия": "🪪", "fullname": "🪪",
                "email": "✉️", "почта": "✉️", "mail": "✉️",
                "адрес": "🏠", "address": "🏠", "прописка": "🏠",
                "дата рожд": "🗓️", "bday": "🗓️", "birthday": "🗓️",
                "инн": "🔢", "inn": "🔢", "снилс": "🟢", "snils": "🟢",
                "паспорт": "📕", "passport": "📕",
                "госномер": "🔲", "номер авто": "🔲",
                "марка": "🛻", "модель": "🛻", "авто": "🛻",
                "логин": "👤", "login": "👤", "username": "👤",
                "пароль": "🔒", "password": "🔒",
                "ip": "📡", "карта": "🏦", "card": "🏦",
                "компания": "💼", "company": "💼",
                "город": "🏙️", "city": "🏙️",
            }
            def _get_field_icon2(fname):
                fl = fname.lower()
                for kw, icon in FIELD_ICONS_MAP2.items():
                    if kw in fl:
                        return icon
                return "▪️"
            def _esc2(s):
                return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"','&quot;')

            def build_html_summary(query, results_count, collected, fio_with_bday, section_titles, sort_phones):
                now_str = datetime.now().strftime("%d.%m.%Y %H:%M")

                # ── СБОР МАППИНГА: значение → список баз (для tooltip) ──
                # Парсим groups, уже сгруппированные по источнику
                _vsources = {}  # {field_type: {value: [db_display_name, ...]}}
                for _src, _gdata in groups.items():
                    _raw_src = str(_src)
                    if _raw_src.startswith("api:"):
                        _sname = "API"
                    else:
                        _sname = os.path.splitext(os.path.basename(_raw_src))[0]
                    # Ищем отображаемое имя из первой записи (строка ═══)
                    _display = _sname
                    for _rt in _gdata["records"]:
                        for _rl in _rt.splitlines():
                            if "═══" in _rl:
                                _cand = _rl.strip("═ 🌐📁🚗🏠💼👤📋").strip()
                                if _cand:
                                    _display = _cand
                                    break
                        break
                    for _rt in _gdata["records"]:
                        for _rl in _rt.splitlines():
                            _rs = _rl.strip()
                            if not _rs or "═══" in _rs or "───" in _rs:
                                continue
                            if ": " in _rs:
                                _rfn, _rfv = _rs.split(": ", 1)
                                _rfv = _rfv.strip()
                                if not _rfv or _rfv in ("—", "-", ""):
                                    continue
                                _ft = detect_field_type(_rfv)
                                if _ft not in _vsources:
                                    _vsources[_ft] = {}
                                if _rfv not in _vsources[_ft]:
                                    _vsources[_ft][_rfv] = []
                                if _display not in _vsources[_ft][_rfv]:
                                    _vsources[_ft][_rfv].append(_display)

                def _sources_tooltip(key, value):
                    """Возвращает строку для атрибута title: список баз где найдено значение."""
                    # Для ФИО с датой "Иванов Иван (01.01.1990)" — пробуем и полное и без даты
                    lookup_values = [value]
                    import re as _re
                    _stripped = _re.sub(r"\s*\(\d[\d./-]+\)\s*$", "", value).strip()
                    if _stripped != value:
                        lookup_values.append(_stripped)

                    for _lv in lookup_values:
                        srcs = _vsources.get(key, {}).get(_lv, [])
                        if srcs:
                            return "📂 Источники: " + ", ".join(srcs)
                        for _ft_dict in _vsources.values():
                            if _lv in _ft_dict:
                                return "📂 Источники: " + ", ".join(_ft_dict[_lv])
                    return ""

                # ── СВОДНЫЕ КАРТОЧКИ (верхняя часть) ──
                def make_cards(key, items):
                    if not items:
                        return ""
                    icon_map = {
                        "phone": "📞", "fio": "🪪", "email": "✉️", "address": "🏠",
                        "birthday": "🗓️", "car_num": "🔲", "car_model": "🛻",
                        "car_color": "🖌️", "inn": "🔢", "snils": "🟢", "passport": "📕",
                        "city": "🏙️", "login": "👤", "password": "🔒", "ip": "📡",
                        "card": "🏦", "company": "💼", "relatives": "🫂",
                        "incidents": "⚠️", "violations": "🚔", "other": "▫️",
                    }
                    color_map = {
                        "phone": "#4ade80", "fio": "#60a5fa", "email": "#f472b6",
                        "address": "#fb923c", "birthday": "#a78bfa", "car_num": "#34d399",
                        "car_model": "#38bdf8", "car_color": "#f59e0b", "inn": "#e879f9",
                        "snils": "#4ade80", "passport": "#f87171", "city": "#60a5fa",
                        "login": "#94a3b8", "password": "#f87171", "ip": "#38bdf8",
                        "card": "#fbbf24", "company": "#a78bfa", "relatives": "#34d399",
                        "incidents": "#fb923c", "violations": "#ef4444", "other": "#94a3b8",
                    }
                    icon = icon_map.get(key, "▪️")
                    color = color_map.get(key, "#94a3b8")
                    title = section_titles.get(key, key)
                    items_html = ""
                    for item in items:
                        if key == "incidents" and "  →  " in item:
                            inc_title, inc_fields = item.split("  →  ", 1)
                            fields_html = "".join(
                                f'<span class="inc-field">{f.strip()}</span>'
                                for f in inc_fields.split("  |  ") if f.strip()
                            )
                            items_html += f'<div class="item inc-item"><span class="inc-title">{inc_title}</span>{fields_html}</div>'
                        elif key in ("incidents", "violations"):
                            # Многострочные записи — рендерим каждую строку отдельно
                            lines_v = item.splitlines()
                            first_line = lines_v[0] if lines_v else item
                            rest_html = "".join(
                                f'<span class="inc-field">{_esc2(l.strip())}</span>'
                                for l in lines_v[1:] if l.strip()
                            )
                            items_html += f'<div class="item inc-item"><span class="inc-title">{_esc2(first_line.strip("• │┌└─ "))}</span>{rest_html}</div>'
                        else:
                            tooltip = _sources_tooltip(key, item)
                            tooltip_attr = f' title="{_esc2(tooltip)}"' if tooltip else ''
                            items_html += f'<div class="item copyable"{tooltip_attr} onclick="copyText(this)" data-val="{_esc2(item)}">{_esc2(item)}</div>'
                    return f'''
                    <div class="card" style="--accent:{color}">
                        <div class="card-header">
                            <span class="card-icon">{icon}</span>
                            <span class="card-title">{title}</span>
                            <span class="card-count">{len(items)}</span>
                        </div>
                        <div class="card-body">{items_html}</div>
                    </div>'''

                cards_html = ""
                for key, title in section_titles.items():
                    if key == "phone":
                        items = sort_phones(collected[key])
                    else:
                        items = sorted(collected[key])
                    if items:
                        cards_html += make_cards(key, items)

                total_fields = sum(len(v) for v in collected.values())

                # ── ДЕТАЛИЗАЦИЯ ПО БАЗАМ (нижняя часть) + КНОПКИ-ЯКОРЯ ──
                sidebar_btns = ""
                detail_blocks_html = ""
                db_idx = 0
                for src, gdata in groups.items():
                    db_t = gdata["db_type"]
                    color = db_colors_map.get(db_t, "#94a3b8")
                    type_label = db_type_names_map.get(db_t, "База данных")
                    raw_src = str(src)
                    if raw_src.startswith("api:"):
                        src_name = "API"
                    else:
                        src_name = os.path.splitext(os.path.basename(raw_src))[0]

                    records_html = ""
                    valid_count = 0
                    block_db_name = ""
                    block_desc = ""

                    # Вычисляем display_name заранее (из заголовка ═══ первой записи)
                    # чтобы использовать в tooltip ячеек ДО конца цикла
                    _pre_display = src_name
                    for _pr in gdata["records"]:
                        for _pl in _pr.splitlines():
                            if "═══" in _pl:
                                _cand = _pl.strip("═ 🌐📁🚗🏠💼👤📋").strip()
                                if _cand:
                                    _pre_display = _cand
                                    break
                        break
                    display_name = _pre_display  # будет уточнён после цикла

                    for i, rec_text in enumerate(gdata["records"], 1):
                        lines_r = rec_text.splitlines()
                        cells_html = ""
                        for ln in lines_r:
                            s = ln.strip()
                            if not s:
                                continue
                            if "═══" in s:
                                name_candidate = s.strip("═ 🌐").strip()
                                if name_candidate and not block_db_name:
                                    block_db_name = name_candidate
                                continue
                            if len(s) > 60 and ": " not in s[:40] and not block_desc:
                                block_desc = s
                                continue
                            if ": " in s:
                                fname, fval = s.split(": ", 1)
                                fname_clean = fname.strip().lstrip("📱👤📍✉️🔢🏢🏦📕🟢📡🔒👤🌐▪️🎂🚗🔲🛻🖌️💼🫂⚠️📋📅 ")
                                fval = fval.strip()
                                if not fval or fval in ("—", "-", ""):
                                    continue
                                icon = _get_field_icon2(fname_clean)
                                _ft = detect_field_type(fval)
                                _tip = _sources_tooltip(_ft, fval)
                                _tip_attr = f'title="{_esc2(_tip)}"' if _tip else f'title="📂 {_esc2(display_name)}"'
                                cells_html += f'''<div class="cell copyable" {_tip_attr} onclick="copyText(this)" data-val="{_esc2(fval)}">
                                    <div class="cell-icon">{icon}</div>
                                    <div class="cell-body">
                                        <div class="cell-label">{_esc2(fname_clean)}</div>
                                        <div class="cell-value">{_esc2(fval)}</div>
                                    </div>
                                </div>'''
                            elif s and "═══" not in s and "───" not in s and len(s) <= 60:
                                cells_html += f'<div class="cell-plain">{_esc2(s)}</div>'

                        if not cells_html:
                            continue
                        valid_count += 1
                        records_html += f'''<div class="record">
                            <div class="rec-header">
                                <span class="rec-num">{valid_count}</span>
                            </div>
                            <div class="cells-grid">{cells_html}</div>
                        </div>'''

                    if not records_html:
                        continue

                    display_name = block_db_name or src_name  # финальное уточнение
                    anchor_id = f"db_{db_idx}"
                    desc_html = f'<div class="db-desc">{_esc2(block_desc)}</div>' if block_desc else ""

                    # Кнопка в боковой панели
                    sidebar_btns += f'''<button class="nav-btn" data-target="{anchor_id}" style="--bc:{color}">
                        <span class="nav-dot" style="background:{color};box-shadow:0 0 6px {color}"></span>
                        <span class="nav-label">{_esc2(display_name)}</span>
                        <span class="nav-count">{valid_count}</span>
                    </button>'''

                    detail_blocks_html += f'''<div class="db-block" id="{anchor_id}" style="--bc:{color}">
                        <div class="db-header">
                            <div class="db-left">
                                <div class="db-type-badge">{type_label}</div>
                                <div class="db-title">{_esc2(display_name)}</div>
                                {desc_html}
                            </div>
                            <div class="db-count-pill">{valid_count} зап.</div>
                        </div>
                        <div class="db-body">{records_html}</div>
                    </div>'''
                    db_idx += 1

                html = f'''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Сводка: {_esc2(query)}</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Unbounded:wght@400;700;900&display=swap');
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#060912;--surface:#0d1120;--surface2:#111827;--surface3:#1a2236;
  --border:#1e2d4a;--text:#e8eeff;--muted:#5a7aaa;--dim:#2a3f6a;
  --blue:#3b82f6;--blue-hi:#7cb9ff;--blue-sat:#2563eb;
  --glow:rgba(59,130,246,.35);
}}
body{{
  background:var(--bg);color:var(--text);
  font-family:'Times New Roman',Times,serif;font-size:13px;
  min-height:100vh;
  background-image:
    radial-gradient(ellipse 90% 55% at 50% -8%,rgba(59,130,246,.28) 0%,transparent 60%),
    radial-gradient(ellipse 40% 30% at 80% 80%,rgba(37,99,235,.12) 0%,transparent 50%),
    repeating-linear-gradient(0deg,transparent,transparent 39px,rgba(59,130,246,.04) 39px,rgba(59,130,246,.04) 40px),
    repeating-linear-gradient(90deg,transparent,transparent 39px,rgba(59,130,246,.04) 39px,rgba(59,130,246,.04) 40px);
}}

/* ── LAYOUT ── */
.page-wrap{{display:flex;min-height:100vh}}

/* ── SIDEBAR ── */
.sidebar{{
  width:232px;flex-shrink:0;
  position:sticky;top:0;height:100vh;overflow-y:auto;
  background:rgba(13,17,32,.97);border-right:1px solid var(--border);
  padding:1.4rem .9rem;display:flex;flex-direction:column;gap:.35rem;
  scrollbar-width:thin;scrollbar-color:var(--border) transparent;
}}
.sidebar-title{{
  font-family:'Times New Roman',Times,serif;font-size:.7rem;font-weight:700;
  letter-spacing:.15em;text-transform:uppercase;color:var(--blue-hi);
  padding:.4rem .6rem 1.1rem;
}}
/* ── NAV BUTTON — с анимацией при клике ── */
.nav-btn{{
  display:flex;align-items:center;gap:.6rem;
  background:transparent;border:1px solid transparent;
  border-radius:9px;padding:.6rem .8rem;
  cursor:pointer;text-align:left;width:100%;
  transition:background .15s,border-color .15s,transform .12s,box-shadow .15s;
  color:var(--text);font-family:'Times New Roman',Times,serif;font-size:.82rem;
  position:relative;overflow:hidden;
  -webkit-tap-highlight-color:transparent;
}}
/* ripple-слой */
.nav-btn::after{{
  content:'';
  position:absolute;left:50%;top:50%;
  width:0;height:0;border-radius:50%;
  background:rgba(59,130,246,.45);
  transform:translate(-50%,-50%);
  opacity:0;pointer-events:none;
  transition:none;
}}
.nav-btn.rippling::after{{
  width:280px;height:280px;opacity:0;
  transition:width .5s ease,height .5s ease,opacity .6s ease;
}}
.nav-btn:hover{{
  background:rgba(59,130,246,.14);
  border-color:rgba(59,130,246,.38);
  transform:translateX(3px);
  box-shadow:0 0 12px rgba(59,130,246,.12);
}}
.nav-btn:active{{
  transform:translateX(1px) scale(.97);
  background:rgba(59,130,246,.22);
  border-color:rgba(59,130,246,.6);
}}
.nav-dot{{width:8px;height:8px;border-radius:50%;flex-shrink:0;box-shadow:0 0 6px currentColor}}
.nav-label{{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.82rem}}
.nav-count{{
  font-size:.72rem;font-weight:700;
  background:rgba(59,130,246,.22);color:var(--blue-hi);
  border-radius:10px;padding:.15rem .6rem;flex-shrink:0;
  border:1px solid rgba(59,130,246,.3);
}}
.nav-divider{{margin:.7rem .2rem;border:none;border-top:1px solid var(--border)}}

/* ── MAIN ── */
.main{{flex:1;min-width:0;padding:2.2rem 1.8rem}}
.wrapper{{max-width:960px;margin:0 auto}}

/* ── HEADER ── */
.header{{
  background:linear-gradient(135deg,#0f1829 0%,#0d1120 60%);
  border:1px solid rgba(59,130,246,.35);border-radius:20px;
  padding:2.2rem 2.8rem;margin-bottom:2.2rem;position:relative;overflow:hidden;
  box-shadow:0 0 40px rgba(59,130,246,.1),inset 0 1px 0 rgba(124,185,255,.12);
}}
.header::before{{
  content:'';position:absolute;inset:0;
  background:radial-gradient(ellipse 70% 60% at 0% 0%,rgba(59,130,246,.13) 0%,transparent 60%);
  pointer-events:none;
}}
.header::after{{
  content:'';position:absolute;top:0;left:0;right:0;height:3px;
  background:linear-gradient(90deg,transparent 5%,var(--blue) 30%,var(--blue-hi) 50%,var(--blue) 70%,transparent 95%);
  border-radius:20px 20px 0 0;
  filter:blur(.5px);
}}
.header-label{{
  font-family:'Times New Roman',Times,serif;font-size:.72rem;font-weight:700;
  letter-spacing:.15em;text-transform:uppercase;color:var(--blue-hi);margin-bottom:.5rem;
}}
.header-query{{
  font-family:'Times New Roman',Times,serif;font-size:1.6rem;font-weight:900;
  color:#fff;word-break:break-all;line-height:1.2;margin-bottom:1.1rem;
  text-shadow:0 0 30px var(--glow),0 2px 4px rgba(0,0,0,.5);
}}
.header-meta{{display:flex;flex-wrap:wrap;gap:.8rem}}
.meta-badge{{
  display:flex;align-items:center;gap:.5rem;
  background:rgba(59,130,246,.1);border:1px solid rgba(59,130,246,.25);
  border-radius:9px;padding:.32rem .7rem;font-size:.78rem;color:var(--muted);
}}
.meta-badge strong{{color:var(--blue-hi);font-size:.82rem}}

/* ── SECTION DIVIDER ── */
.section-label{{display:flex;align-items:center;gap:1rem;margin:2.8rem 0 1.4rem}}
.section-label-text{{
  font-family:'Times New Roman',Times,serif;font-size:.7rem;font-weight:700;
  letter-spacing:.15em;text-transform:uppercase;color:var(--blue-hi);white-space:nowrap;
}}
.section-label-line{{flex:1;height:1px;background:linear-gradient(90deg,rgba(59,130,246,.7),transparent)}}

/* ── CARDS GRID ── */
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:1.1rem}}
.card{{
  background:linear-gradient(145deg,#0f1829 0%,#0d1120 100%);
  border:1px solid rgba(59,130,246,.22);border-radius:16px;
  overflow:hidden;position:relative;
  animation:fadeUp .4s ease both;
  transition:transform .22s,box-shadow .22s,border-color .22s;
}}
.card::before{{
  content:'';position:absolute;inset:0;
  background:linear-gradient(135deg,rgba(59,130,246,.08) 0%,transparent 55%);
  pointer-events:none;
}}
.card:hover{{
  transform:translateY(-5px);
  border-color:rgba(124,185,255,.55);
  box-shadow:0 10px 40px rgba(59,130,246,.22),0 0 0 1px rgba(59,130,246,.18);
}}
@keyframes fadeUp{{from{{opacity:0;transform:translateY(20px)}}to{{opacity:1;transform:translateY(0)}}}}
.card-header{{
  display:flex;align-items:center;gap:.7rem;padding:1.1rem 1.4rem;
  border-bottom:1px solid rgba(59,130,246,.18);
  background:linear-gradient(90deg,rgba(59,130,246,.18),transparent 80%);
}}
.card-icon{{font-size:1.1rem}}
.card-title{{
  font-family:'Times New Roman',Times,serif;font-size:.78rem;font-weight:700;
  letter-spacing:.04em;text-transform:uppercase;color:var(--blue-hi);flex:1;
}}
.card-count{{
  background:rgba(59,130,246,.25);color:var(--blue-hi);
  border-radius:20px;padding:.15rem .55rem;font-size:.72rem;font-weight:700;
  border:1px solid rgba(59,130,246,.35);
}}
.card-body{{padding:1rem 1.4rem;display:flex;flex-direction:column;gap:.45rem}}

/* ── ITEM ── */
.item{{
  padding:.6rem .9rem;border-radius:8px;
  font-size:1.05rem;font-weight:800;color:var(--text);word-break:break-all;
  background:rgba(17,24,39,.8);
  border-left:2.5px solid rgba(59,130,246,.5);
  transition:background .12s,border-color .12s,transform .08s;
  position:relative;overflow:visible;
}}
.item:hover{{
  background:rgba(59,130,246,.12);
  border-left-color:var(--blue-hi);
}}
.item.copyable{{cursor:pointer;user-select:none}}
.item.copyable:active{{
  transform:scale(.96);
  background:rgba(59,130,246,.22);
  border-left-color:#fff;
}}
/* highlight on click — без overflow:hidden, без ripple-псевдоэлемента */
.item.copied{{
  background:rgba(74,222,128,.16) !important;
  border-left-color:#4ade80 !important;
  color:#86efac;
}}
.item.copyable[title]:hover::before{{
  content:attr(title);
  position:fixed;
  background:#0d1120;border:1px solid rgba(59,130,246,.7);color:var(--blue-hi);
  font-size:.85rem;font-weight:700;padding:.4rem .8rem;border-radius:8px;white-space:nowrap;
  z-index:9999;pointer-events:none;max-width:400px;
  box-shadow:0 6px 24px rgba(0,0,0,.8),0 0 0 1px rgba(59,130,246,.2);
  left:var(--mx,50%);top:var(--my,50%);transform:translate(-50%,-130%);
}}
.inc-item{{flex-direction:column;gap:.3rem;padding:.65rem .9rem}}
.inc-title{{font-weight:800;color:#fb923c;font-size:1rem}}
.inc-field{{display:block;font-size:.88rem;font-weight:700;color:var(--muted);padding-left:.5rem}}

/* ── DB BLOCKS ── */
.db-block{{
  background:linear-gradient(145deg,#0f1829 0%,#0d1120 100%);
  border:1px solid rgba(59,130,246,.22);
  border-radius:18px;margin-bottom:1.5rem;overflow:hidden;
  scroll-margin-top:1.5rem;transition:box-shadow .22s,border-color .22s;
}}
.db-block:hover{{
  box-shadow:0 8px 36px rgba(59,130,246,.18),0 0 0 1px rgba(124,185,255,.4);
  border-color:rgba(124,185,255,.45);
}}
.db-header{{
  display:flex;align-items:center;justify-content:space-between;
  padding:1.1rem 1.5rem;border-bottom:1px solid rgba(59,130,246,.15);
  background:linear-gradient(90deg,rgba(59,130,246,.16) 0%,transparent 65%);
}}
.db-left{{display:flex;flex-direction:column;gap:.3rem}}
.db-type-badge{{font-size:.65rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--blue-hi);opacity:.75}}
.db-title{{
  font-family:'Times New Roman',Times,serif;font-size:.95rem;font-weight:900;
  color:var(--blue-hi);letter-spacing:.02em;
  text-shadow:0 0 20px rgba(59,130,246,.5);
}}
.db-desc{{font-size:.75rem;line-height:1.5;color:#8aadcc;max-width:640px}}
.db-count-pill{{
  background:rgba(59,130,246,.2);color:var(--blue-hi);
  border-radius:20px;padding:.18rem .7rem;font-size:.72rem;font-weight:700;
  border:1px solid rgba(59,130,246,.35);white-space:nowrap;
}}
.db-body{{padding:1.1rem 1.3rem;display:flex;flex-direction:column;gap:1.1rem}}
.record{{background:rgba(17,24,39,.7);border-radius:12px;overflow:hidden;border:1px solid var(--border)}}
.rec-header{{
  display:flex;align-items:center;gap:.6rem;
  padding:.6rem .95rem;background:rgba(26,34,54,.9);border-bottom:1px solid var(--border);
}}
.rec-num{{
  width:20px;height:20px;border-radius:50%;font-size:.65rem;font-weight:700;
  display:flex;align-items:center;justify-content:center;
  background:rgba(59,130,246,.25);color:var(--blue-hi);flex-shrink:0;
}}
.cells-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:1px;background:var(--border)}}
.cell{{
  display:flex;align-items:flex-start;gap:.6rem;
  padding:.7rem .9rem;background:rgba(17,24,39,.8);
  transition:background .12s,transform .08s;cursor:pointer;
  position:relative;overflow:visible;
}}
.cell:hover{{background:rgba(59,130,246,.1)}}
.cell.copyable:active{{
  background:rgba(59,130,246,.2);
  transform:scale(.98);
}}
/* ripple на ячейках */
.cell.copyable::after{{
  content:'';position:absolute;left:50%;top:50%;
  width:0;height:0;border-radius:50%;
  background:rgba(124,185,255,.35);
  transform:translate(-50%,-50%);opacity:0;pointer-events:none;transition:none;
}}
.cell.copyable.rippling::after{{
  width:250px;height:250px;opacity:0;
  transition:width .45s ease,height .45s ease,opacity .55s ease;
}}
.cell.copied{{background:rgba(74,222,128,.1) !important}}
.cell.copied .cell-value{{color:#86efac}}
/* tooltip на ячейках детализации */
.cell.copyable[title]:hover::before{{
  content:attr(title);
  position:fixed;
  background:#0d1120;border:1px solid rgba(59,130,246,.7);color:var(--blue-hi);
  font-size:.85rem;font-weight:700;padding:.4rem .8rem;border-radius:8px;white-space:nowrap;
  z-index:9999;pointer-events:none;max-width:400px;
  box-shadow:0 6px 24px rgba(0,0,0,.8),0 0 0 1px rgba(59,130,246,.2);
  left:var(--mx,50%);top:var(--my,50%);transform:translate(-50%,-130%);
}}
.cell-icon{{font-size:.9rem;flex-shrink:0;margin-top:.05rem}}
.cell-body{{display:flex;flex-direction:column;gap:.18rem;min-width:0}}
.cell-label{{font-size:.65rem;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}}
.cell-value{{font-size:1rem;font-weight:800;color:var(--text);word-break:break-all;line-height:1.45}}
.cell-plain{{
  padding:.4rem .7rem;background:rgba(17,24,39,.7);font-size:.72rem;color:var(--dim);
  border-top:1px solid var(--border);grid-column:1/-1;
}}

/* ── FOOTER ── */
.footer{{
  margin-top:3rem;text-align:center;font-size:.65rem;color:var(--muted);
  padding-top:1rem;border-top:1px solid var(--border);padding-bottom:2.5rem;
}}

/* stagger */
.card:nth-child(1){{animation-delay:.04s}}.card:nth-child(2){{animation-delay:.09s}}
.card:nth-child(3){{animation-delay:.14s}}.card:nth-child(4){{animation-delay:.19s}}
.card:nth-child(5){{animation-delay:.24s}}.card:nth-child(n+6){{animation-delay:.29s}}

@media(max-width:680px){{
  .sidebar{{display:none}}
  .main{{padding:1rem .9rem}}
  .header-query{{font-size:1.2rem}}
}}
</style>
</head>
<body>
<div class="page-wrap">

  <nav class="sidebar">
    <div class="sidebar-title">Базы данных</div>
    <hr class="nav-divider">
    {sidebar_btns}
  </nav>

  <div class="main"><div class="wrapper">

    <div class="header">
      <div class="header-label">📂 Отчёт по поиску</div>
      <div class="header-query">{_esc2(query)}</div>
      <div class="header-meta">
        <div class="meta-badge">🕐 <strong>{now_str}</strong></div>
        <div class="meta-badge">📦 Источников: <strong>{results_count}</strong></div>
        <div class="meta-badge">🔢 Значений: <strong>{total_fields}</strong></div>
      </div>
    </div>

    <div class="section-label">
      <div class="section-label-text">📊 Сводка</div>
      <div class="section-label-line"></div>
    </div>
    <div class="grid">{cards_html}</div>

    <div class="section-label" style="margin-top:3.5rem">
      <div class="section-label-text">🗄️ Детализация по базам</div>
      <div class="section-label-line"></div>
    </div>
    {detail_blocks_html}

    <div class="footer">Сгенерировано автоматически · {now_str}</div>
  </div></div>
</div>

<script>
/* mouse-position для tooltip */
document.addEventListener('mousemove', function(e) {{
  document.documentElement.style.setProperty('--mx', e.clientX + 'px');
  document.documentElement.style.setProperty('--my', e.clientY + 'px');
}});

/* ripple helper */
function ripple(el) {{
  el.classList.remove('rippling');
  void el.offsetWidth;
  el.classList.add('rippling');
  setTimeout(function(){{ el.classList.remove('rippling'); }}, 650);
}}

/* nav buttons — scroll + ripple */
document.querySelectorAll('.nav-btn').forEach(function(btn) {{
  btn.addEventListener('click', function() {{
    ripple(this);
    var target = this.getAttribute('data-target');
    if (target) {{
      var el = document.getElementById(target);
      if (el) el.scrollIntoView({{behavior:'smooth',block:'start'}});
    }}
  }});
}});

/* copy items + cells */
function copyText(el) {{
  ripple(el);
  var val = el.getAttribute('data-val') || el.innerText || '';
  if (!val) return;
  if (navigator.clipboard && navigator.clipboard.writeText) {{
    navigator.clipboard.writeText(val)
      .then(function(){{ showCopied(el); }})
      .catch(function(){{ fallbackCopy(val, el); }});
  }} else {{ fallbackCopy(val, el); }}
}}
function fallbackCopy(text, el) {{
  var ta = document.createElement('textarea');
  ta.value = text; ta.style.position='fixed'; ta.style.opacity='0';
  document.body.appendChild(ta); ta.focus(); ta.select();
  try {{ document.execCommand('copy'); showCopied(el); }} catch(e) {{}}
  document.body.removeChild(ta);
}}
function showCopied(el) {{
  el.classList.add('copied');
  setTimeout(function(){{ el.classList.remove('copied'); }}, 1600);
}}
</script>
</body>
</html>'''
                return html

            safe_q = re.sub(r"[^\w\-]", "_", query)[:30]
            html_content = build_html_summary(
                query=query,
                results_count=len(results),
                collected=collected,
                fio_with_bday=fio_with_bday,
                section_titles=section_titles,
                sort_phones=sort_phones,
            )
            file_bytes = io.BytesIO(html_content.encode("utf-8"))
            file_bytes.name = f"summary_{safe_q}.html"
            bot.send_document(
                call.message.chat.id,
                file_bytes,
                caption=f"🗂 Сводка по запросу: <b>{query}</b>\n\n🌐 Откройте файл в браузере для красивого отображения",
                parse_mode="HTML",
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[smart_summary] Ошибка: {e}")
            bot.send_message(call.message.chat.id, "❌ Не удалось сформировать сводку.")
        return
    
    if call.data == "menu_support":
        bot.answer_callback_query(call.id)
        is_banned, _ = check_banned(user_id)
        if is_banned:
            _edit_to_content(call, "🚫 Вы забанены и не можете писать в поддержку!", parse_mode=None)
            return
        user_support_request[user_id] = True
        _edit_to_content(
            call,
            "📝 *СЛУЖБА ПОДДЕРЖКИ*\n\nНапишите ваш вопрос следующим сообщением. Администратор ответит в ближайшее время.\n\nОтправьте /cancel для отмены.",
        )
        notify_admins_support(user_id, call.from_user.first_name or "Пользователь")
        return

    # ── Кастомная кнопка меню ───────────────────────────────────────────────
    if call.data == "menu_custom":
        bot.answer_callback_query(call.id)
        cb_text, cb_photo = get_bot_text_full("custom_button")
        if cb_photo:
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass
            back_markup = types.InlineKeyboardMarkup()
            back_markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="menu_profile"))
            bot.send_photo(
                call.message.chat.id,
                cb_photo,
                caption=(cb_text or "")[:1024],
                reply_markup=back_markup,
                parse_mode="HTML",
            )
        else:
            _edit_to_content(call, cb_text or "—", parse_mode="HTML")
        return

    if call.data == "menu_profile":
        bot.answer_callback_query(call.id)
        if user_id in waiting_for_search:
            waiting_for_search[user_id] = False
        # Просто редактируем текущее сообщение обратно в inline-меню
        try:
            bot.edit_message_text(
                "👇 Выберите действие:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=inline_main_menu(),
            )
        except:
            bot.send_message(
                call.message.chat.id,
                "👇 Выберите действие:",
                reply_markup=inline_main_menu(),
            )
        return

    if call.data in ["sub_1day", "sub_7days", "sub_30days"]:
        bot.answer_callback_query(call.id)
        if call.data == "sub_1day":
            update_subscription_status(user_id, "active", "1day", 1)
            bot.send_message(
                call.message.chat.id,
                "✅ Подписка на 1 день активирована!",
                reply_markup=inline_main_menu(),
            )
            return
        if call.data == "sub_7days":
            update_subscription_status(user_id, "active", "7days", 7)
            bot.send_message(
                call.message.chat.id,
                "✅ Подписка на 7 дней активирована!",
                reply_markup=inline_main_menu(),
            )
            return
        update_subscription_status(user_id, "active", "30days", 30)
        bot.send_message(
            call.message.chat.id,
            "✅ Подписка на 30 дней активирована!",
            reply_markup=inline_main_menu(),
        )
        return

    # Проверка прав для админ-функций
    has_permission, _, _ = check_admin_permission(user_id)
    if (
        user_id != MAIN_ADMIN_ID
        and not has_permission
        and not call.data.startswith("reply_to_")
        and call.data != "close_support_list"
    ):
        bot.answer_callback_query(call.id, "⛔ Доступ запрещен")
        return

    data = call.data

    if data.startswith("secret_toggle_"):
        try:
            record_id = int(data.replace("secret_toggle_", ""))
        except ValueError:
            bot.answer_callback_query(call.id, "❌ Некорректный идентификатор", show_alert=True)
            return

        current_state = get_search_data_secret(record_id)
        if current_state is None:
            bot.answer_callback_query(call.id, "❌ Запись не найдена", show_alert=True)
            return

        new_state = not current_state
        secreted_by = call.from_user.id if new_state else None
        secreted_query = last_search_query.get(call.from_user.id) if new_state else None
        if not set_search_data_secret(record_id, new_state, secreted_by=secreted_by, secreted_query=secreted_query):
            bot.answer_callback_query(call.id, "❌ Не удалось обновить запись", show_alert=True)
            return

        # Определяем источник для тоста
        db_source_label = "локальная БД"
        if user_id in user_search_results:
            for i, res in enumerate(user_search_results[user_id]["results"]):
                if res[2] == record_id:
                    db_source_label = res[4] if len(res) >= 5 else "локальная БД"
                    lst = list(res)
                    lst[3] = new_state
                    user_search_results[user_id]["results"][i] = tuple(lst)
                    break

        if new_state:
            toast = f"🔒 [{db_source_label}] скрыто\n📌 Только эта запись"
        else:
            toast = f"🔓 [{db_source_label}] рассекречено\n📌 Только эта запись"
        try:
            bot.answer_callback_query(call.id, toast, show_alert=True)
        except Exception as e:
            print(f"[secret_toggle] answer_callback_query: {e}")
        # Перерисовываем текущую страницу со всеми кнопками
        current_page = user_search_results[user_id]["current_page"] if user_id in user_search_results else 0
        show_search_result_page(call.message.chat.id, user_id, current_page, call.message.message_id)
        return

    # ========== ЗАСЕКРЕЧИВАНИЕ API-РЕЗУЛЬТАТОВ ==========
    if data.startswith("secret_api_"):
        api_hash = data[len("secret_api_"):]
        current_secret = is_api_result_secret(api_hash)
        new_state = not current_secret

        api_source = "API"
        if user_id in user_search_results:
            for res in user_search_results[user_id]["results"]:
                if len(res) >= 5 and isinstance(res[2], str) and res[2] == f"api:{api_hash}":
                    api_source = res[4]
                    break

        secreted_query = last_search_query.get(user_id)
        ok = set_api_result_secret(
            api_hash, api_source, new_state,
            secreted_by=call.from_user.id if new_state else None,
            secreted_query=secreted_query if new_state else None,
        )
        if not ok:
            bot.answer_callback_query(call.id, "❌ Не удалось обновить запись", show_alert=True)
            return

        # Обновляем is_secret в хранилище результатов
        if user_id in user_search_results:
            for i, res in enumerate(user_search_results[user_id]["results"]):
                if len(res) >= 5 and isinstance(res[2], str) and res[2] == f"api:{api_hash}":
                    lst = list(res)
                    lst[3] = new_state
                    user_search_results[user_id]["results"][i] = tuple(lst)
                    break

        if new_state:
            toast = f"🔒 [{api_source}] скрыто\n📌 Только эта запись"
        else:
            toast = f"🔓 [{api_source}] рассекречено\n📌 Только эта запись"
        try:
            bot.answer_callback_query(call.id, toast, show_alert=True)
        except Exception as e:
            print(f"[secret_api] answer_callback_query: {e}")
        # Перерисовываем текущую страницу со всеми кнопками
        current_page = user_search_results[user_id]["current_page"] if user_id in user_search_results else 0
        show_search_result_page(call.message.chat.id, user_id, current_page, call.message.message_id)
        return

    # ========== ЗАСЕКРЕЧИВАНИЕ/РАССЕКРЕЧИВАНИЕ ВСЕХ РЕЗУЛЬТАТОВ ПО ЗАПРОСУ ==========
    if data.startswith("secret_all_"):
        try:
            target_uid = int(data.replace("secret_all_", ""))
        except ValueError:
            try:
                bot.answer_callback_query(call.id, "❌ Ошибка")
            except:
                pass
            return

        if target_uid not in user_search_results:
            try:
                bot.answer_callback_query(call.id, "❌ Результаты поиска устарели")
            except:
                pass
            return

        search_data = user_search_results[target_uid]
        results = search_data["results"]
        secreted_query = last_search_query.get(user_id) or search_data.get("query", "")

        # Определяем режим: если все уже засекречены — рассекречиваем, иначе — засекречиваем
        all_secret = bool(results) and all(r[3] for r in results if r[2] is not None)
        new_state = not all_secret  # True = засекретить, False = рассекретить

        # --- 1. Быстро обрабатываем уже загруженные результаты (из кэша, без сетевых запросов) ---
        count = 0
        for i, res in enumerate(results):
            text_info, photo_path, rec_id, is_secret, db_source = res
            if rec_id is None:
                continue
            if is_secret == new_state:
                continue

            if isinstance(rec_id, str) and rec_id.startswith("api:"):
                api_hash = rec_id[4:]
                ok = set_api_result_secret(
                    api_hash, db_source, new_state,
                    secreted_by=call.from_user.id if new_state else None,
                    secreted_query=secreted_query if new_state else None,
                )
            else:
                ok = set_search_data_secret(
                    rec_id, new_state,
                    secreted_by=call.from_user.id if new_state else None,
                    secreted_query=secreted_query if new_state else None,
                )
            if ok:
                lst = list(res)
                lst[3] = new_state
                results[i] = tuple(lst)
                count += 1

        # --- НЕМЕДЛЕННО отвечаем на callback (до истечения 5-сек лимита Telegram) ---
        action_emoji = "🔒" if new_state else "🔓"
        action_word = "Скрыто" if new_state else "Рассекречено"
        try:
            bot.answer_callback_query(
                call.id,
                f"{action_emoji} {action_word} {count} записей из кэша\n⏳ Проверяю все API в фоне...",
            )
        except Exception as e:
            print(f"[secret_all] answer_callback_query: {e}")

        # Перерисовываем страницу сразу (с уже обновлёнными кэш-записями)
        current_page = search_data.get("current_page", 0)
        try:
            show_search_result_page(call.message.chat.id, target_uid, current_page, call.message.message_id)
        except Exception as e:
            print(f"[secret_all] show_search_result_page: {e}")

        # --- 2. Тяжёлые сетевые запросы — в фоновом потоке, не блокируем polling ---
        if new_state and secreted_query:
            def _background_secret(chat_id, secreted_query, admin_id):
                extra_count = 0
                # LeakOSINT
                try:
                    extra_leak = api_search(secreted_query, limit=10000, lang=API_DEFAULT_LANG)
                    for api_text, _, _, _ in extra_leak:
                        h = _make_api_hash(api_text)
                        if not is_api_result_secret(h):
                            set_api_result_secret(h, "LEAKOSINT", True,
                                                  secreted_by=admin_id,
                                                  secreted_query=secreted_query)
                            extra_count += 1
                except Exception as e:
                    print(f"[secret_all bg LeakOSINT] {e}")

                # BigBase
                try:
                    extra_bb = bigbase_search(secreted_query)
                    for bb_text, _, _, _ in extra_bb:
                        h = _make_api_hash(bb_text)
                        if not is_api_result_secret(h):
                            set_api_result_secret(h, "BIGBASE", True,
                                                  secreted_by=admin_id,
                                                  secreted_query=secreted_query)
                            extra_count += 1
                except Exception as e:
                    print(f"[secret_all bg BigBase] {e}")

                # Локальная БД
                try:
                    all_hits = sqlite_search(secreted_query, include_secret=True)
                    for rec_id_local, _, _, _, is_sec_local in all_hits:
                        if not is_sec_local:
                            set_search_data_secret(rec_id_local, True,
                                                   secreted_by=admin_id,
                                                   secreted_query=secreted_query)
                            extra_count += 1
                except Exception as e:
                    print(f"[secret_all bg SQLite] {e}")

                if extra_count > 0:
                    try:
                        bot.send_message(
                            chat_id,
                            f"🔒 Фоновая проверка завершена: скрыто ещё {extra_count} записей по всем API.",
                        )
                    except Exception as e:
                        print(f"[secret_all bg notify] {e}")
                else:
                    try:
                        bot.send_message(
                            chat_id,
                            "✅ Фоновая проверка завершена: новых незасекреченных записей не найдено.",
                        )
                    except Exception as e:
                        print(f"[secret_all bg notify] {e}")

            threading.Thread(
                target=_background_secret,
                args=(call.message.chat.id, secreted_query, call.from_user.id),
                daemon=True,
            ).start()

        return

    # ========== УБРАТЬ ВСЁ СКРЫТОЕ: запрос подтверждения ==========
    if data == "secret_clear_all_ask":
        if not has_perm(user_id, "secret"):
            bot.answer_callback_query(call.id, "⛔ Нет прав!", show_alert=True)
            return
        rows = get_secret_search_records(limit=1000)
        api_rows = get_all_api_secrets(limit=1000)
        total = len(rows) + len(api_rows)
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("✅ Да, убрать всё", callback_data="secret_clear_all_confirm"),
            types.InlineKeyboardButton("❌ Отмена", callback_data="secret_clear_all_cancel"),
        )
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except:
            pass
        bot.send_message(
            call.message.chat.id,
            f"⚠️ Вы точно хотите рассекретить все {total} записей?\n\n"
            f"📁 Из локальной базы: {len(rows)}\n"
            f"🌐 Из внешних API: {len(api_rows)}\n\n"
            "Это действие нельзя отменить.",
            reply_markup=markup,
        )
        bot.answer_callback_query(call.id)
        return

    # ========== УБРАТЬ ВСЁ СКРЫТОЕ: подтверждено ==========
    if data == "secret_clear_all_confirm":
        if not has_perm(user_id, "secret"):
            bot.answer_callback_query(call.id, "⛔ Нет прав!", show_alert=True)
            return
        try:
            conn = sqlite3.connect("users.db")
            cursor = conn.cursor()
            # Сбрасываем is_secret в локальной базе
            cursor.execute(
                "UPDATE search_data SET is_secret = 0, secreted_by = NULL, secreted_query = NULL WHERE is_secret = 1"
            )
            local_count = cursor.rowcount
            # Удаляем все записи из api_secret_results
            cursor.execute("DELETE FROM api_secret_results")
            api_count = cursor.rowcount
            conn.commit()
            conn.close()
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            except:
                pass
            bot.send_message(
                call.message.chat.id,
                f"✅ Готово! Всё скрытое рассекречено.\n\n"
                f"📁 Локальных записей: {local_count}\n"
                f"🌐 API-записей: {api_count}",
            )
            bot.answer_callback_query(call.id, "✅ Всё рассекречено!")
        except Exception as e:
            bot.answer_callback_query(call.id, f"❌ Ошибка: {e}", show_alert=True)
        return

    # ========== УБРАТЬ ВСЁ СКРЫТОЕ: отмена ==========
    if data == "secret_clear_all_cancel":
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            bot.send_message(call.message.chat.id, "❌ Отменено. Записи остались скрытыми.")
        except:
            pass
        bot.answer_callback_query(call.id, "Отменено")
        return

    if data.startswith("reply_to_") or data.startswith("start_livechat_"):
        if not has_perm(user_id, "support"):
            bot.answer_callback_query(
                call.id, "⛔ Нет прав для поддержки", show_alert=True
            )
            return
        prefix = "reply_to_" if data.startswith("reply_to_") else "start_livechat_"
        target_id = int(data.replace(prefix, ""))

        # Открываем двусторонний живой чат
        support_mode[user_id] = target_id
        live_chat[target_id] = user_id

        # Удаляем пользователя из ожидающих
        if target_id in user_support_request:
            del user_support_request[target_id]

        bot.answer_callback_query(call.id, f"✅ Чат открыт с пользователем {target_id}")
        try:
            bot.edit_message_text(
                f"✅ Живой чат открыт с пользователем `{target_id}`\n"
                f"Пишите сообщения — они будут пересылаться напрямую.\n"
                f"Для завершения диалога отправьте /endchat",
                call.message.chat.id,
                call.message.message_id,
                parse_mode="Markdown",
            )
        except:
            bot.send_message(
                user_id,
                f"✅ Живой чат открыт с пользователем `{target_id}`\n"
                f"Пишите сообщения — они пересылаются напрямую.\n"
                f"Для завершения: /endchat",
                parse_mode="Markdown",
            )
        try:
            bot.send_message(
                target_id,
                "✅ Администратор подключился. Можете общаться!\n"
                "Для завершения диалога отправьте /endchat",
            )
        except:
            pass
        return

    if data == "close_support_list":
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id, "Список закрыт")
        return

    # Статистика
    elif data == "stats_general" or data.startswith("stats_general_period_"):
        if not has_perm(user_id, "stats"):
            bot.answer_callback_query(
                call.id, "⛔ Нет прав для просмотра статистики", show_alert=True
            )
            return

        # Определяем период
        period = "all"
        if data.startswith("stats_general_period_"):
            period = data.replace("stats_general_period_", "")

        period_labels = {
            "1d":  "📅 1 день",
            "7d":  "📅 7 дней",
            "30d": "📅 30 дней",
            "1y":  "📅 1 год",
            "all": "📅 Всё время",
        }
        period_sql = {
            "1d":  "datetime('now', '-1 day')",
            "7d":  "datetime('now', '-7 days')",
            "30d": "datetime('now', '-30 days')",
            "1y":  "datetime('now', '-365 days')",
            "all": None,
        }

        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()

        # --- Пользователи (всегда total, без фильтра периода) ---
        total_users = cursor.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active_subs = cursor.execute(
            'SELECT COUNT(*) FROM users WHERE subscription_status IN ("active", "forever")'
        ).fetchone()[0]
        forever_subs = cursor.execute(
            'SELECT COUNT(*) FROM users WHERE subscription_status = "forever"'
        ).fetchone()[0]
        banned_users = cursor.execute(
            "SELECT COUNT(*) FROM users WHERE banned = 1"
        ).fetchone()[0]

        # --- Поиски за период ---
        since_sql = period_sql[period]
        if since_sql:
            where = f"WHERE search_time >= {since_sql}"
            where_fail = f"WHERE search_time >= {since_sql} AND found = 0"
            where_ok   = f"WHERE search_time >= {since_sql} AND found = 1"
        else:
            where = ""
            where_fail = "WHERE found = 0"
            where_ok   = "WHERE found = 1"

        total_searches = cursor.execute(f"SELECT COUNT(*) FROM search_history {where}").fetchone()[0]
        ok_searches    = cursor.execute(f"SELECT COUNT(*) FROM search_history {where_ok}").fetchone()[0]
        fail_searches  = cursor.execute(f"SELECT COUNT(*) FROM search_history {where_fail}").fetchone()[0]
        unique_users   = cursor.execute(f"SELECT COUNT(DISTINCT user_id) FROM search_history {where}").fetchone()[0]

        # --- Неудачные по источнику API ---
        if since_sql:
            api_fail_rows = cursor.execute(
                f"SELECT COALESCE(api_source,'sqlite'), COUNT(*) FROM search_history "
                f"WHERE search_time >= {since_sql} AND found = 0 "
                f"GROUP BY api_source ORDER BY COUNT(*) DESC"
            ).fetchall()
        else:
            api_fail_rows = cursor.execute(
                "SELECT COALESCE(api_source,'sqlite'), COUNT(*) FROM search_history "
                "WHERE found = 0 "
                "GROUP BY api_source ORDER BY COUNT(*) DESC"
            ).fetchall()

        # --- Топ-3 пользователя с неудачными запросами за период ---
        if since_sql:
            top_fail_users = cursor.execute(
                f"SELECT sh.user_id, COALESCE(u.username, u.first_name, 'ID '||sh.user_id), COUNT(*) as cnt "
                f"FROM search_history sh LEFT JOIN users u ON sh.user_id = u.user_id "
                f"WHERE sh.search_time >= {since_sql} AND sh.found = 0 "
                f"GROUP BY sh.user_id ORDER BY cnt DESC LIMIT 3"
            ).fetchall()
        else:
            top_fail_users = cursor.execute(
                "SELECT sh.user_id, COALESCE(u.username, u.first_name, 'ID '||sh.user_id), COUNT(*) as cnt "
                "FROM search_history sh LEFT JOIN users u ON sh.user_id = u.user_id "
                "WHERE sh.found = 0 "
                "GROUP BY sh.user_id ORDER BY cnt DESC LIMIT 3"
            ).fetchall()

        # --- Последние 3 неудачных запроса ---
        if since_sql:
            last_fails = cursor.execute(
                f"SELECT sh.user_id, COALESCE(u.username, u.first_name, 'ID '||sh.user_id), "
                f"sh.query, COALESCE(sh.api_source,'sqlite'), sh.search_time "
                f"FROM search_history sh LEFT JOIN users u ON sh.user_id = u.user_id "
                f"WHERE sh.search_time >= {since_sql} AND sh.found = 0 "
                f"ORDER BY sh.search_time DESC LIMIT 3"
            ).fetchall()
        else:
            last_fails = cursor.execute(
                "SELECT sh.user_id, COALESCE(u.username, u.first_name, 'ID '||sh.user_id), "
                "sh.query, COALESCE(sh.api_source,'sqlite'), sh.search_time "
                "FROM search_history sh LEFT JOIN users u ON sh.user_id = u.user_id "
                "WHERE sh.found = 0 "
                "ORDER BY sh.search_time DESC LIMIT 3"
            ).fetchall()

        conn.close()

        # --- Формируем текст ---
        pct_fail = round(fail_searches / total_searches * 100) if total_searches else 0
        pct_ok   = 100 - pct_fail

        api_fail_lines = ""
        api_icons = {"leakosint": "🌍", "bigbase": "📊", "all": "🔀", "sqlite": "📁"}
        for src, cnt in api_fail_rows:
            icon = api_icons.get(src.lower(), "▪️")
            api_fail_lines += f"\n║   {icon} {src.upper()}: {cnt}"

        top_fail_lines = ""
        for uid, name, cnt in top_fail_users:
            top_fail_lines += f"\n║   👤 @{name} (ID:{uid}) — {cnt} раз"

        last_fail_lines = ""
        for uid, name, q, src, ts in last_fails:
            short_q = q[:20] + "…" if len(q) > 20 else q
            short_ts = ts[5:16] if ts else "—"
            last_fail_lines += f"\n║   {short_ts} | {src.upper()} | @{name}\n║   🔍 {short_q}"

        period_label = period_labels.get(period, "Всё время")
        text = (
            f"📊 ОБЩАЯ СТАТИСТИКА — {period_label}\n"
            f"╔════════════════════════════╗\n"
            f"║ 👥 Всего пользователей: {total_users}\n"
            f"║ 💎 Активных подписок: {active_subs}\n"
            f"║ ♾️  Вечных подписок: {forever_subs}\n"
            f"║ 🚫 Забанено: {banned_users}\n"
            f"╠════════════════════════════╣\n"
            f"║ 🔍 ПОИСКИ ЗА ПЕРИОД\n"
            f"║ Всего запросов: {total_searches}\n"
            f"║ Уникальных юзеров: {unique_users}\n"
            f"║ ✅ Найдено: {ok_searches} ({pct_ok}%)\n"
            f"║ ❌ Не найдено: {fail_searches} ({pct_fail}%)\n"
            f"╠════════════════════════════╣\n"
            f"║ ❌ НЕУДАЧНЫЕ ПО ИСТОЧНИКУ"
            f"{api_fail_lines if api_fail_lines else chr(10)+'║   нет данных'}\n"
        )
        if top_fail_lines:
            text += (
                f"╠════════════════════════════╣\n"
                f"║ 🏴 ТОП ЮЗЕРОВ С ПРОВАЛАМИ"
                f"{top_fail_lines}\n"
            )
        if last_fail_lines:
            text += (
                f"╠════════════════════════════╣\n"
                f"║ 🕐 ПОСЛЕДНИЕ НЕУДАЧИ"
                f"{last_fail_lines}\n"
            )
        text += "╚════════════════════════════╝"

        # Кнопки периодов
        markup = types.InlineKeyboardMarkup(row_width=4)
        periods = [("1д", "1d"), ("7д", "7d"), ("30д", "30d"), ("1г", "1y")]
        period_btns = [
            types.InlineKeyboardButton(
                f"{'▶' if period == p else ''}{label}",
                callback_data=f"stats_general_period_{p}"
            )
            for label, p in periods
        ]
        markup.add(*period_btns)
        markup.add(
            types.InlineKeyboardButton("📅 Всё время", callback_data="stats_general_period_all"),
            types.InlineKeyboardButton("◀️ Назад", callback_data="stats_back"),
        )

        try:
            bot.edit_message_text(
                text, call.message.chat.id, call.message.message_id, reply_markup=markup
            )
        except Exception as e:
            print(f"[stats_general] edit error: {e}")
        return

    elif data == "stats_showdb":
        if not has_perm(user_id, "stats"):
            bot.answer_callback_query(
                call.id, "⛔ Нет прав для просмотра статистики", show_alert=True
            )
            return
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute(
            "SELECT user_id, username, first_name, last_name, subscription_status, subscription_type, subscription_start, subscription_end, created_at FROM users"
        )
        users = cursor.fetchall()

        if users:
            text = f"🗃️ БАЗА ДАННЫХ ПОЛЬЗОВАТЕЛЕЙ (всего: {len(users)})\n\nПоследние 5 записей:\n\n"
            for user in users[:5]:
                text += f"🆔 ID: {user[0]}\n"
                text += f"👤 Username: @{user[1] if user[1] else 'Нет'}\n"
                text += f"📝 Имя: {user[2] if user[2] else 'Нет'}\n"
                text += f"📌 Статус: {user[4]}\n"
                text += f"💎 Тип: {user[5] if user[5] else 'Нет'}\n"
                text += f"📅 Начало: {user[6] if user[6] else 'Нет'}\n"
                text += f"⏰ Конец: {user[7] if user[7] else 'Нет'}\n"
                text += "─" * 30 + "\n"

            if len(users) > 5:
                text += f"... и ещё {len(users) - 5} пользователей\n\n"
            text += "📥 Нажмите кнопку ниже чтобы скачать ПОЛНЫЙ список в Excel"
        else:
            text = "❌ База данных пуста"

        conn.close()

        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("📥 Скачать всю базу (Excel)", callback_data="stats_export"),
            types.InlineKeyboardButton("🔎 Найти пользователя",       callback_data="stats_searchuser"),
            types.InlineKeyboardButton("◀️ Назад",                    callback_data="stats_back"),
        )

        bot.edit_message_text(
            text, call.message.chat.id, call.message.message_id, reply_markup=markup
        )
        return

    elif data == "stats_allhistory":
        if not has_perm(user_id, "stats"):
            bot.answer_callback_query(
                call.id, "⛔ Нет прав для просмотра статистики", show_alert=True
            )
            return
        try:
            conn = sqlite3.connect("users.db")
            cursor = conn.cursor()

            total_searches = cursor.execute(
                "SELECT COUNT(*) FROM search_history"
            ).fetchone()[0]
            total_users_searched = cursor.execute(
                "SELECT COUNT(DISTINCT user_id) FROM search_history"
            ).fetchone()[0]
            found_count = cursor.execute(
                "SELECT COUNT(*) FROM search_history WHERE found = 1"
            ).fetchone()[0]
            not_found_count = cursor.execute(
                "SELECT COUNT(*) FROM search_history WHERE found = 0"
            ).fetchone()[0]

            cursor.execute("""
                SELECT s.user_id, u.username, u.first_name, s.query, s.found, s.search_time
                FROM search_history s
                LEFT JOIN users u ON s.user_id = u.user_id
                ORDER BY datetime(s.search_time) DESC LIMIT 5
            """)
            history = cursor.fetchall()
            conn.close()

            text = f"""🔍 **ОБЩАЯ СТАТИСТИКА ПОИСКА**
╔════════════════════════════╗
║ Всего запросов: {total_searches}
║ Пользователей искали: {total_users_searched}
║ ✅ Найдено: {found_count}
║ ❌ Не найдено: {not_found_count}
╚════════════════════════════╝

📜 **ПОСЛЕДНИЕ 5 ЗАПРОСОВ:**\n"""

            for row in history:
                uid, username, first_name, query, found, search_time = row
                status = "✅" if found else "❌"
                name = username or first_name or "Неизвестный"
                text += (
                    f"\n{status} `{query}`\n👤 @{name} (ID: {uid})\n🕐 {search_time}\n"
                )

        except Exception as e:
            text = f"❌ Ошибка: {e}"

        markup = types.InlineKeyboardMarkup()
        btn_back = types.InlineKeyboardButton("◀️ Назад", callback_data="stats_back")
        markup.add(btn_back)

        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
        )
        return

    elif data == "stats_userhistory":
        if not has_perm(user_id, "stats"):
            bot.answer_callback_query(
                call.id, "⛔ Нет прав для просмотра статистики", show_alert=True
            )
            return
        admin_action[user_id] = {"action": "stats_userhistory_input"}
        bot.edit_message_text(
            "📌 **ИСТОРИЯ ПОЛЬЗОВАТЕЛЯ**\n\nВведите ID пользователя:",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="Markdown",
        )
        return

    elif data == "stats_back":
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("📊 Общая статистика",   callback_data="stats_general"),
            types.InlineKeyboardButton("🗃️ База данных",        callback_data="stats_showdb"),
        )
        markup.add(
            types.InlineKeyboardButton("🔍 Вся история",        callback_data="stats_allhistory"),
            types.InlineKeyboardButton("📌 История юзера",      callback_data="stats_userhistory"),
        )
        markup.add(
            types.InlineKeyboardButton("🏆 Топ запросов",       callback_data="stats_topqueries"),
            types.InlineKeyboardButton("🔥 Активные юзеры",     callback_data="stats_activeusers"),
        )
        markup.add(
            types.InlineKeyboardButton("🆕 Новые пользователи", callback_data="stats_newusers"),
            types.InlineKeyboardButton("📅 Активность сегодня", callback_data="stats_today"),
        )
        markup.add(
            types.InlineKeyboardButton("🔎 Найти пользователя", callback_data="stats_searchuser"),
            types.InlineKeyboardButton("📥 Экспорт базы",       callback_data="stats_export"),
        )
        markup.add(
            types.InlineKeyboardButton("📈 Полный отчёт (HTML)", callback_data="stats_fullreport"),
        )
        markup.add(types.InlineKeyboardButton("◀️ Назад", callback_data="stats_back"))

        try:
            bot.edit_message_text(
                "📊 **СТАТИСТИКА БОТА**\n\nВыберите раздел:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup,
                parse_mode="Markdown",
            )
        except:
            bot.answer_callback_query(call.id, "ℹ️ Вы уже в главном меню")
        return

    # ── НОВЫЕ ОБРАБОТЧИКИ СТАТИСТИКИ ────────────────────────────

    elif data == "stats_fullreport":
        if not has_perm(user_id, "stats"):
            bot.answer_callback_query(call.id, "⛔ Нет прав", show_alert=True)
            return
        bot.answer_callback_query(call.id, "⏳ Генерирую полный отчёт...")
        try:
            buf = generate_stats_html_report()
            bot.send_document(
                call.message.chat.id,
                buf,
                caption=(
                    f"📈 <b>Полный отчёт статистики</b>\n"
                    f"📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
                    f"Откройте файл в браузере — внутри интерактивные графики 🎯"
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            bot.send_message(call.message.chat.id, f"❌ Ошибка генерации отчёта: {e}")
        return

    elif data == "stats_export":
        if not has_perm(user_id, "stats"):
            bot.answer_callback_query(call.id, "⛔ Нет прав", show_alert=True)
            return
        bot.answer_callback_query(call.id, "⏳ Формирую файл...")
        try:
            if EXCEL_SUPPORT:
                buf = export_users_to_excel()
                bot.send_document(
                    call.message.chat.id,
                    buf,
                    caption=f"📥 Полный экспорт базы пользователей\n📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}",
                )
            else:
                buf = export_users_to_txt()
                bot.send_document(
                    call.message.chat.id,
                    buf,
                    caption=f"📥 Экспорт базы пользователей (txt)\n⚠️ Установите openpyxl для Excel-формата\n📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}",
                )
        except Exception as e:
            bot.send_message(call.message.chat.id, f"❌ Ошибка экспорта: {e}")
        return

    elif data == "stats_topqueries":
        if not has_perm(user_id, "stats"):
            bot.answer_callback_query(call.id, "⛔ Нет прав", show_alert=True)
            return
        rows = get_top_queries(20)
        if not rows:
            text = "📭 Нет истории поиска"
        else:
            text = "🏆 ТОП-20 ПОПУЛЯРНЫХ ЗАПРОСОВ\n\n"
            for i, (q, cnt) in enumerate(rows, 1):
                bar = "█" * min(cnt, 20)
                text += f"{i:2}. [{cnt:5}x] {bar}\n    {q}\n\n"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("◀️ Назад", callback_data="stats_back"))
        try:
            bot.edit_message_text(text[:4096], call.message.chat.id, call.message.message_id, reply_markup=markup)
        except:
            bot.send_message(call.message.chat.id, text[:4096], reply_markup=markup)
        return

    elif data == "stats_activeusers":
        if not has_perm(user_id, "stats"):
            bot.answer_callback_query(call.id, "⛔ Нет прав", show_alert=True)
            return
        rows = get_most_active_users(10)
        if not rows:
            text = "📭 Нет данных"
        else:
            text = "🔥 ТОП-10 АКТИВНЫХ ПОЛЬЗОВАТЕЛЕЙ\n\n"
            for i, (uid2, uname, fname, cnt) in enumerate(rows, 1):
                name = f"@{uname}" if uname else (fname or f"ID{uid2}")
                text += f"{i:2}. {name} — {cnt} запросов\n    🆔 {uid2}\n\n"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("◀️ Назад", callback_data="stats_back"))
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup)
        except:
            bot.send_message(call.message.chat.id, text, reply_markup=markup)
        return

    elif data == "stats_newusers":
        if not has_perm(user_id, "stats"):
            bot.answer_callback_query(call.id, "⛔ Нет прав", show_alert=True)
            return
        markup = types.InlineKeyboardMarkup(row_width=3)
        markup.add(
            types.InlineKeyboardButton("За 1 день",  callback_data="stats_newusers_1"),
            types.InlineKeyboardButton("За 7 дней",  callback_data="stats_newusers_7"),
            types.InlineKeyboardButton("За 30 дней", callback_data="stats_newusers_30"),
        )
        markup.add(types.InlineKeyboardButton("◀️ Назад", callback_data="stats_back"))
        try:
            bot.edit_message_text("🆕 Выберите период:", call.message.chat.id, call.message.message_id, reply_markup=markup)
        except:
            bot.send_message(call.message.chat.id, "🆕 Выберите период:", reply_markup=markup)
        return

    elif data.startswith("stats_newusers_"):
        if not has_perm(user_id, "stats"):
            bot.answer_callback_query(call.id, "⛔ Нет прав", show_alert=True)
            return
        days = int(data.split("_")[-1])
        rows = get_new_users_period(days)
        if not rows:
            text = f"📭 Новых пользователей за {days} дн. нет"
        else:
            text = f"🆕 НОВЫЕ ПОЛЬЗОВАТЕЛИ ЗА {days} ДНЕЙ ({len(rows)} чел.)\n\n"
            for uid2, uname, fname, status, created in rows[:30]:
                name = f"@{uname}" if uname else (fname or f"ID{uid2}")
                text += f"• {name} — {status}\n  🕐 {created}\n\n"
            if len(rows) > 30:
                text += f"... и ещё {len(rows)-30} пользователей"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("◀️ Назад", callback_data="stats_newusers"))
        try:
            bot.edit_message_text(text[:4096], call.message.chat.id, call.message.message_id, reply_markup=markup)
        except:
            bot.send_message(call.message.chat.id, text[:4096], reply_markup=markup)
        return

    elif data == "stats_today":
        if not has_perm(user_id, "stats"):
            bot.answer_callback_query(call.id, "⛔ Нет прав", show_alert=True)
            return
        s = get_activity_today()
        found_pct = round(s["found"] / s["searches"] * 100) if s["searches"] > 0 else 0
        text = (
            f"📅 АКТИВНОСТЬ СЕГОДНЯ\n"
            f"({datetime.now().strftime('%d.%m.%Y')})\n\n"
            f"╔════════════════════════╗\n"
            f"║ 🔍 Поисков:        {s['searches']:>5}\n"
            f"║ 👥 Уник. пользов.: {s['unique_users']:>5}\n"
            f"║ 🆕 Новых юзеров:   {s['new_users']:>5}\n"
            f"║ ✅ Найдено:    {s['found']:>5} ({found_pct}%)\n"
            f"╚════════════════════════╝"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("◀️ Назад", callback_data="stats_back"))
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup)
        except:
            bot.send_message(call.message.chat.id, text, reply_markup=markup)
        return

    elif data == "stats_searchuser":
        if not has_perm(user_id, "stats"):
            bot.answer_callback_query(call.id, "⛔ Нет прав", show_alert=True)
            return
        admin_action[user_id] = {"action": "stats_searchuser_input"}
        try:
            bot.edit_message_text(
                "🔎 ПОИСК ПОЛЬЗОВАТЕЛЯ\n\nВведите ID, @username или имя:",
                call.message.chat.id,
                call.message.message_id,
            )
        except:
            bot.send_message(call.message.chat.id, "🔎 Введите ID, @username или имя:")
        return

    elif data.startswith("stats_viewuserhistory_"):
        if not has_perm(user_id, "stats"):
            bot.answer_callback_query(call.id, "⛔ Нет прав", show_alert=True)
            return
        try:
            target_id = int(data.split("_")[-1])
            conn = sqlite3.connect("users.db")
            cursor = conn.cursor()
            history = get_user_history_with_first(cursor, target_id, include_first=True)
            conn.close()
            if not history:
                text = f"📭 У пользователя {target_id} нет истории"
            else:
                text = f"📜 ИСТОРИЯ ЗАПРОСОВ (ID {target_id})\n\n"
                for i, (_, query, found, search_time) in enumerate(history, 1):
                    status = "✅" if found else "❌"
                    text += f"{i}. {status} {query}\n   🕐 {search_time}\n\n"
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("◀️ Назад", callback_data="stats_back"))
            bot.send_message(call.message.chat.id, text[:4096], reply_markup=markup)
        except Exception as e:
            bot.answer_callback_query(call.id, f"❌ Ошибка: {e}", show_alert=True)
        return

    # ══════════════ ИСТОРИЯ ПОЛЬЗОВАТЕЛЯ: КНОПКИ УПРАВЛЕНИЯ ══════════════

    elif data.startswith("uh_trips_"):
        if not has_perm(user_id, "users") and user_id != MAIN_ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔ Нет прав", show_alert=True)
            return
        target_id = int(data.split("_")[-1])
        conn = sqlite3.connect("users.db")
        trips_now = conn.execute("SELECT COALESCE(trips,0) FROM users WHERE user_id=?", (target_id,)).fetchone()
        conn.close()
        trips_now = trips_now[0] if trips_now else 0
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("➕ 10",   callback_data=f"uh_tadd_{target_id}_10"),
            types.InlineKeyboardButton("➕ 50",   callback_data=f"uh_tadd_{target_id}_50"),
            types.InlineKeyboardButton("➕ 100",  callback_data=f"uh_tadd_{target_id}_100"),
            types.InlineKeyboardButton("➕ 500",  callback_data=f"uh_tadd_{target_id}_500"),
            types.InlineKeyboardButton("➖ 10",   callback_data=f"uh_tsub_{target_id}_10"),
            types.InlineKeyboardButton("➖ 50",   callback_data=f"uh_tsub_{target_id}_50"),
            types.InlineKeyboardButton("🗑 Обнулить", callback_data=f"uh_tzero_{target_id}"),
        )
        markup.add(types.InlineKeyboardButton("◀️ Назад", callback_data="stats_back"))
        bot.send_message(
            call.message.chat.id,
            f"🎫 *Управление трипами*\n\nID: `{target_id}`\nТекущий баланс: *{trips_now}* трипов\n\nВыберите действие:",
            parse_mode="Markdown",
            reply_markup=markup,
        )
        return

    elif data.startswith("uh_tadd_") or data.startswith("uh_tsub_") or data.startswith("uh_tzero_"):
        if not has_perm(user_id, "users") and user_id != MAIN_ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔ Нет прав", show_alert=True)
            return
        parts = data.split("_")
        if data.startswith("uh_tzero_"):
            target_id = int(parts[-1])
            conn = sqlite3.connect("users.db")
            conn.execute("UPDATE users SET trips=0 WHERE user_id=?", (target_id,))
            conn.commit()
            conn.close()
            bot.answer_callback_query(call.id, f"✅ Трипы обнулены для {target_id}", show_alert=True)
        else:
            action = parts[1]  # tadd / tsub
            target_id = int(parts[2])
            amount = int(parts[3])
            if action == "tsub":
                amount = -amount
            conn = sqlite3.connect("users.db")
            conn.execute("UPDATE users SET trips=MAX(0, COALESCE(trips,0)+?) WHERE user_id=?", (amount, target_id))
            new_bal = conn.execute("SELECT COALESCE(trips,0) FROM users WHERE user_id=?", (target_id,)).fetchone()[0]
            conn.commit()
            conn.close()
            sign = "+" if amount > 0 else ""
            bot.answer_callback_query(call.id, f"✅ {sign}{amount} трипов → баланс: {new_bal}", show_alert=True)
        return

    elif data.startswith("uh_sub_"):
        if not has_perm(user_id, "users") and user_id != MAIN_ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔ Нет прав", show_alert=True)
            return
        target_id = int(data.split("_")[-1])
        conn = sqlite3.connect("users.db")
        row = conn.execute(
            "SELECT subscription_status, subscription_type, subscription_end FROM users WHERE user_id=?", (target_id,)
        ).fetchone()
        conn.close()
        status_now = row[0] if row else "inactive"
        end_now = row[2] if row else "—"
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("📅 +1 день",    callback_data=f"uh_sgive_{target_id}_1"),
            types.InlineKeyboardButton("📅 +7 дней",    callback_data=f"uh_sgive_{target_id}_7"),
            types.InlineKeyboardButton("📅 +30 дней",   callback_data=f"uh_sgive_{target_id}_30"),
            types.InlineKeyboardButton("📅 +90 дней",   callback_data=f"uh_sgive_{target_id}_90"),
            types.InlineKeyboardButton("💎 Вечная",     callback_data=f"uh_sforever_{target_id}"),
            types.InlineKeyboardButton("❌ Отключить",  callback_data=f"uh_soff_{target_id}"),
        )
        markup.add(types.InlineKeyboardButton("◀️ Назад", callback_data="stats_back"))
        bot.send_message(
            call.message.chat.id,
            f"💳 *Управление подпиской*\n\nID: `{target_id}`\nСтатус: *{status_now}*\nДо: {end_now or '—'}\n\nВыберите действие:",
            parse_mode="Markdown",
            reply_markup=markup,
        )
        return

    elif data.startswith("uh_sgive_") or data.startswith("uh_sforever_") or data.startswith("uh_soff_"):
        if not has_perm(user_id, "users") and user_id != MAIN_ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔ Нет прав", show_alert=True)
            return
        parts = data.split("_")
        if data.startswith("uh_sgive_"):
            target_id = int(parts[2])
            days = int(parts[3])
            update_subscription_status(target_id, "active", "admin_gift", days)
            bot.answer_callback_query(call.id, f"✅ Подписка +{days} дн. выдана пользователю {target_id}", show_alert=True)
        elif data.startswith("uh_sforever_"):
            target_id = int(parts[2])
            update_subscription_status(target_id, "forever", "forever")
            bot.answer_callback_query(call.id, f"💎 Вечная подписка выдана пользователю {target_id}", show_alert=True)
        elif data.startswith("uh_soff_"):
            target_id = int(parts[2])
            update_subscription_status(target_id, "inactive")
            bot.answer_callback_query(call.id, f"❌ Подписка отключена для {target_id}", show_alert=True)
        return

    elif data.startswith("uh_ban_") or data.startswith("uh_unban_"):
        if not has_perm(user_id, "users") and user_id != MAIN_ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔ Нет прав", show_alert=True)
            return
        target_id = int(data.split("_")[-1])
        banning = data.startswith("uh_ban_")
        if banning:
            # Дополнительная защита от бана администраторов через callback
            if target_id == MAIN_ADMIN_ID:
                bot.answer_callback_query(call.id, "⛔ Нельзя забанить главного админа!", show_alert=True)
                return
            try:
                conn_chk = sqlite3.connect("users.db")
                adm_chk = conn_chk.cursor().execute(
                    "SELECT 1 FROM admin_permissions WHERE user_id = ? AND is_active = 1",
                    (target_id,)
                ).fetchone()
                conn_chk.close()
                if adm_chk:
                    bot.answer_callback_query(
                        call.id,
                        "⛔ Нельзя забанить администратора!\nСначала отзовите его права через «Управление админами».",
                        show_alert=True
                    )
                    return
            except Exception:
                pass
            success, result = ban_user(user_id, target_id, "Заблокирован через историю пользователя")
            alert_text = f"🚫 Пользователь {target_id} заблокирован" if success else f"❌ Ошибка: {result}"
        else:
            success, result = unban_user(user_id, target_id)
            alert_text = f"✅ Пользователь {target_id} разблокирован" if success else f"❌ Ошибка: {result}"

        bot.answer_callback_query(call.id, alert_text, show_alert=True)

        if success:
            # Обновляем кнопку: бан → разбан и наоборот
            try:
                is_ghost_active = ghost_mode.get(user_id) == target_id
                new_markup = types.InlineKeyboardMarkup(row_width=1)
                new_markup.add(
                    types.InlineKeyboardButton("🎫 Управление трипами",   callback_data=f"uh_trips_{target_id}"),
                    types.InlineKeyboardButton("💳 Управление подпиской", callback_data=f"uh_sub_{target_id}"),
                    types.InlineKeyboardButton(
                        "✅ Разблокировать" if banning else "🚫 Заблокировать",
                        callback_data=f"uh_unban_{target_id}" if banning else f"uh_ban_{target_id}"
                    ),
                    types.InlineKeyboardButton(
                        "👻 Призрак: выкл" if is_ghost_active else "👻 Призрак: вкл",
                        callback_data=f"uh_ghost_{target_id}"
                    ),
                    types.InlineKeyboardButton("◀️ Назад", callback_data="stats_back"),
                )
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=new_markup)
            except Exception:
                pass
        return

    elif data.startswith("uh_ghost_"):
        if not has_perm(user_id, "stats") and user_id != MAIN_ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔ Нет прав", show_alert=True)
            return
        target_id = int(data.split("_")[-1])

        if ghost_mode.get(user_id) == target_id:
            # Выключаем призрак
            del ghost_mode[user_id]
            ghost_reverse.pop(target_id, None)
            bot.answer_callback_query(call.id, f"👻 Режим призрака выключен для {target_id}", show_alert=True)
            ghost_label = "👻 Призрак: вкл"
        else:
            # Включаем призрак — снимаем старый, если был
            old_target = ghost_mode.pop(user_id, None)
            if old_target:
                ghost_reverse.pop(old_target, None)
            # Снимаем призрак с этого юзера у другого админа если был
            old_admin = ghost_reverse.pop(target_id, None)
            if old_admin and old_admin != user_id:
                ghost_mode.pop(old_admin, None)
                try:
                    bot.send_message(old_admin, f"⚠️ Режим призрака за пользователем {target_id} перехвачен другим администратором.")
                except:
                    pass

            ghost_mode[user_id] = target_id
            ghost_reverse[target_id] = user_id
            bot.answer_callback_query(call.id, f"👻 Режим призрака включён! Вы видите всё что делает {target_id}", show_alert=True)
            bot.send_message(
                user_id,
                f"👻 *Режим призрака активирован*\n\n"
                f"🎯 Цель: `{target_id}`\n"
                f"📨 Все сообщения этого пользователя будут дублироваться вам.\n"
                f"✉️ Когда напишете что-нибудь — ваше сообщение будет отправлено ему.\n\n"
                f"Для выхода нажмите кнопку 👻 Призрак: выкл в меню истории пользователя.",
                parse_mode="Markdown"
            )
            ghost_label = "👻 Призрак: выкл"

        # Обновляем кнопку
        try:
            is_banned_now = False
            try:
                conn_tmp = sqlite3.connect("users.db")
                row_tmp = conn_tmp.cursor().execute("SELECT banned FROM users WHERE user_id=?", (target_id,)).fetchone()
                conn_tmp.close()
                is_banned_now = bool(row_tmp and row_tmp[0])
            except:
                pass
            updated_markup = types.InlineKeyboardMarkup(row_width=1)
            updated_markup.add(
                types.InlineKeyboardButton("🎫 Управление трипами",    callback_data=f"uh_trips_{target_id}"),
                types.InlineKeyboardButton("💳 Управление подпиской",  callback_data=f"uh_sub_{target_id}"),
                types.InlineKeyboardButton(
                    "🚫 Заблокировать" if not is_banned_now else "✅ Разблокировать",
                    callback_data=f"uh_ban_{target_id}" if not is_banned_now else f"uh_unban_{target_id}"
                ),
                types.InlineKeyboardButton(ghost_label, callback_data=f"uh_ghost_{target_id}"),
                types.InlineKeyboardButton("◀️ Назад", callback_data="stats_back"),
            )
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=updated_markup)
        except Exception:
            pass
        return

    # ══════════════ ОЧИСТКА ВСЕЙ БД ПОЛЬЗОВАТЕЛЕЙ ══════════════

    if data == "stats_cleardb_ask":
        if not has_perm(user_id, "stats") and user_id != MAIN_ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔ Нет прав", show_alert=True)
            return
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("✅ ДА, удалить ВСЕХ пользователей", callback_data="stats_cleardb_confirm"),
            types.InlineKeyboardButton("❌ Отмена", callback_data="stats_back"),
        )
        bot.send_message(
            call.message.chat.id,
            "⚠️ *ВНИМАНИЕ!*\n\n"
            "Вы собираетесь полностью очистить базу данных пользователей.\n\n"
            "Будут удалены:\n"
            "• Все записи пользователей\n"
            "• Вся история поиска\n"
            "• Все события пользователей\n\n"
            "❗ Это действие *необратимо*!\n\nВы уверены?",
            parse_mode="Markdown",
            reply_markup=markup,
        )
        return

    elif data == "stats_cleardb_confirm":
        if not has_perm(user_id, "stats") and user_id != MAIN_ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔ Нет прав", show_alert=True)
            return
        try:
            conn = sqlite3.connect("users.db")
            cursor = conn.cursor()
            cursor.execute("DELETE FROM search_history")
            cursor.execute("DELETE FROM user_events")
            # Удаляем всех пользователей, кроме самого главного админа
            cursor.execute("DELETE FROM users WHERE user_id != ?", (MAIN_ADMIN_ID,))
            deleted_count = cursor.rowcount
            conn.commit()
            conn.close()
            bot.send_message(
                call.message.chat.id,
                f"✅ *База данных очищена!*\n\n"
                f"🗑 Удалено пользователей: {deleted_count}\n"
                f"🗑 История поиска — очищена\n"
                f"🗑 События пользователей — очищены\n\n"
                f"👑 Главный администратор сохранён.",
                parse_mode="Markdown",
            )
        except Exception as e:
            bot.answer_callback_query(call.id, f"❌ Ошибка: {e}", show_alert=True)
        return

    # ══════════════ УДАЛЕНИЕ КОНКРЕТНОГО ПОЛЬЗОВАТЕЛЯ ══════════════

    elif data.startswith("uh_deleteuser_ask_"):
        if not has_perm(user_id, "users") and user_id != MAIN_ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔ Нет прав", show_alert=True)
            return
        target_id = int(data.split("_")[-1])
        if target_id == MAIN_ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔ Нельзя удалить главного администратора!", show_alert=True)
            return
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("✅ ДА, удалить", callback_data=f"uh_deleteuser_confirm_{target_id}"),
            types.InlineKeyboardButton("❌ Отмена", callback_data="stats_back"),
        )
        bot.send_message(
            call.message.chat.id,
            f"⚠️ *ВНИМАНИЕ!*\n\n"
            f"Вы собираетесь удалить пользователя `{target_id}` из базы данных.\n\n"
            f"Будут удалены:\n"
            f"• Запись пользователя (подписка, трипы, бан)\n"
            f"• История поиска\n"
            f"• События (входы/выходы)\n\n"
            f"❗ Это действие *необратимо*!\n\nВы уверены?",
            parse_mode="Markdown",
            reply_markup=markup,
        )
        return

    elif data.startswith("uh_deleteuser_confirm_"):
        if not has_perm(user_id, "users") and user_id != MAIN_ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔ Нет прав", show_alert=True)
            return
        target_id = int(data.split("_")[-1])
        if target_id == MAIN_ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔ Нельзя удалить главного администратора!", show_alert=True)
            return
        try:
            # Если был режим призрака — выключаем
            if ghost_mode.get(user_id) == target_id:
                del ghost_mode[user_id]
                ghost_reverse.pop(target_id, None)
            conn = sqlite3.connect("users.db")
            cursor = conn.cursor()
            cursor.execute("DELETE FROM search_history WHERE user_id = ?", (target_id,))
            cursor.execute("DELETE FROM user_events WHERE user_id = ?", (target_id,))
            cursor.execute("DELETE FROM users WHERE user_id = ?", (target_id,))
            conn.commit()
            conn.close()
            bot.send_message(
                call.message.chat.id,
                f"✅ *Пользователь `{target_id}` удалён из базы данных.*\n\n"
                f"🗑 Удалены: профиль, история поиска, события.",
                parse_mode="Markdown",
            )
        except Exception as e:
            bot.answer_callback_query(call.id, f"❌ Ошибка: {e}", show_alert=True)
        return

    if data == "admin_createcode":
        markup = types.InlineKeyboardMarkup(row_width=2)
        btn1 = types.InlineKeyboardButton(
            "🕐 1 час", callback_data="code_duration_hour"
        )
        btn2 = types.InlineKeyboardButton(
            "📅 1 день", callback_data="code_duration_day"
        )
        btn3 = types.InlineKeyboardButton(
            "♾ Навсегда", callback_data="code_duration_forever"
        )
        btn4 = types.InlineKeyboardButton("❌ Отмена", callback_data="admin_back")
        markup.add(btn1, btn2, btn3, btn4)

        bot.edit_message_text(
            "🔑 СОЗДАНИЕ КОДА\n\nВыберите срок действия:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
        )
        return

    elif data == "admin_grant":
        admin_action[user_id] = {"action": "grant_admin"}
        bot.edit_message_text(
            "👑 ВЫДАЧА ПРАВ\n\nВведите ID пользователя:",
            call.message.chat.id,
            call.message.message_id,
        )
        return

    elif data == "admin_revoke":
        admin_action[user_id] = {"action": "revoke_admin"}
        bot.edit_message_text(
            "❌ ОТОЗВАТЬ ПРАВА\n\nВведите ID пользователя:",
            call.message.chat.id,
            call.message.message_id,
        )
        return

    elif data == "admin_list":
        permissions = get_admin_permissions_list()

        if not permissions:
            text = "📭 Нет пользователей с админ-правами"
        else:
            text = "👑 СПИСОК АДМИНОВ\n\n"
            for perm in permissions:
                target_id, username, first_name, granted_at, expires_at, is_active = (
                    perm
                )
                name = first_name or username or "Неизвестно"

                try:
                    if "." in expires_at:
                        expires_dt = datetime.strptime(
                            expires_at, "%Y-%m-%d %H:%M:%S.%f"
                        )
                    else:
                        expires_dt = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S")

                    if expires_dt < datetime.now():
                        status = "❌ ИСТЕКЛИ"
                    else:
                        days_left = (expires_dt - datetime.now()).days
                        status = f"✅ {days_left} дн."
                except:
                    status = "❌ Ошибка"

                text += (
                    f"👤 {name}\n🆔 {target_id}\n⏰ До: {expires_at}\n📊 {status}\n\n"
                )

        markup = types.InlineKeyboardMarkup()
        btn_back = types.InlineKeyboardButton("◀️ Назад", callback_data="admin_back")
        markup.add(btn_back)

        bot.edit_message_text(
            text, call.message.chat.id, call.message.message_id, reply_markup=markup
        )
        return

    elif data == "admin_back":
        markup = types.InlineKeyboardMarkup(row_width=2)
        btn1 = types.InlineKeyboardButton(
            "🔑 Создать код", callback_data="admin_createcode"
        )
        btn2 = types.InlineKeyboardButton(
            "👑 Выдать права", callback_data="admin_grant"
        )
        btn3 = types.InlineKeyboardButton(
            "❌ Отозвать права", callback_data="admin_revoke"
        )
        btn4 = types.InlineKeyboardButton(
            "📋 Список админов", callback_data="admin_list"
        )
        btn5 = types.InlineKeyboardButton("◀️ Назад", callback_data="admin_back")
        markup.add(btn1, btn2, btn3, btn4, btn5)

        try:
            bot.edit_message_text(
                "👑 УПРАВЛЕНИЕ АДМИНАМИ\n\nВыберите действие:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup,
            )
        except:
            bot.answer_callback_query(call.id, "ℹ️ Вы уже в главном меню")
        return

    # Модерация
    elif data == "mod_ban":
        if not has_perm(user_id, "users"):
            bot.answer_callback_query(
                call.id, "⛔ Нет прав для модерации", show_alert=True
            )
            return
        admin_action[user_id] = {"action": "ban_user"}
        bot.edit_message_text(
            "🚫 БАН ПОЛЬЗОВАТЕЛЯ\n\nВведите ID и причину:\nФормат: 123456789 Спам",
            call.message.chat.id,
            call.message.message_id,
        )
        return

    elif data == "mod_unban":
        if not has_perm(user_id, "users"):
            bot.answer_callback_query(
                call.id, "⛔ Нет прав для модерации", show_alert=True
            )
            return
        admin_action[user_id] = {"action": "unban_user"}
        bot.edit_message_text(
            "✅ РАЗБАН\n\nВведите ID пользователя:",
            call.message.chat.id,
            call.message.message_id,
        )
        return

    elif data == "mod_banlist":
        if not has_perm(user_id, "users"):
            bot.answer_callback_query(
                call.id, "⛔ Нет прав для модерации", show_alert=True
            )
            return
        banned = get_banned_users()

        if not banned:
            text = "📭 Нет забаненных пользователей"
        else:
            text = "🚫 ЗАБАНЕННЫЕ ПОЛЬЗОВАТЕЛИ\n\n"
            for user in banned:
                uid, username, first_name, reason, created_at = user
                name = first_name or username or "Неизвестно"
                date = created_at.split()[0] if created_at else "?"
                text += f"👤 {name}\n🆔 {uid}\n📌 {reason}\n📅 {date}\n\n"

        markup = types.InlineKeyboardMarkup()
        btn_back = types.InlineKeyboardButton("◀️ Назад", callback_data="mod_back")
        markup.add(btn_back)

        bot.edit_message_text(
            text, call.message.chat.id, call.message.message_id, reply_markup=markup
        )
        return

    elif data == "mod_floodsettings":
        if not has_perm(user_id, "users"):
            bot.answer_callback_query(
                call.id, "⛔ Нет прав для модерации", show_alert=True
            )
            return
        admin_action[user_id] = {"action": "floodsettings"}
        text = f"""📊 ТЕКУЩИЕ НАСТРОЙКИ:
max_messages: {flood_settings["max_messages"]}
period_seconds: {flood_settings["period_seconds"]}
warnings_before_ban: {flood_settings["warnings_before_ban"]}

Введите: параметр значение
Например: max_messages 10"""

        bot.edit_message_text(text, call.message.chat.id, call.message.message_id)
        return

    elif data == "mod_clearflood":
        if not has_perm(user_id, "users"):
            bot.answer_callback_query(
                call.id, "⛔ Нет прав для модерации", show_alert=True
            )
            return
        admin_action[user_id] = {"action": "clearflood"}
        bot.edit_message_text(
            "🧹 СБРОС ФЛУДА\n\nВведите ID пользователя:",
            call.message.chat.id,
            call.message.message_id,
        )
        return

    elif data == "mod_back":
        markup = types.InlineKeyboardMarkup(row_width=2)
        btn1 = types.InlineKeyboardButton("🚫 Забанить", callback_data="mod_ban")
        btn2 = types.InlineKeyboardButton("✅ Разбанить", callback_data="mod_unban")
        btn3 = types.InlineKeyboardButton(
            "📋 Список банов", callback_data="mod_banlist"
        )
        btn4 = types.InlineKeyboardButton(
            "🛡️ Настройки флуда", callback_data="mod_floodsettings"
        )
        btn5 = types.InlineKeyboardButton(
            "🧹 Сбросить флуд", callback_data="mod_clearflood"
        )
        btn6 = types.InlineKeyboardButton("◀️ Назад", callback_data="mod_back")
        markup.add(btn1, btn2, btn3, btn4, btn5, btn6)

        try:
            bot.edit_message_text(
                "🛡️ МОДЕРАЦИЯ\n\nВыберите действие:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup,
            )
        except:
            bot.answer_callback_query(call.id, "ℹ️ Вы уже в главном меню")
        return

    # Файлы
    elif data == "file_create":
        if not has_perm(user_id, "files"):
            bot.answer_callback_query(
                call.id, "⛔ Нет прав для работы с файлами", show_alert=True
            )
            return
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton(
                "📤 Загрузить файл (TXT/CSV/XLSX)", callback_data="file_create_upload"
            ),
            types.InlineKeyboardButton(
                "✍️ Ввести текст вручную", callback_data="file_create_manual"
            ),
            types.InlineKeyboardButton("◀️ Назад", callback_data="file_back"),
        )
        bot.edit_message_text(
            "📝 СОЗДАНИЕ НОВОЙ БАЗЫ\n\nКак хотите добавить данные?",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
        )
        return

    elif data == "file_create_upload":
        if not has_perm(user_id, "files"):
            bot.answer_callback_query(
                call.id, "⛔ Нет прав для работы с файлами", show_alert=True
            )
            return
        pending_uploads[user_id] = {"waiting": True}
        bot.edit_message_text(
            "📤 ЗАГРУЗКА БАЗЫ\n\n"
            "Отправьте файл с базой прямо в этот чат.\n\n"
            "✅ Поддерживаются форматы:\n"
            "• TXT — строки через ; или блоки текста\n"
            "• CSV — таблица с разделителями\n"
            "• XLSX — Excel таблица (нужен openpyxl)\n\n"
            "🤖 Бот сам определит:\n"
            "• Что такое телефон, ФИО, адрес, авто\n"
            "• Тип базы (авто, имущество, люди...)\n\n"
            "Отправьте /cancel для отмены",
            call.message.chat.id,
            call.message.message_id,
        )
        return

    elif data == "file_create_manual":
        if not has_perm(user_id, "files"):
            bot.answer_callback_query(
                call.id, "⛔ Нет прав для работы с файлами", show_alert=True
            )
            return
        admin_create_file[user_id] = {"mode": "waiting_name", "data": {}}
        bot.edit_message_text(
            "✍️ СОЗДАНИЕ ФАЙЛА ВРУЧНУЮ\n\n"
            "Введите название файла:\n"
            "Например: avto_москва или imush_база\n\n"
            "💡 Если в названии есть avto, imush, фио —\n"
            "бот автоматически подберёт иконки под тип.\n\n"
            "Отправьте /cancel для отмены",
            call.message.chat.id,
            call.message.message_id,
        )
        return

    elif data == "file_edit":
        if not has_perm(user_id, "files"):
            bot.answer_callback_query(
                call.id, "⛔ Нет прав для работы с файлами", show_alert=True
            )
            return
        if not os.path.exists(DATA_DIR):
            os.makedirs(DATA_DIR)

        files = os.listdir(DATA_DIR)
        txt_files = [f for f in files if f.endswith(".txt")]

        if not txt_files:
            bot.answer_callback_query(
                call.id, "📁 Нет текстовых файлов для редактирования!"
            )
            return

        markup = types.InlineKeyboardMarkup(row_width=1)
        for file in sorted(txt_files)[:10]:
            btn = types.InlineKeyboardButton(f"📄 {file}", callback_data=f"edit_{file}")
            markup.add(btn)

        btn_back = types.InlineKeyboardButton("◀️ Назад", callback_data="file_back")
        markup.add(btn_back)

        bot.edit_message_text(
            "📝 РЕДАКТИРОВАНИЕ ФАЙЛА\n\nВыберите файл:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
        )
        return

    elif data == "file_rename":
        if not has_perm(user_id, "files"):
            bot.answer_callback_query(
                call.id, "⛔ Нет прав для работы с файлами", show_alert=True
            )
            return
        if not os.path.exists(DATA_DIR):
            os.makedirs(DATA_DIR)

        files = os.listdir(DATA_DIR)
        txt_files = [f for f in files if f.endswith(".txt")]

        if not txt_files:
            bot.answer_callback_query(
                call.id, "📁 Нет текстовых файлов для переименования!"
            )
            return

        markup = types.InlineKeyboardMarkup(row_width=1)
        for file in sorted(txt_files)[:10]:
            btn = types.InlineKeyboardButton(
                f"📄 {file}", callback_data=f"rename_{file}"
            )
            markup.add(btn)

        btn_back = types.InlineKeyboardButton("◀️ Назад", callback_data="file_back")
        markup.add(btn_back)

        bot.edit_message_text(
            "✏️ ПЕРЕИМЕНОВАНИЕ ФАЙЛА\n\nВыберите файл:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
        )
        return

    elif data == "file_list":
        if not has_perm(user_id, "files"):
            bot.answer_callback_query(
                call.id, "⛔ Нет прав для работы с файлами", show_alert=True
            )
            return
        try:
            if not os.path.exists(DATA_DIR):
                os.makedirs(DATA_DIR)

            files = os.listdir(DATA_DIR)

            if files:
                text = "📁 ФАЙЛЫ В ПАПКЕ DATA:\n\n"
                txt_files = [f for f in files if f.endswith(".txt")]
                img_files = [
                    f for f in files if f.endswith((".jpg", ".jpeg", ".png", ".webp"))
                ]

                if txt_files:
                    text += "📄 Текстовые файлы:\n"
                    for f in sorted(txt_files):
                        size = os.path.getsize(os.path.join(DATA_DIR, f))
                        text += f"• {f} ({size} байт)\n"
                    text += "\n"

                if img_files:
                    text += "🖼 Изображения:\n"
                    for f in sorted(img_files):
                        size = os.path.getsize(os.path.join(DATA_DIR, f))
                        text += f"• {f} ({size} байт)\n"
            else:
                text = "📁 Папка data/ пуста"

        except Exception as e:
            text = f"❌ Ошибка: {e}"

        markup = types.InlineKeyboardMarkup()
        btn_back = types.InlineKeyboardButton("◀️ Назад", callback_data="file_back")
        markup.add(btn_back)

        bot.edit_message_text(
            text, call.message.chat.id, call.message.message_id, reply_markup=markup
        )
        return

    elif data == "file_delete":
        if not has_perm(user_id, "files"):
            bot.answer_callback_query(
                call.id, "⛔ Нет прав для работы с файлами", show_alert=True
            )
            return
        admin_create_file[user_id] = {"mode": "waiting_delete", "data": {}}
        bot.edit_message_text(
            "🗑 УДАЛЕНИЕ ФАЙЛА\n\n"
            "Введите имя файла для удаления:\n"
            "Например: Иванов Иван.txt или фото.jpg\n\n"
            "❗ Файл будет удален безвозвратно!\n"
            "Отправьте /cancel для отмены",
            call.message.chat.id,
            call.message.message_id,
        )
        return

    elif data == "file_back":
        markup = types.InlineKeyboardMarkup(row_width=2)
        btn1 = types.InlineKeyboardButton(
            "📝 Создать файл", callback_data="file_create"
        )
        btn2 = types.InlineKeyboardButton("✏️ Редактировать", callback_data="file_edit")
        btn3 = types.InlineKeyboardButton(
            "📋 Переименовать", callback_data="file_rename"
        )
        btn4 = types.InlineKeyboardButton("📁 Список файлов", callback_data="file_list")
        btn5 = types.InlineKeyboardButton("🗑️ Удалить файл", callback_data="file_delete")
        btn6 = types.InlineKeyboardButton("◀️ Назад", callback_data="file_back")
        markup.add(btn1, btn2, btn3, btn4, btn5, btn6)

        try:
            bot.edit_message_text(
                "📁 РАБОТА С ФАЙЛАМИ\n\nВыберите действие:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup,
            )
        except:
            bot.answer_callback_query(call.id, "ℹ️ Вы уже в главном меню")
        return

    # Тексты
    elif data == "view_all_texts":
        if not has_perm(user_id, "texts"):
            bot.answer_callback_query(
                call.id, "⛔ Нет прав для управления текстами", show_alert=True
            )
            return
        texts = list_bot_texts()

        if not texts:
            bot.answer_callback_query(call.id, "📭 Нет текстов")
            return

        text = "📄 ВСЕ ТЕКСТЫ БОТА:\n\n"
        for key, content, updated_by, updated_at, *_ in texts:
            preview = content[:50] + "..." if len(content) > 50 else content
            text += f"{key}:\n{preview}\n🕐 {updated_at}\n\n"

            if len(text) > 3500:
                bot.send_message(call.message.chat.id, text)
                text = ""

        if text:
            bot.send_message(call.message.chat.id, text)

        bot.answer_callback_query(call.id)
        return

    elif data == "texts_back":
        texts = list_bot_texts()

        if not texts:
            bot.answer_callback_query(call.id, "📭 Нет текстов")
            return

        markup = types.InlineKeyboardMarkup(row_width=2)
        for key, _, _, _, *__ in texts[:8]:
            btn = types.InlineKeyboardButton(
                f"📝 {key}", callback_data=f"edittext_{key}"
            )
            markup.add(btn)

        btn_adminhelp = types.InlineKeyboardButton("👑 Текст АДМИН-ПАНЕЛИ", callback_data="edittext_admin_help")
        btn_custlabel = types.InlineKeyboardButton("🔤 Название кнопки", callback_data="edittext_custom_button_label")
        btn_custtext  = types.InlineKeyboardButton("📌 Текст кнопки", callback_data="edittext_custom_button")
        btn_view = types.InlineKeyboardButton("📄 Все тексты", callback_data="view_all_texts")
        btn_back = types.InlineKeyboardButton("◀️ Назад", callback_data="texts_back")
        markup.add(btn_adminhelp)
        markup.add(btn_custlabel, btn_custtext)
        markup.add(btn_view, btn_back)

        try:
            bot.edit_message_text(
                "📝 УПРАВЛЕНИЕ ТЕКСТАМИ\n\nВыберите текст для редактирования:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup,
            )
        except:
            bot.answer_callback_query(call.id, "ℹ️ Вы уже в главном меню")
        return

    # Коды — шаг 1: выбор длительности → переход к выбору прав
    if data.startswith("code_duration_"):
        duration = data.replace("code_duration_", "")

        # Сохраняем длительность, переходим к выбору прав
        admin_create_file[user_id] = {
            "mode": "waiting_code_name",
            "data": {"duration": duration, "permissions": []},
        }

        markup = types.InlineKeyboardMarkup(row_width=1)
        for perm_key, perm_label in ALL_PERMISSIONS.items():
            markup.add(
                types.InlineKeyboardButton(
                    f"☐ {perm_label}", callback_data=f"codeperm_{perm_key}"
                )
            )
        markup.add(
            types.InlineKeyboardButton("✅ Все права", callback_data="codeperm_all")
        )
        markup.add(
            types.InlineKeyboardButton("➡️ Продолжить", callback_data="codeperm_done")
        )
        markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data="admin_back"))

        bot.edit_message_text(
            f"🔑 СОЗДАНИЕ КОДА\n\n"
            f"Срок действия: {get_duration_text(duration)}\n\n"
            f"🔐 Выберите права для этого кода:\n"
            f"(нажмите на право чтобы добавить/убрать)\n\n"
            f"Когда закончите — нажмите ➡️ Продолжить",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
        )
        return

    # Коды — шаг 2: переключение отдельного права
    elif data.startswith("codeperm_") and data != "codeperm_done":
        perm_key = data.replace("codeperm_", "")

        if (
            user_id not in admin_create_file
            or admin_create_file[user_id].get("mode") != "waiting_code_name"
        ):
            bot.answer_callback_query(call.id, "❌ Сначала начните создание кода")
            return

        current_perms = admin_create_file[user_id]["data"].get("permissions", [])
        duration = admin_create_file[user_id]["data"]["duration"]

        if perm_key == "all":
            # Выбрать все права
            current_perms = list(ALL_PERMISSIONS.keys())
        elif perm_key in current_perms:
            current_perms.remove(perm_key)
        else:
            current_perms.append(perm_key)

        admin_create_file[user_id]["data"]["permissions"] = current_perms

        # Перерисовываем кнопки с отметками
        markup = types.InlineKeyboardMarkup(row_width=1)
        for k, label in ALL_PERMISSIONS.items():
            check = "✅" if k in current_perms else "☐"
            markup.add(
                types.InlineKeyboardButton(
                    f"{check} {label}", callback_data=f"codeperm_{k}"
                )
            )
        markup.add(
            types.InlineKeyboardButton("✅ Все права", callback_data="codeperm_all")
        )
        markup.add(
            types.InlineKeyboardButton("➡️ Продолжить", callback_data="codeperm_done")
        )
        markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data="admin_back"))

        selected_text = (
            ", ".join(ALL_PERMISSIONS[p] for p in current_perms)
            if current_perms
            else "не выбрано"
        )
        bot.edit_message_text(
            f"🔑 СОЗДАНИЕ КОДА\n\n"
            f"Срок действия: {get_duration_text(duration)}\n\n"
            f"🔐 Выберите права для этого кода:\n"
            f"Выбрано: {selected_text}\n\n"
            f"Когда закончите — нажмите ➡️ Продолжить",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
        )
        return

    # Коды — шаг 3: подтверждение прав → переход к вводу имени кода
    elif data == "codeperm_done":
        if (
            user_id not in admin_create_file
            or admin_create_file[user_id].get("mode") != "waiting_code_name"
        ):
            bot.answer_callback_query(call.id, "❌ Сначала начните создание кода")
            return

        current_perms = admin_create_file[user_id]["data"].get("permissions", [])
        duration = admin_create_file[user_id]["data"]["duration"]

        if not current_perms:
            bot.answer_callback_query(
                call.id, "⚠️ Выберите хотя бы одно право!", show_alert=True
            )
            return

        # Права выбраны — просим ввести имя кода
        perms_str = ",".join(current_perms)
        admin_create_file[user_id]["data"]["permissions_str"] = perms_str

        selected_labels = ", ".join(ALL_PERMISSIONS[p] for p in current_perms)
        bot.edit_message_text(
            f"🔑 СОЗДАНИЕ КОДА\n\n"
            f"⏰ Срок: {get_duration_text(duration)}\n"
            f"🔐 Права: {selected_labels}\n\n"
            f"📝 Введите желаемый код (например: admin123)\n"
            f"или отправьте random для случайного кода:\n\n"
            f"Отправьте /cancel для отмены",
            call.message.chat.id,
            call.message.message_id,
        )
        return

    # Редактирование файлов
    elif data.startswith("edit_"):
        filename = data[5:]
        filepath = os.path.join(DATA_DIR, filename)

        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()

            bot.edit_message_text(
                f"📄 РЕДАКТИРОВАНИЕ: {filename}\n\n"
                f"Текущее содержимое:\n{content[:500]}{'...' if len(content) > 500 else ''}\n\n"
                f"📝 Отправь новое содержимое файла.\n"
                f"Отправь /cancel для отмены",
                call.message.chat.id,
                call.message.message_id,
            )

            admin_create_file[user_id] = {
                "mode": "editing",
                "data": {"filename": filename.replace(".txt", "")},
            }
        else:
            bot.answer_callback_query(call.id, "❌ Файл не найден")
        return

    elif data.startswith("rename_"):
        filename = data[7:]

        bot.edit_message_text(
            f"✏️ ПЕРЕИМЕНОВАНИЕ: {filename}\n\n"
            f"Введите новое имя для файла (без расширения .txt):\n"
            f"Например: Новый Иванов\n\n"
            f"Отправь /cancel для отмены",
            call.message.chat.id,
            call.message.message_id,
        )

        admin_create_file[user_id] = {
            "mode": "renaming",
            "data": {"old_filename": filename.replace(".txt", "")},
        }
        return

    # Редактирование текстов
    if data.startswith("edittext_"):
        if not has_perm(user_id, "texts"):
            bot.answer_callback_query(
                call.id, "⛔ Нет прав для управления текстами", show_alert=True
            )
            return
        key = data.replace("edittext_", "")

        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute("SELECT text, photo_id FROM bot_texts WHERE key = ?", (key,))
        result = cursor.fetchone()
        conn.close()

        current_text  = (result[0] if result and result[0] else "—")
        current_photo = (result[1] if result and result[1] else None)
        photo_status  = "📷 Есть фото" if current_photo else "🖼 Без фото"

        admin_create_file[user_id] = {
            "mode": f"editing_text_{key}",
            "data": {"key": key, "current_photo": current_photo},
        }

        # Для admin_help показываем подсказку о плейсхолдерах
        if key == "admin_help":
            extra_hint = (
                "\n\n💡 <b>Доступные плейсхолдеры</b> (подставляются автоматически):\n"
                "<code>{stats_line}</code> — /stats\n"
                "<code>{admin_panel_line}</code> — /admin_panel\n"
                "<code>{mod_line}</code> — /moderation\n"
                "<code>{texts_line}</code> — /texts\n"
                "<code>{files_line}</code> — /files\n"
                "<code>{secret_line}</code> — /secret_list\n"
                "<code>{block_line}</code> — /block_query\n"
                "<code>{broadcast_line}</code> — /broadcast\n"
                "<code>{support_line}</code> — /supportlist\n"
                "<code>{createtrip_line}</code> — /createtrip\n"
            )
        else:
            extra_hint = ""

        hint = (
            f"✏️ РЕДАКТИРОВАНИЕ: <b>{key}</b>\n\n"
            f"Текущий текст:\n<code>{current_text[:400]}{'...' if len(current_text) > 400 else ''}</code>\n\n"
            f"Статус фото: {photo_status}{extra_hint}\n\n"
            f"📝 <b>Как обновить:</b>\n"
            f"• Отправь <b>текст</b> — обновится только текст, фото сохранится\n"
            f"• Отправь <b>фото</b> — обновится только фото, текст сохранится\n"
            f"• Отправь <b>фото с подписью</b> — обновятся оба\n"
            f"• Отправь <code>-</code> для сброса фото\n\n"
            f"Отправь /cancel для отмены"
        )

        # Кнопка "Показать полный текст" появляется только если текст был обрезан
        hint_markup = None
        if len(current_text) > 400:
            hint_markup = types.InlineKeyboardMarkup()
            hint_markup.add(
                types.InlineKeyboardButton(
                    "👁 Показать полный текущий текст",
                    callback_data=f"showfulltext_{key}"
                )
            )

        try:
            bot.edit_message_text(hint, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=hint_markup)
        except:
            bot.send_message(call.message.chat.id, hint, parse_mode="HTML", reply_markup=hint_markup)
        return

    # Показ полного текста (вызывается кнопкой из edittext_)
    if data.startswith("showfulltext_"):
        if not has_perm(user_id, "texts"):
            bot.answer_callback_query(call.id, "⛔ Нет прав", show_alert=True)
            return
        key = data.replace("showfulltext_", "")

        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute("SELECT text FROM bot_texts WHERE key = ?", (key,))
        result = cursor.fetchone()
        conn.close()

        full_text = result[0] if result and result[0] else "—"

        # Telegram ограничивает сообщения ~4096 символов; разбиваем если нужно
        chunks = [full_text[i:i+3800] for i in range(0, len(full_text), 3800)]
        for idx, chunk in enumerate(chunks):
            part_label = f" (часть {idx+1}/{len(chunks)})" if len(chunks) > 1 else ""
            bot.send_message(
                call.message.chat.id,
                f"📄 Полный текст <b>{key}</b>{part_label}:\n\n<code>{chunk}</code>",
                parse_mode="HTML"
            )
        bot.answer_callback_query(call.id)
        return


# ========== ПЕРИОДИЧЕСКАЯ ОЧИСТКА ПАМЯТИ ==========
def cleanup_memory():
    while True:
        time.sleep(3600)
        try:
            current_time = time.time()
            for user_id in list(user_messages.keys()):
                user_messages[user_id] = [
                    ts for ts in user_messages[user_id] if current_time - ts < 3600
                ]

            global notified_support_users
            for user_id in list(notified_support_users.keys()):
                if current_time - notified_support_users[user_id] > 3600:
                    del notified_support_users[user_id]

            print("🧹 Очистка памяти выполнена")
        except:
            pass


# ========== ЗАГРУЗКА БАЗ ЧЕРЕЗ ТЕЛЕГРАМ ==========

# Хранит состояние: ожидаем ли от этого admin_id подтверждение загрузки
pending_uploads = {}  # {user_id: {"filename": ..., "records": ..., "db_type": ..., "sep": ...}}


@bot.message_handler(content_types=["document"])
def handle_document_upload(message):
    user_id = message.from_user.id

    # Только те у кого есть право files
    if not has_perm(user_id, "files"):
        bot.send_message(message.chat.id, "⛔ У вас нет прав для загрузки баз данных.")
        return

    doc = message.document
    filename = doc.file_name or "база.txt"
    ext = os.path.splitext(filename)[1].lower()

    allowed = [".txt", ".csv", ".xlsx", ".xls"]
    if ext not in allowed:
        bot.send_message(
            message.chat.id,
            f"❌ Формат {ext} не поддерживается.\n\n"
            f"✅ Поддерживаемые форматы: TXT, CSV, XLSX",
        )
        return

    if ext in (".xlsx", ".xls") and not EXCEL_SUPPORT:
        bot.send_message(
            message.chat.id, "❌ Для загрузки Excel установите: pip install openpyxl"
        )
        return

    # Скачиваем файл
    try:
        # Стартовый "таймер" как зелёная полоска. Сообщение дальше будет заменено на инфо.
        msg_wait = bot.send_message(
            message.chat.id, "⏳ Загружаю файл:\n🟩🟩⬜⬜⬜⬜⬜⬜⬜⬜"
        )
        file_info = bot.get_file(doc.file_id)
        raw_bytes = bot.download_file(file_info.file_path)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка загрузки: {e}")
        return

    # Парсим
    try:
        records, sep, db_type = parse_uploaded_file(raw_bytes, filename)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка анализа файла: {e}")
        return

    if not records:
        bot.send_message(
            message.chat.id, "❌ Файл пустой или не удалось распознать записи."
        )
        return

    # Тип базы в читаемом виде
    db_type_names = {
        "avto": "🚗 Автомобили",
        "imush": "🏠 Имущество",
        "work": "💼 Сотрудники/Работа",
        "people": "👤 Люди/Контакты",
        "default": "📋 Общая база",
    }
    db_type_readable = db_type_names.get(db_type, "📋 Общая база")

    sep_readable = {
        ";": "точка с запятой (;)",
        ",": "запятая (,)",
        "\t": "табуляция",
        "blocks": "блоки текста",
        "xlsx": "Excel таблица",
    }.get(sep, sep)

    # Показываем превью первых 3 записей
    preview_lines = [f"📊 АНАЛИЗ ФАЙЛА: {filename}\n"]
    preview_lines.append(f"🗂 Тип базы: {db_type_readable}")
    preview_lines.append(f"📐 Разделитель: {sep_readable}")
    preview_lines.append(f"📝 Записей найдено: {len(records)}\n")
    preview_lines.append("━━━ ПРЕВЬЮ (первые 3 записи) ━━━\n")

    for i, rec in enumerate(records[:3], 1):
        formatted = smart_format_record(rec, db_type)
        preview_lines.append(f"Запись {i}:\n{formatted}\n")

    preview_text = "\n".join(preview_lines)

    # Предлагаем сохранить
    safe_name = "".join(
        c for c in os.path.splitext(filename)[0] if c.isalnum() or c in " .-_"
    )
    pending_uploads[user_id] = {
        "filename": safe_name,
        "raw_bytes": raw_bytes,
        "records": records,
        "db_type": db_type,
        "sep": sep,
        "original_filename": filename,
    }

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton(
            "✅ Сохранить в базу", callback_data=f"upload_confirm_{user_id}"
        ),
        types.InlineKeyboardButton(
            "✏️ Изменить название", callback_data=f"upload_rename_{user_id}"
        ),
        types.InlineKeyboardButton(
            "❌ Отмена", callback_data=f"upload_cancel_{user_id}"
        ),
    )

    # Вместо удаления "таймера" редактируем сообщение, превращая его в инфо
    try:
        bot.edit_message_text(
            preview_text[:4000],
            message.chat.id,
            msg_wait.message_id,
            reply_markup=markup,
        )
    except:
        # Если редактирование не удалось (старое сообщение, нет прав и т.п.) —
        # тогда удалим и отправим новое.
        try:
            bot.delete_message(message.chat.id, msg_wait.message_id)
        except:
            pass
        bot.send_message(message.chat.id, preview_text[:4000], reply_markup=markup)


# Обработчик кнопок загрузки файла
def handle_upload_callbacks(call):
    user_id = call.from_user.id
    data = call.data

    if data.startswith("upload_confirm_"):
        target_uid = int(data.split("_")[-1])
        if target_uid != user_id or user_id not in pending_uploads:
            bot.answer_callback_query(call.id, "❌ Нет данных для сохранения")
            return

        info = pending_uploads[user_id]
        raw_bytes = info["raw_bytes"]
        filename = info["filename"]
        original_ext = os.path.splitext(info["original_filename"])[1].lower()

        try:
            if not os.path.exists(DATA_DIR):
                os.makedirs(DATA_DIR)

            # Сохраняем как txt в формате через ; (одна запись = одна строка)
            save_path = os.path.join(DATA_DIR, f"{filename}.txt")

            # Конвертируем записи в формат field1;field2;field3
            lines = []
            for rec in info["records"]:
                # Убираем пустые поля и лишние пробелы
                fields = [f.strip() for f in rec if f.strip()]
                if fields:
                    lines.append(";".join(fields))
            content = "\n".join(lines)

            with open(save_path, "w", encoding="utf-8") as f:
                f.write(content)

            # Сохраняем заголовки если есть
            if info.get("headers"):
                headers_path = os.path.join(DATA_DIR, f"{filename}.headers")
                with open(headers_path, "w", encoding="utf-8") as hf:
                    hf.write(";".join(info["headers"]))

            del pending_uploads[user_id]

            # ========== СРАЗУ ДОБАВЛЯЕМ В SQLite ==========
            try:
                added = sqlite_add_records(f"{filename}.txt", info["records"], info["db_type"])
                print(f"[sqlite_import] Загрузка через Telegram: {filename}.txt +{added} записей")
            except Exception as e:
                print(f"[sqlite_import] Ошибка записи в SQLite: {e}")

            bot.edit_message_text(
                f"✅ База успешно сохранена!\n\n"
                f"📁 Файл: {filename}.txt\n"
                f"📝 Записей: {len(info['records'])}\n"
                f"🗂 Тип: {info['db_type']}\n\n"
                f"🔍 Теперь пользователи могут искать по этой базе.",
                call.message.chat.id,
                call.message.message_id,
            )
        except Exception as e:
            bot.answer_callback_query(
                call.id, f"❌ Ошибка сохранения: {e}", show_alert=True
            )

    elif data.startswith("upload_rename_"):
        target_uid = int(data.split("_")[-1])
        if target_uid != user_id or user_id not in pending_uploads:
            bot.answer_callback_query(call.id, "❌ Нет данных")
            return

        admin_create_file[user_id] = {"mode": "upload_rename", "data": {}}
        bot.edit_message_text(
            "✏️ Введите новое название для файла (без расширения):\n"
            "Например: avto_москва_2024\n\n"
            "Отправьте /cancel для отмены",
            call.message.chat.id,
            call.message.message_id,
        )

    elif data.startswith("upload_cancel_"):
        target_uid = int(data.split("_")[-1])
        if user_id in pending_uploads:
            del pending_uploads[user_id]
        bot.edit_message_text(
            "❌ Загрузка отменена.", call.message.chat.id, call.message.message_id
        )




# ========== ОТСЛЕЖИВАНИЕ УХОДА ПОЛЬЗОВАТЕЛЕЙ ==========
@bot.my_chat_member_handler()
def track_user_leave(update):
    """
    Срабатывает когда пользователь:
    - блокирует бота (new_status = 'kicked')
    - разблокирует бота (new_status = 'member')
    Логирует события в таблицу user_events.
    """
    try:
        user_id   = update.from_user.id
        old_status = update.old_chat_member.status   # 'member', 'kicked', etc.
        new_status = update.new_chat_member.status

        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()

        if new_status == "kicked" and old_status != "kicked":
            # Пользователь заблокировал бота — фиксируем уход
            cursor.execute(
                "INSERT INTO user_events (user_id, event_type, event_at) VALUES (?, 'left', ?)",
                (user_id, datetime.now()),
            )
            logging.info(f"[user_events] Пользователь {user_id} заблокировал бота (left)")

        elif new_status == "member" and old_status == "kicked":
            # Пользователь разблокировал бота — фиксируем возвращение
            cursor.execute(
                "INSERT INTO user_events (user_id, event_type, event_at) VALUES (?, 'joined', ?)",
                (user_id, datetime.now()),
            )
            logging.info(f"[user_events] Пользователь {user_id} разблокировал бота (joined)")

        conn.commit()
        conn.close()
    except Exception as e:
        logging.warning(f"[track_user_leave] Ошибка: {e}")


# ========== ЗАПУСК БОТА ==========
if __name__ == "__main__":
    print("✅ Бот запущен с ОБЯЗАТЕЛЬНОЙ подпиской на канал!")
    print("📋 Информация берется из файлов в папке data/")
    print(
        "📢 Основные команды: /start, /help, /stats, /admin_panel, /moderation, /texts, /files"
    )
    print("💬 Команды поддержки: /support, /supportlist, /reply, /stopreply, /endchat")
    print(f"🔍 Проверка подписки на канал: {REQUIRED_CHANNEL}")

    # ========== РЕГИСТРАЦИЯ КОМАНД И КНОПКИ МЕНЮ — В ФОНЕ ==========
    # Фоновый поток: не блокирует старт при таймауте/прокси, повторяет до 5 раз
    def _setup_telegram_commands():
        for attempt in range(1, 6):
            try:
                bot.set_my_commands([
                    telebot.types.BotCommand("start",     "👤 Личный кабинет"),
                    telebot.types.BotCommand("help",      "📚 Список команд"),
                    telebot.types.BotCommand("myhistory", "📜 История поиска"),
                ])
                # Восстанавливаем кнопку "/" — Mini App мог заменить её на web_app
                resp = requests.post(
                    f"https://api.telegram.org/bot{TOKEN}/setChatMenuButton",
                    json={"menu_button": {"type": "commands"}},
                    timeout=10,
                )
                if resp.ok:
                    print("✅ Команды и кнопка меню (/) успешно настроены")
                else:
                    print(f"⚠️ setChatMenuButton: {resp.text}")
                return
            except Exception as e:
                if attempt < 5:
                    print(f"⚠️ Попытка {attempt}/5 не удалась: {e} — повтор через 10 сек...")
                    time.sleep(10)
                else:
                    print(f"❌ Не удалось настроить команды после 5 попыток: {e}")

    threading.Thread(target=_setup_telegram_commands, daemon=True).start()

    # ========== ИМПОРТ СУЩЕСТВУЮЩИХ ФАЙЛОВ В SQLite ==========
    print("⏳ Импорт баз данных из data/ в SQLite...")
    print("🧹 Очистка устаревших записей из SQLite...")
    sqlite_cleanup_dead_files()
    import_all_txt_to_sqlite()
    print("✅ SQLite-индекс готов. Поиск будет мгновенным!")

    cleanup_thread = threading.Thread(target=cleanup_memory, daemon=True)
    cleanup_thread.start()
    bot.infinity_polling()