"""保存先パスの決定まわり（Windows のファイル名制約への対応を含む）。"""
import re
from datetime import datetime
from pathlib import Path

from src.config import OUTPUT_DIR

INVALID_CHARS_REGEX = re.compile(r'[\\/:*?"<>|\r\n\t]')
MAX_NAME_LENGTH = 100  # ファイル名
MAX_DIR_NAME_LENGTH = 50  # 講義名・資料タイトルのフォルダ名（パス全体が Windows の上限 260 文字を超えないように）
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def sanitize_filename(name: str, max_length: int = MAX_NAME_LENGTH) -> str:
    """Windows のファイル名・フォルダ名として安全な文字列にする。長い場合も拡張子は残す。"""
    cleaned = INVALID_CHARS_REGEX.sub("_", name or "").strip(" .")
    if not cleaned:
        return "untitled"
    if len(cleaned) > max_length:
        suffix = Path(cleaned).suffix
        suffix = suffix if 0 < len(suffix) <= 10 else ""
        cleaned = cleaned[: max_length - len(suffix)].rstrip(" .") + suffix
    if cleaned.split(".")[0].upper() in RESERVED_NAMES:  # CON.pdf なども Windows では作れない
        cleaned = "_" + cleaned
    return cleaned


def get_material_target_dir(course_name: str, section_name: str = "", base_dir: Path = None) -> Path:
    """保存先ディレクトリ: <保存先>/<講義名>/<資料タイトル>/"""
    parts = [sanitize_filename(course_name, MAX_DIR_NAME_LENGTH)]
    if section_name:
        parts.append(sanitize_filename(section_name, MAX_DIR_NAME_LENGTH))
    target_dir = (base_dir or OUTPUT_DIR).joinpath(*parts)
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir


def resolve_save_path(target_dir: Path, original_filename: str) -> Path:
    """同名ファイルがあれば連番を付けて衝突を避ける。"""
    first = target_dir / sanitize_filename(original_filename)
    dest, counter = first, 1
    while dest.exists():
        dest = target_dir / f"{first.stem}_{counter}{first.suffix}"
        counter += 1
    return dest


def archive_old_version(path: Path) -> Path:
    """更新前の旧版を `<名前>_旧版YYYYMMDD-HHMMSS.<拡張子>` に退避して返す。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archived = path.with_name(f"{path.stem}_旧版{stamp}{path.suffix}")
    counter = 1
    while archived.exists():  # 同じ秒に2回退避したとき
        archived = path.with_name(f"{path.stem}_旧版{stamp}_{counter}{path.suffix}")
        counter += 1
    path.rename(archived)
    return archived
