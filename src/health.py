"""定期監査の健康診断（--health）。止まっているかどうかを判定し、止まっていれば原因と直すコマンドを示す。

終了コード: 0 = 正常（または意図して OFF）, 1 = 止まっている／問題あり
"""
from datetime import datetime, timedelta

from src.config import config_error, load_config
from src.schedule import last_due_slot, missed_slot, next_slot
from src.state import load_state
from src.winutil import find_watchers, task_registered

RESULT_LABEL = {"ok": "成功", "error": "一部エラー", "login_failed": "ログイン失敗"}
RUNNING_GRACE = timedelta(minutes=15)  # 予定時刻から、この時間内は「実行中かもしれない」として問題にしない


def run_health() -> int:
    cfg = load_config()
    state = load_state()
    times = cfg["daily_run_times"]
    now = datetime.now()
    watchers = find_watchers()
    registered = task_registered()
    last_run = state.get("last_run")
    result = state.get("last_result")
    due = last_due_slot(times, now)
    nxt = next_slot(times, now)
    missed = missed_slot(last_run, times, now)
    skipped = state.get("skipped_slot")
    slept = bool(missed and skipped and datetime.fromisoformat(skipped) >= missed)  # スリープ等で見送った（仕様）
    broken_config = config_error()

    print("\n=== 定期監査の健康診断 ===")
    print(f"  定期チェック      : {'ON' if cfg['check_enabled'] else 'OFF'}")
    print(f"  自動ダウンロード  : {'ON' if cfg['download_enabled'] else 'OFF'}")
    mail = "OFF" if not cfg["email_enabled"] else ("ON（更新があったときだけ）" if cfg["email_only_on_change"] else "ON（毎回）")
    print(f"  結果メール        : {mail}")
    print(f"  自動起動タスク    : {'登録済み' if registered else '未登録'}")
    if watchers:
        print(f"  常駐プロセス      : 動作中 (PID {', '.join(map(str, watchers))})")
    else:
        print("  常駐プロセス      : 動いていません")
    print(f"  巡回時刻          : {', '.join(times)}")
    jitter_note = f"（設定時刻から ±{cfg['jitter_max_minutes']}分の誤差あり）" if cfg["jitter_enabled"] else ""
    print(f"  次の予定          : {nxt:%m/%d %H:%M}{jitter_note}" if nxt else "  次の予定          : なし")
    if last_run:
        print(f"  最終実行          : {last_run.replace('T', ' ')}（{RESULT_LABEL.get(result, '不明')}）")
        if result == "error" and state.get("last_error"):
            print(f"  前回のエラー      : {state['last_error']}")
    else:
        print("  最終実行          : まだ実行されていません")
    if not state.get("initial_sync_at"):
        print("  初回監査          : 未実施")

    if broken_config:
        print(f"\n判定: config.json が壊れているため、既定値（定期チェック OFF）で動いています。\n  {broken_config}")
        print("  → python -m src.main --check-on などで設定し直すと、作り直されます。\n")
        return 1
    if not cfg["check_enabled"]:
        print("\n判定: 止まっているのではなく、定期チェックを OFF にしています（意図した設定なら問題ありません）。")
        print("使うなら /ycu-config か --check-on で ON にします。\n")
        return 0

    problems = []
    if not registered:
        problems.append(("自動起動が未登録です（PC を再起動すると常駐が戻りません）", "python -m src.main --install-task"))
    if not watchers:
        problems.append(("常駐プロセスが止まっています（PC のシャットダウン・再起動・終了など）", "python -m src.main --start"))
    elif missed and due and now - due > RUNNING_GRACE and not slept:
        problems.append((f"常駐は動いていますが、{missed:%m/%d %H:%M} の予定が実行されていません（固まっている可能性）",
                         "python -m src.main --stop のあと --start"))
    if result == "login_failed":
        problems.append(("YCU-Board にログインできません", "python -m src.main --login（承認は本人が行う）"))
    if missed and not watchers:
        problems.append((f"{missed:%m/%d %H:%M} の予定を取りこぼしています", "python -m src.main --once（再開後に1回実行して補う）"))

    if not problems:
        if slept:
            note = f"（{missed:%m/%d %H:%M} の予定は PC のスリープ等で大きく遅れたため見送りました。次の予定から通常どおり動きます）"
        else:
            note = "（予定時刻の直後なので、実行中の可能性があります）" if missed else ""
        print(f"\n判定: 止まっていません。正常に動いています。{note}\n")
        return 0
    print("\n判定: 止まっている、または問題があります。")
    for what, fix in problems:
        print(f"  ・{what}\n      → {fix}")
    print()
    return 1
