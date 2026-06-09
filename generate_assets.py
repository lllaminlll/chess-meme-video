#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Генератор визуальных ассетов для рендерера.

Создаёт набор визуальных ассетов с улучшенным многослойным рендерингом:
  - chess_assets/pieces/{theme}/{code}.png  — фигуры (240×240, прозрачный фон)
  - chess_assets/boards/{theme}/board_240.png — доска (1920×1920)
  - chess_assets/fonts/*.ttf                 — шрифты DejaVu

Фигуры рендерятся в 3 слоя (тень, свечение, основной глиф) через
Image.alpha_composite для объёмного вида.

Запуск:
    python generate_assets.py
"""

import shutil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

THEMES = ["green", "game-room", "dark-wood", "glass"]

# Цвета клеток (light, dark) по темам — обновлённая палитра
BOARD_COLORS = {
    "green":      ((240, 240, 210), (100, 140, 72)),   # классика chess.com
    "game-room":  ((235, 208, 165), (160, 110, 74)),   # орех
    "dark-wood":  ((195, 158, 115), (105, 65, 35)),    # тёмный орех
    "glass":      ((215, 228, 232), (120, 152, 168)),  # стальное стекло
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
    """
    Рисует фигуру в три слоя через alpha_composite:
      1. Мягкая тень (смещённый глиф, размытый GaussianBlur)
      2. Свечение / ореол (чуть шире, размытый)
      3. Основной глиф (чёткий, полная непрозрачность)
    """
    font_size = int(SQUARE * 0.80)
    sw = max(5, SQUARE // 28)   # base stroke width for sharp layer
    cx, cy = SQUARE // 2, SQUARE // 2

    font = ImageFont.truetype(font_path, font_size)
    base = Image.new("RGBA", (SQUARE, SQUARE), (0, 0, 0, 0))

    # --- Layer 1: soft shadow ---
    shadow_layer = Image.new("RGBA", (SQUARE, SQUARE), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_layer)
    shadow_alpha = 140 if is_white else 180
    shadow_draw.text(
        (cx + 4, cy + 5), glyph, font=font, anchor="mm",
        fill=(0, 0, 0, shadow_alpha),
        stroke_width=sw + 3,
        stroke_fill=(0, 0, 0, shadow_alpha),
    )
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=3.5))
    base = Image.alpha_composite(base, shadow_layer)

    # --- Layer 2: glow / halo ---
    glow_layer = Image.new("RGBA", (SQUARE, SQUARE), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow_layer)
    if is_white:
        glow_fill = (255, 250, 230, 90)
        glow_stroke = (255, 250, 230, 90)
    else:
        glow_fill = (90, 70, 40, 60)
        glow_stroke = (90, 70, 40, 60)
    glow_draw.text(
        (cx, cy), glyph, font=font, anchor="mm",
        fill=glow_fill,
        stroke_width=sw + 5,
        stroke_fill=glow_stroke,
    )
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=5))
    base = Image.alpha_composite(base, glow_layer)

    # --- Layer 3: sharp main glyph ---
    sharp_layer = Image.new("RGBA", (SQUARE, SQUARE), (0, 0, 0, 0))
    sharp_draw = ImageDraw.Draw(sharp_layer)
    if is_white:
        fill = (252, 247, 236, 255)    # тёплый кремовый
        stroke = (42, 32, 18, 255)     # тёмный контур
    else:
        fill = (28, 20, 12, 255)       # почти чёрный
        stroke = (208, 192, 168, 255)  # бежевый контур
    sharp_draw.text(
        (cx, cy), glyph, font=font, anchor="mm",
        fill=fill,
        stroke_width=sw,
        stroke_fill=stroke,
    )
    base = Image.alpha_composite(base, sharp_layer)

    return base


def make_board(light: tuple, dark: tuple) -> Image.Image:
    """
    Рисует доску 1920×1920:
      - Основной прямоугольник клетки
      - Тонкая внутренняя рамка для объёма
      - Внешняя рамка доски 3px
    """
    img = Image.new("RGB", (BOARD_PX, BOARD_PX))
    draw = ImageDraw.Draw(img)

    for rank in range(8):
        for file in range(8):
            is_light = (rank + file) % 2 == 0
            color = light if is_light else dark
            x0 = file * SQUARE
            y0 = rank * SQUARE
            x1 = x0 + SQUARE
            y1 = y0 + SQUARE

            # Main cell fill
            draw.rectangle([x0, y0, x1, y1], fill=color)

            # Thin inner border for depth
            if is_light:
                inner_border = tuple(min(255, c + 10) for c in color)
            else:
                inner_border = tuple(max(0, c - 12) for c in color)
            draw.rectangle([x0 + 1, y0 + 1, x1 - 2, y1 - 2], outline=inner_border, width=1)

    # Outer board border
    draw.rectangle([0, 0, BOARD_PX - 1, BOARD_PX - 1], outline=(30, 20, 10), width=3)

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

    # 2. Фигуры (одинаковые для всех тем) + 3. Доски
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
