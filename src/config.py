"""設定の読み書き。値は config.json に保存し、毎回読み直すので実行中の変更も反映される。"""
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config.json"

# 初期値（config.json に無い項目はここが使われる）
DEFAULT_CONFIG: dict = {
    # 機能スイッチ（初期OFF）。ダウンロードは巡回(check)の結果を見て動くので、check が OFF なら定期実行されない。
    "check_enabled": False,  # 2. 定期チェック
    "download_enabled": False,  # 3. 更新があった資料の自動ダウンロード
    "teams_enabled": False,  # 5. Teams（SharePoint）の講義資料も確認・ダウンロードする（ダウンロードは3.に従う）
    "email_enabled": True,  # 4. 結果メール
    "email_only_on_change": False,  # True なら、更新・保存・エラーがあったときだけ送る
    # 1日の巡回時刻（HH:MM）。回数も時刻も自由に増減できる。
    "daily_run_times": ["11:00", "17:00"],
    # 実行時刻の誤差。全員が同じ時刻にアクセスして大学のサーバーに負荷が集中するのを避ける（毎日同じ時刻にもならない）。
    # 予定時刻ごとに ±jitter_max_minutes 分の範囲で、標準偏差 jitter_sigma_minutes 分の正規分布からずらす。
    "jitter_enabled": True,
    "jitter_max_minutes": 10,
    "jitter_sigma_minutes": 4,
    # ダウンロード対象の講義名（部分一致）。空なら全講義。
    "target_courses": [],
    "ycuboard_url": "https://ycuboard.yokohama-cu.ac.jp/",
    "portal_home_url": "https://ycuboard.yokohama-cu.ac.jp/portal/home",
    "id_password_path": "id_password.txt",
    "auth_profile_dir": "data/auth_profile",
    "db_path": "data/history.db",
    "output_dir": "output",  # 資料の保存先
    "enable_notification": True,  # Windowsトースト通知
    "request_delay_seconds": 1.5,
    "maintenance_window": {"enabled": True, "weekday": 1, "start_hour": 1, "end_hour": 6},
    "email": {
        "to": "",  # 空なら id_password.txt のメールアドレス
        "method": "auto",  # auto / outlook / smtp
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "smtp_user": "",  # 空なら id_password.txt のメールアドレス
        "smtp_password_file": "smtp_password.txt",  # 無ければ id_password.txt のパスワード
    },
}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_FILE.exists():
        try:
            user = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            user = {}
        if not isinstance(user, dict):
            user = {}
        # 旧キー automation_enabled からの移行
        if "automation_enabled" in user:
            user.setdefault("check_enabled", user["automation_enabled"])
            user.setdefault("download_enabled", user["automation_enabled"])
        for key, value in user.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
        cfg.pop("automation_enabled", None)
    return cfg


def config_error() -> Optional[str]:
    """config.json が読めない（壊れている）なら理由を返す。無い・正常なら None。

    load_config は読めないと黙って既定値（定期チェック OFF）で動くため、--health で知らせるのに使う。
    """
    if not CONFIG_FILE.exists():
        return None
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return f"config.json を読めません: {str(e)[:120]}"
    return None if isinstance(data, dict) else "config.json の形式が正しくありません"


def save_config(cfg: dict) -> None:
    write_atomic(CONFIG_FILE, json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")


def write_atomic(path: Path, text: str) -> None:
    """書き込み途中のファイルを常駐が読んで「壊れた設定」と見なさないよう、一時ファイルに書いてから置き換える。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def update_config(**changes) -> dict:
    cfg = load_config()
    cfg.update(changes)
    save_config(cfg)
    return cfg


CONFIG = load_config()
AUTH_PROFILE_DIR = BASE_DIR / CONFIG["auth_profile_dir"]
DB_PATH = BASE_DIR / CONFIG["db_path"]
OUTPUT_DIR = BASE_DIR / CONFIG["output_dir"]
ID_PASSWORD_PATH = BASE_DIR / CONFIG["id_password_path"]

for _d in (AUTH_PROFILE_DIR, DB_PATH.parent, OUTPUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ---------- 巡回時刻 ----------
def normalize_time(value: str) -> str:
    """'9:5' '09:05' '0905' などを 'HH:MM' にそろえる。不正なら ValueError。"""
    m = re.fullmatch(r"(\d{1,2})[:：]?(\d{2})", value.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"時刻は HH:MM 形式で指定してください: {value!r}")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def set_daily_run_times(times: List[str]) -> List[str]:
    normalized = sorted({normalize_time(t) for t in times})
    if not normalized:
        raise ValueError("巡回時刻を1つ以上指定してください。")
    update_config(daily_run_times=normalized)
    return normalized


# ---------- 認証情報 ----------
EMAIL_DOMAIN = "yokohama-cu.ac.jp"
_LABEL = r"(?:id|email|e-mail|mail|user|username|password|passwd|pass|pw)"


def get_credentials() -> Tuple[str, str]:
    """
    id_password.txt から (メールアドレス, パスワード) を取り出す。
    形式: `id` / ID / `password` / パスワード の4行（見出し行は省略可）。
    ID は `d123456a` のように @ 以降を省いても、`d123456a@yokohama-cu.ac.jp` でもよい。
    `id: xxx` のような接頭辞も許容する。
    """
    if not ID_PASSWORD_PATH.exists():
        raise FileNotFoundError(
            f"認証情報ファイルが見つかりません: {ID_PASSWORD_PATH}\n"
            "`python -m src.main --init` で作成するか、README の手順に従って作成してください。"
        )

    values = []
    for line in ID_PASSWORD_PATH.read_text(encoding="utf-8-sig").splitlines():
        line = re.sub(rf"^{_LABEL}\s*[:：]\s*", "", line.strip(), flags=re.IGNORECASE)
        if line and not re.fullmatch(_LABEL, line, flags=re.IGNORECASE):
            values.append(line)
    if len(values) < 2:
        raise ValueError(f"{ID_PASSWORD_PATH.name} には ID とパスワードを1行ずつ書いてください。")

    user_id, password = values[0], values[1]
    email = user_id if "@" in user_id else f"{user_id}@{EMAIL_DOMAIN}"
    return email, password


def write_credentials(user_id: str, password: str) -> None:
    ID_PASSWORD_PATH.write_text(f"id\n{user_id.strip()}\npassword\n{password}\n", encoding="utf-8")


# ---------- メンテナンス ----------
def is_in_maintenance() -> bool:
    """YCU-Board の定期メンテナンス（既定: 毎週火曜 1:00〜6:00）か。"""
    mw = load_config()["maintenance_window"]
    if not mw.get("enabled", True):
        return False
    now = datetime.now()
    return now.weekday() == mw["weekday"] and mw["start_hour"] <= now.hour < mw["end_hour"]
