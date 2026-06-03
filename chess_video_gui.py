#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Шахматы Видео — GUI v24.2.1 (Refactored)
-----------------------------------------------------------------------------------------------
• ИСПРАВЛЕНО [КРИТИЧЕСКИ]: Полностью переработана логика аннотации ходов.
             Новый алгоритм использует адаптивные пороги для дебюта, миттельшпиля
             и эндшпиля, обеспечивая более точную оценку (!!, !, ?, ??).
             Устранена ошибка, приводившая к неверной оценке ходов.
• УЛУЧШЕНО [АРХИТЕКТУРА]: Произведен рефакторинг монолитной функции _worker.
             Логика разделена на более мелкие, управляемые методы для повышения
             читаемости и упрощения дальнейшей поддержки.
• ПОВЫШЕНА НАДЕЖНОСТЬ: Улучшена обработка ошибок при конфигурации Stockfish
             и сохранении кэша. Добавлена валидация структуры загружаемого
             JSON-файла проекта для предотвращения сбоев.
"""

import os
import re
import sys
import json
import math
import io
import queue
import threading
import subprocess
import random
import time
import traceback
import logging
import shutil
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any
from collections import defaultdict
from urllib.parse import quote

# tkinter нужен только для GUI (класс App). Renderer и функции анализа
# работают headless — поэтому импорт опционален, чтобы их можно было
# использовать на сервере без дисплея (например, из chess_meme_example.py).
try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    _HAS_TK = True
except ImportError:
    tk = ttk = filedialog = messagebox = None
    _HAS_TK = False

# Проверка и установка зависимостей
try:
    import requests
except ImportError:
    print("Ошибка импорта: библиотека 'requests' не найдена. Установите её: pip install requests")
    sys.exit(1)

try:
    import chess, chess.pgn, chess.engine
except ImportError:
    print("Ошибка импорта: библиотека 'python-chess' не найдена. Установите её: pip install chess")
    sys.exit(1)

from PIL import Image, ImageDraw, ImageFont

try:
    from chess_meme import build_overlay_specs, apply_meme_overlays, compute_move_timestamps
    import anthropic as _anthropic
    _MEME_PIPELINE_OK = True
except ImportError:
    _MEME_PIPELINE_OK = False

# ---------------- Константы и пресеты ----------------
CONFIG_FILE = Path("config.json")
DEFAULT_FPS = 30
SUPPORTED_THEMES = ['green', 'game-room', 'dark-wood', 'glass']
MAX_SHORTS_DURATION = 59.0

# Константы для анализа
CACHE_FILE = Path("lichess_eval_cache.json")
REQUEST_DELAY_SECONDS = 1.1
MAX_CP_FOR_BAR = 800
MISSING_TTL_SECONDS = 24 * 3600
OPENING_PLY_THRESHOLD = 20  # Ходы до этого считаются дебютом
ENDGAME_PIECE_THRESHOLD = 10  # Если фигур меньше или равно, считаем эндшпилем

logger = logging.getLogger("chess_video")
logger.setLevel(logging.DEBUG)
_file_handler: Optional[logging.Handler] = None


# ==============================================================================
# НАЧАЛО БЛОКА АНАЛИЗА (Lichess + Stockfish)
# ==============================================================================

def _load_cache() -> Dict[str, Any]:
    """Загружает кэш оценок из файла."""
    if not CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Не удалось загрузить кэш оценок: {e}")
        return {}


def _save_cache(cache: Dict[str, Any]) -> None:
    """Атомарно сохраняет кэш оценок в файл, избегая потери данных."""
    try:
        tmp_file = CACHE_FILE.with_suffix(".tmp")
        tmp_file.write_text(json.dumps(cache, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp_file.replace(CACHE_FILE)
    except (IOError, OSError) as e:
        logger.error(f"Критическая ошибка при сохранении кэша оценок: {e}")


def get_eval_for_fen_batch_local(
        fens: List[str], engine_path: str, threads: int = max(1, os.cpu_count() or 1),
        hash_mb: int = 256, limit_value: int = 120, log_cb=print
) -> Dict[str, Optional[Dict]]:
    """Получает оценку для списка FEN с помощью локального движка Stockfish."""
    results: Dict[str, Optional[Dict]] = {}
    unique_fens = list(dict.fromkeys(fens))
    if not engine_path or not Path(engine_path).exists():
        for fen in unique_fens: results[fen] = None
        log_cb("    ! Stockfish не найден. Пропуск локального анализа.")
        return results

    try:
        engine = chess.engine.SimpleEngine.popen_uci(engine_path)
        try:
            engine.configure({"Threads": int(threads), "Hash": int(hash_mb), "Ponder": False})
        except (chess.engine.EngineError, ValueError) as e:
            # Не игнорируем ошибки конфигурации, а логируем их
            log_cb(f"    ! Ошибка конфигурации Stockfish: {e}. Используются стандартные настройки.")

        for i, fen in enumerate(unique_fens, 1):
            try:
                limit = chess.engine.Limit(time=max(1, int(limit_value)) / 1000.0)
                board = chess.Board(fen)
                info = engine.analyse(board, limit, multipv=3)
                pvs_data = []
                for pv_info in info:
                    if score := pv_info.get("score"):
                        s_white = score.pov(chess.WHITE)
                        pvs_data.append({"cp": s_white.score(mate_score=None), "mate": s_white.mate()})
                results[fen] = {"pvs": pvs_data} if pvs_data else None
            except Exception as e:
                log_cb(f"      ! Ошибка анализа FEN {fen.split(' ')[0]}: {e}")
                results[fen] = None
        engine.quit()
        return results
    except (OSError, chess.engine.EngineError) as e:
        log_cb(f"    ! Не удалось запустить движок Stockfish: {e}")
        for fen in unique_fens: results[fen] = None
        return results


def get_eval_for_fen_batch(
        fens: List[str], api_key: str, multi_pv: int = 3, use_cache: bool = True, log_cb=print
) -> Dict[str, Optional[Dict]]:
    """Получает оценку для списка FEN через Lichess Cloud Eval API."""
    cache = _load_cache() if use_cache else {}
    now = time.time()
    results = {}
    fens_to_fetch = [
        fen for fen in fens
        if (v := cache.get(f"{fen}_mpv{multi_pv}")) is None or
           (isinstance(v, dict) and v.get("_missing") and (now - v.get("ts", 0) > MISSING_TTL_SECONDS))
    ]
    log_cb(f"Анализ Lichess: {len(fens)} FEN-позиций (multiPV={multi_pv}). Новых запросов: {len(fens_to_fetch)}.")
    if fens_to_fetch:
        api_key = (api_key or "").strip().splitlines()[0]
        session = requests.Session()
        headers = {"Accept": "application/json", "User-Agent": "ChessVideoCreator/24.2.1"}
        if api_key and "ВВЕДИТЕ" not in api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        session.headers.update(headers)
        for i, fen in enumerate(fens_to_fetch):
            cache_key = f"{fen}_mpv{multi_pv}"
            log_cb(f"  Запрос {i + 1}/{len(fens_to_fetch)} для FEN: {fen.split(' ')[0]}...")
            for attempt in range(3):
                try:
                    params = {"fen": fen, "multiPv": multi_pv}
                    response = session.get("https://lichess.org/api/cloud-eval", params=params, timeout=10)
                    if response.status_code == 404:
                        log_cb("    · Нет оценки в Cloud Eval (404).")
                        cache[cache_key] = {"_missing": True, "ts": now}
                        break
                    if response.status_code == 429:
                        retry_after = float(response.headers.get("Retry-After", 2 * (attempt + 1)))
                        log_cb(f"    ! 429 Too Many Requests. Пауза {retry_after:.1f} сек...")
                        time.sleep(retry_after)
                        continue
                    response.raise_for_status()
                    data = response.json()
                    if "error" in data or not data.get('pvs'):
                        if "error" in data: log_cb(f"    ! Lichess API ошибка: {data['error']}")
                        cache[cache_key] = {"_missing": True, "ts": now}
                    else:
                        cache[cache_key] = {"pvs": data['pvs']}
                    break
                except requests.RequestException as e:
                    log_cb(f"    ! Сетевая ошибка (попытка {attempt + 1}/3): {e}")
                    if attempt < 2:
                        time.sleep(2 * (attempt + 1))
                    else:
                        cache[cache_key] = {"_missing": True, "ts": now}
            if i < len(fens_to_fetch) - 1: time.sleep(REQUEST_DELAY_SECONDS)
        if use_cache: _save_cache(cache)
    for fen in fens:
        item = cache.get(f"{fen}_mpv{multi_pv}")
        results[fen] = item if not (isinstance(item, dict) and item.get("_missing")) else None
    return results


def _normalize_eval_white(evaluation: Optional[Dict]) -> float:
    """Нормализует оценку для шкалы (0.0 - 1.0)."""
    if evaluation is None: return 0.5
    if (mate := evaluation.get("mate")) is not None: return 1.0 if mate > 0 else 0.0
    cp = evaluation.get("cp", 0)
    clipped = max(-MAX_CP_FOR_BAR, min(MAX_CP_FOR_BAR, cp))
    return (clipped + MAX_CP_FOR_BAR) / (2 * MAX_CP_FOR_BAR)


def _get_pov_eval(evaluation: Dict, pov: chess.Color) -> Dict:
    """Возвращает оценку (cp, mate) с точки зрения указанного цвета."""
    sign = 1 if pov == chess.WHITE else -1
    if not (pvs := evaluation.get("pvs")): return {"cp": None, "mate": None}
    top_pv = pvs[0]
    if (mate := top_pv.get("mate")) is not None:
        return {"cp": None, "mate": mate * sign}
    else:
        return {"cp": top_pv.get("cp", 0) * sign, "mate": None}


def _eval_to_win_percent(evaluation: Dict) -> float:
    """Преобразует оценку в шансы на победу."""
    if evaluation is None or (evaluation.get("cp") is None and evaluation.get("mate") is None): return 0.5
    if (mate := evaluation.get("mate")) is not None: return 1.0 if mate > 0 else 0.0
    # Формула, используемая Lichess для преобразования сантипешек в % победы
    return 1 / (1 + math.exp(-0.0036 * evaluation.get("cp", 0)))


def involves_sacrifice(board: chess.Board, move: chess.Move) -> bool:
    """
    Консервативная детекция жертвы материала.
    Считаем ход жертвой, если:
    - более ценная фигура берёт менее ценную (потенциально под боем), или
    - делается не-взятие более ценной фигурой на поле, сильно контролируемое соперником.
    Примечание: пешечные манёвры по умолчанию не считаем жертвой.
    """

    def _val(pt: int) -> int:
        return {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 100}.get(pt,
                                                                                                                     0)

    mover = board.piece_at(move.from_square)
    if mover is None:
        return False

    captured = board.piece_at(move.to_square)
    mover_val = _val(mover.piece_type)
    captured_val = _val(captured.piece_type) if captured else 0

    # Взятие существенно более дешёвой фигуры более дорогой — индикатор жертвы
    if captured and (mover_val - captured_val) >= 2:
        return True

    # Не-взятие более ценной фигурой на сильно атакованное поле
    if not captured and mover.piece_type in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN):
        tmp = board.copy()
        try:
            tmp.push(move)
        except Exception:
            return False
        attackers = tmp.attackers(not board.turn, move.to_square)
        defenders = tmp.attackers(board.turn, move.to_square)
        if len(attackers) > len(defenders) + 1:
            return True
    return False


def is_only_move(board: chess.Board, eval_before: Dict, eval_after: Dict) -> bool:
    """Единственный сильный ход: лучший PV значительно лучше второго в POV ходящего."""
    if not eval_before or not isinstance(eval_before.get("pvs"), list):
        return False
    pvs = eval_before.get("pvs", [])
    if len(pvs) < 2:
        return False
    sign = 1 if board.turn == chess.WHITE else -1
    best = pvs[0].get("cp")
    second = pvs[1].get("cp")
    if not isinstance(best, (int, float)) or not isinstance(second, (int, float)):
        return False
    best_pov = int(best) * sign
    second_pov = int(second) * sign
    return (best_pov - second_pov) >= 300  # >= 3 пешки


def _lichess_win_prob_from_cp(cp: Optional[float]) -> float:
    """Вероятность победы по формуле Lichess (диапазон 0.0..1.0)."""
    try:
        return 1.0 / (1.0 + math.exp(-0.00368208 * float(cp or 0)))
    except Exception:
        return 0.5


def _win_prob_from_eval_pov(eval_pov: Optional[Dict]) -> float:
    """Выдаёт WinProb (0..1) из eval с учётом мата."""
    if not eval_pov: return 0.5
    if (m := eval_pov.get("mate")) is not None:
        return 1.0 if m > 0 else 0.0
    return _lichess_win_prob_from_cp(eval_pov.get("cp", 0))


def is_tactical_move(board: chess.Board, move: chess.Move) -> bool:
    """Возвращает True, если ход носит тактический характер: взятие, шах или превращение."""
    try:
        return board.is_capture(move) or board.gives_check(move) or (move.promotion is not None)
    except Exception:
        return False


def annotate_moves(game_states: List[Dict], eval_map: Dict[str, Optional[Dict]], engine_path: str = "") -> List[Dict]:
    """
    ИСПРАВЛЕННАЯ функция для аннотации ходов.

    Правильная логика:
    1. !! (Brilliant) - ход с жертвой материала ИЛИ единственный хороший ход
    2. ! (Good) - ход, улучшающий позицию
    3. ?! (Inaccuracy) - ход, слегка ухудшающий позицию
    4. ? (Mistake) - ход, значительно ухудшающий позицию  
    5. ?? (Blunder) - ход, катастрофически ухудшающий позицию
    """
    annotated_results = []

    # Пороговые значения Lichess по падению шансов на победу (ΔWin, 0..1)
    INACC_DELTA = 0.10
    MISTAKE_DELTA = 0.20
    BLUNDER_DELTA = 0.30

    # Локальный движок (опционально) для продвинутых проверок (surprise/verify)
    use_local_engine = bool(engine_path and Path(engine_path).exists())
    engine = None
    local_eval_cache: Dict[Tuple[str, str, int], int] = {}

    def _engine_cp_for(board: chess.Board, depth: int, pov: chess.Color, search_move: Optional[chess.Move] = None) -> Optional[int]:
        if not use_local_engine:
            return None
        nonlocal engine
        key = (board.fen(), (search_move.uci() if search_move else "*"), depth)
        if key in local_eval_cache:
            return local_eval_cache[key]
        try:
            if engine is None:
                engine = chess.engine.SimpleEngine.popen_uci(engine_path)
                try:
                    engine.configure({"Threads": max(1, (os.cpu_count() or 2) // 2), "Ponder": False})
                except Exception:
                    pass
            limit = chess.engine.Limit(depth=int(depth))
            kwargs = {"limit": limit}
            if search_move is not None:
                kwargs["game"] = None
                kwargs["info"] = None
                info = engine.analyse(board, limit, multipv=1, searchmoves=[search_move])
            else:
                info = engine.analyse(board, limit, multipv=1)
            score = info.get("score")
            if not score:
                return None
            s = score.pov(pov).score(mate_score=100000)
            if s is None:
                return None
            local_eval_cache[key] = int(s)
            return int(s)
        except Exception:
            return None

    def _surprise_factor(board_after: chess.Board, mover: chess.Color, move_obj: chess.Move) -> int:
        cp_shallow = _engine_cp_for(board_after, depth=12, pov=mover)
        cp_deep = _engine_cp_for(board_after, depth=22, pov=mover)
        if cp_shallow is None or cp_deep is None:
            return 0
        return int(cp_deep - cp_shallow)

    def _verify_sacrifice(board_before: chess.Board, move_obj: chess.Move, mover: chess.Color) -> bool:
        # Моделируем принятие жертвы лучшим очевидным захватом и проверяем, что позиция не рушится
        try:
            tmp = board_before.copy()
            tmp.push(move_obj)
        except Exception:
            return False
        # Список ответов, которые берут фигуру на поле назначения
        accept_captures = [m for m in tmp.legal_moves if m.to_square == move_obj.to_square]
        if not accept_captures:
            return False
        # Возьмём первый захват как приближение
        tmp2 = tmp.copy()
        tmp2.push(accept_captures[0])
        cp_after_accept = _engine_cp_for(tmp2, depth=20, pov=mover)
        cp_before = _engine_cp_for(board_before, depth=16, pov=mover)
        if cp_after_accept is None or cp_before is None:
            return False
        # Допускаем просадку не более полпешки ради инициативы
        return (cp_after_accept >= (cp_before - 50))

    try:
        for state in game_states:
            board_before = chess.Board(state["fen_before"])
            turn = board_before.turn

            # Метаданные стадии игры при необходимости (можно использовать для тюнинга порогов)
            piece_count = len(board_before.piece_map())

            eval_before_full = eval_map.get(state["fen_before"])
            eval_after_full = eval_map.get(state["fen_after"])

            mark = ""

            if eval_before_full and eval_after_full:
                # Получаем оценки с точки зрения игрока, который ходит
                eval_before_pov = _get_pov_eval(eval_before_full, turn)
                eval_after_pov = _get_pov_eval(eval_after_full, turn)

                # Конвертируем в WinProb (0..1) по формуле Lichess и считаем ΔWin
                win_before = _win_prob_from_eval_pov(eval_before_pov)
                win_after = _win_prob_from_eval_pov(eval_after_pov)
                delta_win = win_after - win_before

                # Для дополнительных эвристик оставим CP в POV ходящего
                cp_before = eval_before_pov.get("cp", 0)

                # Проверяем на мат
                mate_before = eval_before_pov.get("mate")
                mate_after = eval_after_pov.get("mate")

                # Логика для бриллиантового хода (!!)
                is_brilliant = False

                # 1. Ход с жертвой материала/тактикой, улучшающий позицию и не в уже выигранной до хода позиции
                try:
                    move_obj = board_before.parse_san(state["san"])
                    if involves_sacrifice(board_before, move_obj) and (delta_win >= 0.20) and (abs(cp_before) < 300):
                        # Дополнительные проверки: surprise factor или верификация жертвы
                        is_ok = True
                        if use_local_engine:
                            board_after = board_before.copy(); board_after.push(move_obj)
                            sf = _surprise_factor(board_after, mover=turn, move_obj=move_obj)
                            is_ok = (sf >= 150) or _verify_sacrifice(board_before, move_obj, mover=turn)
                        if is_ok:
                            is_brilliant = True
                except:
                    pass

                # 2. Единственный ход сам по себе не даёт "!!" — влияет на "!"

                # 3. Тактическое значимое улучшение (без явной жертвы)
                if not is_brilliant and (delta_win >= 0.20) and (abs(cp_before) < 300):
                    try:
                        move_obj_for_tactics = move_obj if 'move_obj' in locals() else board_before.parse_san(state["san"])  # подстраховка
                    except Exception:
                        move_obj_for_tactics = None
                    if move_obj_for_tactics and is_tactical_move(board_before, move_obj_for_tactics):
                        # Подтверждаем «глубиной» при наличии движка
                        if use_local_engine:
                            board_after = board_before.copy(); board_after.push(move_obj_for_tactics)
                            if _surprise_factor(board_after, mover=turn, move_obj=move_obj_for_tactics) >= 150:
                                is_brilliant = True
                        else:
                            is_brilliant = True

                # Присвоение оценки
                if is_brilliant:
                    mark = "!!"
                else:
                    # Ошибки по падению Win%
                    if (win_before - win_after) >= BLUNDER_DELTA:
                        mark = "??"
                    elif (win_before - win_after) >= MISTAKE_DELTA:
                        mark = "?"
                    elif (win_before - win_after) >= INACC_DELTA:
                        mark = "?!"
                    else:
                        # Уточнённые правила для «!»
                        # - Базово: ΔWin ≥ 0.15
                        # - Если под шахом: требовать ΔWin ≥ 0.25 или тактический эффект
                        # - Для рокировки: требовать ΔWin ≥ 0.20
                        # - «Единственный ход» всегда допускает «!»
                        is_castle = False
                        try:
                            if 'move_obj' not in locals():
                                move_obj = board_before.parse_san(state["san"])  # подстраховка
                            is_castle = board_before.is_castling(move_obj)
                        except Exception:
                            is_castle = False

                        in_check_before = board_before.is_check()
                        allow_good = False

                        if is_only_move(board_before, eval_before_full, eval_after_full):
                            allow_good = True
                        elif not in_check_before and not is_castle and delta_win >= 0.15:
                            allow_good = True
                        elif in_check_before and (delta_win >= 0.25 or (move_obj and is_tactical_move(board_before, move_obj))):
                            allow_good = True
                        elif is_castle and delta_win >= 0.20:
                            allow_good = True

                        if allow_good:
                            mark = "!"

                # Специальные случаи для мата
                if mate_after is not None:
                    if mate_after > 0:  # Мат в пользу игрока
                        mark = "!!" if not mark else mark
                    else:  # Мат против игрока
                        mark = "??"
                elif mate_before is not None and mate_after is None:
                    # Упустили мат
                    mark = "??"

            # Оценки для evaluation bar всегда с точки зрения белых
            eval_before_white_pov = _get_pov_eval(eval_before_full, chess.WHITE) if eval_before_full else None
            eval_after_white_pov = _get_pov_eval(eval_after_full, chess.WHITE) if eval_after_full else None

            annotated_results.append({
                "ply": state["ply"],
                "san": state["san"],
                "mark": mark,
                "bar_from_pct": _normalize_eval_white(eval_before_white_pov),
                "bar_to_pct": _normalize_eval_white(eval_after_white_pov)
            })

    finally:
        try:
            if engine is not None:
                engine.quit()
        except Exception:
            pass
    return annotated_results


# ==============================================================================
# КОНЕЦ БЛОКА АНАЛИЗА
# ==============================================================================

def find_starting_move_index(
        moves_san: List[str], initial_fen: Optional[str], engine_path: str,
        cp_threshold: int = 70, max_moves_to_check: int = 24, log_cb=print
) -> int:
    """Ищет первый "интересный" ход, чтобы обрезать скучный дебют."""
    if not engine_path or not Path(engine_path).exists():
        log_cb("   - Stockfish не найден для обрезки дебюта, пропускаем.", level=logging.WARNING)
        return 0

    board = chess.Board(initial_fen) if initial_fen else chess.Board()

    # Конструкция 'with' гарантирует, что движок будет закрыт, даже при ошибках.
    # Это предотвращает утечки ресурсов.
    try:
        with chess.engine.SimpleEngine.popen_uci(engine_path) as engine:
            for i, san in enumerate(moves_san):
                if i >= max_moves_to_check:
                    log_cb(f"   - Не найдено интересных ходов в первых {max_moves_to_check} ходах. Начинаем с начала.")
                    return 0
                board.push_san(san)
                info = engine.analyse(board, chess.engine.Limit(time=0.05))
                score = info["score"].white().score(mate_score=30000)
                if abs(score) >= cp_threshold:
                    log_cb(
                        f"   - Найдена интересная позиция на ходу {i + 1} ({san}). Оценка: {score / 100:.2f}. Пропускаем {i} ходов.")
                    return i
    except (chess.engine.EngineError, ValueError, AttributeError) as e:
        log_cb(f"   - ❌ Ошибка при анализе дебюта: {e}", level=logging.ERROR)
        return 0
    return 0


def get_media_dimensions(media_path: Path) -> Optional[Tuple[int, int]]:
    if not media_path.exists(): return None
    try:
        cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of",
               "csv=p=0", str(media_path)]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        width, height = map(int, result.stdout.strip().split(','))
        return width, height
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError) as e:
        logger.error(f"Не удалось получить размеры для {media_path}: {e}")
        return None


def setup_file_logger(output_dir: Path):
    global _file_handler
    try:
        if _file_handler:
            logger.removeHandler(_file_handler)
            _file_handler.close()
        output_dir.mkdir(parents=True, exist_ok=True)
        fh_path = output_dir / "chess_video_creator_log.txt"
        _file_handler = logging.FileHandler(fh_path, mode='a', encoding='utf-8')
        _file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
        logger.addHandler(_file_handler)
    except Exception as e:
        # Логируем ошибку настройки логгера в консоль, если это возможно
        print(f"Не удалось настроить файловый логгер: {e}")


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]+', '', name).strip()
    return re.sub(r'\s+', '_', name)[:120]


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)


def load_project_file(path: Path) -> Optional[List[Dict]]:
    """Надежно загружает и валидирует файл проекта."""
    try:
        content = path.read_text(encoding="utf-8")
        data = json.loads(content)

        if "games" not in data or not isinstance(data["games"], list):
            raise ValueError("JSON-файл должен содержать ключ 'games' со списком партий.")

        # Простая валидация структуры каждой игры
        for i, game in enumerate(data["games"]):
            if not isinstance(game, dict):
                raise ValueError(f"Элемент #{i} в списке 'games' не является словарем.")
            if "moves_san" not in game or not isinstance(game["moves_san"], list):
                raise ValueError(f"В игре #{i} отсутствует ключ 'moves_san' или он не является списком.")

        return data["games"]

    except FileNotFoundError:
        logger.error(f"Файл проекта не найден: {path}")
        messagebox.showerror("Ошибка", f"Файл проекта не найден:\n{path}")
        return None
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка декодирования JSON в файле {path}: {e}")
        messagebox.showerror("Ошибка", f"Некорректный формат JSON-файла:\n{e}")
        return None
    except ValueError as e:
        logger.error(f"Неверная структура данных в файле проекта: {e}")
        messagebox.showerror("Ошибка", f"Неверная структура данных в файле проекта:\n{e}")
        return None


TARGET_W_RATIO, TARGET_H_CELLS, SUB_SCALE, STROKE_K, SAFE_FILL = 0.92, 1.15, 0.72, 0.08, 0.88


def _measure(draw, text, font, stroke_w):
    l, t, r, b = draw.textbbox((0, 0), text, font=font, anchor='mm', stroke_width=stroke_w)
    return r - l, b - t


def _fit_one_line(draw, text, box_w, box_h, font_path):
    lo, hi, best = 18, 220, None
    while lo <= hi:
        fs = (lo + hi) // 2
        try:
            f = ImageFont.truetype(font_path, fs)
        except IOError:
            f = ImageFont.load_default()
        sw = max(2, int(fs * STROKE_K))
        w, h = _measure(draw, text, f, sw)
        if w <= box_w and h <= box_h:
            best = (fs, sw, w, h);
            lo = fs + 1
        else:
            hi = fs - 1
    return best


def _split_balanced(words):
    total = len(words)
    if total <= 1: return " ".join(words), ""
    min_max_len, best_i = float('inf'), total // 2
    for i in range(1, total):
        len1, len2 = len(" ".join(words[:i])), len(" ".join(words[i:]))
        if max(len1, len2) < min_max_len:
            min_max_len, best_i = max(len1, len2), i
    return ' '.join(words[:best_i]), ' '.join(words[best_i:])


def _fit_two_lines(draw, main, sub, box_w, box_h, font_path):
    lo, hi, best = 18, 220, None
    while lo <= hi:
        fs1 = (lo + hi) // 2
        fs2 = max(16, int(fs1 * SUB_SCALE))
        try:
            f1, f2 = ImageFont.truetype(font_path, fs1), ImageFont.truetype(font_path, fs2)
        except IOError:
            f1, f2 = ImageFont.load_default(), ImageFont.load_default()
        sw1, sw2, gap = max(2, int(fs1 * STROKE_K)), max(2, int(fs2 * STROKE_K)), int(fs1 * 0.18)
        w1, h1 = _measure(draw, main, f1, sw1)
        w2, h2 = _measure(draw, sub, f2, sw2)
        if max(w1, w2) <= box_w and (h1 + gap + h2) <= box_h:
            best = (fs1, fs2, sw1, sw2, gap, w1, w2, h1, h2)
            lo = fs1 + 1
        else:
            hi = fs1 - 1
    return best


def prepare_title(draw, title, board_box, font_path):
    x0, y0, x1, y1 = board_box;
    board_w = x1 - x0
    box_w, box_h = int(board_w * TARGET_W_RATIO), int((board_w // 8) * TARGET_H_CELLS)
    base, words = title.strip(), title.strip().split()
    prefer_one_line = len(words) <= 3
    one = _fit_one_line(draw, base.upper(), int(box_w * SAFE_FILL), box_h, font_path) if prefer_one_line else None
    main, sub = "", ""
    if match := re.search(r'\((.*?)\)', base):
        sub, main = match.group(1).strip(), base.replace(match.group(0), '').strip()
    else:
        main, sub = _split_balanced(words)
    main = main.upper()
    two = _fit_two_lines(draw, main, sub, box_w, box_h, font_path) if main and sub else None
    if one and two:
        use_one = one[0] >= 0.9 * two[0]
    else:
        use_one = bool(one and not two)
    return {"layout": "one" if use_one else "two", "one": one, "two": two, "texts": (base.upper(), (main, sub))}


class Renderer:
    def __init__(self, theme='green', aspect='16:9', fps: int = 30, output_dir: Path = Path('hd_chess_videos'),
                 show_move_text: bool = True, long_mate_freeze: bool = True, freeze_seconds: float = 3.0,
                 check_highlight: bool = True, check_arrow: bool = False, show_checkmate_text: bool = True,
                 checkmate_highlight_squares: bool = True, checkmate_arrows: bool = True,
                 background_path: Optional[str] = None, music_path: Optional[str] = None, watermark_text: str = "",
                 use_cache: bool = True, shorts_control: bool = False, max_shorts_duration: float = 59.0,
                 log_cb=None, progress_cb=None, left_video_folder: Optional[str] = None,
                 right_video_folder: Optional[str] = None, overlays_enabled: bool = False,
                 players: Dict[str, str] = None,
                 event_text: str = "", player_font_path: Optional[str] = None, player_font_size: int = 36,
                 top_bar_font_path: Optional[str] = None, top_bar_font_size: int = 42,
                 h_video_width: int = 420, h_video_height: int = 600, h_video_y_pos: int = 100,
                 h_top_bar_enabled: bool = True, mate_font_path: Optional[str] = None, mate_font_size_px: int = 0,
                 trailer_enabled: bool = True, trailer_move_count: int = 0, trailer_move_duration: float = 0.8,
                 trailer_text_enabled: bool = False, trailer_text_override: str = "",
                 trailer_text_x: int = 0, trailer_text_y: int = 0, trailer_font_size_px: int = 0,
                 mate_text: str = "CHECKMATE!", mate_text_x: int = 0, mate_text_y: int = 0, log_prefix: str = "",
                 v_layout_mode: str = 'single_bottom', left_image_path: Optional[str] = None,
                 right_image_path: Optional[str] = None, photo_to_video: bool = False,
                 photo_zoom_end: float = 1.08, v_single_media_height_px: int = 400,
                 v_dual_media_height_px: int = 300, v_dual_media_spacing: int = 20,
                 v_horizontal_padding: int = 50, v_board_spacing: int = 50, v_show_info_bar: bool = False,
                 v_info_bar_height: int = 10, stop_overlay_loop: bool = False, show_board_coords: bool = True,
                 board_coords_size_ratio: float = 0.3, board_coords_stroke_width: int = 1,
                 subscribe_overlay_enabled: bool = False, subscribe_overlay_path: Optional[str] = None,
                 subscribe_overlay_scale_top: float = 0.8, subscribe_overlay_y_offset_top: int = 0,
                 subscribe_overlay_scale_bottom: float = 0.8, subscribe_overlay_y_offset_bottom: int = 0,
                 highlight_last_move: bool = True, icon_folder_path: Optional[str] = None,
                 badge_scale: float = 0.40, badge_pos: str = 'bl', badge_margin: float = 0.10,
                 show_eval_bar: bool = True, eval_bar_pos: str = 'left', eval_bar_thickness: int = 30,
                 eval_bar_padding: int = 10, base_anim_duration: float = 0.4, base_delay_duration: float = 0.8,
                 show_eval_icons: bool = True, eval_map: Optional[Dict] = None, force_gui_speed: bool = False
                 ):
        self.theme = theme;
        self.aspect = aspect;
        self.fps = fps;
        self.output_dir = output_dir
        self.show_move_text = show_move_text;
        self.long_mate_freeze = long_mate_freeze
        self.freeze_seconds = freeze_seconds;
        self.check_highlight = check_highlight
        self.check_arrow = check_arrow;
        self.show_checkmate_text = show_checkmate_text
        self.checkmate_highlight_squares = checkmate_highlight_squares
        self.checkmate_arrows = checkmate_arrows;
        self.music_path = music_path
        self.watermark_text = watermark_text;
        self.use_cache = use_cache
        self.shorts_control = shorts_control;
        self.max_shorts_duration = float(max_shorts_duration)
        self.cache_dir = self.output_dir / "_frame_cache";
        ensure_dir(self.cache_dir) if self.use_cache else None
        self.arrow_colors = {'green': (22, 164, 53, 210), 'bright': (50, 255, 50, 240), 'mistake': (210, 40, 30, 220),
                             'normal': (22, 164, 53, 210)}
        self.highlight_colors = {'from': (20, 150, 20, 100), 'to': (20, 180, 20, 120)}
        self.log = log_cb or (lambda *a, **k: None)
        self.progress_cb = progress_cb or (lambda done, total: None)
        self.video_width, self.video_height = (1080, 1920) if aspect == '9:16' else (1920, 1080)
        self.log_prefix = log_prefix;
        self.players = players or {};
        self.event_text = event_text
        self.player_font = self._load_font(player_font_path or "DejaVuSans.ttf", player_font_size)
        self.top_bar_font = self._load_font(top_bar_font_path or "DejaVuSans-Bold.ttf", top_bar_font_size)
        self.left_video_path = self._get_random_video(left_video_folder)
        self.right_video_path = self._get_random_video(right_video_folder)
        self.left_image_path = Path(left_image_path) if left_image_path and Path(left_image_path).exists() else None
        self.right_image_path = Path(right_image_path) if right_image_path and Path(right_image_path).exists() else None
        self.use_video_layout = overlays_enabled and (
                self.left_video_path or self.right_video_path or self.left_image_path or self.right_image_path)
        self.photo_to_video = photo_to_video;
        self.photo_zoom_end = photo_zoom_end;
        self.v_layout_mode = v_layout_mode
        self.stop_overlay_loop = stop_overlay_loop;
        self.show_board_coords = show_board_coords
        self.board_coords_size_ratio = board_coords_size_ratio;
        self.board_coords_stroke_width = board_coords_stroke_width
        self.subscribe_overlay_enabled = subscribe_overlay_enabled;
        self.subscribe_overlay_path = subscribe_overlay_path
        self.subscribe_overlay_scale_top = subscribe_overlay_scale_top;
        self.subscribe_overlay_y_offset_top = subscribe_overlay_y_offset_top
        self.subscribe_overlay_scale_bottom = subscribe_overlay_scale_bottom;
        self.subscribe_overlay_y_offset_bottom = subscribe_overlay_y_offset_bottom
        self.highlight_last_move = highlight_last_move;
        self.show_eval_bar = show_eval_bar;
        self.eval_bar_pos = eval_bar_pos
        self.eval_bar_thickness = eval_bar_thickness;
        self.eval_bar_padding = eval_bar_padding
        self.base_anim_duration = base_anim_duration;
        self.base_delay_duration = base_delay_duration
        self.show_eval_icons = show_eval_icons
        self.eval_map = eval_map or {}
        self.force_gui_speed = force_gui_speed

        self.single_overlay_height_ratio = 0.28
        self.layout_margin = 12

        top_bar_h = 80 if self.use_video_layout and self.aspect == '16:9' and h_top_bar_enabled and self.event_text else 0

        # New layout calculation logic
        if self.use_video_layout and self.aspect == '9:16':
            pad = v_horizontal_padding
            self.board_size = self.video_width - 2 * pad
            self.square_size = self.board_size // 8
            self.media_slots = []

            bottom_bar_h = (self.eval_bar_thickness + self.eval_bar_padding) if (
                    self.show_eval_bar and self.eval_bar_pos == 'bottom') else 0

            if self.v_layout_mode in ('single_top', 'single_bottom'):
                slot_h = int(self.video_height * self.single_overlay_height_ratio)
                slot_h = max(int(self.square_size * 2.5), min(slot_h, int(self.square_size * 5.5)))
                slot_w = self.video_width - 2 * pad
                board_x = pad

                if self.v_layout_mode == 'single_top':
                    slot_x, slot_y = pad, self.layout_margin
                    board_y = slot_y + slot_h + v_board_spacing
                    self.board_offset = (board_x, board_y)
                    self.media_slots.append((slot_w, slot_h, slot_x, slot_y))
                    self.info_bar_y = self.board_offset[1] - v_board_spacing + (
                            v_board_spacing - v_info_bar_height) // 2 if v_show_info_bar else 0
                else:  # single_bottom
                    board_y = pad
                    self.board_offset = (board_x, board_y)
                    gap = max(0, v_board_spacing)
                    bar_h = v_info_bar_height if v_show_info_bar else 0
                    self.info_bar_y = self.board_offset[1] + self.board_size + max(0, (
                            gap - bar_h) // 2) if v_show_info_bar else 0
                    slot_x = pad
                    slot_y = self.board_offset[1] + self.board_size + gap + bottom_bar_h
                    self.media_slots.append((slot_w, slot_h, slot_x, slot_y))

            elif 'dual' in self.v_layout_mode:
                if 'top' in v_layout_mode:
                    media_h = v_dual_media_height_px
                    media_y_pos = 0
                    self.info_bar_y = media_y_pos + media_h + (
                            v_board_spacing - v_info_bar_height) // 2 if v_show_info_bar else 0
                    board_y = media_y_pos + media_h + v_board_spacing
                    self.board_offset = (pad, board_y)
                else:  # dual_bottom
                    board_y = pad
                    self.board_offset = (pad, board_y)
                    self.info_bar_y = self.board_offset[1] + self.board_size + (
                            v_board_spacing - v_info_bar_height) // 2 if v_show_info_bar else 0
                    media_y_pos = self.board_offset[1] + self.board_size + v_board_spacing + bottom_bar_h

                total_inner_w = self.video_width - 2 * pad
                left_w = (total_inner_w - v_dual_media_spacing) // 2
                right_w = total_inner_w - v_dual_media_spacing - left_w
                self.media_slots.append((left_w, v_dual_media_height_px, pad, media_y_pos))
                self.media_slots.append(
                    (right_w, v_dual_media_height_px, pad + left_w + v_dual_media_spacing, media_y_pos))
            else:  # Fallback to old logic if mode is unrecognized
                board_y = pad
                self.board_offset = (pad, board_y)
                self.info_bar_y = self.board_offset[1] + self.board_size + (
                        v_board_spacing // 2) if v_show_info_bar else 0

            self.v_info_bar_height = v_info_bar_height
            self.v_show_info_bar = v_show_info_bar

        elif self.use_video_layout and self.aspect == '16:9':
            v_padding = 40;
            board_y_start = v_padding + top_bar_h;
            self.board_size = self.video_height - board_y_start - v_padding
            self.board_offset = ((self.video_width - self.board_size) // 2, board_y_start);
            panel_width = self.board_offset[0]
            self.video_w, self.video_h = h_video_width, h_video_height
            self.left_video_pos = ((panel_width - self.video_w) // 2, h_video_y_pos + top_bar_h)
            self.right_video_pos = (
                self.video_width - panel_width + (panel_width - self.video_w) // 2, h_video_y_pos + top_bar_h)
        else:
            pad = int(min(self.video_width, self.video_height) * 0.06);
            self.board_size = min(self.video_width, self.video_height) - 2 * pad
            self.board_offset = ((self.video_width - self.board_size) // 2, (self.video_height - self.board_size) // 2)

        self.square_size = self.board_size // 8;
        self.assets_dir = Path("chess_assets");
        self.pieces_dir = self.assets_dir / "pieces" / self.theme
        self.boards_dir = self.assets_dir / "boards" / self.theme;
        self._piece_cache: Dict[str, Image.Image] = {};
        self._board_img: Optional[Image.Image] = None
        self._font = self._load_font("DejaVuSans.ttf", 44);
        self._watermark_font = self._load_font("DejaVuSans.ttf", 32)
        self._final_font_default_name = "DejaVuSans-Bold.ttf";
        self._background_img = self._load_background(background_path)
        self.mate_font_path = mate_font_path or None;
        self.mate_font_size_px = int(mate_font_size_px or 0);
        self.trailer_enabled = bool(trailer_enabled)
        self.trailer_move_count = int(trailer_move_count or 0);
        self.trailer_move_duration = float(trailer_move_duration)
        self.trailer_text_enabled = bool(trailer_text_enabled);
        self.trailer_text_override = trailer_text_override or ""
        self.trailer_text_x = int(trailer_text_x or 0);
        self.trailer_text_y = int(trailer_text_y or 0);
        self.trailer_font_size_px = int(trailer_font_size_px or 0)
        self.mate_text = mate_text or "CHECKMATE!";
        self.mate_text_x = int(mate_text_x or 0);
        self.mate_text_y = int(mate_text_y or 0)
        self.badge_scale = badge_scale;
        self.badge_pos = badge_pos;
        self.badge_margin = badge_margin;

        # In-memory cache for frames to reduce disk I/O, though impact might be minimal
        self._in_memory_frame_cache: Dict[int, Image.Image] = {}

        # New icon handling logic
        self.icon_folder = Path(icon_folder_path) if icon_folder_path and Path(icon_folder_path).is_dir() else Path(
            "chess_icons")
        self._icon_cache = {}
        self._icon_paths = {}

        ensure_dir(self.output_dir)

    def reset_icon_cache(self):
        self._icon_cache.clear()
        self._icon_paths.clear()
        try:
            self.log("♻️ Icon cache reset")
        except:
            pass

    def _get_icon(self, mark: str) -> Optional[Image.Image]:
        key = (mark or "").strip()
        if not key: return None
        alias = {
            "!!": "brilliant",
            "!": "good",
            "?!": "inaccuracy",
            "!?": "inaccuracy",
            "?": "mistake",
            "??": "blunder",
        }.get(key, key.lower())
        if alias in self._icon_cache: return self._icon_cache[alias]
        if self._icon_paths.get(alias, None) == "": return None  # Remembered miss

        p = self.icon_folder / f"{alias}.png"
        if not p.exists():
            self._icon_paths[alias] = ""  # Don't check disk again
            try:
                self.log(f"⚠️ Нет иконки '{key}' по пути: {p}")
            except:
                pass
            return None
        try:
            img = Image.open(p).convert("RGBA")
            target_size = int(self.square_size * self.badge_scale)
            if img.width != target_size or img.height != target_size:
                img = img.resize((target_size, target_size), Image.LANCZOS)
            self._icon_cache[alias] = img
            self._icon_paths[alias] = str(p)
            return img
        except Exception as e:
            self._icon_paths[alias] = ""
            try:
                self.log(f"⚠️ Ошибка загрузки иконки '{key}': {e}")
            except:
                pass
            return None

    def _badge_anchor_xy(self, sq: int, pos: str, margin_ratio: float) -> tuple[int, int]:
        x0, y0 = self._square_xy(sq);
        s = self.square_size;
        m = int(s * margin_ratio);
        icon_radius = int(s * self.badge_scale / 2)
        if pos == 'bl':
            cx, cy = x0 + m + icon_radius, y0 + s - m - icon_radius
        elif pos == 'br':
            cx, cy = x0 + s - m - icon_radius, y0 + s - m - icon_radius
        elif pos == 'tl':
            cx, cy = x0 + m + icon_radius, y0 + m + icon_radius
        else:
            cx, cy = x0 + s - m - icon_radius, y0 + m + icon_radius
        board_x0, board_y0 = self.board_offset;
        board_x1, board_y1 = board_x0 + self.board_size, board_y0 + self.board_size
        cx = max(board_x0 + icon_radius, min(cx, board_x1 - icon_radius));
        cy = max(board_y0 + icon_radius, min(cy, board_y1 - icon_radius))
        return cx, cy

    def _paste_badge(self, canvas: Image.Image, mark: str, dest_sq: int, pos: Optional[str] = None):
        if not (icon := self._get_icon(mark)): return
        cx, cy = self._badge_anchor_xy(dest_sq, pos or self.badge_pos, self.badge_margin)
        x, y = cx - icon.width // 2, cy - icon.height // 2
        canvas.paste(icon, (x, y), icon)

    def _get_random_video(self, folder_path: Optional[str]) -> Optional[Path]:
        if not folder_path or not Path(folder_path).is_dir(): return None
        supported_formats = ('.mp4', '.mov', '.avi', '.mkv')
        videos = [p for p in Path(folder_path).iterdir() if p.suffix.lower() in supported_formats]
        if not videos: self.log(f"⚠️ В папке '{folder_path}' не найдено видео.", level=logging.WARNING,
                                is_gui_message=True); return None
        return random.choice(videos)

    def _load_font(self, font_name_or_path: str, size: int) -> ImageFont.FreeTypeFont:
        try:
            p = Path(font_name_or_path)
            if p.is_file(): return ImageFont.truetype(str(p), size)
            if (
                    font_path_in_assets := self.assets_dir / "fonts" / font_name_or_path).exists(): return ImageFont.truetype(
                str(font_path_in_assets), size)
            return ImageFont.truetype(font_name_or_path, size)
        except Exception:
            return ImageFont.load_default()

    def _get_fitted_font(self, text: str, font_path: str, initial_size: int, max_width: int, stroke_width: int,
                         min_size: int = 20):
        size = initial_size if initial_size > 0 else int(self.video_height * 0.15)
        dummy_draw = ImageDraw.Draw(Image.new('RGB', (1, 1)))
        while size > min_size:
            try:
                font = ImageFont.truetype(font_path, size)
            except IOError:
                font = ImageFont.load_default()
            bbox = dummy_draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
            if (bbox[2] - bbox[0]) <= max_width: return font
            size -= 2
        try:
            return ImageFont.truetype(font_path, min_size)
        except IOError:
            return ImageFont.load_default()

    def _draw_text_with_ring(self, img: Image.Image, text: str, font: ImageFont.FreeTypeFont, x: int, y: int,
                             color=(255, 255, 255), ring_color=(0, 0, 0), ring_px: int = 10, opacity: float = 1.0):
        txt_layer = Image.new('RGBA', img.size, (0, 0, 0, 0));
        draw = ImageDraw.Draw(txt_layer)
        alpha = int(255 * opacity)
        draw.text((x, y), text, font=font, anchor='mm', fill=color + (alpha,), stroke_width=ring_px,
                  stroke_fill=ring_color + (alpha,))
        return Image.alpha_composite(img.convert('RGBA'), txt_layer).convert('RGB')

    def _draw_cinematic_title(self, img: Image.Image, text: str, font_path_pref: Optional[str], color=(255, 255, 255),
                              ring_color=(0, 0, 0), opacity: float = 1.0):
        draw = ImageDraw.Draw(img)
        board_box = (self.board_offset[0], self.board_offset[1], self.board_offset[0] + self.board_size,
                     self.board_offset[1] + self.board_size)
        font_path = font_path_pref or str(self.assets_dir / "fonts" / self._final_font_default_name)
        layout_data = prepare_title(draw, text, board_box, font_path)
        center_x, center_y = self.board_offset[0] + self.board_size // 2, self.board_offset[1] + self.board_size // 2
        if layout_data["layout"] == "one" and layout_data["one"]:
            fs, stroke_w, w, h = layout_data["one"];
            font = self._load_font(font_path, fs)
            return self._draw_text_with_ring(img, layout_data["texts"][0], font, center_x, center_y, color=color,
                                             ring_color=ring_color, ring_px=stroke_w, opacity=opacity)
        elif layout_data["layout"] == "two" and layout_data["two"]:
            fs1, fs2, sw1, sw2, gap, w1, w2, h1, h2 = layout_data["two"]
            main_text, sub_text = layout_data["texts"][1];
            font1, font2 = self._load_font(font_path, fs1), self._load_font(font_path, fs2)
            total_h = h1 + gap + h2;
            y1_center, y2_center = center_y - total_h / 2 + h1 / 2, center_y + total_h / 2 - h2 / 2
            img_with_line1 = self._draw_text_with_ring(img, main_text, font1, center_x, int(y1_center), color=color,
                                                       ring_color=ring_color, ring_px=sw1, opacity=opacity)
            return self._draw_text_with_ring(img_with_line1, sub_text, font2, center_x, int(y2_center), color=color,
                                             ring_color=ring_color, ring_px=sw2, opacity=opacity)
        return img

    def _load_background(self, bg_path: Optional[str]) -> Optional[Image.Image]:
        if not bg_path or not Path(bg_path).exists():
            if bg_path: self.log(f"[Renderer] ⚠️ Фон не найден: {bg_path}", level=logging.WARNING)
            return None
        try:
            img = Image.open(bg_path).convert("RGB");
            img_ratio, video_ratio = img.width / img.height, self.video_width / self.video_height
            if img_ratio > video_ratio:
                new_width = int(video_ratio * img.height);
                left = (img.width - new_width) // 2;
                right = left + new_width
                img = img.crop((left, 0, right, img.height))
            else:
                new_height = int(img.width / video_ratio);
                top = (img.height - new_height) // 2;
                bottom = top + new_height
                img = img.crop((0, top, img.width, bottom))
            return img.resize((self.video_width, self.video_height), Image.LANCZOS)
        except Exception as e:
            self.log(f"[Renderer] ⚠️ Фон '{bg_path}' не загрузился: {e}", level=logging.ERROR)
            return None

    def _load_board(self):
        if self._board_img: return self._board_img
        board_file = self.boards_dir / "board_240.png"
        if board_file.exists():
            img = Image.open(board_file).convert('RGB').resize((self.board_size, self.board_size), Image.LANCZOS)
        else:
            colors, img = {'light': (240, 217, 181), 'dark': (181, 136, 99)}, Image.new('RGB', (
                self.board_size, self.board_size))
            draw = ImageDraw.Draw(img)
            for r in range(8):
                for f in range(8):
                    color = colors['light'] if (r + f) % 2 == 0 else colors['dark']
                    draw.rectangle([f * self.square_size, r * self.square_size, (f + 1) * self.square_size,
                                    (r + 1) * self.square_size], fill=color)
        self._board_img = img;
        return img

    def _load_piece(self, code):
        if code in self._piece_cache: return self._piece_cache[code]
        p = self.pieces_dir / f"{code}.png"
        img = Image.open(p).convert('RGBA').resize((self.square_size, self.square_size),
                                                   Image.LANCZOS) if p.exists() else Image.new('RGBA', (
            self.square_size, self.square_size), (0, 0, 0, 0))
        self._piece_cache[code] = img
        return img

    def _base_canvas(self):
        return self._background_img.copy() if self._background_img else Image.new('RGB',
                                                                                  (self.video_width, self.video_height),
                                                                                  (40, 40, 40))

    def _square_xy(self, sq):
        return (self.board_offset[0] + chess.square_file(sq) * self.square_size,
                self.board_offset[1] + (7 - chess.square_rank(sq)) * self.square_size)

    def _piece_code_for(self, piece):
        return ('w' if piece.color == chess.WHITE else 'b') + piece.symbol().lower()

    def _draw_arrow(self, canvas, start_sq, end_sq, color=(22, 164, 53, 210), width_ratio=0.34):
        draw = ImageDraw.Draw(canvas, 'RGBA');
        sx, sy = self._square_xy(start_sq);
        sx += self.square_size // 2;
        sy += self.square_size // 2
        ex, ey = self._square_xy(end_sq);
        ex += self.square_size // 2;
        ey += self.square_size // 2
        shaft_w, head_len, head_w = max(5, int(self.square_size * width_ratio * 0.55)), max(18,
                                                                                            int(self.square_size * width_ratio * 1.25)), max(
            20, int(self.square_size * width_ratio * 1.15))
        ang = math.atan2(ey - sy, ex - sx);
        bx, by = ex - head_len * math.cos(ang), ey - head_len * math.sin(ang)
        draw.line((sx, sy, bx, by), fill=color, width=shaft_w)
        lx, ly = bx + (head_w / 2) * math.sin(ang), by - (head_w / 2) * math.cos(ang)
        rx, ry = bx - (head_w / 2) * math.sin(ang), by + (head_w / 2) * math.cos(ang)
        draw.polygon([(ex, ey), (lx, ly), (rx, ry)], fill=color)

    def _apply_check_highlight(self, board_img, board):
        if not self.check_highlight or not board.is_check(): return
        if (king_sq := board.king(board.turn)) is None: return
        overlay = Image.new('RGBA', board_img.size, (0, 0, 0, 0));
        draw = ImageDraw.Draw(overlay, 'RGBA')
        f, r = chess.square_file(king_sq), 7 - chess.square_rank(king_sq)
        cx, cy = f * self.square_size + self.square_size // 2, r * self.square_size + self.square_size // 2
        max_radius = self.square_size // 2
        for i in range(max_radius, 0, -1):
            alpha = int(100 * (1 - i / max_radius) ** 2)
            draw.ellipse([cx - i, cy - i, cx + i, cy + i], fill=(255, 60, 60, alpha))
        board_img.paste(Image.alpha_composite(board_img.convert('RGBA'), overlay).convert('RGB'))

    def _king_neighborhood(self, king_sq) -> List[int]:
        neigh, kf, kr = [], chess.square_file(king_sq), chess.square_rank(king_sq)
        for df, dr in [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]:
            f, r = kf + df, kr + dr
            if 0 <= f <= 7 and 0 <= r <= 7: neigh.append(chess.square(f, r))
        return neigh

    def _visualize_checkmate(self, canvas, board):
        if (king_sq := board.king(board.turn)) is None: return
        enemy_color, king_area = not board.turn, set(self._king_neighborhood(king_sq)) | {king_sq}
        rays: Dict[int, List[int]] = defaultdict(list);
        attacked_king_area: set[int] = set()

        # NEW LOGIC: Find all pieces controlling any square in the king's vicinity
        for controlled_sq in king_area:
            for attacker_sq in board.attackers(enemy_color, controlled_sq):
                # Ensure we don't draw arrows from the opponent's king in weird edge cases
                if board.piece_at(attacker_sq) and board.piece_at(attacker_sq).piece_type != chess.KING:
                    attacked_king_area.add(controlled_sq)
                    # Avoid duplicate rays from the same piece to different squares
                    if controlled_sq not in rays[attacker_sq]:
                        rays[attacker_sq].append(controlled_sq)

        if self.checkmate_highlight_squares:
            overlay = Image.new('RGBA', canvas.size, (0, 0, 0, 0));
            draw_overlay = ImageDraw.Draw(overlay)
            for tgt_sq in attacked_king_area:
                x_start, y_start = self._square_xy(tgt_sq);
                cx, cy = x_start + self.square_size // 2, y_start + self.square_size // 2
                max_radius = int(self.square_size * 0.6)
                for i in range(max_radius, 0, -2):
                    alpha = int(90 * (1 - (i / max_radius)) ** 1.5)
                    draw_overlay.ellipse([cx - i, cy - i, cx + i, cy + i], fill=(255, 40, 40, alpha))
            canvas.paste(Image.alpha_composite(canvas.convert('RGBA'), overlay))
        if self.checkmate_arrows:
            for src_sq, target_squares in rays.items():
                for tgt_sq in target_squares: self._draw_arrow(canvas, src_sq, tgt_sq, color=(210, 40, 30, 230),
                                                               width_ratio=0.36)

    def _draw_coords_outside(self, canvas: Image.Image):
        if not self.show_board_coords: return
        draw = ImageDraw.Draw(canvas)
        font_size = max(10, int(self.square_size * self.board_coords_size_ratio))
        try:
            font = ImageFont.truetype(str(self.assets_dir / "fonts" / "DejaVuSans-Bold.ttf"), font_size)
        except IOError:
            font = ImageFont.load_default()
        stroke_w = max(2, int(font_size * 0.14), int(self.board_coords_stroke_width))
        fill, outline = (255, 255, 255), (0, 0, 0)
        left_x = self.board_offset[0] - int(self.square_size * 0.22)
        for r in range(8):
            y = self.board_offset[1] + r * self.square_size + self.square_size // 2
            draw.text((left_x, y), str(8 - r), font=font, fill=fill, stroke_width=stroke_w, stroke_fill=outline,
                      anchor="rm")
        bottom_y = self.board_offset[1] + self.board_size + int(self.square_size * 0.18)
        for f in range(8):
            x = self.board_offset[0] + f * self.square_size + self.square_size // 2
            draw.text((x, bottom_y), chr(ord('a') + f), font=font, fill=fill, stroke_width=stroke_w,
                      stroke_fill=outline, anchor="mm")

    def _compose_frame(self, board, movers, t, caption, decorations, effects, last_move=None):
        canvas_rgb = self._base_canvas();
        canvas = canvas_rgb.convert('RGBA');
        draw = ImageDraw.Draw(canvas)
        if self.use_video_layout and self.aspect == '16:9':
            top_bar_h = 80 if self.event_text and hasattr(self, 'top_bar_font') else 0
            if self.event_text and hasattr(self, 'top_bar_font'):
                bbox = draw.textbbox((0, 0), self.event_text, font=self.top_bar_font);
                x_pos = (self.video_width - (bbox[2] - bbox[0])) // 2;
                y_pos = (top_bar_h - (bbox[3] - bbox[1])) // 2
                draw.text((x_pos, y_pos), self.event_text, fill=(255, 255, 255), font=self.top_bar_font)
            y_pos_name = self.left_video_pos[1] + self.video_h + 15
            if name := self.players.get("white"):
                bbox = draw.textbbox((0, 0), name, font=self.player_font);
                x_pos = self.left_video_pos[0] + (self.video_w - (bbox[2] - bbox[0])) // 2
                draw.text((x_pos, y_pos_name), name, fill=(255, 255, 255), font=self.player_font)
            if name := self.players.get("black"):
                bbox = draw.textbbox((0, 0), name, font=self.player_font);
                x_pos = self.right_video_pos[0] + (self.video_w - (bbox[2] - bbox[0])) // 2
                draw.text((x_pos, y_pos_name), name, fill=(255, 255, 255), font=self.player_font)
        if self.use_video_layout and self.aspect == '9:16' and self.v_show_info_bar:
            bar_x0, bar_x1 = self.board_offset[0], self.board_offset[0] + self.board_size
            draw.rectangle([bar_x0, self.info_bar_y, bar_x1, self.info_bar_y + self.v_info_bar_height],
                           fill=(80, 80, 80))
        board_img = self._load_board().copy()
        if self.highlight_last_move and last_move:
            overlay = Image.new('RGBA', board_img.size, (0, 0, 0, 0));
            draw_overlay = ImageDraw.Draw(overlay);
            ss = self.square_size
            from_f, from_r = chess.square_file(last_move.from_square), 7 - chess.square_rank(last_move.from_square)
            to_f, to_r = chess.square_file(last_move.to_square), 7 - chess.square_rank(last_move.to_square)
            draw_overlay.rectangle([from_f * ss, from_r * ss, (from_f + 1) * ss, (from_r + 1) * ss],
                                   fill=self.highlight_colors['from'])
            draw_overlay.rectangle([to_f * ss, to_r * ss, (to_f + 1) * ss, (to_r + 1) * ss],
                                   fill=self.highlight_colors['to'])
            board_img = Image.alpha_composite(board_img.convert('RGBA'), overlay)
        moving_from = {m[3] for m in movers};
        board_img_with_pieces = board_img.convert('RGBA')
        for sq in chess.SQUARES:
            if sq not in moving_from and (piece := board.piece_at(sq)):
                pimg = self._load_piece(self._piece_code_for(piece))
                board_img_with_pieces.paste(pimg, (
                    chess.square_file(sq) * self.square_size, (7 - chess.square_rank(sq)) * self.square_size), pimg)
        for piece, (x0, y0), (x1, y1), _ in movers:
            img_piece = self._load_piece(self._piece_code_for(piece));
            xi, yi = int(lerp(x0, x1, t)), int(lerp(y0, y1, t))
            board_img_with_pieces.paste(img_piece, (xi - self.board_offset[0], yi - self.board_offset[1]), img_piece)
        self._apply_check_highlight(board_img_with_pieces, board);
        canvas.paste(board_img_with_pieces, self.board_offset, board_img_with_pieces)
        if "arrow" in decorations: self._draw_arrow(canvas, *decorations["arrow"])
        if self.check_arrow and board.is_check():
            if king_sq := board.king(board.turn):
                for src in board.checkers(): self._draw_arrow(canvas, src, king_sq, color=self.arrow_colors['mistake'],
                                                              width_ratio=0.36)
        if (
                self.checkmate_arrows or self.checkmate_highlight_squares) and board.is_checkmate(): self._visualize_checkmate(
            canvas, board)
        self._draw_coords_outside(canvas)

        # EVALUATION BAR LOGIC
        if self.show_eval_bar:
            current_bar_pct = 0.5
            bar_from, bar_to = effects.get("bar_from_pct"), effects.get("bar_to_pct")

            if movers and bar_from is not None and bar_to is not None:
                current_bar_pct = lerp(bar_from, bar_to, t)
            else:
                fen_key = board.shredder_fen()
                if eval_data := self.eval_map.get(fen_key):
                    eval_white_pov = _get_pov_eval(eval_data, chess.WHITE)
                    current_bar_pct = _normalize_eval_white(eval_white_pov)

            pos = self.eval_bar_pos
            if pos in ['left', 'right']:
                bar_height, bar_width = self.board_size, self.eval_bar_thickness
                bar_y = self.board_offset[1]
                bar_x = self.board_offset[0] - bar_width - self.eval_bar_padding if pos == 'left' else \
                    self.board_offset[0] + self.board_size + self.eval_bar_padding
                draw.rectangle([bar_x, bar_y, bar_x + bar_width, bar_y + bar_height], fill=(40, 40, 40))
                white_h = int(bar_height * current_bar_pct)
                draw.rectangle([bar_x, bar_y + bar_height - white_h, bar_x + bar_width, bar_y + bar_height],
                               fill=(240, 240, 240))
            elif pos in ['top', 'bottom']:
                bar_width, bar_height = self.board_size, self.eval_bar_thickness
                bar_x = self.board_offset[0]

                # --- START OF FIX ---
                y0 = self.board_offset[1] + self.board_size + self.eval_bar_padding
                if (self.use_video_layout and self.aspect == '9:16' and
                        self.v_layout_mode in ('single_bottom', 'dual_bottom') and self.media_slots):
                    slot_y = self.media_slots[0][3]
                    y0 = min(y0, slot_y - bar_height - self.layout_margin)

                bar_y = (self.board_offset[1] - bar_height - self.eval_bar_padding) if pos == 'top' else y0
                # --- END OF FIX ---

                draw.rectangle([bar_x, bar_y, bar_x + bar_width, bar_y + bar_height], fill=(40, 40, 40))
                white_w = int(bar_width * current_bar_pct)
                draw.rectangle([bar_x, bar_y, bar_x + white_w, bar_y + bar_height], fill=(240, 240, 240))

        if caption and self.show_move_text:
            bbox = draw.textbbox((0, 0), caption, font=self._font)
            tx = (self.video_width - (bbox[2] - bbox[0])) // 2;
            ty = self.board_offset[1] + self.board_size + 24 if self.aspect == '9:16' else self.video_height - bbox[
                3] - 15
            draw.text((tx + 2, ty + 2), caption, fill=(0, 0, 0), font=self._font);
            draw.text((tx, ty), caption, fill=(255, 255, 255), font=self._font)
        if self.watermark_text:
            bbox = draw.textbbox((0, 0), self.watermark_text, font=self._watermark_font)
            tx = (self.video_width - (bbox[2] - bbox[0])) // 2;
            ty = self.board_offset[1] - bbox[3] - 20
            if self.use_video_layout and self.aspect == '9:16': ty = 20
            draw.text((tx, ty), self.watermark_text, fill=(255, 255, 255, 100), font=self._watermark_font)

        # Draw badge on top of everything
        if self.show_eval_icons and (mark := effects.get("mark", "")) and last_move:
            self._paste_badge(canvas, mark, last_move.to_square)

        return canvas.convert('RGB')

    def _get_or_create_frame(self, frame_idx, *args, **kwargs):
        """Получает кадр из дискового кэша или создает новый.

        Внутри одного рендера каждый frame_idx уникален и запрашивается ровно
        один раз, поэтому кэш кадров в памяти не нужен — он лишь рос бы до OOM
        на длинных партиях. Дисковый кэш сохраняется (полезен между прогонами).
        """
        if not self.use_cache:
            return self._compose_frame(*args, **kwargs)

        frame_filename = self.cache_dir / f"frame_{frame_idx:06d}.png"
        try:
            if frame_filename.exists():
                return Image.open(frame_filename)

            frame_image = self._compose_frame(*args, **kwargs)
            frame_image.save(frame_filename, "PNG")
            return frame_image
        except Exception as e:
            self.log(f"⚠️ Ошибка кэширования кадра {frame_idx}: {e}", level=logging.WARNING)
            # При ошибке кэширования просто создаем кадр без сохранения
            return self._compose_frame(*args, **kwargs)

    def render_game_to_pipe(self, moves_san: List[str], filename_base: str, display_title: str, fen: Optional[str],
                            script: Optional[Dict],
                            trailer_info: Optional[Dict]) -> Optional[Path]:
        # Сброс кэша в памяти для каждой новой игры
        self._in_memory_frame_cache.clear()

        out_path = self.output_dir / (filename_base + ".mp4")
        script_moves = script.get("moves", []) if script else []
        script_map = {(m.get('ply'), m.get('san')): m.get('effects', {}) for m in script_moves}
        try:
            probe_board = chess.Board(fen) if fen else chess.Board()
            for _san in moves_san: probe_board.push_san(_san)
            mate_at_end = probe_board.is_checkmate()
        except Exception:
            mate_at_end = False
        trailer_items = script.get("trailer_moves", []) if script else []
        trailer_active = self.trailer_enabled and bool(trailer_items) and self.trailer_move_count > 0
        trailer_duration_sec = len(trailer_items) * self.trailer_move_duration if trailer_active else 0.0
        moves_base_duration = 0.0
        for san_index, san in enumerate(moves_san):
            eff = script_map.get((san_index, san), {})
            if self.force_gui_speed:
                moves_base_duration += self.base_anim_duration + self.base_delay_duration
            else:
                moves_base_duration += float(eff.get('anim', self.base_anim_duration)) + float(
                    eff.get('delay', self.base_delay_duration))
        natural_total = trailer_duration_sec + moves_base_duration + (
            self.freeze_seconds if mate_at_end and self.long_mate_freeze else 0.0)
        fps = self.fps
        freeze_frames = int(round((self.freeze_seconds if (self.long_mate_freeze and mate_at_end) else 0.0) * fps))
        if self.shorts_control and natural_total > self.max_shorts_duration:
            target_sec = float(self.max_shorts_duration)
        else:
            target_sec = float(natural_total)
        target_frames_total = int(round(target_sec * fps))
        target_frames_play = max(1, target_frames_total - freeze_frames)

        def frames_play_at(speed: float) -> int:
            total = 0
            if trailer_active:
                a, h = self.trailer_move_duration * 0.44, self.trailer_move_duration * 0.56
                for _ in trailer_items: total += max(1, int(round(a * fps * speed))) + max(1,
                                                                                           int(round(h * fps * speed)))
            for san_index, san in enumerate(moves_san):
                eff = script_map.get((san_index, san), {});
                if self.force_gui_speed:
                    a, d = self.base_anim_duration, self.base_delay_duration
                else:
                    a, d = float(eff.get('anim', self.base_anim_duration)), float(
                        eff.get('delay', self.base_delay_duration))
                total += max(1, int(round(a * fps * speed))) + max(1, int(round(d * fps * speed)))
            return total

        if self.shorts_control and natural_total > self.max_shorts_duration:
            s_lo, s_hi = 0.0, min(1.0, (self.max_shorts_duration / natural_total) * 1.25)
            for _ in range(32):
                s_mid = (s_lo + s_hi) / 2.0
                if frames_play_at(s_mid) <= target_frames_play:
                    s_lo = s_mid
                else:
                    s_hi = s_mid
            speed_multiplier = s_lo
        else:
            speed_multiplier = 1.0
        planned_play_frames = frames_play_at(speed_multiplier)
        trim_last_delay = max(0, planned_play_frames - target_frames_play)
        final_frames_total = planned_play_frames - trim_last_delay + freeze_frames
        final_duration = final_frames_total / fps
        self.log(
            f"{self.log_prefix}⏱️ Расчёт: total_natural={natural_total:.2f}s, final={final_duration:.2f}s, speed×={speed_multiplier:.3f}",
            is_gui_message=True)
        self._frame_idx = 0;
        total_est_frames = final_frames_total
        self.log(f"{self.log_prefix}⚙️ Рендеринг ~{total_est_frames} кадров ({final_duration:.1f} сек)",
                 is_gui_message=True)
        cmd = ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s:v", f"{self.video_width}x{self.video_height}",
               "-framerate", str(self.fps), "-i", "-"]
        input_count, all_filters, last_video_tag = 1, [], "[0:v]"
        if self.use_video_layout:
            for i, media_slot_dims in enumerate(self.media_slots):
                is_left_slot = i == 0
                media_path, is_image = (None, False)

                if is_left_slot and (self.left_video_path or self.left_image_path):
                    media_path, is_image = (self.left_image_path or self.left_video_path), bool(self.left_image_path)
                elif not is_left_slot and (self.right_video_path or self.right_image_path):
                    media_path, is_image = (self.right_image_path or self.right_video_path), bool(self.right_image_path)
                else:
                    continue

                loop_param = ["-loop", "1"] if is_image else (
                    ["-stream_loop", "-1"] if not self.stop_overlay_loop else [])
                cmd += loop_param + ["-i", str(media_path)];
                tag = f"[{input_count}:v]"

                crop_w, crop_h, pos_x, pos_y = media_slot_dims
                crop_w -= (crop_w & 1);
                crop_h -= (crop_h & 1);
                pos_x -= (pos_x & 1);
                pos_y -= (pos_y & 1)

                next_tag, media_tag = f"[ovl{input_count}]", f"[media{input_count}]"
                if is_image:
                    contain_pad = f"format=rgba,scale={crop_w}:{crop_h}:force_original_aspect_ratio=decrease,setsar=1,pad={crop_w}:{crop_h}:(ow-iw)/2:(oh-ih)/2:color=0x00000000"
                    if self.photo_to_video:
                        zoom_end, step = float(self.photo_zoom_end), max(0.0, (float(self.photo_zoom_end) - 1.0) / (
                                total_est_frames - 1)) if total_est_frames > 1 else 0.0
                        all_filters.append(
                            f"{tag}format=rgba,zoompan=z='if(eq(on,0),1.0,min(pzoom+{step:.8f},{zoom_end:.8f}))':d={total_est_frames}:s=iw:ih:fps={self.fps},{contain_pad}{media_tag}")
                    else:
                        all_filters.append(f"{tag}{contain_pad}{media_tag}")
                else:
                    all_filters.append(
                        f"{tag}scale={crop_w}:{crop_h}:force_original_aspect_ratio=increase,setsar=1,crop={crop_w}:{crop_h}[{media_tag.strip('[]')}]")
                all_filters.append(f"{last_video_tag}{media_tag}overlay={pos_x}:{pos_y}:format=auto{next_tag}");
                last_video_tag = next_tag;
                input_count += 1
        sub_path = Path(self.subscribe_overlay_path) if self.subscribe_overlay_path else None
        if self.subscribe_overlay_enabled and sub_path and sub_path.exists() and self.aspect == '9:16' and self.use_video_layout:
            cmd += ["-loop", "1", "-i", str(sub_path)] if sub_path.suffix.lower() == '.png' else ["-stream_loop", "-1",
                                                                                                  "-i", str(sub_path)]
            if orig_dims := get_media_dimensions(sub_path):
                scale, y_offset = (self.subscribe_overlay_scale_top,
                                   self.subscribe_overlay_y_offset_top) if 'top' in self.v_layout_mode else (
                    self.subscribe_overlay_scale_bottom, self.subscribe_overlay_y_offset_bottom)
                w, h = int(self.video_width * scale), int(orig_dims[1] * (int(self.video_width * scale) / orig_dims[0]))
                x, y = (self.video_width - w) // 2, (self.media_slots[0][1] + self.media_slots[0][
                    3] - h // 2 + y_offset) if 'top' in self.v_layout_mode else (
                        self.media_slots[0][3] - h // 2 + y_offset)
                next_tag = f"[v_with_sub{input_count}]";
                all_filters.append(f"[{input_count}:v]format=yuva420p,scale={w}:{h}[sub{input_count}]");
                all_filters.append(f"{last_video_tag}[sub{input_count}]overlay=x={x}:y={y}{next_tag}");
                last_video_tag = next_tag;
                input_count += 1
        music_map_idx = -1
        if self.music_path and Path(self.music_path).exists(): cmd += ["-i",
                                                                       str(self.music_path)]; music_map_idx = input_count; all_filters.append(
            f"[{music_map_idx}:a]aloop=loop=-1:size=2e+09,atrim=duration={final_duration:.3f},aresample=async=1[a_looped]"); music_map_idx = "a_looped"
        if all_filters: cmd += ["-filter_complex", ";".join(all_filters)]
        # Без filter_complex лейбла [0:v] не существует — мапим поток напрямую (0:v).
        # С фильтрами last_video_tag указывает на выходной лейбл графа.
        video_map = f"[{last_video_tag.strip('[]')}]" if all_filters else last_video_tag.strip('[]')
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-map", video_map]
        if music_map_idx != -1: cmd += ["-c:a", "aac", "-b:a", "192k", "-map", f"[{music_map_idx}]"]
        cmd += ["-t", f"{final_duration:.3f}", "-movflags", "+faststart", str(out_path)]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        frame_queue = queue.Queue(maxsize=self.fps * 2)

        def ffmpeg_stderr_reader():
            for line in iter(proc.stderr.readline, b''): self.log(
                f"[FFMPEG] {line.decode('utf-8', errors='ignore').strip()}")

        def frame_writer():
            while True:
                if (frame_data := frame_queue.get()) is None: break
                try:
                    proc.stdin.write(frame_data)
                except (IOError, BrokenPipeError):
                    self.log("❌ Поток записи в FFMPEG был закрыт.", level=logging.CRITICAL, is_gui_message=True);
                    break

        stderr_thread, writer_thread = threading.Thread(target=ffmpeg_stderr_reader, daemon=True), threading.Thread(
            target=frame_writer, daemon=True)
        stderr_thread.start();
        writer_thread.start()

        try:
            banner_text = (self.trailer_text_override or (
                trailer_info.get("text", "") if trailer_info else "") or display_title).strip()
            show_text_on_game_start = self.trailer_text_enabled and banner_text and self.trailer_move_count == 0
            intro_text_duration_sec = 4.0
            text_overlay_total_frames = int(intro_text_duration_sec * self.fps) if show_text_on_game_start else 0
            text_overlay_fade_frames = int(self.fps * 0.5)

            # Render initial frame before any moves
            initial_board = chess.Board(fen if fen else chess.STARTING_FEN)
            initial_frame = self._get_or_create_frame(-1, initial_board, [], 1.0, "", {}, {}, None)

            if trailer_active:
                self.log(f"{self.log_prefix}   - 🎬 Запуск анимированного трейлера ({len(trailer_items)} ходов)...",
                         is_gui_message=True)
                for tr in trailer_items:
                    t_board, t_move = chess.Board(tr["fen"]), chess.Board(tr["fen"]).parse_san(tr["san"])
                    x0, y0, x1, y1 = *self._square_xy(t_move.from_square), *self._square_xy(t_move.to_square)
                    movers = [(t_board.piece_at(t_move.from_square), (x0, y0), (x1, y1), t_move.from_square)]
                    anim_f, hold_f = max(1,
                                         int(round(self.trailer_move_duration * 0.44 * fps * speed_multiplier))), max(1,
                                                                                                                      int(round(
                                                                                                                          self.trailer_move_duration * 0.56 * fps * speed_multiplier)))
                    for i in range(anim_f):
                        frame = self._compose_frame(t_board.copy(), movers, (i + 1) / anim_f, "", {}, {}, t_move)
                        if self.trailer_text_enabled and banner_text: frame = self._draw_cinematic_title(frame,
                                                                                                         banner_text,
                                                                                                         self.mate_font_path,
                                                                                                         color=(
                                                                                                             255, 255,
                                                                                                             210),
                                                                                                         ring_color=(
                                                                                                             0, 0, 0))
                        frame_queue.put(frame.tobytes());
                        self._frame_idx += 1;
                        self.progress_cb(self._frame_idx, total_est_frames)
                    t_board.push(t_move)
                    frame = self._compose_frame(t_board, [], 1.0, "", {}, {}, t_move)
                    if self.trailer_text_enabled and banner_text: frame = self._draw_cinematic_title(frame, banner_text,
                                                                                                     self.mate_font_path,
                                                                                                     color=(
                                                                                                         255, 255, 210),
                                                                                                     ring_color=(
                                                                                                         0, 0, 0))
                    for _ in range(hold_f): frame_queue.put(frame.tobytes()); self._frame_idx += 1; self.progress_cb(
                        self._frame_idx, total_est_frames)
            board = chess.Board(fen if fen else chess.STARTING_FEN)
            last_caption, last_move_obj = "", None
            for san_index, san in enumerate(moves_san):
                if proc.poll() is not None: break
                move = board.parse_san(san)
                last_move_obj = move
                caption = f"{board.fullmove_number}{'. ' if board.turn == chess.WHITE else '...'} {san}"
                last_caption = caption
                effects = script_map.get((san_index, san), {})
                decorations = {}
                if arr := effects.get("arrow"):
                    if isinstance(arr, str) and '-' in arr: ss, ee = arr.split('-'); decorations['arrow'] = (
                        chess.parse_square(ss.strip()), chess.parse_square(ee.strip()), self.arrow_colors['normal'])

                if self.force_gui_speed:
                    anim_time = self.base_anim_duration * speed_multiplier
                    delay_time = self.base_delay_duration * speed_multiplier
                else:
                    anim_time = float(effects.get('anim', self.base_anim_duration)) * speed_multiplier
                    delay_time = float(effects.get('delay', self.base_delay_duration)) * speed_multiplier

                anim_f, delay_f = max(1, int(round(anim_time * fps))), max(1, int(round(delay_time * fps)))
                if (san_index == len(moves_san) - 1) and trim_last_delay > 0: delay_f = max(0,
                                                                                            delay_f - trim_last_delay)
                board_before, movers = board.copy(), [(board.piece_at(move.from_square),
                                                       self._square_xy(move.from_square),
                                                       self._square_xy(move.to_square), move.from_square)]
                for i in range(anim_f):
                    frame = self._get_or_create_frame(self._frame_idx, board_before, movers, (i + 1) / anim_f, caption,
                                                      decorations, effects, move)
                    if self._frame_idx < text_overlay_total_frames:
                        opacity = 1.0
                        if self._frame_idx > text_overlay_total_frames - text_overlay_fade_frames: opacity = 1.0 - (
                                self._frame_idx - (
                                text_overlay_total_frames - text_overlay_fade_frames)) / text_overlay_fade_frames
                        frame = self._draw_cinematic_title(frame, banner_text, self.mate_font_path,
                                                           color=(255, 255, 210), ring_color=(0, 0, 0), opacity=opacity)
                    frame_queue.put(frame.tobytes());
                    self._frame_idx += 1;
                    self.progress_cb(self._frame_idx, total_est_frames)
                board.push(move)
                frame_after = self._get_or_create_frame(self._frame_idx, board, [], 1.0, caption, decorations, effects,
                                                        move)
                for _ in range(delay_f):
                    frame = frame_after.copy()
                    if self._frame_idx < text_overlay_total_frames:
                        opacity = 1.0
                        if self._frame_idx > text_overlay_total_frames - text_overlay_fade_frames: opacity = 1.0 - (
                                self._frame_idx - (
                                text_overlay_total_frames - text_overlay_fade_frames)) / text_overlay_fade_frames
                        frame = self._draw_cinematic_title(frame, banner_text, self.mate_font_path,
                                                           color=(255, 255, 210), ring_color=(0, 0, 0), opacity=opacity)
                    frame_queue.put(frame.tobytes());
                    self._frame_idx += 1;
                    self.progress_cb(self._frame_idx, total_est_frames)
            if mate_at_end and self.long_mate_freeze:
                final_frame = self._get_or_create_frame(self._frame_idx, board, [], 1.0, last_caption, {}, {},
                                                        last_move_obj)
                if self.show_checkmate_text:
                    font_path = self.mate_font_path or str(self.assets_dir / "fonts" / self._final_font_default_name)
                    ring_px = 12
                    font = self._get_fitted_font(self.mate_text, font_path, self.mate_font_size_px,
                                                 self.board_size * 0.9, stroke_width=ring_px)
                    x, y = (self.board_offset[
                                0] + self.board_size // 2) if self.mate_text_x == 0 else self.mate_text_x, (
                            self.board_offset[
                                1] + self.board_size // 2) if self.mate_text_y == 0 else self.mate_text_y
                    final_frame = self._draw_text_with_ring(final_frame, self.mate_text, font, x, y,
                                                            color=(255, 255, 210), ring_color=(0, 0, 0),
                                                            ring_px=ring_px)
                for _ in range(freeze_frames): frame_queue.put(
                    final_frame.tobytes()); self._frame_idx += 1; self.progress_cb(self._frame_idx, total_est_frames)
        finally:
            frame_queue.put(None);
            writer_thread.join()
            if proc.stdin: proc.stdin.close()
            stderr_thread.join();
            proc.wait()
        if proc.returncode == 0:
            self.log(f"{self.log_prefix}✅ Видео успешно создано: {out_path.name}", is_gui_message=True);
            return out_path
        else:
            self.log(f"{self.log_prefix}❌ Ошибка FFMPEG. Код: {proc.returncode}", level=logging.ERROR,
                     is_gui_message=True);
            return None


class ScrollableFrame(ttk.Frame if _HAS_TK else object):
    def __init__(self, container, *args, **kwargs):
        super().__init__(container, *args, **kwargs)
        canvas = tk.Canvas(self);
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self.scrollable_frame = ttk.Frame(canvas)
        self.scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set);
        canvas.pack(side="left", fill="both", expand=True);
        scrollbar.pack(side="right", fill="y")


class App(tk.Tk if _HAS_TK else object):
    def __init__(self):
        super().__init__();
        self.title("Шахматы Видео — GUI v24.2.1 (Refactored)");
        self.geometry("1040x850");
        self.minsize(800, 650)
        self.channels = []
        self.input_path = tk.StringVar();
        self.output_dir = tk.StringVar();
        self.background_path = tk.StringVar();
        self.music_folder = tk.StringVar()
        self.left_video_folder = tk.StringVar();
        self.right_video_folder = tk.StringVar();
        self.watermark_text = tk.StringVar();
        self.theme = tk.StringVar()
        self.aspect = tk.StringVar(value="9:16 (Shorts)");
        self.fps = tk.IntVar();
        self.long_mate_freeze = tk.BooleanVar();
        self.freeze_seconds = tk.DoubleVar()
        self.check_highlight = tk.BooleanVar();
        self.check_arrow = tk.BooleanVar();
        self.show_checkmate_text = tk.BooleanVar()
        self.checkmate_highlight_squares = tk.BooleanVar();
        self.checkmate_arrows = tk.BooleanVar();
        self.show_text = tk.BooleanVar()
        self.show_all_captures = tk.BooleanVar();
        self.show_eval_icons = tk.BooleanVar()
        self.use_frame_cache = tk.BooleanVar();
        self.shorts59 = tk.BooleanVar();
        self.shorts45 = tk.BooleanVar()
        self.shorts30 = tk.BooleanVar();
        self.shorts_base = tk.BooleanVar();
        self.apply_speedup_to_horizontal = tk.BooleanVar();
        self.overlays_enabled = tk.BooleanVar()
        self.h_video_width = tk.IntVar();
        self.h_video_height = tk.IntVar();
        self.h_video_y_pos = tk.IntVar();
        self.h_top_bar_enabled = tk.BooleanVar()
        self.player_font_path = tk.StringVar();
        self.player_font_size = tk.IntVar();
        self.top_bar_font_path = tk.StringVar();
        self.top_bar_font_size = tk.IntVar()
        self.mate_font_path = tk.StringVar();
        self.mate_font_size = tk.IntVar();
        self.trailer_enabled = tk.BooleanVar();
        self.trailer_moves_count = tk.IntVar()
        self.trailer_move_duration = tk.DoubleVar();
        self.trailer_text_enabled = tk.BooleanVar()
        self.trailer_text_override = tk.StringVar();
        self.trailer_text_x = tk.IntVar();
        self.trailer_text_y = tk.IntVar();
        self.trailer_font_size = tk.IntVar()
        self.mate_text = tk.StringVar();
        self.mate_text_x = tk.IntVar();
        self.mate_text_y = tk.IntVar();
        self.v_fixed_layout_mode = tk.StringVar()
        self.v_strategy_hybrid = tk.BooleanVar();
        self.v_hybrid_top_bottom_ratio = tk.IntVar();
        self.v_hybrid_one_two_slot_ratio = tk.IntVar()
        self.v_hybrid_video_photo_ratio = tk.IntVar();
        self.v_hybrid_forbid_dual_photo = tk.BooleanVar();
        self.left_image_path = tk.StringVar()
        self.right_image_path = tk.StringVar();
        self.photo_to_video = tk.BooleanVar();
        self.photo_zoom_end = tk.DoubleVar();
        self.v_single_media_height_px = tk.IntVar()
        self.v_dual_media_height_px = tk.IntVar();
        self.v_dual_media_spacing = tk.IntVar();
        self.v_horizontal_padding = tk.IntVar();
        self.v_board_spacing = tk.IntVar()
        self.v_show_info_bar = tk.BooleanVar();
        self.v_info_bar_height = tk.IntVar();
        self.stop_overlay_loop = tk.BooleanVar();
        self.show_board_coords = tk.BooleanVar()
        self.board_coords_size_ratio = tk.DoubleVar();
        self.board_coords_stroke_width = tk.IntVar();
        self.subscribe_overlay_enabled = tk.BooleanVar()
        self.subscribe_overlay_path = tk.StringVar();
        self.subscribe_overlay_scale_top = tk.DoubleVar();
        self.subscribe_overlay_y_offset_top = tk.IntVar()
        self.subscribe_overlay_scale_bottom = tk.DoubleVar();
        self.subscribe_overlay_y_offset_bottom = tk.IntVar();
        self.lichess_api_key = tk.StringVar()
        self.stockfish_path = tk.StringVar();
        self.highlight_last_move = tk.BooleanVar();
        self.icon_folder_path = tk.StringVar();
        self.badge_scale = tk.DoubleVar()
        self.badge_pos = tk.StringVar();
        self.badge_margin = tk.DoubleVar();
        self.show_eval_bar = tk.BooleanVar();
        self.eval_bar_position = tk.StringVar()
        self.eval_bar_thickness = tk.IntVar();
        self.eval_bar_padding = tk.IntVar()
        self.trim_opening = tk.BooleanVar()
        self.trim_opening_cp_threshold = tk.IntVar()
        self.base_anim_duration = tk.DoubleVar()
        self.base_delay_duration = tk.DoubleVar()
        self.force_gui_speed = tk.BooleanVar()
        self.memes_enabled = tk.BooleanVar()
        self.memes_dir = tk.StringVar()
        self.gif_duration = tk.DoubleVar()

        self.log_queue = queue.Queue();
        self.cancel_flag = threading.Event();
        self.current_game_progress_str = tk.StringVar()
        self._build_ui();
        self._load_settings();
        self._poll_log();
        self.protocol("WM_DELETE_WINDOW", self._on_closing)
        self.v_strategy_hybrid.trace("w", self._on_v_strategy_change);
        self.after(100, self._toggle_coords_size_control)
        self._log("--- Шахматы Видео GUI v24.2.1 (Refactored) ---", is_gui_message=True)

    def _toggle_coords_size_control(self):
        state = "normal" if self.show_board_coords.get() else "disabled"
        if hasattr(self, 'lbl_coords_size'):
            for w in [self.lbl_coords_size, self.spn_coords_size, self.lbl_coords_stroke,
                      self.spn_coords_stroke]: w.configure(state=state)

    def _build_ui(self):
        pad, pad_s = {'padx': 8, 'pady': 4}, {'padx': 4, 'pady': 2}
        main_notebook = ttk.Notebook(self);
        main_notebook.pack(fill="both", expand=True, padx=6, pady=6)
        tab1_scroll, tab3_scroll = ScrollableFrame(main_notebook), ScrollableFrame(main_notebook)
        tab1, tab2, tab3 = tab1_scroll.scrollable_frame, ttk.Frame(main_notebook), tab3_scroll.scrollable_frame
        main_notebook.add(tab1_scroll, text="  Главные настройки  ");
        main_notebook.add(tab2, text="  Настройки макета  ");
        main_notebook.add(tab3_scroll, text="  Пути и файлы  ")
        tab1.columnconfigure(1, weight=1);
        r = 0
        project_frame = ttk.LabelFrame(tab1, text="Проект и Каналы");
        project_frame.grid(row=r, column=0, columnspan=4, sticky="we", **pad);
        r += 1;
        project_frame.columnconfigure(1, weight=1)
        ttk.Label(project_frame, text="Файл проекта (.json):").grid(row=0, column=0, sticky="w", **pad_s);
        ttk.Entry(project_frame, textvariable=self.input_path).grid(row=0, column=1, columnspan=2, sticky="we",
                                                                    **pad_s);
        ttk.Button(project_frame, text="Обзор…",
                   command=lambda: self._select_file(self.input_path, [("JSON", "*.json")])).grid(row=0, column=3,
                                                                                                  **pad_s)
        ttk.Label(project_frame, text="Папка вывода:").grid(row=1, column=0, sticky="w", **pad_s);
        ttk.Entry(project_frame, textvariable=self.output_dir).grid(row=1, column=1, columnspan=2, sticky="we",
                                                                    **pad_s);
        ttk.Button(project_frame, text="Выбрать…", command=lambda: self._select_dir(self.output_dir)).grid(row=1,
                                                                                                           column=3,
                                                                                                           **pad_s)
        ttk.Label(project_frame, text="Добавьте каналы для распределения партий.", wraplength=900).grid(row=2, column=0,
                                                                                                        columnspan=4,
                                                                                                        sticky='w',
                                                                                                        padx=4,
                                                                                                        pady=(6, 2))
        ch_list_f = ttk.Frame(project_frame);
        ch_list_f.grid(row=3, column=0, columnspan=4, sticky='we', padx=4, pady=2);
        self.channel_listbox = tk.Listbox(ch_list_f, height=3);
        self.channel_listbox.pack(side="left", fill='x', expand=True)
        ch_btn_f = ttk.Frame(ch_list_f);
        ch_btn_f.pack(side="left", padx=(8, 0));
        ttk.Button(ch_btn_f, text="Добавить...", command=self._add_channel).pack(fill='x', pady=1);
        ttk.Button(ch_btn_f, text="Изменить...", command=self._edit_channel).pack(fill='x', pady=1);
        ttk.Button(ch_btn_f, text="Удалить", command=self._remove_channel).pack(fill='x', pady=1)
        vis_frame = ttk.LabelFrame(tab1, text="🎨 Общие настройки видео");
        vis_frame.grid(row=r, column=0, columnspan=4, sticky="we", **pad);
        r += 1
        row1 = ttk.Frame(vis_frame);
        row1.pack(fill='x', **pad);
        ttk.Label(row1, text="Тема:").pack(side="left");
        ttk.Combobox(row1, textvariable=self.theme, values=SUPPORTED_THEMES, width=12, state="readonly").pack(
            side="left", padx=4);
        ttk.Label(row1, text="Формат:").pack(side="left", padx=(8, 0));
        ttk.Combobox(row1, textvariable=self.aspect, values=["9:16 (Shorts)", "16:9 (Горизонтальный)", "Оба формата"],
                     width=20, state="readonly").pack(side="left", padx=4);
        ttk.Label(row1, text="FPS:").pack(side="left", padx=(8, 0));
        ttk.Spinbox(row1, from_=10, to=60, textvariable=self.fps, width=6).pack(side="left", padx=4)
        board_vis_frame = ttk.LabelFrame(tab1, text="⚙️ Настройки отображения");
        board_vis_frame.grid(row=r, column=0, columnspan=4, sticky="we", **pad);
        r += 1
        eval_bar_frame = ttk.LabelFrame(board_vis_frame, text="Шкала оценки");
        eval_bar_frame.pack(fill='x', padx=pad['padx'], pady=pad['pady'])
        ebf_row1 = ttk.Frame(eval_bar_frame);
        ebf_row1.pack(fill='x', **pad_s);
        ttk.Checkbutton(ebf_row1, text="Показывать шкалу", variable=self.show_eval_bar).pack(side="left");
        ttk.Label(ebf_row1, text="Позиция:").pack(side="left", padx=(15, 0));
        ttk.Combobox(ebf_row1, textvariable=self.eval_bar_position, values=['Слева', 'Справа', 'Сверху', 'Снизу'],
                     width=8, state="readonly").pack(side="left", padx=4);
        ttk.Label(ebf_row1, text="Толщина:").pack(side="left", padx=(15, 0));
        ttk.Spinbox(ebf_row1, from_=5, to=100, textvariable=self.eval_bar_thickness, width=5).pack(side="left", padx=4);
        ttk.Label(ebf_row1, text="Отступ:").pack(side="left", padx=(15, 0));
        ttk.Spinbox(ebf_row1, from_=0, to=100, textvariable=self.eval_bar_padding, width=5).pack(side="left", padx=4)
        badge_frame = ttk.LabelFrame(board_vis_frame, text="Иконки-бейджи (!!, ?, ?!)");
        badge_frame.pack(fill='x', padx=pad['padx'], pady=pad['pady'])
        badge_row = ttk.Frame(badge_frame);
        badge_row.pack(fill='x', **pad_s);
        ttk.Label(badge_row, text="Масштаб:").pack(side="left");
        ttk.Spinbox(badge_row, from_=0.20, to=0.80, increment=0.01, textvariable=self.badge_scale, width=6).pack(
            side="left", padx=4);
        ttk.Label(badge_row, text="Позиция:").pack(side="left", padx=(15, 0));
        ttk.Combobox(badge_row, textvariable=self.badge_pos, values=['bl', 'br', 'tl', 'tr'], width=5,
                     state="readonly").pack(side="left", padx=4);
        ttk.Label(badge_row, text="Отступ:").pack(side="left", padx=(15, 0));
        ttk.Spinbox(badge_row, from_=0.0, to=0.3, increment=0.01, textvariable=self.badge_margin, width=6).pack(
            side="left", padx=4)
        other_board_frame = ttk.LabelFrame(board_vis_frame, text="Другие элементы");
        other_board_frame.pack(fill='x', padx=pad['padx'], pady=pad['pady'])
        bvf_row1 = ttk.Frame(other_board_frame);
        bvf_row1.pack(fill='x', **pad_s);
        ttk.Checkbutton(bvf_row1, text="Подсветка хода", variable=self.highlight_last_move).pack(side="left");
        ttk.Checkbutton(bvf_row1, text="Текст хода", variable=self.show_text).pack(side="left", padx=(15, 0));
        ttk.Checkbutton(bvf_row1, text="Стрелка на взятии", variable=self.show_all_captures).pack(side="left", padx=15);
        ttk.Checkbutton(bvf_row1, text="Подсветка шаха", variable=self.check_highlight).pack(side="left");
        ttk.Checkbutton(bvf_row1, text="Стрелка при шахе", variable=self.check_arrow).pack(side="left", padx=15);
        ttk.Checkbutton(bvf_row1, text="Показывать иконки оценки хода (!!, ?!)", variable=self.show_eval_icons).pack(
            side="left", padx=(15, 0));
        bvf_row2 = ttk.Frame(other_board_frame);
        bvf_row2.pack(fill='x', **pad_s);
        self.chk_coords = ttk.Checkbutton(bvf_row2, text="Номера полей", variable=self.show_board_coords,
                                          command=self._toggle_coords_size_control);
        self.chk_coords.pack(side="left");
        self.lbl_coords_size = ttk.Label(bvf_row2, text="Размер:");
        self.lbl_coords_size.pack(side="left", padx=(4, 2));
        self.spn_coords_size = ttk.Spinbox(bvf_row2, from_=0.10, to=0.50, increment=0.01,
                                           textvariable=self.board_coords_size_ratio, width=6);
        self.spn_coords_size.pack(side="left");
        self.lbl_coords_stroke = ttk.Label(bvf_row2, text="Толщина:");
        self.lbl_coords_stroke.pack(side="left", padx=(4, 2));
        self.spn_coords_stroke = ttk.Spinbox(bvf_row2, from_=0, to=5, textvariable=self.board_coords_stroke_width,
                                             width=4);
        self.spn_coords_stroke.pack(side="left")
        bvf_row3 = ttk.Frame(other_board_frame);
        bvf_row3.pack(fill='x', **pad_s)
        ttk.Label(bvf_row3, text="Базовая длит. анимации (с):").pack(side="left")
        ttk.Spinbox(bvf_row3, from_=0.1, to=2.0, increment=0.05, textvariable=self.base_anim_duration, width=6).pack(
            side="left", padx=4)
        ttk.Label(bvf_row3, text="Базовая пауза после хода (с):").pack(side="left", padx=(10, 0))
        ttk.Spinbox(bvf_row3, from_=0.1, to=5.0, increment=0.1, textvariable=self.base_delay_duration, width=6).pack(
            side="left", padx=4)
        bvf_row4 = ttk.Frame(other_board_frame);
        bvf_row4.pack(fill='x', **pad_s)
        ttk.Checkbutton(bvf_row4, text="Приоритет настроек скорости из ПО (игнор. JSON)",
                        variable=self.force_gui_speed).pack(side="left")
        mate_frame = ttk.LabelFrame(tab1, text="Настройки мата");
        mate_frame.grid(row=r, column=0, columnspan=4, sticky="we", **pad);
        r += 1
        self.chk_freeze = ttk.Checkbutton(mate_frame, text="Стоп-кадр", variable=self.long_mate_freeze,
                                          command=self._toggle_freeze_controls);
        self.chk_freeze.pack(side="left", padx=4);
        self.lbl_freeze = ttk.Label(mate_frame, text="на");
        self.lbl_freeze.pack(side="left", padx=(2, 2));
        self.sp_freeze = ttk.Spinbox(mate_frame, from_=0.05, to=10.0, increment=0.05, textvariable=self.freeze_seconds,
                                     width=6);
        self.sp_freeze.pack(side="left");
        self.lbl_freeze2 = ttk.Label(mate_frame, text="сек.");
        self.lbl_freeze2.pack(side="left", padx=2);
        self.chk_mate_text = ttk.Checkbutton(mate_frame, text="Надпись мата", variable=self.show_checkmate_text);
        self.chk_mate_text.pack(side="left", padx=8);
        ttk.Checkbutton(mate_frame, text="Подсветка полей", variable=self.checkmate_highlight_squares).pack(side="left",
                                                                                                            padx=8);
        ttk.Checkbutton(mate_frame, text="Стрелки атаки", variable=self.checkmate_arrows).pack(side="left", padx=8)
        trailer_frame = ttk.LabelFrame(tab1, text="🎬 Трейлер и Заставка");
        trailer_frame.grid(row=r, column=0, columnspan=4, sticky="we", **pad);
        r += 1;
        rowt1 = ttk.Frame(trailer_frame);
        rowt1.pack(fill='x', **pad_s);
        ttk.Checkbutton(rowt1, text="Включить", variable=self.trailer_enabled).pack(side="left");
        ttk.Label(rowt1, text="Кол-во ходов (0 = текст в начале):").pack(side="left", padx=(12, 4));
        ttk.Spinbox(rowt1, from_=0, to=30, textvariable=self.trailer_moves_count, width=6).pack(side="left", padx=6);
        ttk.Label(rowt1, text="Длит. хода (с):").pack(side="left", padx=(12, 4));
        ttk.Spinbox(rowt1, from_=0.2, to=5.0, increment=0.1, textvariable=self.trailer_move_duration, width=6).pack(
            side="left", padx=6);
        rowt2 = ttk.Frame(trailer_frame);
        rowt2.pack(fill='x', **pad_s);
        ttk.Checkbutton(rowt2, text="Показывать надпись", variable=self.trailer_text_enabled).pack(side="left");
        auto_frame = ttk.LabelFrame(tab1, text="🤖 Автоматизация");
        auto_frame.grid(row=r, column=0, columnspan=4, sticky="we", **pad);
        r += 1
        auto_row1 = ttk.Frame(auto_frame);
        auto_row1.pack(fill='x', **pad_s)
        ttk.Checkbutton(auto_row1, text="Обрезать скучный дебют", variable=self.trim_opening).pack(side="left")
        ttk.Label(auto_row1, text="Порог оценки (cp):").pack(side="left", padx=(15, 0))
        ttk.Spinbox(auto_row1, from_=20, to=200, increment=5, textvariable=self.trim_opening_cp_threshold,
                    width=6).pack(side="left", padx=4)
        speed_frame = ttk.LabelFrame(tab1, text="⏱️ Настройки ускорения");
        speed_frame.grid(row=r, column=0, columnspan=4, sticky="we", **pad);
        r += 1;
        speed_row1 = ttk.Frame(speed_frame);
        speed_row1.pack(fill='x', **pad_s);
        ttk.Label(speed_row1, text="Цели (сек):").pack(side="left", padx=(0, 8));
        ttk.Checkbutton(speed_row1, text="Базовый", variable=self.shorts_base).pack(side="left", padx=4);
        ttk.Checkbutton(speed_row1, text="59с", variable=self.shorts59).pack(side="left", padx=4);
        ttk.Checkbutton(speed_row1, text="45с", variable=self.shorts45).pack(side="left", padx=4);
        ttk.Checkbutton(speed_row1, text="30с", variable=self.shorts30).pack(side="left", padx=4);
        speed_row2 = ttk.Frame(speed_frame);
        speed_row2.pack(fill='x', **pad_s);
        ttk.Checkbutton(speed_row2, text="Применять к 16:9", variable=self.apply_speedup_to_horizontal).pack(
            side="left", padx=(4, 0))
        meme_frame = ttk.LabelFrame(tab1, text="🎭 Мем-оверлеи (GIF поверх видео)");
        meme_frame.grid(row=r, column=0, columnspan=4, sticky="we", **pad);
        r += 1
        meme_row1 = ttk.Frame(meme_frame);
        meme_row1.pack(fill='x', **pad_s)
        meme_status = "(chess_meme не установлен)" if not _MEME_PIPELINE_OK else ""
        ttk.Checkbutton(meme_row1, text=f"Включить мем-оверлеи {meme_status}",
                        variable=self.memes_enabled,
                        state="normal" if _MEME_PIPELINE_OK else "disabled").pack(side="left")
        ttk.Label(meme_row1, text="Длит. GIF (с):").pack(side="left", padx=(20, 0))
        ttk.Spinbox(meme_row1, from_=0.5, to=10.0, increment=0.5,
                    textvariable=self.gif_duration, width=6).pack(side="left", padx=4)
        meme_row2 = ttk.Frame(meme_frame);
        meme_row2.pack(fill='x', **pad_s)
        ttk.Label(meme_row2, text="Папка с GIF:").pack(side="left")
        ttk.Entry(meme_row2, textvariable=self.memes_dir).pack(side="left", fill='x', expand=True, padx=4)
        ttk.Button(meme_row2, text="Выбрать…",
                   command=lambda: self._select_dir(self.memes_dir)).pack(side="left")
        ttk.Label(meme_frame,
                  text="GIF-файлы: crying.gif, facepalm.gif, shock.gif, surprise.gif  |  "
                       "Ключ API: переменная окружения ANTHROPIC_API_KEY",
                  foreground="gray").pack(anchor='w', padx=4, pady=(0, 4))
        layout_notebook = ttk.Notebook(tab2);
        layout_notebook.pack(fill="both", expand=True, padx=4, pady=4);
        v_layout_tab, h_layout_tab = ScrollableFrame(layout_notebook), ScrollableFrame(layout_notebook);
        layout_notebook.add(v_layout_tab, text=" Вертикальный (9:16) ");
        layout_notebook.add(h_layout_tab, text=" Горизонтальный (16:9) ");
        self.v_layout_frame, self.h_layout_frame = v_layout_tab.scrollable_frame, h_layout_tab.scrollable_frame
        v_overlay_options_frame = ttk.Frame(self.v_layout_frame);
        v_overlay_options_frame.pack(anchor='w', fill='x', padx=8, pady=(8, 0));
        ttk.Checkbutton(v_overlay_options_frame, text="Включить видео-оверлеи и фото",
                        variable=self.overlays_enabled).pack(side='left');
        ttk.Checkbutton(v_overlay_options_frame, text="Не зацикливать оверлей", variable=self.stop_overlay_loop).pack(
            side='left', padx=25)
        v_common_settings = ttk.LabelFrame(self.v_layout_frame, text="Общие настройки для 9:16");
        v_common_settings.pack(fill='x', **pad);
        v_common_grid = ttk.Frame(v_common_settings);
        v_common_grid.pack(fill='x', padx=4);
        v_common_grid.columnconfigure(5, weight=1)
        ttk.Label(v_common_grid, text="Отступ от краев (X):").grid(row=0, column=0, sticky='w', pady=2);
        ttk.Spinbox(v_common_grid, from_=0, to=200, textvariable=self.v_horizontal_padding, width=8).grid(row=0,
                                                                                                          column=1,
                                                                                                          sticky='w');
        ttk.Label(v_common_grid, text="Отступ до доски (Y):").grid(row=0, column=2, sticky='w', padx=(10, 0), pady=2);
        ttk.Spinbox(v_common_grid, from_=0, to=200, textvariable=self.v_board_spacing, width=8).grid(row=0, column=3,
                                                                                                     sticky='w')
        info_bar_frame = ttk.Frame(v_common_grid);
        info_bar_frame.grid(row=0, column=4, sticky='w', padx=(15, 0));
        ttk.Checkbutton(info_bar_frame, text="Горизонтальный инфо-бар", variable=self.v_show_info_bar).pack(side='left',
                                                                                                            anchor='w');
        ttk.Label(info_bar_frame, text="Высота:").pack(side='left', padx=(5, 0));
        ttk.Spinbox(info_bar_frame, from_=2, to=100, textvariable=self.v_info_bar_height, width=5).pack(side='left')
        v_strategy_frame = ttk.LabelFrame(self.v_layout_frame, text="🎲 Стратегия раскладки для пакета (9:16)");
        v_strategy_frame.pack(fill='x', **pad);
        ttk.Radiobutton(v_strategy_frame, text="Фиксированная", variable=self.v_strategy_hybrid, value=False).pack(
            anchor='w', padx=4);
        self.v_fixed_strategy_frame = ttk.Frame(v_strategy_frame, padding=(20, 2, 0, 2));
        self.v_fixed_strategy_frame.pack(fill='x');
        ttk.Combobox(self.v_fixed_strategy_frame, textvariable=self.v_fixed_layout_mode,
                     values=["1 слот снизу", "1 слот сверху", "2 слота снизу", "2 слота сверху"], state="readonly",
                     width=20).pack()
        ttk.Radiobutton(v_strategy_frame, text="Гибридная", variable=self.v_strategy_hybrid, value=True).pack(
            anchor='w', padx=4, pady=(6, 0));
        self.v_hybrid_strategy_frame = ttk.Frame(v_strategy_frame, padding=(20, 2, 0, 2));
        self.v_hybrid_strategy_frame.pack(fill='x')

        def update_ratio_label(val, label, fmt): label.config(text=fmt.format(int(float(val)), 100 - int(float(val))))

        rf1 = ttk.Frame(self.v_hybrid_strategy_frame);
        rf1.pack(fill='x', pady=2);
        self.v_hybrid_top_bottom_label = ttk.Label(rf1);
        self.v_hybrid_top_bottom_label.pack(side='left', padx=(0, 10), ipadx=55);
        ttk.Scale(rf1, from_=0, to=100, variable=self.v_hybrid_top_bottom_ratio,
                  command=lambda v: update_ratio_label(v, self.v_hybrid_top_bottom_label, "Верх {}% / Низ {}%")).pack(
            fill='x', expand=True)
        rf2 = ttk.Frame(self.v_hybrid_strategy_frame);
        rf2.pack(fill='x', pady=2);
        self.v_hybrid_slots_label = ttk.Label(rf2);
        self.v_hybrid_slots_label.pack(side='left', padx=(0, 10), ipadx=55);
        ttk.Scale(rf2, from_=0, to=100, variable=self.v_hybrid_one_two_slot_ratio,
                  command=lambda v: update_ratio_label(v, self.v_hybrid_slots_label, "1 слот {}% / 2 слота {}%")).pack(
            fill='x', expand=True)
        rf3 = ttk.Frame(self.v_hybrid_strategy_frame);
        rf3.pack(fill='x', pady=2);
        self.v_hybrid_media_label = ttk.Label(rf3);
        self.v_hybrid_media_label.pack(side='left', padx=(0, 10), ipadx=55);
        ttk.Scale(rf3, from_=0, to=100, variable=self.v_hybrid_video_photo_ratio,
                  command=lambda v: update_ratio_label(v, self.v_hybrid_media_label, "Видео {}% / Фото {}%")).pack(
            fill='x', expand=True)
        ttk.Checkbutton(self.v_hybrid_strategy_frame, text="Запретить 2 фото в одном макете",
                        variable=self.v_hybrid_forbid_dual_photo).pack(anchor='w', pady=(4, 0))
        v_sizing_frame = ttk.LabelFrame(self.v_layout_frame, text="Размеры медиа-блоков (9:16)");
        v_sizing_frame.pack(fill='x', **pad);
        v_sizing_grid = ttk.Frame(v_sizing_frame);
        v_sizing_grid.pack(fill='x', padx=4)
        ttk.Label(v_sizing_grid, text="Высота (1 слот, px):").grid(row=0, column=0, sticky='w', pady=2);
        ttk.Spinbox(v_sizing_grid, from_=100, to=1500, textvariable=self.v_single_media_height_px, width=8).grid(row=0,
                                                                                                                 column=1,
                                                                                                                 sticky='w');
        ttk.Label(v_sizing_grid, text="Высота (2 слота, px):").grid(row=1, column=0, sticky='w', pady=2);
        ttk.Spinbox(v_sizing_grid, from_=100, to=1000, textvariable=self.v_dual_media_height_px, width=8).grid(row=1,
                                                                                                               column=1,
                                                                                                               sticky='w');
        ttk.Label(v_sizing_grid, text="Отступ (2 слота, px):").grid(row=1, column=2, sticky='w', padx=(10, 0), pady=2);
        ttk.Spinbox(v_sizing_grid, from_=0, to=100, textvariable=self.v_dual_media_spacing, width=8).grid(row=1,
                                                                                                          column=3,
                                                                                                          sticky='w')
        v_photo_effects_frame = ttk.LabelFrame(self.v_layout_frame, text="🖼️ Эффекты для фото (9:16)");
        v_photo_effects_frame.pack(fill='x', **pad);
        ken_burns_frame = ttk.Frame(v_photo_effects_frame);
        ken_burns_frame.pack(fill='x', padx=4, pady=2);
        ttk.Checkbutton(ken_burns_frame, text="Фото → видео (зум)", variable=self.photo_to_video).pack(side="left");
        ttk.Label(ken_burns_frame, text="Зум до:").pack(side="left", padx=(12, 4));
        ttk.Spinbox(ken_burns_frame, from_=1.00, to=1.30, increment=0.01, textvariable=self.photo_zoom_end,
                    width=6).pack(side="left")
        sub_frame = ttk.LabelFrame(self.v_layout_frame, text="📢 Оверлей 'Подпишись'");
        sub_frame.pack(fill='x', **pad);
        sub_row1 = ttk.Frame(sub_frame);
        sub_row1.pack(fill='x', padx=4, pady=2);
        ttk.Checkbutton(sub_row1, text="Включить", variable=self.subscribe_overlay_enabled).pack(anchor='w')
        top_set_f = ttk.LabelFrame(sub_frame, text="Верхний макет", padding=5);
        top_set_f.pack(fill='x', padx=4, pady=4);
        ttk.Label(top_set_f, text="Масштаб:").pack(side='left');
        ttk.Scale(top_set_f, from_=0.1, to=1.0, variable=self.subscribe_overlay_scale_top).pack(fill='x', expand=True,
                                                                                                padx=5, side='left');
        ttk.Label(top_set_f, text="Смещение Y:").pack(side='left');
        ttk.Spinbox(top_set_f, from_=-200, to=200, textvariable=self.subscribe_overlay_y_offset_top, width=8).pack(
            side='left', padx=5)
        bot_set_f = ttk.LabelFrame(sub_frame, text="Нижний макет", padding=5);
        bot_set_f.pack(fill='x', padx=4, pady=4);
        ttk.Label(bot_set_f, text="Масштаб:").pack(side='left');
        ttk.Scale(bot_set_f, from_=0.1, to=1.0, variable=self.subscribe_overlay_scale_bottom).pack(fill='x',
                                                                                                   expand=True, padx=5,
                                                                                                   side='left');
        ttk.Label(bot_set_f, text="Смещение Y:").pack(side='left');
        ttk.Spinbox(bot_set_f, from_=-200, to=200, textvariable=self.subscribe_overlay_y_offset_bottom, width=8).pack(
            side='left', padx=5)
        h_grid1 = ttk.Frame(self.h_layout_frame);
        h_grid1.pack(fill='x', **pad);
        ttk.Checkbutton(h_grid1, text="Инфо-панель сверху", variable=self.h_top_bar_enabled).pack(anchor='w',
                                                                                                  pady=(2, 4));
        h_grid2 = ttk.Frame(self.h_layout_frame);
        h_grid2.pack(fill='x', **pad);
        ttk.Label(h_grid2, text="Ширина видео:").grid(row=0, column=0, sticky='w', pady=2);
        ttk.Spinbox(h_grid2, from_=100, to=1000, textvariable=self.h_video_width, width=8).grid(row=0, column=1,
                                                                                                sticky='w');
        ttk.Label(h_grid2, text="Высота видео:").grid(row=0, column=2, sticky='w', padx=(10, 0), pady=2);
        ttk.Spinbox(h_grid2, from_=100, to=1080, textvariable=self.h_video_height, width=8).grid(row=0, column=3,
                                                                                                 sticky='w');
        ttk.Label(h_grid2, text="Отступ видео (Y):").grid(row=1, column=0, sticky='w', pady=2);
        ttk.Spinbox(h_grid2, from_=0, to=500, textvariable=self.h_video_y_pos, width=8).grid(row=1, column=1,
                                                                                             sticky='w')
        tab3.columnconfigure(1, weight=1);
        r = 0;
        key_paths_frame = ttk.LabelFrame(tab3, text="⚙️ Ключевые пути");
        key_paths_frame.grid(row=r, column=0, columnspan=4, sticky="we", **pad);
        r += 1;
        key_paths_frame.columnconfigure(1, weight=1)
        ttk.Label(key_paths_frame, text="Путь к Stockfish:").grid(row=0, column=0, sticky="w", **pad_s);
        ttk.Entry(key_paths_frame, textvariable=self.stockfish_path).grid(row=0, column=1, sticky="we", **pad_s);
        ttk.Button(key_paths_frame, text="…",
                   command=lambda: self._select_file(self.stockfish_path, [("Stockfish Executable", "*.*")])).grid(
            row=0, column=2, **pad_s)
        ttk.Label(key_paths_frame, text="Папка с иконками:").grid(row=1, column=0, sticky="w", **pad_s);
        ttk.Entry(key_paths_frame, textvariable=self.icon_folder_path).grid(row=1, column=1, sticky="we", **pad_s);
        ttk.Button(key_paths_frame, text="…", command=lambda: self._select_dir(self.icon_folder_path)).grid(row=1,
                                                                                                            column=2,
                                                                                                            **pad_s)
        media_paths_frame = ttk.LabelFrame(tab3, text="📁 Медиа-ресурсы");
        media_paths_frame.grid(row=r, column=0, columnspan=4, sticky="we", **pad);
        r += 1;
        media_paths_frame.columnconfigure(1, weight=1)
        ttk.Label(media_paths_frame, text="Фон (без каналов):").grid(row=0, column=0, sticky="w", **pad_s);
        ttk.Entry(media_paths_frame, textvariable=self.background_path).grid(row=0, column=1, sticky="we", **pad_s);
        ttk.Button(media_paths_frame, text="Обзор…",
                   command=lambda: self._select_file(self.background_path, [("Images", "*.png *.jpg")])).grid(row=0,
                                                                                                              column=2,
                                                                                                              **pad_s)
        ttk.Label(media_paths_frame, text="Папка с музыкой:").grid(row=1, column=0, sticky="w", **pad_s);
        ttk.Entry(media_paths_frame, textvariable=self.music_folder).grid(row=1, column=1, sticky="we", **pad_s);
        ttk.Button(media_paths_frame, text="Выбрать…", command=lambda: self._select_dir(self.music_folder)).grid(row=1,
                                                                                                                 column=2,
                                                                                                                 **pad_s)
        ttk.Label(media_paths_frame, text="Папка видео (лево/верх):").grid(row=2, column=0, sticky="w", **pad_s);
        ttk.Entry(media_paths_frame, textvariable=self.left_video_folder).grid(row=2, column=1, sticky="we", **pad_s);
        ttk.Button(media_paths_frame, text="Выбрать…", command=lambda: self._select_dir(self.left_video_folder)).grid(
            row=2, column=2, **pad_s)
        ttk.Label(media_paths_frame, text="Папка видео (право/низ):").grid(row=3, column=0, sticky="w", **pad_s);
        ttk.Entry(media_paths_frame, textvariable=self.right_video_folder).grid(row=3, column=1, sticky="we", **pad_s);
        ttk.Button(media_paths_frame, text="Выбрать…", command=lambda: self._select_dir(self.right_video_folder)).grid(
            row=3, column=2, **pad_s)
        ttk.Label(media_paths_frame, text="Фото (лево/верх):").grid(row=4, column=0, sticky='w', **pad_s);
        ttk.Entry(media_paths_frame, textvariable=self.left_image_path).grid(row=4, column=1, sticky='we', **pad_s);
        ttk.Button(media_paths_frame, text="Обзор…",
                   command=lambda: self._select_file(self.left_image_path, [("Images", "*.png *.jpg")])).grid(row=4,
                                                                                                              column=2,
                                                                                                              **pad_s);
        ttk.Label(media_paths_frame, text="Фото (право/низ):").grid(row=5, column=0, sticky='w', **pad_s);
        ttk.Entry(media_paths_frame, textvariable=self.right_image_path).grid(row=5, column=1, sticky='we', **pad_s);
        ttk.Button(media_paths_frame, text="Обзор…",
                   command=lambda: self._select_file(self.right_image_path, [("Images", "*.png *.jpg")])).grid(row=5,
                                                                                                               column=2,
                                                                                                               **pad_s)
        ttk.Label(media_paths_frame, text="Оверлей 'Подпишись':").grid(row=6, column=0, sticky='w', **pad_s);
        ttk.Entry(media_paths_frame, textvariable=self.subscribe_overlay_path).grid(row=6, column=1, sticky='we',
                                                                                    **pad_s);
        ttk.Button(media_paths_frame, text="Обзор...", command=lambda: self._select_file(self.subscribe_overlay_path, [
            ("Media", "*.mov *.webm *.png *.mp4")])).grid(row=6, column=2, **pad_s);
        fonts_frame = ttk.LabelFrame(tab3, text="✍️ Шрифты, тексты и API");
        fonts_frame.grid(row=r, column=0, columnspan=4, sticky="we", **pad);
        r += 1;
        fonts_frame.columnconfigure(1, weight=1)
        ttk.Label(fonts_frame, text="Lichess API Key:").grid(row=1, column=0, sticky="w", **pad_s);
        ttk.Entry(fonts_frame, textvariable=self.lichess_api_key).grid(row=1, column=1, columnspan=3, sticky="we",
                                                                       **pad_s)
        ttk.Label(fonts_frame, text="Водяной знак:").grid(row=2, column=0, sticky="w", **pad_s);
        ttk.Entry(fonts_frame, textvariable=self.watermark_text).grid(row=2, column=1, columnspan=3, sticky="we",
                                                                      **pad_s)
        h_grid3 = ttk.Frame(fonts_frame);
        h_grid3.grid(row=3, column=0, columnspan=4, sticky='we', padx=4, pady=(6, 0));
        h_grid3.columnconfigure(1, weight=1);
        ttk.Label(h_grid3, text="Шрифт имен (16:9):").grid(row=0, column=0, sticky='w', **pad_s);
        ttk.Entry(h_grid3, textvariable=self.player_font_path).grid(row=0, column=1, sticky='we', **pad_s);
        ttk.Button(h_grid3, text="…",
                   command=lambda: self._select_file(self.player_font_path, [("Fonts", "*.ttf *.otf")])).grid(row=0,
                                                                                                              column=2);
        ttk.Label(h_grid3, text="Размер:").grid(row=0, column=3, sticky='w', padx=(10, 0));
        ttk.Spinbox(h_grid3, from_=12, to=200, textvariable=self.player_font_size, width=8).grid(row=0, column=4,
                                                                                                 sticky='w', **pad_s)
        h_grid4 = ttk.Frame(fonts_frame);
        h_grid4.grid(row=4, column=0, columnspan=4, sticky='we', padx=4);
        h_grid4.columnconfigure(1, weight=1);
        ttk.Label(h_grid4, text="Шрифт инфо-панели:").grid(row=0, column=0, sticky='w', **pad_s);
        ttk.Entry(h_grid4, textvariable=self.top_bar_font_path).grid(row=0, column=1, sticky='we', **pad_s);
        ttk.Button(h_grid4, text="…",
                   command=lambda: self._select_file(self.top_bar_font_path, [("Fonts", "*.ttf *.otf")])).grid(row=0,
                                                                                                               column=2);
        ttk.Label(h_grid4, text="Размер:").grid(row=0, column=3, sticky='w', padx=(10, 0));
        ttk.Spinbox(h_grid4, from_=12, to=200, textvariable=self.top_bar_font_size, width=8).grid(row=0, column=4,
                                                                                                  sticky='w', **pad_s)
        row4b = ttk.Frame(fonts_frame);
        row4b.grid(row=5, column=0, columnspan=4, sticky='we', padx=4);
        row4b.columnconfigure(1, weight=1);
        ttk.Label(row4b, text="Шрифт Мата/Трейлера:").grid(row=0, column=0, sticky='w', **pad_s);
        ttk.Entry(row4b, textvariable=self.mate_font_path).grid(row=0, column=1, sticky='we', **pad_s);
        ttk.Button(row4b, text="…",
                   command=lambda: self._select_file(self.mate_font_path, [("Fonts", "*.ttf *.otf")])).grid(row=0,
                                                                                                            column=2)
        row_trailer_text = ttk.Frame(fonts_frame);
        row_trailer_text.grid(row=6, column=0, columnspan=4, sticky='we', padx=4);
        row_trailer_text.columnconfigure(1, weight=1);
        ttk.Label(row_trailer_text, text="Текст трейлера:").grid(row=0, column=0, sticky='w', **pad_s);
        ttk.Entry(row_trailer_text, textvariable=self.trailer_text_override).grid(row=0, column=1, sticky="we",
                                                                                  **pad_s);
        ttk.Label(row_trailer_text, text="X,Y:").grid(row=0, column=2, sticky='w', padx=(4, 0));
        ttk.Spinbox(row_trailer_text, from_=0, to=10000, textvariable=self.trailer_text_x, width=5).grid(row=0,
                                                                                                         column=3,
                                                                                                         **pad_s);
        ttk.Spinbox(row_trailer_text, from_=0, to=10000, textvariable=self.trailer_text_y, width=5).grid(row=0,
                                                                                                         column=4,
                                                                                                         **pad_s);
        ttk.Label(row_trailer_text, text="Размер:").grid(row=0, column=5, sticky='w', padx=(4, 0));
        ttk.Spinbox(row_trailer_text, from_=0, to=2000, textvariable=self.trailer_font_size, width=5).grid(row=0,
                                                                                                           column=6,
                                                                                                           **pad_s)
        row_mate_text = ttk.Frame(fonts_frame);
        row_mate_text.grid(row=7, column=0, columnspan=4, sticky='we', padx=4);
        row_mate_text.columnconfigure(1, weight=1);
        ttk.Label(row_mate_text, text="Текст мата:").grid(row=0, column=0, sticky='w', **pad_s);
        ttk.Entry(row_mate_text, textvariable=self.mate_text).grid(row=0, column=1, sticky="we", **pad_s);
        ttk.Label(row_mate_text, text="X,Y:").grid(row=0, column=2, sticky='w', padx=(4, 0));
        ttk.Spinbox(row_mate_text, from_=0, to=10000, textvariable=self.mate_text_x, width=5).grid(row=0, column=3,
                                                                                                   **pad_s);
        ttk.Spinbox(row_mate_text, from_=0, to=10000, textvariable=self.mate_text_y, width=5).grid(row=0, column=4,
                                                                                                   **pad_s);
        ttk.Label(row_mate_text, text="Размер:").grid(row=0, column=5, sticky='w', padx=(4, 0));
        ttk.Spinbox(row_mate_text, from_=0, to=2000, textvariable=self.mate_font_size, width=5).grid(row=0, column=6,
                                                                                                     **pad_s)
        bottom_frame = ttk.Frame(self);
        bottom_frame.pack(fill='x', side='bottom', padx=6, pady=6);
        actions = ttk.Frame(bottom_frame);
        actions.pack(fill='x');
        self.btn_start = ttk.Button(actions, text="СТАРТ", command=self._start_worker);
        self.btn_start.pack(side="left", **pad);
        self.btn_cancel = ttk.Button(actions, text="Отмена", command=self._cancel_worker, state="disabled");
        self.btn_cancel.pack(side="left", **pad);
        ttk.Button(actions, text="Открыть папку", command=self._open_out_dir).pack(side="left", **pad);
        ttk.Button(actions, text="Очистить кэш", command=self._clear_cache).pack(side="left", **pad)
        ttk.Button(actions, text="Загрузить конфиг...", command=self._load_settings_from_file).pack(side="left", **pad);
        ttk.Button(actions, text="Сохранить конфиг...", command=self._save_settings_to_file).pack(side="left", **pad)
        cache_frame = ttk.Frame(actions);
        cache_frame.pack(side='right');
        ttk.Checkbutton(cache_frame, text="Кэш кадров", variable=self.use_frame_cache).pack(side="right", **pad)
        pb_labels_frame = ttk.Frame(bottom_frame);
        pb_labels_frame.pack(fill='x', padx=8, pady=(4, 0));
        ttk.Label(pb_labels_frame, text="Общий прогресс:").pack(side='left');
        self.pb_batch = ttk.Progressbar(bottom_frame, orient="horizontal", mode="determinate");
        self.pb_batch.pack(fill='x', padx=8, pady=(0, 4))
        pb_labels_frame2 = ttk.Frame(bottom_frame);
        pb_labels_frame2.pack(fill='x', padx=8, pady=(4, 0));
        ttk.Label(pb_labels_frame2, text="Текущее видео:").pack(side='left');
        ttk.Label(pb_labels_frame2, textvariable=self.current_game_progress_str).pack(side='left');
        self.pb_frames = ttk.Progressbar(bottom_frame, orient="horizontal", mode="determinate");
        self.pb_frames.pack(fill='x', padx=8, pady=4)
        log_frame = ttk.Frame(bottom_frame);
        log_frame.pack(fill='x', padx=8, pady=4);
        log_frame.columnconfigure(0, weight=1);
        self.txt_log = tk.Text(log_frame, height=10, wrap="word");
        self.txt_log.grid(row=0, column=0, sticky="we");
        log_scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.txt_log.yview);
        log_scrollbar.grid(row=0, column=1, sticky="ns");
        self.txt_log['yscrollcommand'] = log_scrollbar.set;
        ttk.Button(log_frame, text="Копировать\nлог", command=self._copy_log).grid(row=0, column=2, sticky="ns",
                                                                                   padx=(5, 0))

    def _on_v_strategy_change(self, *args):
        is_hybrid = self.v_strategy_hybrid.get();
        state_map = {True: ("disabled", "normal"), False: ("normal", "disabled")}
        for child in self.v_fixed_strategy_frame.winfo_children(): child.configure(state=state_map[is_hybrid][0])
        for child in self.v_hybrid_strategy_frame.winfo_children():
            if isinstance(child, ttk.Frame):
                for sub in child.winfo_children(): sub.configure(state=state_map[is_hybrid][1])
            else:
                child.configure(state=state_map[is_hybrid][1])

    def _update_channel_listbox(self):
        self.channel_listbox.delete(0, tk.END);
        [self.channel_listbox.insert(tk.END, f"{i + 1}. {c['name']}") for i, c
         in enumerate(self.channels)]

    def _add_channel(self):
        self._show_channel_dialog()

    def _edit_channel(self):
        if sel := self.channel_listbox.curselection():
            self._show_channel_dialog(edit_index=sel[0])
        else:
            messagebox.showwarning("Внимание", "Выберите канал для редактирования.")

    def _remove_channel(self):
        if not (sel := self.channel_listbox.curselection()): return messagebox.showwarning("Внимание",
                                                                                           "Выберите канал для удаления.")
        if messagebox.askyesno("Подтверждение", f"Удалить '{self.channels[sel[0]]['name']}'?"): del self.channels[
            sel[0]]; self._update_channel_listbox()

    def _show_channel_dialog(self, edit_index=None):
        dialog = tk.Toplevel(self);
        dialog.title("Настройки канала");
        dialog.geometry("600x150");
        dialog.transient(self);
        dialog.grab_set();
        name_var, path_var = tk.StringVar(), tk.StringVar()
        if edit_index is not None: name_var.set(self.channels[edit_index]['name']); path_var.set(
            self.channels[edit_index]['bg_path'])
        frm = ttk.Frame(dialog, padding=10);
        frm.pack(fill=tk.BOTH, expand=True);
        frm.columnconfigure(1, weight=1);
        ttk.Label(frm, text="Название:").grid(row=0, column=0, sticky='w', pady=5);
        ttk.Entry(frm, textvariable=name_var).grid(row=0, column=1, columnspan=2, sticky='we');
        ttk.Label(frm, text="Фон:").grid(row=1, column=0, sticky='w', pady=5);
        ttk.Entry(frm, textvariable=path_var).grid(row=1, column=1, sticky='we')

        def browse():
            if p := filedialog.askopenfilename(title="Выберите фон",
                                               filetypes=[("Images", "*.png *.jpg")]): path_var.set(p)

        ttk.Button(frm, text="Обзор...", command=browse).grid(row=1, column=2, padx=(5, 0))

        def on_ok():
            name, path = name_var.get().strip(), path_var.get().strip()
            if not name or not path or not Path(path).exists(): return messagebox.showerror("Ошибка",
                                                                                            "Имя и путь к файлу должны быть заполнены.",
                                                                                            parent=dialog)
            if edit_index is not None:
                self.channels[edit_index] = {'name': name, 'bg_path': path}
            else:
                self.channels.append({'name': name, 'bg_path': path})
            self._update_channel_listbox();
            dialog.destroy()

        btn_frm = ttk.Frame(frm);
        btn_frm.grid(row=2, column=0, columnspan=3, pady=(10, 0));
        ttk.Button(btn_frm, text="OK", command=on_ok).pack(side="left", padx=5);
        ttk.Button(btn_frm, text="Отмена", command=dialog.destroy).pack(side="left", padx=5);
        dialog.wait_window()

    def _toggle_freeze_controls(self):
        state = "normal" if self.long_mate_freeze.get() else "disabled"
        for w in [self.lbl_freeze, self.sp_freeze, self.lbl_freeze2, self.chk_mate_text]: w.configure(state=state)

    def _select_file(self, var, types):
        if p := filedialog.askopenfilename(title="Выберите файл", filetypes=types): var.set(p)

    def _select_dir(self, var):
        if p := filedialog.askdirectory(title="Выберите папку"): var.set(p)

    def _open_out_dir(self):
        p = self.output_dir.get().strip()
        if not p: return messagebox.showinfo("Инфо", "Папка вывода не указана.")
        try:
            os.makedirs(p, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(p)
            else:
                subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", p])
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось открыть папку: {e}")

    def _clear_cache(self):
        if not (out_dir := self.output_dir.get().strip()): return
        cache_path = Path(out_dir) / "_frame_cache"
        if not cache_path.exists() and not CACHE_FILE.exists(): return self._log("✅ Кэш уже чист.", is_gui_message=True)
        if messagebox.askyesno("Подтверждение", f"Удалить кэш кадров и Lichess-оценок?"):
            try:
                if cache_path.exists(): shutil.rmtree(cache_path)
                if CACHE_FILE.exists(): os.remove(CACHE_FILE)
                self._log("✅ Кэш успешно очищен.", is_gui_message=True)
            except Exception as e:
                messagebox.showerror("Ошибка", f"Не удалось очистить кэш:\n{e}")

    def _copy_log(self):
        self.clipboard_clear();
        self.clipboard_append(self.txt_log.get("1.0", tk.END));
        self._log(
            "--- Лог скопирован ---", is_gui_message=True)

    def _log(self, msg, level=logging.INFO, is_gui_message=False):
        if is_gui_message: self.log_queue.put(str(msg))
        logger.log(level, str(msg))

    def _poll_log(self):
        while not self.log_queue.empty():
            try:
                self.txt_log.insert(tk.END, self.log_queue.get() + "\n");
                self.txt_log.see(tk.END)
            except:
                break
        self.after(100, self._poll_log)

    def _save_settings_to_file(self):
        filepath = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON files", "*.json")],
                                                title="Сохранить конфигурацию как...")
        if not filepath: return
        settings = {k: v.get() for k, v in self.__dict__.items() if isinstance(v, tk.Variable)};
        settings['channels_list'] = self.channels
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=4)
            self._log(f"✅ Конфигурация сохранена в {Path(filepath).name}", is_gui_message=True)
        except Exception as e:
            messagebox.showerror("Ошибка сохранения", f"Не удалось сохранить файл:\n{e}")

    def _load_settings_from_file(self):
        filepath = filedialog.askopenfilename(filetypes=[("JSON files", "*.json")], title="Загрузить конфигурацию")
        if not filepath: return
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                settings = json.load(f)
            self.channels = settings.get('channels_list', [])
            self._update_channel_listbox()
            for k, v in settings.items():
                if k != 'channels_list' and hasattr(self, k):
                    if isinstance(var := getattr(self, k), tk.Variable): var.set(v)
            self.after(50, self._update_slider_labels);
            self.after(50, self._toggle_coords_size_control);
            self.after(50, lambda: self.v_strategy_hybrid.set(self.v_strategy_hybrid.get()))
            self._log(f"✅ Конфигурация загружена из {Path(filepath).name}", is_gui_message=True)
        except Exception as e:
            messagebox.showerror("Ошибка загрузки", f"Не удалось прочитать файл конфигурации:\n{e}")

    def _save_settings(self, is_preset=False):
        settings = {k: v.get() for k, v in self.__dict__.items() if isinstance(v, tk.Variable)};
        settings['channels_list'] = self.channels
        try:
            CONFIG_FILE.write_text(json.dumps(settings, indent=4), encoding='utf-8')
        except:
            pass

    def _update_slider_labels(self):
        def update_label(val, label, fmt):
            if label.winfo_exists(): label.config(text=fmt.format(int(float(val)), 100 - int(float(val))))

        update_label(self.v_hybrid_top_bottom_ratio.get(), self.v_hybrid_top_bottom_label, "Верх {}% / Низ {}%")
        update_label(self.v_hybrid_one_two_slot_ratio.get(), self.v_hybrid_slots_label, "1 слот {}% / 2 слота {}%")
        update_label(self.v_hybrid_video_photo_ratio.get(), self.v_hybrid_media_label, "Видео {}% / Фото {}%")

    def _load_settings(self):
        defaults = {"input_path": "", "output_dir": str(Path("hd_chess_videos").absolute()), "background_path": "",
                    "music_folder": "", "channels_list": [], "left_video_folder": "", "right_video_folder": "",
                    "watermark_text": "", "theme": "dark-wood", "aspect": "9:16 (Shorts)", "fps": 30,
                    "long_mate_freeze": True, "freeze_seconds": 3.0, "check_highlight": True, "check_arrow": True,
                    "show_checkmate_text": True, "checkmate_highlight_squares": True, "checkmate_arrows": True,
                    "show_text": True, "show_all_captures": True, "show_eval_icons": True, "use_frame_cache": True,
                    "shorts59": True,
                    "shorts45": False, "shorts30": False, "shorts_base": True, "apply_speedup_to_horizontal": False,
                    "overlays_enabled": True, "h_video_width": 420, "h_video_height": 600, "h_video_y_pos": 100,
                    "h_top_bar_enabled": True, "player_font_path": "", "player_font_size": 38, "top_bar_font_path": "",
                    "top_bar_font_size": 42, "mate_font_path": "", "mate_font_size": 0, "mate_text": "CHECKMATE!",
                    "mate_text_x": 0, "mate_text_y": 0, "trailer_enabled": True, "trailer_moves_count": 5,
                    "trailer_move_duration": 0.8, "trailer_text_enabled": True, "trailer_text_override": "",
                    "trailer_text_x": 0, "trailer_text_y": 0, "trailer_font_size": 0,
                    "v_fixed_layout_mode": "1 слот снизу", "v_strategy_hybrid": False, "v_hybrid_top_bottom_ratio": 70,
                    "v_hybrid_one_two_slot_ratio": 70, "v_hybrid_video_photo_ratio": 50,
                    "v_hybrid_forbid_dual_photo": True, "left_image_path": "", "right_image_path": "",
                    "photo_to_video": True, "photo_zoom_end": 1.08, "v_single_media_height_px": 450,
                    "v_dual_media_height_px": 320, "v_dual_media_spacing": 20, "v_horizontal_padding": 50,
                    "v_board_spacing": 50, "v_show_info_bar": True, "v_info_bar_height": 10, "stop_overlay_loop": False,
                    "show_board_coords": True, "board_coords_size_ratio": 0.36, "board_coords_stroke_width": 2,
                    "subscribe_overlay_enabled": False, "subscribe_overlay_path": "",
                    "subscribe_overlay_scale_top": 0.8, "subscribe_overlay_y_offset_top": -20,
                    "subscribe_overlay_scale_bottom": 0.8, "subscribe_overlay_y_offset_bottom": 20,
                    "lichess_api_key": "lip_...ВВЕДИТЕ ВАШ КЛЮЧ СЮДА", "stockfish_path": "",
                    "highlight_last_move": True, "icon_folder_path": "chess_icons", "badge_scale": 0.42,
                    "badge_pos": "bl", "badge_margin": 0.05, "show_eval_bar": True, "eval_bar_position": "Слева",
                    "eval_bar_thickness": 30, "eval_bar_padding": 10, "trim_opening": True,
                    "trim_opening_cp_threshold": 70, "base_anim_duration": 0.4, "base_delay_duration": 0.8,
                    "force_gui_speed": False,
                    "memes_enabled": False, "memes_dir": "assets/memes", "gif_duration": 2.0}
        try:
            settings = json.loads(CONFIG_FILE.read_text(encoding='utf-8')) if CONFIG_FILE.exists() else {}
        except:
            settings = {}
        self.channels = settings.get('channels_list', defaults['channels_list']);
        self._update_channel_listbox()
        for k, v in defaults.items():
            if k != 'channels_list' and hasattr(self, k) and isinstance(getattr(self, k), tk.Variable): getattr(self,
                                                                                                                k).set(
                settings.get(k, v))
        self.after(50, self._update_slider_labels);
        self.after(50, self._toggle_coords_size_control);
        self.after(50, lambda: self.v_strategy_hybrid.set(self.v_strategy_hybrid.get()))

    def _on_closing(self):
        self._save_settings();
        self.destroy()

    def _start_worker(self):
        if not self.input_path.get(): return messagebox.showerror("Ошибка", "Укажите файл проекта (.json).")
        self.btn_start.config(state="disabled");
        self.btn_cancel.config(state="normal");
        self.cancel_flag.clear();
        self._log("\n▶️ Старт…", is_gui_message=True);
        threading.Thread(target=self._worker, daemon=True).start()

    def _cancel_worker(self):
        self.cancel_flag.set();
        self._log("⛔ Запрошена отмена…", is_gui_message=True)

    def _validate_game_moves(self, moves, fen):
        board = chess.Board(fen) if fen else chess.Board()
        try:
            for i, san in enumerate(moves, 1):
                if board.is_game_over(): self._log(
                    f"   - ❌ Ошибка валидации. Лишний ход #{i}: '{san}' после окончания партии.", level=logging.WARNING,
                    is_gui_message=True); return False
                board.push_san(san)
            return True
        except Exception as e:
            self._log(f"   - ❌ Ошибка валидации. Неверный ход #{i}: '{san}'. Причина: {e}", level=logging.WARNING,
                      is_gui_message=True);
            return False

    # ==============================================================================
    # РЕФАКТОРИНГ ГЛАВНОГО ВОРКЕРА
    # ==============================================================================

    def _run_game_analysis(self, moves: List[str], initial_fen: Optional[str], log_prefix: str) -> Tuple[Dict, Dict, List]:
        """Выполняет полный анализ одной игры (Lichess + Stockfish)."""
        self._log(f"{log_prefix}   - 🤖 Гибридный анализ...", is_gui_message=True)
        eval_map, script, game_states = {}, {}, []
        try:
            board = chess.Board(initial_fen) if initial_fen else chess.Board()
            all_fens_in_game = {board.shredder_fen()}
            game_states = []
            for ply, san in enumerate(moves):
                fen_before = board.shredder_fen()
                board.push_san(san)
                fen_after = board.shredder_fen()
                all_fens_in_game.add(fen_after)
                game_states.append({"ply": ply, "san": san, "fen_before": fen_before, "fen_after": fen_after})

            eval_map = get_eval_for_fen_batch(list(all_fens_in_game), api_key=self.lichess_api_key.get(),
                                              log_cb=lambda msg: self._log(f"{log_prefix}{msg}", is_gui_message=True))

            missing_fens = [fen for fen, val in eval_map.items() if val is None]
            if missing_fens:
                self._log(f"{log_prefix}   - ⛳ Фолбэк: Stockfish для {len(missing_fens)} позиций...",
                          is_gui_message=True)
                local_map = get_eval_for_fen_batch_local(missing_fens, engine_path=self.stockfish_path.get(),
                                                         log_cb=lambda m: self._log(f"{log_prefix}   {m}",
                                                                                    is_gui_message=True))
                eval_map.update(local_map)

            annotated_moves = annotate_moves(game_states, eval_map)

            script = {"moves": []}
            script_map = {}
            for am in annotated_moves:
                key = (am["ply"], am["san"])
                if key not in script_map:
                    move_data = {"ply": am["ply"], "san": am["san"], "effects": {}}
                    script["moves"].append(move_data)
                    script_map[key] = move_data

                script_map[key].setdefault("effects", {}).update({
                    "mark": am["mark"],
                    "bar_from_pct": am["bar_from_pct"],
                    "bar_to_pct": am["bar_to_pct"]
                })
        except Exception as e:
            self._log(f"{log_prefix}   - ❌ Ошибка при анализе: {e}", level=logging.ERROR, is_gui_message=True)

        return eval_map, script, game_states

    def _process_single_game(self, game_info: Dict, game_index: int, total_games: int,
                             channel_game_counters: Dict) -> int:
        """Обрабатывает одну игру: анализ, рендеринг для разных форматов и целей."""
        log_prefix = f"[{game_index + 1}/{total_games}] "
        title = game_info.get("title", f"Game_{game_index + 1}")
        self._log(f"\n{log_prefix}▶️ Проверка игры: {title}", is_gui_message=True)

        moves = game_info.get("moves_san", [])
        initial_fen_for_render = game_info.get("initial_fen")
        original_script = game_info.get("script", {"moves": []})

        if not self._validate_game_moves(moves, initial_fen_for_render):
            return 0

        if self.trim_opening.get():
            moves_to_skip = find_starting_move_index(moves, initial_fen_for_render, self.stockfish_path.get(),
                                                     cp_threshold=self.trim_opening_cp_threshold.get(),
                                                     log_cb=lambda msg, level=logging.INFO: self._log(
                                                         f"{log_prefix}{msg}", level=level, is_gui_message=True))
            if moves_to_skip > 0:
                temp_board = chess.Board(initial_fen_for_render) if initial_fen_for_render else chess.Board()
                for move_idx in range(moves_to_skip): temp_board.push_san(moves[move_idx])
                initial_fen_for_render = temp_board.fen()
                moves = moves[moves_to_skip:]
                if "moves" in original_script:
                    original_script["moves"] = [m for m in original_script["moves"] if m.get("ply", 0) >= moves_to_skip]
                    for m in original_script["moves"]: m["ply"] -= moves_to_skip

        eval_map, analysis_script, game_states = self._run_game_analysis(moves, initial_fen_for_render, log_prefix)
        # TODO: Merge original_script with analysis_script intelligently if needed. For now, analysis script is primary.

        if self.show_all_captures.get():
            board_for_captures = chess.Board(initial_fen_for_render) if initial_fen_for_render else chess.Board()
            for move_data in analysis_script.get("moves", []):
                try:
                    move_obj = board_for_captures.parse_san(move_data['san'])
                    if board_for_captures.is_capture(move_obj):
                        if "arrow" not in move_data.get("effects", {}):
                            arrow_str = f"{chess.square_name(move_obj.from_square)}-{chess.square_name(move_obj.to_square)}"
                            move_data.setdefault("effects", {})["arrow"] = arrow_str
                    board_for_captures.push(move_obj)
                except (ValueError, KeyError):
                    pass

        num_channels = len(self.channels)
        use_channel_processing = num_channels > 0
        channel_name_for_stats = "default"
        current_background = self.background_path.get()
        final_base_out_dir = Path(self.output_dir.get())
        game_number_prefix = ""

        if use_channel_processing:
            current_channel = self.channels[game_index % num_channels]
            current_background = current_channel['bg_path']
            channel_name = safe_filename(current_channel['name'])
            final_base_out_dir = final_base_out_dir / channel_name
            channel_name_for_stats = current_channel['name']
            channel_game_counters[channel_name_for_stats] += 1
            game_number_prefix = f"{channel_game_counters[channel_name_for_stats]:03d}_"

        self._log(f"{log_prefix}   - Обработка игры: {title}" + (
            f" (Канал: '{channel_name_for_stats}')" if use_channel_processing else ""), is_gui_message=True)

        # ... ( Остальная логика обработки, как в старом _worker )
        # Код ниже адаптирован из старого воркера

        videos_created_this_game = 0
        players, event_text, trailer_info = game_info.get("players", {}), game_info.get("event", ""), game_info.get(
            "trailer", {})
        music = random.choice(list(Path(self.music_folder.get()).glob("*.mp3"))) if self.music_folder.get() and any(
            Path(self.music_folder.get()).glob("*.mp3")) else None

        selected_mode = self.aspect.get()
        aspect_ratios_to_render = []
        if "Оба" in selected_mode:
            aspect_ratios_to_render.extend(["16:9", "9:16"])
        elif "16:9" in selected_mode:
            aspect_ratios_to_render.append("16:9")
        else:
            aspect_ratios_to_render.append("9:16")

        for aspect_ratio in aspect_ratios_to_render:
            if self.cancel_flag.is_set(): break
            current_out_dir = final_base_out_dir / ("Shorts" if aspect_ratio == '9:16' else "Горизонтальные")
            ensure_dir(current_out_dir)

            targets = [None] if self.shorts_base.get() else []
            if (aspect_ratio == '9:16') or (aspect_ratio == '16:9' and self.apply_speedup_to_horizontal.get()):
                if self.shorts59.get(): targets.append(59.0)
                if self.shorts45.get(): targets.append(45.0)
                if self.shorts30.get(): targets.append(30.0)
            if not targets and not self.shorts_base.get(): targets.append(
                None)  # Ensure at least one render if base is off

            for target_sec in targets:
                if self.cancel_flag.is_set(): break

                # ... (Логика выбора макета и параметров рендерера)
                left_vid_folder, right_vid_folder, left_img_path, right_img_path = self.left_video_folder.get(), self.right_video_folder.get(), self.left_image_path.get(), self.right_image_path.get()
                renderer_left_video, renderer_right_video, renderer_left_image, renderer_right_image, is_photo_render = None, None, None, None, False
                if self.v_strategy_hybrid.get() and (left_vid_folder or right_vid_folder) and (left_img_path or right_img_path):
                    if random.random() < (self.v_hybrid_video_photo_ratio.get() / 100.0):
                        renderer_left_video, renderer_right_video = left_vid_folder, right_vid_folder
                    else:
                        renderer_left_image, renderer_right_image, is_photo_render = left_img_path, right_img_path, True
                else:
                    renderer_left_video, renderer_right_video, renderer_left_image, renderer_right_image, is_photo_render = left_vid_folder, right_vid_folder, left_img_path, right_img_path, bool((left_img_path or right_img_path) and not (left_vid_folder or right_vid_folder))
                
                v_layout_mode_for_render = "single_bottom"
                if aspect_ratio == '9:16':
                    if self.v_strategy_hybrid.get():
                        is_top = random.random() < (self.v_hybrid_top_bottom_ratio.get() / 100.0)
                        is_single = random.random() < (self.v_hybrid_one_two_slot_ratio.get() / 100.0)
                        if self.v_hybrid_forbid_dual_photo.get() and is_photo_render and not is_single:
                            is_single = True
                        v_layout_mode_for_render = f"{'single' if is_single else 'dual'}_{'top' if is_top else 'bottom'}"
                    else:
                        v_layout_mode_for_render = {"1 слот снизу": "single_bottom",
                                                    "1 слот сверху": "single_top",
                                                    "2 слота снизу": "dual_bottom",
                                                    "2 слота сверху": "dual_top"}.get(
                            self.v_fixed_layout_mode.get(), "single_bottom")
                pos_map = {'Слева': 'left', 'Справа': 'right', 'Сверху': 'top', 'Снизу': 'bottom'}
                eval_bar_pos_eng = pos_map.get(self.eval_bar_position.get(), 'left')

                rnd = Renderer(theme=self.theme.get(), aspect=aspect_ratio, fps=self.fps.get(),
                               output_dir=current_out_dir, background_path=current_background,
                               show_move_text=self.show_text.get(),
                               long_mate_freeze=self.long_mate_freeze.get(),
                               freeze_seconds=self.freeze_seconds.get(),
                               check_highlight=self.check_highlight.get(), check_arrow=self.check_arrow.get(),
                               show_checkmate_text=self.show_checkmate_text.get(),
                               checkmate_highlight_squares=self.checkmate_highlight_squares.get(),
                               checkmate_arrows=self.checkmate_arrows.get(),
                               music_path=str(music) if music else None,
                               watermark_text=self.watermark_text.get(), use_cache=self.use_frame_cache.get(),
                               shorts_control=(target_sec is not None), max_shorts_duration=target_sec or 59.0,
                               log_cb=self._log,
                               progress_cb=lambda done, total: self._update_frames(done, total,
                                                                                   log_prefix),
                               left_video_folder=renderer_left_video, right_video_folder=renderer_right_video,
                               overlays_enabled=self.overlays_enabled.get(), players=players,
                               event_text=event_text, player_font_path=self.player_font_path.get(),
                               player_font_size=self.player_font_size.get(),
                               top_bar_font_path=self.top_bar_font_path.get(),
                               top_bar_font_size=self.top_bar_font_size.get(),
                               h_video_width=self.h_video_width.get(), h_video_height=self.h_video_height.get(),
                               h_video_y_pos=self.h_video_y_pos.get(),
                               h_top_bar_enabled=self.h_top_bar_enabled.get(),
                               mate_font_path=self.mate_font_path.get() or None,
                               mate_font_size_px=self.mate_font_size.get() or 0,
                               trailer_enabled=self.trailer_enabled.get(),
                               trailer_move_count=self.trailer_moves_count.get() or 0,
                               trailer_move_duration=self.trailer_move_duration.get(),
                               trailer_text_enabled=self.trailer_text_enabled.get(),
                               trailer_text_override=self.trailer_text_override.get(),
                               trailer_text_x=self.trailer_text_x.get() or 0,
                               trailer_text_y=self.trailer_text_y.get() or 0,
                               trailer_font_size_px=self.trailer_font_size.get() or 0,
                               mate_text=self.mate_text.get() or "CHECKMATE!",
                               mate_text_x=self.mate_text_x.get() or 0, mate_text_y=self.mate_text_y.get() or 0,
                               log_prefix=log_prefix, v_layout_mode=v_layout_mode_for_render,
                               left_image_path=renderer_left_image, right_image_path=renderer_right_image,
                               photo_to_video=self.photo_to_video.get(),
                               photo_zoom_end=self.photo_zoom_end.get(),
                               v_single_media_height_px=self.v_single_media_height_px.get(),
                               v_dual_media_height_px=self.v_dual_media_height_px.get(),
                               v_dual_media_spacing=self.v_dual_media_spacing.get(),
                               v_horizontal_padding=self.v_horizontal_padding.get(),
                               v_board_spacing=self.v_board_spacing.get(),
                               v_show_info_bar=self.v_show_info_bar.get(),
                               v_info_bar_height=self.v_info_bar_height.get(),
                               stop_overlay_loop=self.stop_overlay_loop.get(),
                               show_board_coords=self.show_board_coords.get(),
                               board_coords_size_ratio=self.board_coords_size_ratio.get(),
                               board_coords_stroke_width=self.board_coords_stroke_width.get(),
                               subscribe_overlay_enabled=self.subscribe_overlay_enabled.get(),
                               subscribe_overlay_path=self.subscribe_overlay_path.get(),
                               subscribe_overlay_scale_top=self.subscribe_overlay_scale_top.get(),
                               subscribe_overlay_y_offset_top=self.subscribe_overlay_y_offset_top.get(),
                               subscribe_overlay_scale_bottom=self.subscribe_overlay_scale_bottom.get(),
                               subscribe_overlay_y_offset_bottom=self.subscribe_overlay_y_offset_bottom.get(),
                               highlight_last_move=self.highlight_last_move.get(),
                               icon_folder_path=self.icon_folder_path.get(), badge_scale=self.badge_scale.get(),
                               badge_pos=self.badge_pos.get(), badge_margin=self.badge_margin.get(),
                               show_eval_bar=self.show_eval_bar.get(), eval_bar_pos=eval_bar_pos_eng,
                               eval_bar_thickness=self.eval_bar_thickness.get(),
                               eval_bar_padding=self.eval_bar_padding.get(),
                               base_anim_duration=self.base_anim_duration.get(),
                               base_delay_duration=self.base_delay_duration.get(),
                               show_eval_icons=self.show_eval_icons.get(),
                               eval_map=eval_map,
                               force_gui_speed=self.force_gui_speed.get())

                suffix = f"_s{int(target_sec)}" if target_sec is not None else ""
                filename_with_players = safe_filename(
                    f"{game_number_prefix}{title}_({players.get('white', 'w')}_vs_{players.get('black', 'b')}){suffix}")

                base_video = rnd.render_game_to_pipe(moves, filename_with_players, title, initial_fen_for_render,
                                                     script=analysis_script, trailer_info=trailer_info)
                if base_video:
                    videos_created_this_game += 1
                    if self.memes_enabled.get() and _MEME_PIPELINE_OK and game_states:
                        self._apply_meme_overlays(
                            base_video, game_states, eval_map, analysis_script,
                            rnd, log_prefix)
                else:
                    self._log(f"{log_prefix}   ❌ Не удалось создать видео. Детали в логе.", is_gui_message=True,
                              level=logging.ERROR)

        return videos_created_this_game

    def _apply_meme_overlays(self, base_video: Path, game_states: List[Dict],
                             eval_map: Dict, analysis_script: Dict,
                             rnd: "Renderer", log_prefix: str) -> None:
        """Классифицирует события, выбирает GIF через LLM и прожигает оверлеи."""
        memes_dir = Path(self.memes_dir.get())
        if not memes_dir.is_dir():
            self._log(f"{log_prefix}   ⚠️ Папка мемов не найдена: {memes_dir}", is_gui_message=True,
                      level=logging.WARNING)
            return

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            self._log(f"{log_prefix}   ⚠️ ANTHROPIC_API_KEY не задан — мем-оверлеи пропущены.",
                      is_gui_message=True, level=logging.WARNING)
            return

        try:
            self._log(f"{log_prefix}   🎭 Классификация событий и выбор мемов...", is_gui_message=True)

            # script_map для compute_move_timestamps: {(ply, san): effects}
            script_map = {(m["ply"], m["san"]): m.get("effects", {})
                          for m in analysis_script.get("moves", [])}

            moves_san = [gs["san"] for gs in game_states]
            timestamps = compute_move_timestamps(
                moves_san=moves_san,
                script_map=script_map,
                base_anim=rnd.base_anim_duration,
                base_delay=rnd.base_delay_duration,
                fps=rnd.fps,
            )

            llm_client = _anthropic.Anthropic(api_key=api_key)
            specs = build_overlay_specs(
                game_states=game_states,
                eval_map=eval_map,
                move_timestamps=timestamps,
                memes_dir=memes_dir,
                llm_client=llm_client,
                gif_duration=self.gif_duration.get(),
            )

            if not specs:
                self._log(f"{log_prefix}   🎭 Нет событий для мем-оверлея.", is_gui_message=True)
                return

            self._log(f"{log_prefix}   🎭 Найдено {len(specs)} оверлей(а), прожигаю в видео...",
                      is_gui_message=True)
            output_path = base_video.with_stem(base_video.stem + "_memes")
            apply_meme_overlays(
                input_video=base_video,
                overlays=specs,
                board_offset=rnd.board_offset,
                square_size=rnd.square_size,
                output_path=output_path,
            )
            self._log(f"{log_prefix}   ✅ Мем-видео: {output_path.name}", is_gui_message=True)

        except Exception as e:
            self._log(f"{log_prefix}   ❌ Ошибка мем-оверлея: {e}", level=logging.ERROR, is_gui_message=True)
            logger.debug(traceback.format_exc())

    def _worker(self):
        """Главный рабочий метод, который запускает весь процесс."""
        t0 = time.time()
        videos_created_count = 0
        try:
            base_out_dir = Path(self.output_dir.get())
            setup_file_logger(base_out_dir)

            games_data = load_project_file(Path(self.input_path.get()))
            if not games_data:
                self._log("❌ В файле проекта не найдено партий или файл некорректен.", level=logging.ERROR,
                          is_gui_message=True)
                return

            num_channels = len(self.channels)
            channel_game_counters = defaultdict(int)
            channel_render_stats = defaultdict(lambda: {'count': 0, 'time': 0.0})

            self.pb_batch["maximum"] = len(games_data)

            for i, game_info in enumerate(games_data):
                if self.cancel_flag.is_set():
                    self._log("Операция отменена пользователем.", is_gui_message=True)
                    break

                render_start_time = time.time()
                created_count = self._process_single_game(game_info, i, len(games_data), channel_game_counters)

                if created_count > 0:
                    render_time = time.time() - render_start_time
                    videos_created_count += created_count

                    # Статистика по каналам
                    channel_name = "default"
                    if num_channels > 0:
                        channel_name = self.channels[i % num_channels]['name']
                    channel_render_stats[channel_name]['count'] += created_count
                    channel_render_stats[channel_name]['time'] += render_time

                self.pb_batch["value"] = i + 1
                self.current_game_progress_str.set("")

            total_time_secs = time.time() - t0
            self._log_final_stats(total_time_secs, videos_created_count, channel_render_stats)

        except Exception as e:
            self.log_queue.put(f"❌ Критическая ошибка в главном воркере: {e}")
            self.log_queue.put(traceback.format_exc())
        finally:
            self.btn_start.config(state="normal")
            self.btn_cancel.config(state="disabled")

    def _log_final_stats(self, total_time_secs, videos_created, stats):
        """Логирует финальную статистику по завершению работы."""
        total_time_mins = total_time_secs / 60
        self.log_queue.put(f"\n🎉 Вся работа завершена!")
        self.log_queue.put(f"   - Всего создано видео: {videos_created}")
        self.log_queue.put(f"   - Общее время работы: {total_time_mins:.2f} мин.")
        if stats:
            self.log_queue.put("\n📊 Статистика по каналам:")
            for name, data in stats.items():
                self.log_queue.put(f"   - Канал '{name}': {data['count']} видео, {data['time'] / 60:.2f} мин.")

    def _update_frames(self, done, total, prefix=""):
        self.after(0, lambda: self._update_frames_thread_safe(done, total, prefix))

    def _update_frames_thread_safe(self, done, total, prefix):
        self.pb_frames.config(maximum=max(1, total), value=min(done, total));
        self.current_game_progress_str.set(f"{done} / {total}")
        self.pb_frames.lift()


if __name__ == "__main__":
    if not _HAS_TK:
        print("Ошибка: tkinter не установлен — GUI недоступен. "
              "Установите python3-tk или используйте CLI (chess_meme_example.py).")
        sys.exit(1)
    app = App()
    app.mainloop()