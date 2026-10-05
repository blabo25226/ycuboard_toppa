"""保存先パスの決定まわり（Windows のファイル名制約への対応を含む）。"""
import re
from datetime import datetime
from pathlib import Path

from src.config import OUTPUT_DIR

INVALID_CHARS_REGEX = re.compile(r'[\\/:*?"<>|\r\n\t]')
MAX_NAME_LENGTH = 100


def sanitize_filename(name: str) -> str:
    """Windows のファイル名・フォルダ名として安全な文字列にする。長い場合も拡張子は残す。"""
    cleaned = INVALID_CHARS_REGEX.sub("_", name or "").strip(" .")
    if not cleaned:
        return "untitled"
    if len(cleaned) > MAX_NAME_LENGTH:
        suffix = Path(cleaned).suffix
        suffix = suffix if 0 < len(suffix) <= 10 else ""
        cleaned = cleaned[: MAX_NAME_LENGTH - len(suffix)].rstrip(" .") + suffix
    return cleaned


def get_material_target_dir(course_name: str, section_name: str = "", base_dir: Path = None) -> Path:
    """保存先ディレクトリ: <保存先>/<講義名>/<資料タイトル>/"""
    parts = [sanitize_filename(course_name)]
    if section_name:
        parts.append(sanitize_filename(section_name))
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
    path.rename(archived)
    return archived
