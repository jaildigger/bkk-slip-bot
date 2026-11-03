"""Распознавание платёжек (слипов) Bangkok Bank.

OCR делает RapidOCR, дальше текст разбирается по меткам ("Amount", "From", "To", ...).
OCR то вставляет, то теряет пробелы ("03Nov26,15:23", "Bankreferenceno."),
поэтому метки сравниваются без пробелов и без учёта регистра.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional

import numpy as np
from PIL import Image

_ocr = None


def _get_ocr():
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr = RapidOCR()
    return _ocr


@dataclass
class Slip:
    paid_at: datetime
    amount: float
    fee: float = 0.0
    sender_name: str = ""
    sender_account: str = ""
    recipient_name: str = ""
    recipient_account: str = ""
    recipient_bank: str = ""
    bank_ref: str = ""
    tx_ref: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["paid_at"] = self.paid_at.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Slip":
        d = dict(d)
        d["paid_at"] = datetime.fromisoformat(d["paid_at"])
        return cls(**d)


class SlipParseError(Exception):
    pass


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def _ocr_lines(image_bytes: bytes) -> list[str]:
    """OCR + склейка фрагментов в строки (по вертикали), слева направо."""
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except (OSError, ValueError):
        raise SlipParseError("Файл не похож на картинку.")
    result, _ = _get_ocr()(np.array(img))
    if not result:
        return []
    boxes = []
    for box, text, _score in result:
        ys = [p[1] for p in box]
        boxes.append((min(ys), max(ys), box[0][0], text.strip()))
    boxes.sort(key=lambda b: (b[0] + b[1]) / 2)

    lines: list[list[tuple]] = []
    for b in boxes:
        center = (b[0] + b[1]) / 2
        if lines:
            last = lines[-1]
            top = min(x[0] for x in last)
            bottom = max(x[1] for x in last)
            if top <= center <= bottom:
                last.append(b)
                continue
        lines.append([b])
    # Метку ("From") и значение ("MR JOHN ...") держим отдельными элементами,
    # но в порядке чтения.
    out: list[str] = []
    for line in lines:
        for b in sorted(line, key=lambda x: x[2]):
            if b[3]:
                out.append(b[3])
    return out


_DATE_RE = re.compile(r"(\d{1,2})\s*([A-Za-z]{3})\s*(\d{2,4})\s*,?\s*(\d{1,2}):(\d{2})")
_MONEY_RE = re.compile(r"(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2})|\d+\.\d{2})")


def _money(s: str) -> Optional[float]:
    m = _MONEY_RE.search(s)
    if not m:
        return None
    return float(re.sub(r"[,\s]", "", m.group(1)))


def _parse_date(s: str) -> Optional[datetime]:
    m = _DATE_RE.search(s)
    if not m:
        return None
    day, mon, year, hh, mm = m.groups()
    if len(year) == 2:
        year = "20" + year
    try:
        return datetime.strptime(f"{day} {mon.title()} {year} {hh}:{mm}", "%d %b %Y %H:%M")
    except ValueError:
        return None


def _index_of(lines: list[str], label: str) -> int:
    key = _norm(label)
    for i, t in enumerate(lines):
        if _norm(t).startswith(key):
            return i
    return -1


def _after(lines: list[str], label: str, count: int) -> list[str]:
    i = _index_of(lines, label)
    if i < 0:
        return []
    return lines[i + 1:i + 1 + count]


def parse_lines(lines: list[str]) -> Slip:
    joined = _norm(" ".join(lines))
    if "bangkokbank" not in joined:
        raise SlipParseError("Не похоже на платёжку Bangkok Bank.")

    paid_at = None
    for t in lines:
        paid_at = _parse_date(t)
        if paid_at:
            break

    amount = None
    for t in _after(lines, "Amount", 2):
        amount = _money(t)
        if amount is not None:
            break
    if amount is None:
        raise SlipParseError("Не удалось прочитать сумму.")

    fee = 0.0
    for t in _after(lines, "Fee", 1):
        fee = _money(t) or 0.0

    sender = _after(lines, "From", 3)
    recipient = _after(lines, "To", 3)

    bank_ref = next((t for t in _after(lines, "Bank reference no", 1) if t.isdigit()), "")
    tx_ref = ""
    for t in _after(lines, "Transaction reference", 1):
        digits = re.sub(r"\D", "", t)
        if len(digits) >= 12:
            tx_ref = digits

    # Номер транзакции начинается с YYYYMMDDHHMM — запасной источник даты.
    if paid_at is None and tx_ref:
        try:
            paid_at = datetime.strptime(tx_ref[:12], "%Y%m%d%H%M")
        except ValueError:
            pass
    if paid_at is None:
        raise SlipParseError("Не удалось прочитать дату.")

    def pick(items: list[str], i: int) -> str:
        return items[i] if len(items) > i else ""

    return Slip(
        paid_at=paid_at,
        amount=amount,
        fee=fee,
        sender_name=pick(sender, 0),
        sender_account=pick(sender, 1),
        recipient_name=pick(recipient, 0),
        recipient_account=pick(recipient, 1),
        recipient_bank=pick(recipient, 2),
        bank_ref=bank_ref,
        tx_ref=tx_ref,
    )


def parse_slip(image_bytes: bytes) -> Slip:
    lines = _ocr_lines(image_bytes)
    if not lines:
        raise SlipParseError("На картинке не найден текст.")
    return parse_lines(lines)
