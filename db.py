"""SQLite-хранилище. Картинки не храним — только распознанные поля.

Категории и платежи привязаны к chat_id: у каждого чата свой учёт
(бота можно добавить и в общий семейный чат).
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from slip_parser import Slip

DEFAULT_CATEGORIES = ["Электричество", "Вода", "Уборка", "Прочая аренда", "Бадминтон"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id      INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    name    TEXT NOT NULL,
    name_key TEXT NOT NULL,                 -- name.casefold(): NOCASE в SQLite не работает для кириллицы
    UNIQUE (chat_id, name_key)
);
CREATE TABLE IF NOT EXISTS payments (
    id                INTEGER PRIMARY KEY,
    chat_id           INTEGER NOT NULL,
    category_id       INTEGER NOT NULL REFERENCES categories(id),
    paid_at           TEXT NOT NULL,          -- ISO, местное время слипа
    month             TEXT NOT NULL,          -- 'YYYY-MM' для группировки
    amount            REAL NOT NULL,
    fee               REAL NOT NULL DEFAULT 0,
    sender_name       TEXT,
    sender_account    TEXT,
    recipient_name    TEXT,
    recipient_account TEXT,
    recipient_bank    TEXT,
    bank_ref          TEXT,
    tx_ref            TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_tx ON payments(chat_id, tx_ref) WHERE tx_ref <> '';
CREATE INDEX IF NOT EXISTS ix_payments_month ON payments(chat_id, month);
"""


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    # --- категории ---
    def categories(self, chat_id: int) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT id, name FROM categories WHERE chat_id = ? ORDER BY id", (chat_id,)
        ).fetchall()
        if not rows:
            for name in DEFAULT_CATEGORIES:
                self.add_category(chat_id, name)
            return self.categories(chat_id)
        return rows

    def add_category(self, chat_id: int, name: str) -> int:
        """Создаёт категорию или возвращает существующую с тем же именем."""
        name = " ".join(name.split())
        key = name.casefold()
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO categories (chat_id, name, name_key) VALUES (?, ?, ?)",
                (chat_id, name, key),
            )
        row = self.conn.execute(
            "SELECT id FROM categories WHERE chat_id = ? AND name_key = ?", (chat_id, key)
        ).fetchone()
        return row["id"]

    def category_name(self, chat_id: int, category_id: int) -> Optional[str]:
        row = self.conn.execute(
            "SELECT name FROM categories WHERE chat_id = ? AND id = ?", (chat_id, category_id)
        ).fetchone()
        return row["name"] if row else None

    # --- платежи ---
    def find_by_tx(self, chat_id: int, tx_ref: str) -> Optional[sqlite3.Row]:
        if not tx_ref:
            return None
        return self.conn.execute(
            "SELECT p.*, c.name AS category FROM payments p JOIN categories c ON c.id = p.category_id "
            "WHERE p.chat_id = ? AND p.tx_ref = ?",
            (chat_id, tx_ref),
        ).fetchone()

    def add_payment(self, chat_id: int, category_id: int, s: Slip) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO payments (chat_id, category_id, paid_at, month, amount, fee, "
                "sender_name, sender_account, recipient_name, recipient_account, recipient_bank, "
                "bank_ref, tx_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chat_id, category_id, s.paid_at.isoformat(timespec="minutes"),
                    s.paid_at.strftime("%Y-%m"), s.amount, s.fee,
                    s.sender_name, s.sender_account, s.recipient_name, s.recipient_account,
                    s.recipient_bank, s.bank_ref, s.tx_ref,
                ),
            )
        return cur.lastrowid

    def delete_payment(self, chat_id: int, payment_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM payments WHERE chat_id = ? AND id = ?", (chat_id, payment_id)
            )
        return cur.rowcount > 0

    def set_category(self, chat_id: int, payment_id: int, category_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE payments SET category_id = ? WHERE chat_id = ? AND id = ?",
                (category_id, chat_id, payment_id),
            )
        return cur.rowcount > 0

    def suggest_category(self, chat_id: int, recipient_account: str) -> Optional[int]:
        """Категория последнего платежа тому же получателю — как подсказка."""
        if not recipient_account:
            return None
        row = self.conn.execute(
            "SELECT category_id FROM payments WHERE chat_id = ? AND recipient_account = ? "
            "ORDER BY paid_at DESC LIMIT 1",
            (chat_id, recipient_account),
        ).fetchone()
        return row["category_id"] if row else None

    def month_payments(self, chat_id: int, month: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT p.id, p.paid_at, p.amount, p.fee, p.recipient_name, p.recipient_account, "
            "p.recipient_bank, p.bank_ref, p.tx_ref, p.category_id, c.name AS category "
            "FROM payments p JOIN categories c ON c.id = p.category_id "
            "WHERE p.chat_id = ? AND p.month = ? ORDER BY p.paid_at",
            (chat_id, month),
        ).fetchall()

    def month_totals(self, chat_id: int, month: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT c.id AS category_id, c.name AS category, SUM(p.amount) AS total, COUNT(*) AS cnt "
            "FROM payments p JOIN categories c ON c.id = p.category_id "
            "WHERE p.chat_id = ? AND p.month = ? GROUP BY c.id ORDER BY total DESC",
            (chat_id, month),
        ).fetchall()

    def months_summary(self, chat_id: int, limit: int = 12) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT month, SUM(amount) AS total, COUNT(*) AS cnt FROM payments "
            "WHERE chat_id = ? GROUP BY month ORDER BY month DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
