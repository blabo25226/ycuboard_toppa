"""環境の診断（--doctor）。ブラウザは起動せず、秘密の値も表示しない。"""
import platform
import subprocess
import sys
from typing import List, Tuple

from src.config import AUTH_PROFILE_DIR, CONFIG_FILE, ID_PASSWORD_PATH, get_credentials

OK, NG, INFO = "OK  ", "NG  ", "情報"


def _check_chromium() -> Tuple[bool, str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, "playwright が未インストール → pip install -r requirements.txt"
    try:
        with sync_playwright() as p:
            path = p.chromium.executable_path
        from pathlib import Path

        if Path(path).exists():
            return True, "playwright と Chromium が利用可能"
        return False, "Chromium が未インストール → playwright install chromium"
    except Exception as e:  # noqa: BLE001
        return False, f"playwright の確認に失敗: {str(e).splitlines()[0][:100]}"


def _check_credentials() -> Tuple[bool, str]:
    if not ID_PASSWORD_PATH.exists():
        return False, f"{ID_PASSWORD_PATH.name} がありません → python -m src.main --init"
    try:
        email, password = get_credentials()
    except ValueError as e:
        return False, str(e)
    user_id = email.split("@")[0]
    masked = user_id[:4] + "*" * max(len(user_id) - 4, 0)
    return True, f"ID: {masked}@{email.split('@', 1)[1]} / パスワード: 記入あり"


def check_task() -> Tuple[bool, str]:
    try:
        r = subprocess.run(["schtasks", "/query", "/tn", "YCUBoardWatcher"], capture_output=True)
        return r.returncode == 0, "登録済み" if r.returncode == 0 else "未登録（--install-task で登録）"
    except Exception:  # noqa: BLE001
        return False, "確認できませんでした"


def run_doctor() -> int:
    """診断結果を表示し、必須項目がすべて OK なら 0 を返す。"""
    required: List[Tuple[str, bool, str]] = []
    info: List[Tuple[str, bool, str]] = []

    required.append(("OS", platform.system() == "Windows", f"{platform.system()} {platform.release()}" + ("" if platform.system() == "Windows" else "（Windows 以外は未対応）")))
    py_ok = sys.version_info >= (3, 10)
    required.append(("Python", py_ok, f"{platform.python_version()}" + ("" if py_ok else "（3.10 以上が必要）")))
    required.append(("Playwright", *_check_chromium()))
    required.append(("認証情報", *_check_credentials()))

    logged_before = (AUTH_PROFILE_DIR / "Default").exists()
    info.append(("ログイン履歴", logged_before, "あり（2回目以降のログイン確認が可能）" if logged_before else "なし（初回ログインが必要: --login）"))
    info.append(("config.json", CONFIG_FILE.exists(), "あり" if CONFIG_FILE.exists() else "なし（既定値で動作。設定を変えると作成される）"))
    info.append(("自動起動タスク", *check_task()))

    print("\n=== 環境診断 ===")
    for name, ok, msg in required:
        print(f"  [{OK if ok else NG}] {name:<10} {msg}")
    for name, ok, msg in info:
        print(f"  [{INFO}] {name:<10} {msg}")
    all_ok = all(ok for _, ok, _ in required)
    print("\n" + ("必須項目はすべて OK です。" if all_ok else "NG の項目を解消してください。") + "\n")
    return 0 if all_ok else 1
