import pymysql
from pymysql.cursors import DictCursor
from config import DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME

def get_connection():
    return pymysql.connect(
        host=DB_HOST,
        port=int(DB_PORT),
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        cursorclass=DictCursor,
        autocommit=True
    )

def init_db():
    conn = get_connection()
    with conn.cursor() as cursor:
        # Таблица для пользователей
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INT PRIMARY KEY AUTO_INCREMENT,
            telegram_chat_id BIGINT NOT NULL,
            telegram_username VARCHAR(255),
            telegram_first_name VARCHAR(255),
            telegram_last_name VARCHAR(255),
            real_first_name VARCHAR(255),
            real_last_name VARCHAR(255),
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        
        # Таблица для логирования действий
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_actions (
            id INT PRIMARY KEY AUTO_INCREMENT,
            user_id INT NOT NULL,
            action TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """)
    conn.close()

def register_user(chat_id, username, first_name, last_name):
    conn = get_connection()
    with conn.cursor() as cursor:
        cursor.execute("SELECT id FROM users WHERE telegram_chat_id = %s", (chat_id,))
        existing = cursor.fetchone()
        if not existing:
            cursor.execute("""
                INSERT INTO users (telegram_chat_id, telegram_username, telegram_first_name, telegram_last_name)
                VALUES (%s, %s, %s, %s)
            """, (chat_id, username, first_name, last_name))
    conn.close()

def save_real_name(chat_id, real_first_name, real_last_name):
    conn = get_connection()
    with conn.cursor() as cursor:
        cursor.execute("""
            UPDATE users
            SET real_first_name = %s, real_last_name = %s
            WHERE telegram_chat_id = %s
        """, (real_first_name, real_last_name, chat_id))
    conn.close()

def get_user_by_chat_id(chat_id):
    conn = get_connection()
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM users WHERE telegram_chat_id = %s", (chat_id,))
        user = cursor.fetchone()
    conn.close()
    return user

def log_user_action(user_id, action: str):
    conn = get_connection()
    with conn.cursor() as cursor:
        cursor.execute("""
            INSERT INTO user_actions (user_id, action)
            VALUES (%s, %s)
        """, (user_id, action))
    conn.close()

def get_all_chat_ids():
    conn = get_connection()
    with conn.cursor() as cursor:
        cursor.execute("SELECT telegram_chat_id FROM users")
        results = cursor.fetchall()
        # Извлекаем chat_id из словарей для апшедулера
        chat_ids = [row['telegram_chat_id'] for row in results]
    conn.close()
    return chat_ids
