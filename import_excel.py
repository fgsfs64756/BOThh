import pandas as pd
import sqlite3
import os
import sys

# Путь к базе данных (рядом со скриптом)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "workers.db")

# Названия колонок — измени если нужно
COLUMNS = ["fio", "phone", "car_number", "car_color", "car_model", "city", "tariff"]

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS workers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fio TEXT,
            phone TEXT,
            car_number TEXT,
            car_color TEXT,
            car_model TEXT,
            city TEXT,
            tariff TEXT
        )
    """)
    conn.commit()
    conn.close()

def import_excel(filepath):
    if not os.path.exists(filepath):
        print(f"❌ Файл не найден: {filepath}")
        return

    print(f"📂 Читаю файл: {filepath}")
    df = pd.read_excel(filepath, header=None, dtype=str)
    df.columns = COLUMNS[:len(df.columns)]
    df = df.fillna("")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Очищаем старые данные
    cursor.execute("DELETE FROM workers")

    added = 0
    for _, row in df.iterrows():
        cursor.execute("""
            INSERT INTO workers (fio, phone, car_number, car_color, car_model, city, tariff)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            str(row.get("fio", "")).strip(),
            str(row.get("phone", "")).strip(),
            str(row.get("car_number", "")).strip(),
            str(row.get("car_color", "")).strip(),
            str(row.get("car_model", "")).strip(),
            str(row.get("city", "")).strip(),
            str(row.get("tariff", "")).strip(),
        ))
        added += 1

    conn.commit()
    conn.close()

    print(f"✅ Готово! Загружено записей: {added}")
    print(f"💾 База сохранена: {DB_PATH}")

if __name__ == "__main__":
    init_db()
    if len(sys.argv) > 1:
        import_excel(sys.argv[1])
    else:
        print("Использование: python import_excel.py путь_к_файлу.xlsx")
        print("Пример:        python import_excel.py baza.xlsx")
