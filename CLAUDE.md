# chess-meme-video — контекст проекта

## Что это

Локальный инструмент, который берёт шахматную партию (PGN) и генерирует
видео, накладывая мем-GIF на клетки доски в ответ на события в партии —
шах, мат, взятие фигуры, зевок, жертва. Цель: короткие вирусные ролики
под Shorts/Reels/TikTok, где мем подбирается **по смыслу** события,
а не по жёсткому правилу.

Главный принцип — **разделение «что посчитано» и «как показано»**.
Stockfish считает цифры, LLM решает драматургию, рендерер только рисует.
Каждую стадию можно менять независимо.

---

## Стек

| Слой | Технология |
|---|---|
| Язык | Python 3.10+ |
| Шахматная логика | `python-chess` — PGN, FEN, UCI, правила |
| Анализ позиций | Stockfish (локально, UCI) + Lichess Cloud Eval API (fallback) |
| Рендеринг кадров | `Pillow` — сборка PNG-кадров в памяти |
| Видео-рендеринг | `ffmpeg` через `subprocess.Popen` (rawvideo pipe → stdin) |
| LLM (выбор мемов) | `anthropic` SDK — `claude-haiku-4-5-20251001` |
| GUI | `tkinter` + `ttk` |
| Кэш оценок | `lichess_eval_cache.json` (FEN → pvs) |
| Кэш LLM | `llm_meme_cache.json` (FEN + event_type → meme) |

**Системные зависимости (не pip):** `ffmpeg`, `ffprobe`, `stockfish` — в PATH.
**API-ключ:** `ANTHROPIC_API_KEY` в переменных окружения.

---

## Архитектура: 4 стадии

```
PGN / позиция
  → Анализатор      (Stockfish + python-chess): eval каждой позиции
  → Классификатор   (правила): что произошло, насколько это драматично
  → Режиссёр        (LLM): какой мем, на какой клетке, когда
  → Рендерер        (ffmpeg): собирает финальное видео с GIF-оверлеями
```

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
├── chess_meme/                 # Пакет мем-пайплайна
│   ├── __init__.py
│   ├── event_classifier.py     # Стадия 2: классификация событий
│   ├── llm_selector.py         # Стадия 3: LLM выбирает GIF
│   ├── meme_overlay.py         # Стадия 4: ffmpeg filter_complex
│   ├── pipeline.py             # Оркестратор: classify → select → OverlaySpec
│   └── cache.py                # FEN-кэш LLM-ответов
│
├── assets/
│   └── memes/                  # *.gif файлы (не в git)
│
├── chess_assets/               # Визуальные ассеты рендерера (не в git)
│   ├── pieces/{theme}/         # PNG фигур 240×240: wk, wq, wr, wb, wn, wp, bk...
│   ├── boards/{theme}/         # board_240.png
│   └── fonts/                  # DejaVuSans.ttf, DejaVuSans-Bold.ttf
│
├── chess_icons/                # Иконки бейджей оценок (не в git)
│   ├── brilliant.png  good.png  inaccuracy.png  mistake.png  blunder.png
│
└── input_pgn/                  # PGN-файлы партий (локально)
```

**Темы досок:** `green`, `game-room`, `dark-wood`, `glass`

---

## Статус реализации

### ✅ Стадия 1 — Анализатор (`chess_video_gui.py`)

- `get_eval_for_fen_batch()` — Lichess Cloud Eval API, multiPV=3, кэш
- `get_eval_for_fen_batch_local()` — Stockfish UCI, fallback для промахов API
- Eval всегда с точки зрения белых для шкалы; с точки зрения ходившего для аннотации
- Обрезка дебюта: `find_starting_move_index()` пропускает ходы пока |cp| < порога
- `annotate_moves()` → аннотация `!!` `!` `?!` `?` `??` по ΔWinProb (формула Lichess)

**Не реализовано из vision:**
- ❌ Точность игрока в % (chess.com-стиль) — только ΔWinProb
- ❌ «Лучший ход» от движка vs фактический (multiPV есть, но не экспортируется в пайплайн)
- ❌ Фаза партии (дебют/миттельшпиль/эндшпиль) как явный параметр события

---

### ✅ Стадия 2 — Классификатор событий (`chess_meme/event_classifier.py`)

Реализованные типы событий (приоритет по убыванию):

| Событие | Условие |
|---|---|
| `checkmate` | `board_after.is_checkmate()` |
| `check` | `board_after.is_check()` |
| `blunder` | ΔWinProb ≥ 0.20 (с точки зрения ходившего) |
| `capture` | взятие фигуры стоимостью ≥ 300 cp (слон/конь и выше) |

**Не реализовано из vision:**
- ❌ Уровни драмы 1–10 (события либо есть, либо нет)
- ❌ Упущенный мат в 1
- ❌ Жертва, оказавшаяся гениальной (как отдельный тип; `!!` есть в аннотации GUI)
- ❌ Камбэк из проигранной позиции
- ❌ Вилка, связка, матовые паттерны

Пороги:
- Блундер для мем-оверлея: `blunder_threshold=0.20` в `event_classifier.py:82`
- Взятие: `capture_min_value_cp=300` в `event_classifier.py:83`

---

### ✅ Стадия 3 — Режиссёр (`chess_meme/llm_selector.py`)

- Haiku получает текстовое описание события и список кандидатов-GIF
- Возвращает JSON `{square, meme, reason}` → парсится с fallback на первый кандидат
- Если кандидат один — API не вызывается
- Кэш по `(fen_before, event_type)` → не платить за повторные позиции

Текущие кандидаты (`MEME_CANDIDATES` в `llm_selector.py:23`):

| Событие | Кандидаты |
|---|---|
| `checkmate` | `crying.gif`, `facepalm.gif` |
| `check` | `shock.gif`, `surprise.gif` |
| `blunder` | `facepalm.gif`, `crying.gif` |
| `capture` | `shock.gif`, `surprise.gif` |

**Не реализовано из vision:**
- ❌ Теги эмоций на мемах и `meta.json` — сейчас просто список файлов
- ❌ Нарративная связность (LLM видит один ход, не дугу партии)
- ❌ Порог драмы для подключения LLM (сейчас всегда вызывается при >1 кандидате)
- ❌ Шаблоны стилей («токсичный комментатор» / «спокойный гроссмейстер»)
- ❌ `duration` и `sound` в JSON-контракте

---

### ✅ Стадия 4 — Рендерер (`chess_video_gui.py` + `chess_meme/meme_overlay.py`)

**Renderer (базовое видео):**
- Анимация ходов, ffmpeg pipe без промежуточных файлов
- Подсветка последнего хода, стрелки на взятиях и шахах
- Визуализация мата (эллипсы + стрелки атакующих фигур)
- Шкала оценки (eval bar), 4 позиции, анимируется между ходами
- Иконки-бейджи (`!!` `!` `?!` `?` `??`) на клетке назначения
- Координаты доски, водяной знак
- Стоп-кадр на мате с кастомным текстом
- Анимированный трейлер (N последних ходов)
- Ускорение до целевой длины 59/45/30 сек (бинарный поиск speed multiplier)
- Форматы: **16:9** (1920×1080) и **9:16** (1080×1920 Shorts)
- Боковые панели: видео или фото (Ken Burns эффект), оверлей «Подпишись»
- Мультиканальный пакетный рендеринг из JSON-проекта

**apply_meme_overlays (GIF-оверлеи):**
- `ffmpeg filter_complex`: scale → trim → overlay с `enable='between(t,...)'`
- Позиция GIF = пиксельные координаты клетки (`_square_bbox`)
- Несколько GIF в одном ffmpeg-вызове, цепочка фильтров

**Не реализовано из vision:**
- ❌ Звуковой дизайн — мемы без звука
- ❌ Авто-нарезка под Shorts (есть ускорение, но не автовырезка лучших моментов)
- ❌ Субтитры-капшены на видео

---

### ❌ Не начато

- **Импорт по ссылке** — вставил URL Lichess/Chess.com → получил PGN → видео
- **Кастомные библиотеки мемов** — пользователь грузит свои GIF с тегами

---

## Мем-ассеты

### Расположение и требования

```
assets/memes/*.gif   # анимированный GIF, любой размер — ffmpeg масштабирует
```

### Добавить новый мем

1. Положить `имя.gif` в `assets/memes/`
2. Добавить `"имя.gif"` в нужный список в `MEME_CANDIDATES` (`chess_meme/llm_selector.py:23`)

---

## Координаты клетки на видео (`meme_overlay._square_bbox`)

```python
file_idx = chess.square_file(square)        # 0=a … 7=h
rank_idx = 7 - chess.square_rank(square)    # 0=rank8, 7=rank1 (экран ↓)
x0 = board_offset[0] + file_idx * square_size
y0 = board_offset[1] + rank_idx * square_size
```

`board_offset` и `square_size` берутся из `Renderer` после рендера.

---

## Ключевые атрибуты Renderer

| Атрибут | Описание |
|---|---|
| `board_offset: Tuple[int,int]` | Пиксели (x, y) верхнего левого угла доски |
| `square_size: int` | Размер клетки в пикселях |
| `base_anim_duration: float` | Длит. анимации хода (сек), default 0.4 |
| `base_delay_duration: float` | Пауза после хода (сек), default 0.8 |
| `video_width, video_height` | 1920×1080 или 1080×1920 |

---

## Формат JSON-проекта (для GUI)

```json
{
  "games": [
    {
      "title": "Kasparov vs Deep Blue",
      "moves_san": ["e4", "c5", "Nf3", "..."],
      "initial_fen": null,
      "players": {"white": "Kasparov", "black": "Deep Blue"},
      "event": "World Chess Championship 1997"
    }
  ]
}
```

---

## Аннотация ходов (GUI-бейджи, не мем-пайплайн)

Пороги ΔWinProb (формула Lichess, диапазон 0..1):

| Метка | Условие |
|---|---|
| `!!` | Жертва материала с ΔWin ≥ 0.20 при ≤300cp до хода, или тактический ход |
| `!` | ΔWin ≥ 0.15 (≥ 0.20 после рокировки, ≥ 0.25 при шахе) |
| `?!` | ΔWin падение ≥ 0.10 |
| `?` | ΔWin падение ≥ 0.20 |
| `??` | ΔWin падение ≥ 0.30, или упущен мат |

Константы в `chess_video_gui.py:322`.

---

## Частые правки

**Добавить тип события:**
→ `chess_meme/event_classifier.py` — добавить ветку в `classify_events()`
→ `chess_meme/llm_selector.py:23` — добавить кандидатов в `MEME_CANDIDATES`

**Изменить пороги мем-пайплайна:**
→ `classify_events(blunder_threshold=...)` в `chess_meme/event_classifier.py:82`

**Изменить длительность GIF:**
→ GUI: спиннер «Длит. GIF» или `build_overlay_specs(gif_duration=2.0)` в `chess_meme/pipeline.py`

**Сменить LLM-модель:**
→ `_DEFAULT_MODEL` в `chess_meme/llm_selector.py:30`

**Запуск CLI (без GUI):**
```bash
ANTHROPIC_API_KEY=sk-... python chess_meme_example.py game.pgn \
    --stockfish /usr/bin/stockfish \
    --memes ./assets/memes \
    --out ./output \
    --theme dark-wood \
    --aspect 9:16
```

---

## Дорожная карта (приоритет сверху вниз)

1. **Уровни драмы 1–10** — добавить в `ChessEvent`, пороговать вызов LLM
2. **Больше типов событий** — упущенный мат в 1, камбэк, жертва-гений
3. **meta.json с тегами эмоций** — режиссёр выбирает по смыслу, не по списку
4. **Нарративная дуга** — LLM получает краткое саммари уже прошедших событий
5. **Звуковой дизайн** — аудио-дорожка к GIF
6. **Авто-нарезка** — 2–3 самых драматичных момента → отдельный Shorts
7. **Импорт по ссылке** — URL Lichess/Chess.com → PGN → видео
8. **Шаблоны стилей** — «токсичный комментатор» / «спокойный гроссмейстер»
9. **Кастомные библиотеки мемов** — пользователь грузит GIF с тегами
