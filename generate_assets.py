#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Генератор плейсхолдер-ассетов для рендерера.

Создаёт минимальный, но рабочий набор визуальных ассетов, чтобы можно было
проверить пайплайн без поиска готовых PNG:
  - chess_assets/pieces/{theme}/{code}.png  — фигуры (240×240, прозрачный фон)
  - chess_assets/boards/{theme}/board_240.png — доска (1920×1920)
  - chess_assets/fonts/*.ttf                 — шрифты DejaVu

Фигуры рисуются сплошными шахматными глифами Unicode (♚♛♜♝♞♟) с обводкой,
поэтому одинаково читаются на светлых и тёмных клетках.

Запуск:
    python generate_assets.py
"""

import shutil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

THEMES = ["green", "game-room", "dark-wood", "glass"]

# Цвета клеток (light, dark) по темам
BOARD_COLORS = {
    "green":      ((238, 238, 210), (118, 150, 86)),
    "game-room":  ((235, 209, 166), (165, 117, 80)),
    "dark-wood":  ((200, 160, 120), (110, 70, 40)),
    "glass":      ((220, 230, 235), (130, 160, 175)),
}

# code → сплошной глиф фигуры (filled chess symbols, U+265A..265F)
PIECE_GLYPHS = {
    "k": "♚", "q": "♛", "r": "♜",
    "b": "♝", "n": "♞", "p": "♟",
}

SQUARE = 240          # размер клетки/фигуры в пикселях
BOARD_PX = SQUARE * 8 # 1920

# Кандидаты системных шрифтов DejaVu
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/DejaVuSans.ttf",
]
_FONT_BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]


def _find_font(candidates):
    for p in candidates:
        if Path(p).exists():
            return p
    return None


def make_piece(glyph: str, is_white: bool, font_path: str) -> Image.Image:
    """Рисует фигуру: сплошной глиф с контрастной обводкой."""
    img = Image.new("RGBA", (SQUARE, SQUARE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(font_path, int(SQUARE * 0.78))

    fill = (245, 245, 245, 255) if is_white else (30, 30, 30, 255)
    stroke = (30, 30, 30, 255) if is_white else (235, 235, 235, 255)

    cx, cy = SQUARE // 2, SQUARE // 2
    draw.text((cx, cy), glyph, font=font, anchor="mm",
              fill=fill, stroke_width=max(3, SQUARE // 40), stroke_fill=stroke)
    return img


def make_board(light, dark) -> Image.Image:
    """Рисует доску 1920×1920 двумя цветами клеток."""
    img = Image.new("RGB", (BOARD_PX, BOARD_PX))
    draw = ImageDraw.Draw(img)
    for rank in range(8):
        for file in range(8):
            color = light if (rank + file) % 2 == 0 else dark
            draw.rectangle(
                [file * SQUARE, rank * SQUARE, (file + 1) * SQUARE, (rank + 1) * SQUARE],
                fill=color,
            )
    return img


def main():
    root = Path(__file__).parent
    font_path = _find_font(_FONT_CANDIDATES)
    if not font_path:
        raise SystemExit(
            "Не найден DejaVuSans.ttf. Установите fonts-dejavu или укажите путь в _FONT_CANDIDATES."
        )

    # 1. Шрифты → chess_assets/fonts/
    fonts_dir = root / "chess_assets" / "fonts"
    fonts_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(font_path, fonts_dir / "DejaVuSans.ttf")
    bold = _find_font(_FONT_BOLD_CANDIDATES)
    if bold:
        shutil.copy(bold, fonts_dir / "DejaVuSans-Bold.ttf")
    print(f"✓ Шрифты → {fonts_dir}")

    # 2. Фигуры (одинаковые для всех тем) + 3. доски
    for theme in THEMES:
        pieces_dir = root / "chess_assets" / "pieces" / theme
        boards_dir = root / "chess_assets" / "boards" / theme
        pieces_dir.mkdir(parents=True, exist_ok=True)
        boards_dir.mkdir(parents=True, exist_ok=True)

        for sym, glyph in PIECE_GLYPHS.items():
            make_piece(glyph, is_white=True, font_path=font_path).save(pieces_dir / f"w{sym}.png")
            make_piece(glyph, is_white=False, font_path=font_path).save(pieces_dir / f"b{sym}.png")

        light, dark = BOARD_COLORS[theme]
        make_board(light, dark).save(boards_dir / "board_240.png")
        print(f"✓ Тема '{theme}': 12 фигур + доска")

    print("\nГотово. Ассеты в chess_assets/. Можно запускать рендеринг.")


if __name__ == "__main__":
    main()
