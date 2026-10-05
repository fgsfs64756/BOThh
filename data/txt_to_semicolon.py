#!/usr/bin/env python3
"""
txt_to_semicolon.py — меняет разделитель в TXT файле с запятой на точку с запятой
Положите рядом с .txt файлом и запустите:
    python txt_to_semicolon.py
или укажите файл явно:
    python txt_to_semicolon.py myfile.txt
"""

import sys
import os
import csv
import glob
import time


def convert(input_path: str):
    output_path = os.path.splitext(input_path)[0] + "_semicolon.txt"
    total = 0
    start = time.time()

    # Определяем кодировку
    encoding = "utf-8"
    for enc in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            with open(input_path, "r", encoding=enc) as f:
                f.read(1024)
            encoding = enc
            break
        except:
            continue

    print(f"Читаю: {input_path} (кодировка: {encoding})")

    with open(input_path, "r", encoding=encoding, newline="") as fin, \
         open(output_path, "w", encoding=encoding, newline="") as fout:

        reader = csv.reader(fin, delimiter=",")
        writer = csv.writer(fout, delimiter=";", quoting=csv.QUOTE_MINIMAL)

        for row in reader:
            writer.writerow(row)
            total += 1
            if total % 100_000 == 0:
                print(f"  обработано: {total:,} строк  ({time.time()-start:.1f}с)", end="\r")

    elapsed = time.time() - start
    size_mb = os.path.getsize(output_path) / 1024 / 1024
    print(f"\nГотово! {total:,} строк за {elapsed:.1f}с")
    print(f"Файл: {output_path}  ({size_mb:.1f} МБ)")


def main():
    if len(sys.argv) > 1:
        path = sys.argv[1]
        if not os.path.exists(path):
            print(f"Файл не найден: {path}")
            sys.exit(1)
        convert(path)
        return

    script_dir = os.path.dirname(os.path.abspath(__file__))
    files = glob.glob(os.path.join(script_dir, "*.txt"))

    if not files:
        print("Файлы .txt не найдены рядом со скриптом.")
        print("Использование: python txt_to_semicolon.py [файл.txt]")
        sys.exit(1)

    if len(files) == 1:
        convert(files[0])
    else:
        print("Найдено несколько файлов:")
        for idx, f in enumerate(files, 1):
            print(f"  {idx}. {os.path.basename(f)}")
        choice = input("Выберите номер (или Enter чтобы конвертировать все): ").strip()
        if choice == "":
            for f in files:
                convert(f)
        elif choice.isdigit() and 1 <= int(choice) <= len(files):
            convert(files[int(choice) - 1])
        else:
            print("Неверный выбор.")
            sys.exit(1)


if __name__ == "__main__":
    main()
