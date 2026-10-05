"""実行状態（最終実行・ログイン失敗の通知済みフラグ）を data/state.json に保存する。"""
import json
from datetime import datetime

from src.config import DB_PATH

STATE_FILE = DB_PATH.parent / "state.json"


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def update_state(**changes) -> dict:
    state = load_state()
    state.update(changes)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
