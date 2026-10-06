"""保存した資料を、Google Drive for Desktop の同期フォルダなど、任意のフォルダにも複製する。

output/ を基準とし、同じフォルダ構成のまま、無い・サイズや更新日時が違うファイルだけをコピーする。
同期は Drive for Desktop が行うので、Google の認証設定は要らない。
"""
import logging
import shutil
from pathlib import Path

from src.config import OUTPUT_DIR, load_config

logger = logging.getLogger(__name__)


def _same(src: Path, dest: Path) -> bool:
    a, b = src.stat(), dest.stat()
    return a.st_size == b.st_size and int(a.st_mtime) == int(b.st_mtime)


def mirror_to_drive(summary: dict) -> None:
    """output/ 配下のファイルを drive_dir に複製し、結果を summary["drive"] に入れる。失敗しても巡回は止めない。"""
    cfg = load_config()
    if not cfg["drive_enabled"]:
        return
    result = {"dir": cfg["drive_dir"], "copied": 0}
    summary["drive"] = result
    try:
        dest_root = _destination(cfg["drive_dir"])
        for src in sorted(OUTPUT_DIR.rglob("*")):
            if not src.is_file() or src.suffix == ".part":
                continue
            dest = dest_root / src.relative_to(OUTPUT_DIR)
            if dest.exists() and _same(src, dest):
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".part")
            shutil.copy2(src, tmp)
            tmp.replace(dest)
            result["copied"] += 1
        logger.info("Google Drive へコピー: %d 件 (%s)", result["copied"], dest_root)
    except Exception as e:  # noqa: BLE001 - Drive 側の不具合で巡回を止めない
        logger.error("Google Drive へのコピーに失敗しました: %s", e)
        summary["errors"].append(f"Google Drive へのコピー: {(str(e).splitlines() or [type(e).__name__])[0][:200]}")


def _destination(drive_dir: str) -> Path:
    if not drive_dir:
        raise RuntimeError("コピー先が未設定です（--set-drive-dir フォルダ）")
    path = Path(drive_dir)
    if not Path(path.anchor).exists():
        raise RuntimeError(f"ドライブに接続できません: {path.anchor}（Google Drive for Desktop が起動しているか確認してください）")
    path.mkdir(parents=True, exist_ok=True)
    return path


def validate_drive_dir(value: str) -> str:
    """--set-drive-dir の検証。絶対パスで、ドライブ（またはネットワーク）が存在し、output/ の中ではないこと。"""
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"コピー先は絶対パスで指定してください: {value!r}（例: H:\\マイドライブ\\YCU-Board）")
    if not Path(path.anchor).exists():
        raise ValueError(f"ドライブが見つかりません: {path.anchor}")
    resolved = path.resolve()
    if resolved == OUTPUT_DIR.resolve() or OUTPUT_DIR.resolve() in resolved.parents:
        raise ValueError("コピー先を保存先（output/）の中にはできません。")
    return str(path)
