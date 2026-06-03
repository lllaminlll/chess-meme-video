# chess-meme-video — контекст проекта

## Что это

Инструмент для автоматического создания шахматных highlight-видео с мем-GIF-оверлеями.
Два режима работы: **GUI** (`chess_video_gui.py`) для пакетного рендеринга и
**CLI** (`chess_meme_example.py`) для разовых запусков.

---

## Стек

| Слой | Технология |
|---|---|
| Шахматная логика | `python-chess` — разбор PGN, FEN, UCI, правила |
| Анализ позиций | Stockfish (локально, UCI) + Lichess Cloud Eval API (fallback) |
| Рендеринг кадров | `Pillow` — сборка PNG-кадров в памяти |
| Видео-рендеринг | `ffmpeg` через `subprocess.Popen` (rawvideo pipe → stdin) |
| Выбор мемов | `anthropic` SDK — `claude-haiku-4-5-20251001` |
| GUI | `tkinter` + `ttk` |
| Кэш оценок | JSON-файл `lichess_eval_cache.json` (FEN → pvs) |
| Кэш LLM | JSON-файл `llm_meme_cache.json` (FEN + event_type → meme) |

**Системные зависимости (не pip):** `ffmpeg`, `ffprobe`, `stockfish` — должны быть в PATH.

---

## Структура репозитория

```
chess-meme-video/
├── CLAUDE.md
├── .gitignore
├── requirements_chess_meme.txt
│
├── chess_video_gui.py          # GUI-приложение (Renderer + App)
├── chess_meme_example.py       # CLI: PGN → видео с мемами
│
├── chess_meme/                 # Пакет наложения мемов
│   ├── __init__.py
│   ├── event_classifier.py     # Правила классификации событий
│   ├── llm_selector.py         # LLM выбирает GIF для события
│   ├── meme_overlay.py         # ffmpeg filter_complex для GIF
│   ├── pipeline.py             # Оркестратор: classify → select → OverlaySpec
│   └── cache.py                # FEN-кэш LLM-ответов
│
├── assets/
│   └── memes/                  # *.gif файлы мемов (см. раздел "Мем-ассеты")
│
├── chess_assets/               # Визуальные ассеты рендерера (не в git)
│   ├── pieces/{theme}/         # Фигуры: wk.png, bq.png и т.д. (PNG, 240×240)
│   ├── boards/{theme}/         # Доска: board_240.png (PNG, 1920×1920)
│   └── fonts/                  # DejaVuSans.ttf, DejaVuSans-Bold.ttf
│
├── chess_icons/                # Иконки-бейджи оценок (не в git)
│   ├── brilliant.png           # !!
│   ├── good.png                # !
│   ├── inaccuracy.png          # ?!
│   ├── mistake.png             # ?
│   └── blunder.png             # ??
│
├── input_pgn/                  # PGN-файлы партий (локально)
└── output/                     # Готовые видео (не в git)
```

**Темы досок:** `green`, `game-room`, `dark-wood`, `glass`

---

## Где и как накладываются GIF-мемы

### Пайплайн наложения (`chess_meme/`)

```
PGN + eval_map
      │
      ▼
event_classifier.py → List[ChessEvent]
      │  Приоритет: checkmate > check > blunder > capture
      │  Порог блундера: ΔWinProb ≥ 0.20 (Lichess sigmoid)
      │  Порог взятия: ≥ 300 cp (слон/конь и выше)
      │
      ▼
llm_selector.py → MemeSelection (square, meme, reason)
      │  Модель: claude-haiku-4-5-20251001
      │  Если кандидат один — API не вызывается
      │  Кэш по (fen_before, event_type)
      │
      ▼
pipeline.py → List[OverlaySpec]
      │  OverlaySpec = {gif_path, square (0-63), start_sec, duration_sec}
      │  start_sec = момент окончания анимации хода (delay phase)
      │
      ▼
meme_overlay.py → apply_meme_overlays()
      │  ffmpeg filter_complex:
      │    [N:v] scale=WxH:flags=lanczos,format=rgba
      │    trim=duration=D,setpts=PTS-STARTPTS
      │    overlay=X:Y:enable='between(t,start,end)'
      │  Позиция = _square_bbox(square, board_offset, square_size)
      │  board_offset и square_size берутся из Renderer после рендера
      │
      ▼
output/game_with_memes.mp4
```

### Координаты квадрата на видео (`meme_overlay._square_bbox`)

```python
file_idx = chess.square_file(square)   # 0=a … 7=h
rank_idx = 7 - chess.square_rank(square)  # 0=rank8, 7=rank1 (экран ↓)
x0 = board_offset[0] + file_idx * square_size
y0 = board_offset[1] + rank_idx * square_size
```

---

## Мем-ассеты

### Расположение

```
assets/memes/*.gif
```

### Соответствие событие → файлы

| Событие | Кандидаты GIF |
|---|---|
| `checkmate` | `crying.gif`, `facepalm.gif` |
| `check` | `shock.gif`, `surprise.gif` |
| `blunder` | `facepalm.gif`, `crying.gif` |
| `capture` | `shock.gif`, `surprise.gif` |

Словарь `MEME_CANDIDATES` в `chess_meme/llm_selector.py:23` — единственное место,
где прописаны имена файлов. При добавлении нового GIF — добавить туда же.

### Требования к GIF-файлам

- Формат: анимированный GIF
- Размер: произвольный — ffmpeg масштабирует до размера клетки (`square_size` пикселей)
- Цветовое пространство: ffmpeg конвертирует в `rgba` автоматически
- Продолжительность на экране: `gif_duration` (по умолчанию 2.0 сек), задаётся в `build_overlay_specs()`

---

## Renderer (chess_video_gui.py)

### Ключевые атрибуты после `__init__`

| Атрибут | Описание |
|---|---|
| `board_offset: Tuple[int,int]` | Пиксельные координаты (x, y) верхнего левого угла доски |
| `square_size: int` | Размер одной клетки в пикселях |
| `base_anim_duration: float` | Длительность анимации хода (сек), по умолчанию 0.4 |
| `base_delay_duration: float` | Пауза после хода (сек), по умолчанию 0.8 |
| `video_width, video_height` | 1920×1080 (16:9) или 1080×1920 (9:16) |

### Метод `render_game_to_pipe()`

Принимает список SAN-ходов, скрипт с эффектами и пишет видео через ffmpeg pipe.
Возвращает `Path` к готовому файлу или `None` при ошибке.

**Формат `script`:**
```python
{
  "moves": [
    {"ply": 0, "san": "e4", "effects": {
        "mark": "!",           # символ для иконки-бейджа
        "bar_from_pct": 0.52,  # начало шкалы оценки (0..1, белые)
        "bar_to_pct": 0.55,    # конец шкалы оценки
        "anim": 0.4,           # переопределить длительность анимации
        "delay": 0.8,          # переопределить паузу
        "arrow": "e2-e4"       # нарисовать стрелку
    }},
    ...
  ],
  "trailer_moves": [...]  # ходы для анимированного трейлера
}
```

---

## Аннотация ходов (`annotate_moves` в chess_video_gui.py)

Пороги ΔWinProb (по формуле Lichess, диапазон 0..1):

| Метка | Условие |
|---|---|
| `!!` | Жертва материала с ΔWin ≥ 0.20 при ≤300cp до хода, или тактический ход |
| `!` | ΔWin ≥ 0.15 (или ≥ 0.20 после рокировки, ≥ 0.25 при шахе) |
| `?!` | ΔWin падение ≥ 0.10 |
| `?` | ΔWin падение ≥ 0.20 |
| `??` | ΔWin падение ≥ 0.30, или упущен мат |

---

## Частые задачи

**Добавить новый мем:**
1. Положить `имя.gif` в `assets/memes/`
2. Добавить `"имя.gif"` в нужный список в `MEME_CANDIDATES` (`chess_meme/llm_selector.py:23`)

**Изменить пороги классификации событий:**
- Блундер для мем-оверлея: `classify_events(blunder_threshold=...)` в `chess_meme/event_classifier.py:82`
- Аннотация ходов в GUI: константы `INACC_DELTA / MISTAKE_DELTA / BLUNDER_DELTA` в `chess_video_gui.py:322`

**Изменить длительность GIF на экране:**
- `build_overlay_specs(gif_duration=2.0)` в `chess_meme/pipeline.py`

**Сменить LLM-модель для выбора мемов:**
- `_DEFAULT_MODEL` в `chess_meme/llm_selector.py:30`

**Запустить без GUI:**
```bash
python chess_meme_example.py game.pgn \
    --stockfish /usr/bin/stockfish \
    --memes ./assets/memes \
    --out ./output
```
