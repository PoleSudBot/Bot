from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from zhenxun.services.sekai_resource.b30_constants import (
    B30ConstantsProvider,
    ChartConstant,
    ConstantsTable,
    b30_constants_provider,
    normalize_difficulty,
    parse_constants_csv,
)

B30_LIMIT = 30
DIFFICULTY_ORDER = {
    "easy": 1,
    "normal": 2,
    "hard": 3,
    "expert": 4,
    "master": 5,
    "append": 6,
}
PLAY_RESULT_ORDER = {"": 0, "C": 1, "FC": 2, "AP": 3}


@dataclass(frozen=True)
class UserMusicResult:
    music_id: int
    difficulty: str
    play_result: str = ""
    full_combo: bool = False
    full_perfect: bool = False


@dataclass(frozen=True)
class MusicMeta:
    music_id: int
    title: str = ""
    assetbundle_name: str = ""
    jacket_uri: str = ""
    published_at: int = 0


@dataclass
class B30Entry:
    rank: int
    music_id: int
    title: str
    difficulty: str
    difficulty_label: str
    level: int
    constant: float
    user_rating: float
    play_result: str
    note_count: int
    level_label: str = ""
    assetbundle_name: str = ""
    jacket_uri: str = ""
    published_at: int = 0


@dataclass
class B30Result:
    entries: list[B30Entry]
    average: float
    candidate_count: int
    ap_count: int
    fc_count: int
    missing_constants_count: int
    total_result_count: int


def difficulty_label(value: str) -> str:
    return {
        "easy": "EASY",
        "normal": "NORMAL",
        "hard": "HARD",
        "expert": "EXPERT",
        "master": "MASTER",
        "append": "APPEND",
    }.get(normalize_difficulty(value), str(value or "").upper())


def normalize_play_result(result: UserMusicResult) -> str:
    if result.full_perfect:
        return "AP"
    play_result = str(result.play_result or "").strip().lower()
    if play_result in {
        "all_perfect",
        "full_perfect",
        "ap",
        "allperfect",
        "fullperfect",
    }:
        return "AP"
    if result.full_combo:
        return "FC"
    if play_result in {"full_combo", "fc", "fullcombo"}:
        return "FC"
    if play_result in {"clear", "c", "live_clear", "success"}:
        return "C"
    return ""


def user_rating(constant: float, play_result: str) -> float:
    if play_result == "AP":
        return constant
    if play_result == "FC":
        return constant - 1 if constant >= 33 else constant - 1.5
    return 0


def _best_user_music_results(
    results: list[UserMusicResult],
) -> dict[str, UserMusicResult]:
    # 同一首同一难度可能有多条 playType 记录，先保留最佳完成状态再参与 B30。
    best: dict[str, UserMusicResult] = {}
    for result in results:
        difficulty = normalize_difficulty(result.difficulty)
        if result.music_id <= 0 or not difficulty:
            continue
        key = f"{result.music_id}:{difficulty}"
        current_priority = PLAY_RESULT_ORDER[normalize_play_result(result)]
        previous = best.get(key)
        previous_priority = (
            PLAY_RESULT_ORDER[normalize_play_result(previous)] if previous else -1
        )
        if previous is None or current_priority > previous_priority:
            best[key] = UserMusicResult(
                music_id=result.music_id,
                difficulty=difficulty,
                play_result=result.play_result,
                full_combo=result.full_combo,
                full_perfect=result.full_perfect,
            )
    return best


def calculate_best30(
    results: list[UserMusicResult],
    table: ConstantsTable,
    resolve_meta: Callable[[int, str, ChartConstant], MusicMeta] | None = None,
    resolve_missing_constant: Callable[
        [int, str], ChartConstant | None
    ] | None = None,
) -> B30Result:
    best = _best_user_music_results(results)
    entries: list[B30Entry] = []
    missing_constants = 0
    ap_count = 0
    fc_count = 0

    for result in best.values():
        play_result = normalize_play_result(result)
        if play_result not in {"AP", "FC"}:
            continue
        constant = table.get(result.music_id, result.difficulty)
        if constant is None and resolve_missing_constant:
            constant = resolve_missing_constant(result.music_id, result.difficulty)
        if constant is None or constant.constant <= 0:
            missing_constants += 1
            continue
        if play_result == "AP":
            ap_count += 1
        else:
            fc_count += 1
        meta = (
            resolve_meta(result.music_id, result.difficulty, constant)
            if resolve_meta
            else MusicMeta(result.music_id)
        )
        entries.append(
            B30Entry(
                rank=0,
                music_id=result.music_id,
                title=meta.title or f"歌曲 #{result.music_id}",
                difficulty=result.difficulty,
                difficulty_label=difficulty_label(result.difficulty),
                level=constant.level,
                constant=constant.constant,
                user_rating=user_rating(constant.constant, play_result),
                play_result=play_result,
                note_count=constant.note_count,
                level_label=constant.level_label,
                assetbundle_name=meta.assetbundle_name,
                jacket_uri=meta.jacket_uri,
                published_at=meta.published_at,
            )
        )

    # 排序规则保持和 Moebot-NEXT 一致，避免同分情况下展示顺序漂移。
    entries.sort(
        key=lambda item: (
            -item.user_rating,
            -item.constant,
            -PLAY_RESULT_ORDER.get(item.play_result, 0),
            item.music_id,
            DIFFICULTY_ORDER.get(item.difficulty, 99),
        )
    )
    entries = entries[:B30_LIMIT]
    for index, entry in enumerate(entries, start=1):
        entry.rank = index
    average = (
        sum(entry.user_rating for entry in entries) / len(entries)
        if entries
        else 0
    )
    return B30Result(
        entries=entries,
        average=average,
        candidate_count=ap_count + fc_count,
        ap_count=ap_count,
        fc_count=fc_count,
        missing_constants_count=missing_constants,
        total_result_count=len(best),
    )


def parse_user_music_results(payload: Any) -> list[UserMusicResult]:
    results: list[UserMusicResult] = []
    if not isinstance(payload, list):
        return results
    for item in payload:
        if not isinstance(item, dict):
            continue
        music_id = _as_int(item.get("musicId"))
        difficulty = str(
            item.get("musicDifficultyType") or item.get("musicDifficulty") or ""
        )
        results.append(
            UserMusicResult(
                music_id=music_id,
                difficulty=difficulty,
                play_result=str(item.get("playResult") or ""),
                full_combo=bool(item.get("fullComboFlg")),
                full_perfect=bool(item.get("fullPerfectFlg")),
            )
        )
    return results


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "B30ConstantsProvider",
    "B30Entry",
    "B30Result",
    "ChartConstant",
    "ConstantsTable",
    "MusicMeta",
    "UserMusicResult",
    "b30_constants_provider",
    "calculate_best30",
    "difficulty_label",
    "normalize_difficulty",
    "normalize_play_result",
    "parse_constants_csv",
    "parse_user_music_results",
    "user_rating",
]
