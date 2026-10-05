"""定期監査の健康診断（--health）。止まっている原因と、直すためのコマンドを示す。"""
from src.config import load_config
from src.schedule import last_due_slot, missed_slot
from src.state import load_state
from src.winutil import find_watchers, task_registered

RESULT_LABEL = {"ok": "成功", "error": "一部エラー", "login_failed": "ログイン失敗"}


def run_health() -> int:
    """定期監査が正常に動いているかを診断する。問題があれば原因と対処を表示し、1 を返す。"""
    cfg = load_config()
    state = load_state()
    times = cfg["daily_run_times"]
    watchers = find_watchers()
    registered = task_registered()
    last_run = state.get("last_run")
    result = state.get("last_result")
    due = last_due_slot(times)
    missed = missed_slot(last_run, times)

    print("\n=== 定期監査の健康診断 ===")
    print(f"  定期チェック      : {'ON' if cfg['check_enabled'] else 'OFF'}")
    print(f"  自動起動タスク    : {'登録済み' if registered else '未登録'}")
    if watchers:
        print(f"  常駐プロセス      : 動作中 (PID {', '.join(map(str, watchers))})")
    else:
        print("  常駐プロセス      : 動いていません")
    print(f"  巡回時刻          : {', '.join(times)}" + (f"（直近の予定: {due:%m/%d %H:%M}）" if due else ""))
    if last_run:
        print(f"  最終実行          : {last_run.replace('T', ' ')}（{RESULT_LABEL.get(result, '不明')}）")
    else:
        print("  最終実行          : まだ実行されていません")

    if not cfg["check_enabled"]:
        print("\n定期チェックが OFF です（意図した設定なら問題ありません）。使うなら /ycu-config か --check-on で ON にします。\n")
        return 0

    problems = []
    if not registered:
        problems.append(("自動起動が未登録です（PC を再起動すると常駐が戻りません）", "python -m src.main --install-task"))
    if not watchers:
        problems.append(("常駐プロセスが止まっています（PC のシャットダウン・再起動・終了など）", "python -m src.main --start"))
    if result == "login_failed":
        problems.append(("YCU-Board にログインできません", "python -m src.main --login（承認は本人が行う）"))
    if missed:
        problems.append((f"{missed:%m/%d %H:%M} の予定を取りこぼしています", "python -m src.main --once（再開後に1回実行して補う）"))

    if not problems:
        print("\n正常です。\n")
        return 0
    print("\n[問題]")
    for what, fix in problems:
        print(f"  ・{what}\n      → {fix}")
    print()
    return 1
