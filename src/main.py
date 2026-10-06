"""YCU-Board 資料チェック＆自動ダウンロードの CLI。 `python -m src.main --help` 参照。"""
import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# Windows の cp932 コンソールでも文字化け・例外を出さない
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright

from src.auth import perform_full_login
from src.config import (
    AUTH_PROFILE_DIR,
    BASE_DIR,
    ID_PASSWORD_PATH,
    OUTPUT_DIR,
    load_config,
    normalize_time,
    set_daily_run_times,
    update_config,
    write_credentials,
)
from src.crawler import YCUBoardCrawler
from src.downloader import MAX_DIR_NAME_LENGTH, sanitize_filename
from src.pipeline import run_cycle
from src.schedule import in_maintenance, last_due_slot, missed_slot, planned_for
from src.state import load_state, update_state
from src.winutil import find_watchers, powershell, start_watcher, stop_watchers

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("YCUBoard")


def enable_file_logging() -> None:
    """常駐時のログを logs/ycuboard.log にも残す（ウィンドウ無しで動かすため）。"""
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    handler = logging.FileHandler(log_dir / "ycuboard.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)

TASK_NAME = "YCUBoardWatcher"
CATCH_UP_MINUTES = 120  # PCが寝ていて時刻を過ぎても、この時間内なら起動後に実行する


def open_context(playwright, headless: bool):
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(AUTH_PROFILE_DIR),
        headless=headless,
        accept_downloads=True,
        viewport={"width": 1280, "height": 800},
        args=["--disable-blink-features=AutomationControlled"],
    )


def run_once(*, headless: bool, download: bool, targets=None, send_mail: bool = True, scheduled: bool = False,
             initial: bool = False, dry_run: bool = False):
    with sync_playwright() as p:
        context = open_context(p, headless)
        try:
            return run_cycle(context, download=download, targets=targets, send_mail=send_mail, scheduled=scheduled,
                             initial=initial, dry_run=dry_run)
        finally:
            context.close()


def do_login() -> None:
    with sync_playwright() as p:
        context = open_context(p, headless=False)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            perform_full_login(page)
        finally:
            context.close()
    print("ログインに成功しました。以降は自動でログインされます。")


def do_initial_sync(*, headless: bool, targets, dry_run: bool, send_mail: bool) -> int:
    """
    初回監査: 定期監査を始める前に、すでに公開されている資料を取得し、全講義の現状を「基準」として記録する。
    保存対象は config の target_courses（--course で上書き可）。ダウンロードが OFF のときは記録だけ行う。
    --dry-run なら何も保存せず、保存される資料の一覧だけ表示する。
    """
    from src.state import now_iso, update_state

    cfg = load_config()
    download = cfg["download_enabled"] or bool(targets)
    summary = run_once(headless=headless, download=download, targets=targets, send_mail=send_mail and not dry_run,
                       initial=True, dry_run=dry_run)
    if summary is None:
        return 1

    scope = ", ".join(targets or cfg["target_courses"]) or "(全講義)"
    print(f"\n=== 初回監査{'（予行: 何も保存していません）' if dry_run else ''} ===")
    print(f"確認した講義: {len(summary['courses'])} 件 / 資料の合計: {sum(c['count'] for c in summary['courses'])} 件")
    if not download:
        print("自動ダウンロードが OFF のため、資料は保存せず、現状の記録だけ行いました。")
    else:
        print(f"保存の対象講義: {scope}")
        rows = summary["pending"] if dry_run else summary["downloads"]
        by_course = {}
        for r in rows:
            by_course.setdefault(r["course_name"], []).append(r)
        if not by_course:
            print("  保存する資料はありません（対象講義に公開済みの資料が無い、または保存済み）。")
        for name, items in by_course.items():
            print(f"  ■ {name}: {len(items)} 件" + ("" if dry_run else f" → {OUTPUT_DIR / sanitize_filename(name, MAX_DIR_NAME_LENGTH)}"))
            for r in items:
                print(f"      ・{r['material_title']} / {r['file_name']}")
        print(f"\n{'保存される' if dry_run else '保存した'}資料: {len(rows)} 件")
        skipped = summary.get("teams_skipped", [])
        if skipped:
            print(f"大きいため保存しない Teams のファイル: {len(skipped)} 件（必要なら Teams から直接開いてください）")
            for r in skipped:
                print(f"      ・{r['course_name']} / {r['rel_path']}（{r['size'] / 1e6:.0f} MB）")
    for err in summary["errors"]:
        print(f"  [エラー] {err}")

    if not dry_run:
        update_state(initial_sync_at=now_iso(), initial_sync_scope=scope, initial_sync_files=len(summary["downloads"]))
        print("\n初回監査が完了しました。これ以降の定期監査では、ここからの『差分』だけを検知します。")
    return 1 if summary["errors"] else 0


def _print_audit_result(summary: dict) -> None:
    """監査の結果を画面に出す（メールと同じ内容）。"""
    from src.mailer import build_report

    subject, body = build_report(summary)
    print("\n" + "=" * 60 + f"\n{subject}\n" + "=" * 60)
    print(body)
    mail = {
        "sent": "結果メールを送信しました。",
        "failed": "結果メールの送信に失敗しました（ログを確認してください）。",
        "skipped": "更新・エラーが無いため、メールは送りませんでした（『更新があったときだけ』の設定）。",
        "off": "メールは送っていません（メールが OFF、または --no-mail）。",
    }.get(summary.get("mail"), "")
    print(f"\n完了: 講義 {len(summary['courses'])} 件をチェック / ダウンロード {len(summary['downloads'])} 件。{mail}")


def do_check_login() -> int:
    """保存済みセッションだけで（ブラウザ非表示・人の操作なしで）ログインできるかを確認する。"""
    from src.auth import ensure_logged_in

    with sync_playwright() as p:
        context = open_context(p, headless=True)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            ok = ensure_logged_in(page, timeout_seconds=60)
        finally:
            context.close()
    if ok:
        print("ログイン確認: OK（ブラウザを起動し直しても自動でログインできました）")
        return 0
    print("ログイン確認: NG（承認が必要か、ID/パスワードが違う可能性があります → python -m src.main --login）")
    return 1


def do_check_teams(headless: bool) -> int:
    """保存済みセッションで Teams（SharePoint）に入れるか、履修講義のチームが見つかるかを確認する。"""
    from src.auth import ensure_logged_in
    from src.teams import find_team_sites, open_sharepoint

    with sync_playwright() as p:
        context = open_context(p, headless)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            if not ensure_logged_in(page, timeout_seconds=60):
                print("Teams 確認: NG（先に YCU-Board にログインできません → python -m src.main --login）")
                return 1
            courses = YCUBoardCrawler(page).get_enrolled_courses()
            if not open_sharepoint(page):
                print("Teams 確認: NG（SharePoint にログインできません。承認が必要かもしれません → python -m src.main --check-teams --headful）")
                return 1
            sites = find_team_sites(page, courses, load_state().get("teams_sites", {}))
        finally:
            context.close()
    if sites:
        print("Teams 確認: OK（Teams のサイトに入れました）")
    else:
        print("Teams 確認: NG（Teams のサイトには入れましたが、履修講義に対応するチームが1つも見つかりません）")
    for c in courses:
        info = sites.get(c["id"])
        print(f"  {c['name']}: " + (f"チーム「{info['team']}」" if info else "チームなし（Teams の対象外）"))
    if not sites:
        print("  チーム名に講義名がそのまま含まれていないと対応づけられません（部分一致・昨年度のチームは使いません）。")
    return 0 if sites else 1


def do_test_mail() -> int:
    """自分宛にテストメールを1通送る。"""
    from src.mailer import send_email

    with sync_playwright() as p:
        context = open_context(p, headless=True)
        try:
            ok = send_email(
                "[YCU-Board] テストメール",
                f"YCU-Board 資料チェッカーのテストメールです。({datetime.now():%Y-%m-%d %H:%M})\nこのメールが届いていれば、結果メールも届きます。",
                context,
            )
        finally:
            context.close()
    print("テストメール送信: " + ("OK（受信箱を確認してください）" if ok else "NG（logs やエラー表示を確認してください）"))
    return 0 if ok else 1


def do_list_courses(headless: bool) -> None:
    from src.auth import ensure_logged_in

    with sync_playwright() as p:
        context = open_context(p, headless)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            if not ensure_logged_in(page, timeout_seconds=60):
                print("ログインできませんでした。")
                return
            courses = YCUBoardCrawler(page).get_enrolled_courses()
        finally:
            context.close()
    print("\n=== 履修中の講義 ===")
    for c in courses:
        print(f"  {c['name']}  ({c['id']})")


def do_init() -> None:
    """ID とパスワードを対話入力して id_password.txt を作る。"""
    import getpass

    if ID_PASSWORD_PATH.exists() and input(f"{ID_PASSWORD_PATH.name} は既にあります。上書きしますか？ [y/N] ").lower() != "y":
        return
    user_id = input("YCU の ID（例: d123456a ／ @yokohama-cu.ac.jp は不要）: ").strip()
    password = getpass.getpass("パスワード（入力は表示されません）: ")
    if not user_id or not password:
        print("ID とパスワードの両方が必要です。")
        return
    write_credentials(user_id, password)
    print(f"{ID_PASSWORD_PATH.name} を作成しました。次は: python -m src.main --login")


_powershell = powershell


def do_start() -> int:
    """停止している常駐（定期監査）を起動する。"""
    pids = find_watchers()
    if pids:
        print(f"すでに動いています（PID: {', '.join(map(str, pids))}）。")
        return 0
    print(start_watcher())
    time.sleep(4)
    pids = find_watchers()
    print(f"起動を確認しました（PID: {', '.join(map(str, pids))}）。" if pids else "起動を確認できませんでした。logs/ycuboard.log を確認してください。")
    return 0 if pids else 1


def do_stop() -> int:
    n = stop_watchers()
    print(f"常駐プロセスを {n} 件停止しました。" if n else "動いている常駐プロセスはありません。")
    return 0


def install_task() -> None:
    """Windows ログオン時に `--watch` を自動起動するタスクを登録する（ウィンドウ無し）。"""
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    runner = pythonw if pythonw.exists() else exe
    script = f"""
    $a = New-ScheduledTaskAction -Execute '{runner}' -Argument '-m src.main --watch' -WorkingDirectory '{BASE_DIR}'
    $t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t -Settings $s -Force | Out-Null
    """
    result = _powershell(script)
    if result.returncode != 0:
        print("タスクの登録に失敗しました:\n" + result.stderr.strip())
        sys.exit(1)
    print(f"登録しました。次回のログオンから自動で常駐します（タスク名: {TASK_NAME}）。")
    print("今すぐ開始するには: Start-ScheduledTask -TaskName " + TASK_NAME + "（PowerShell）")


def uninstall_task() -> None:
    result = _powershell(f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false")
    print("自動起動の登録を解除しました。" if result.returncode == 0 else "登録されていません。")


def onoff(value: bool) -> str:
    return "ON " if value else "OFF"


def print_status() -> None:
    cfg = load_config()
    mail = cfg["email"]["to"] or "(id_password.txt のメールアドレス)"
    targets = ", ".join(cfg["target_courses"]) or "(全講義)"
    print("\n=== YCU-Board 自動チェック 設定 ===")
    print(f"  定期チェック      : {onoff(cfg['check_enabled'])}   (--check-on / --check-off)")
    print(f"  自動ダウンロード  : {onoff(cfg['download_enabled'])}   (--download-on / --download-off)")
    print(f"  Teams の資料      : {onoff(cfg['teams_enabled'])}   (--teams-on / --teams-off)  ダウンロードは「自動ダウンロード」と対象講義に従う")
    print(f"  Drive へも複製    : {onoff(cfg['drive_enabled'])}   (--drive-on / --drive-off)  複製先: {cfg['drive_dir'] or '(未設定: --set-drive-dir)'}")
    when = "更新があったときだけ" if cfg["email_only_on_change"] else "毎回"
    print(f"  結果メール        : {onoff(cfg['email_enabled'])}   (--mail-on / --mail-off)  宛先: {mail} / {when} (--mail-changes-only / --mail-always)")
    print(f"  巡回時刻          : {', '.join(cfg['daily_run_times'])}  (1日{len(cfg['daily_run_times'])}回, --set-times)")
    jitter = f"ON（毎回 ±{cfg['jitter_max_minutes']}分の範囲でずらす）" if cfg["jitter_enabled"] else "OFF（設定した時刻ちょうど）"
    print(f"  時刻の誤差        : {jitter}  (--jitter-on / --jitter-off)")
    print(f"  ダウンロード対象  : {targets}  (--set-courses)")
    print(f"  保存先            : {cfg['output_dir']}  (--set-output)")

    from src.doctor import check_task
    from src.state import load_state

    registered, task_msg = check_task()
    state = load_state()
    last = {"ok": "成功", "error": "一部エラー", "login_failed": "ログイン失敗（要 --login）"}.get(state.get("last_result"), "")
    print("  --- 状態 ---")
    print(f"  自動起動          : {task_msg}" + ("" if registered else "  ← 登録しないと定期チェックは動きません"))
    print(f"  最終実行          : {state['last_run'].replace('T', ' ')}  {last}" if state.get("last_run") else "  最終実行          : まだ実行されていません")
    if state.get("initial_sync_at"):
        print(f"  初回監査          : 完了 {state['initial_sync_at'].replace('T', ' ')}（保存 {state.get('initial_sync_files', 0)} 件）")
    else:
        print("  初回監査          : 未実施  ← 定期チェックの前に --initial-sync で既存の資料を取得してください")
    if state.get("login_alert_sent"):
        print("  ※ ログイン失敗をお知らせ済みです。python -m src.main --login で復旧してください。")
    if cfg["teams_enabled"] and state.get("teams_login_failed"):
        print("  ※ 前回 Teams にログインできませんでした。python -m src.main --check-teams --headful で承認してください。")
    print()


def watch(headless: bool) -> None:
    """設定した時刻に巡回する常駐ループ。設定は毎回読み直す（ON/OFFや時刻の変更が即反映される）。"""
    others = find_watchers()
    if others:
        print(f"すでに常駐が動いています（PID: {', '.join(map(str, others))}）。二重起動を避けるため終了します。")
        return
    logger.info("定期監視を開始します（Ctrl+C で終了）")
    print_status()
    done = set()  # (日付, 時刻) 実行済みスロット
    started = datetime.now()
    # 起動前に予定時刻を過ぎていた枠は、下の取りこぼし補完か、仕様による見送りで済んでいるので、ループでは実行しない
    for hm in load_config()["daily_run_times"]:
        nominal = datetime.combine(started.date(), datetime.strptime(hm, "%H:%M").time())
        if planned_for(nominal) <= started:
            done.add((started.date(), hm))

    # PC を閉じていた・常駐が止まっていたなどで直近の予定を取りこぼしていたら、起動直後に1回補う
    cfg = load_config()
    missed = missed_slot(load_state().get("last_run"), cfg["daily_run_times"]) if cfg["check_enabled"] else None
    if missed and in_maintenance(datetime.now()):
        logger.info("取りこぼし（予定 %s）がありますが、メンテナンス時間帯のため補完しません。", missed.strftime("%m/%d %H:%M"))
    elif missed:
        logger.info("取りこぼしを検出（予定 %s）。今すぐ1回実行して補います。", missed.strftime("%m/%d %H:%M"))
        try:
            run_once(headless=headless, download=cfg["download_enabled"], scheduled=True)
        except Exception:  # noqa: BLE001
            logger.exception("取りこぼしの補完に失敗しました")

    while True:
        now = datetime.now()
        cfg = load_config()
        if cfg["check_enabled"]:
            for hm in cfg["daily_run_times"]:
                key = (now.date(), hm)
                if key in done:
                    continue
                nominal = datetime.combine(now.date(), datetime.strptime(hm, "%H:%M").time())
                # 実際の実行予定 = 設定時刻 + 誤差（予定ごとに1回だけ決めて保存。誤差OFFなら設定時刻そのまま）
                slot = planned_for(nominal, create=True, cfg=cfg)
                late = now - slot
                # 起動前に過ぎていた時刻は実行しない。起動後に過ぎた時刻は一定時間内なら実行する。
                if late < timedelta(0) or slot < started - timedelta(minutes=1):
                    continue
                done.add(key)
                if late > timedelta(minutes=CATCH_UP_MINUTES):
                    # スリープ等で大きく遅れた予定は見送る（仕様）。--health が「固まっている」と誤判定しないよう記録する
                    logger.warning("%s の巡回は %d 分以上遅れたため見送りました（スリープ等）。", hm, CATCH_UP_MINUTES)
                    update_state(skipped_slot=slot.isoformat(timespec="seconds"))
                    continue
                if in_maintenance(now):
                    logger.warning("メンテナンス時間帯のため %s の巡回をスキップします。", hm)
                    continue
                logger.info("定期巡回を開始します (設定 %s / 予定 %s)", hm, slot.strftime("%H:%M:%S"))
                try:
                    run_once(headless=headless, download=cfg["download_enabled"], scheduled=True)
                except Exception:  # noqa: BLE001 - 常駐を止めない
                    logger.exception("定期巡回に失敗しました")
        time.sleep(20)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="YCU-Board 資料チェック＆自動ダウンロード")
    setup = p.add_argument_group("セットアップ")
    setup.add_argument("--init", action="store_true", help="ID とパスワードを入力して id_password.txt を作成")
    setup.add_argument("--install-task", action="store_true", help="Windows ログオン時に --watch を自動起動する登録")
    setup.add_argument("--uninstall-task", action="store_true", help="自動起動の登録を解除")
    setup.add_argument("--doctor", action="store_true", help="環境（Python・Playwright・認証情報など）を診断")
    setup.add_argument("--check-login", action="store_true", help="保存済みセッションで自動ログインできるか確認")
    setup.add_argument("--check-teams", action="store_true", help="Teams（SharePoint）に入れるか、履修講義のチームが見つかるか確認")
    setup.add_argument("--test-mail", action="store_true", help="自分宛にテストメールを送信")

    run = p.add_argument_group("実行")
    run.add_argument("--login", action="store_true", help="ブラウザを表示してログイン（初回・セッション切れ時）")
    run.add_argument("--initial-sync", action="store_true", help="初回監査: 定期監査の前に、公開済みの資料を取得し現状を基準として記録")
    run.add_argument("--dry-run", action="store_true", help="--initial-sync と併用: 保存せず、保存される資料の一覧だけ表示")
    run.add_argument("--now", "--once", dest="once", action="store_true",
                     help="今すぐ監査する（定期チェックの ON/OFF に関係なく実行。ダウンロード・メールは設定に従う）")
    run.add_argument("--watch", action="store_true", help="設定した時刻に自動チェックする常駐モード")
    run.add_argument("--start", action="store_true", help="停止している常駐（定期監査）を起動する")
    run.add_argument("--stop", action="store_true", help="動いている常駐を停止する")
    run.add_argument("--health", action="store_true", help="定期監査が正常に動いているか診断（停止・取りこぼしの検出）")
    run.add_argument("--list-courses", action="store_true", help="履修中の講義を一覧表示")
    run.add_argument("--status", action="store_true", help="現在の設定を表示")
    run.add_argument("--with-download", action="store_true", help="--once と併用: 対象講義の資料も保存する")
    run.add_argument("--course", action="append", metavar="講義名", help="--once 時のダウンロード対象(部分一致,複数可)。config を上書き")
    run.add_argument("--no-mail", action="store_true", help="--once 時にメールを送らない")
    run.add_argument("--headful", action="store_true", help="ブラウザを表示して実行")

    sw = p.add_argument_group("ON/OFF 切り替え（初期値はチェック/ダウンロードOFF、メールON）")
    sw.add_argument("--check-on", action="store_true")
    sw.add_argument("--check-off", action="store_true")
    sw.add_argument("--download-on", action="store_true")
    sw.add_argument("--download-off", action="store_true")
    sw.add_argument("--teams-on", action="store_true", help="Teams（SharePoint）の講義資料も確認・ダウンロードする")
    sw.add_argument("--teams-off", action="store_true", help="Teams の確認をやめる")
    sw.add_argument("--drive-on", action="store_true", help="保存した資料を --set-drive-dir のフォルダ（Google Drive など）にも複製する")
    sw.add_argument("--drive-off", action="store_true", help="複製をやめる")
    sw.add_argument("--mail-on", action="store_true")
    sw.add_argument("--mail-off", action="store_true")
    sw.add_argument("--mail-changes-only", action="store_true", help="更新・保存・エラーがあったときだけメールする")
    sw.add_argument("--jitter-on", action="store_true", help="実行時刻に誤差（±10分）を加える（初期値）")
    sw.add_argument("--jitter-off", action="store_true", help="実行時刻に誤差を加えない（設定した時刻ちょうどに実行）")
    sw.add_argument("--mail-always", action="store_true", help="毎回メールする（初期値）")
    sw.add_argument("--on", action="store_true", help="チェックとダウンロードをまとめてON")
    sw.add_argument("--off", action="store_true", help="チェックとダウンロードをまとめてOFF")

    st = p.add_argument_group("設定変更")
    st.add_argument("--set-times", nargs="+", metavar="HH:MM", help="巡回時刻（例: --set-times 11:00 17:00 21:30）")
    st.add_argument("--set-courses", nargs="*", metavar="講義名", help="ダウンロード対象の講義（指定なしで全講義）")
    st.add_argument("--set-output", metavar="フォルダ", help="資料の保存先フォルダ")
    st.add_argument("--set-drive-dir", metavar="フォルダ", help="複製先フォルダ（絶対パス。例: H:\マイドライブ\YCU-Board）")
    st.add_argument("--set-mail-to", metavar="アドレス", help="結果メールの宛先")
    return p


def apply_settings(args) -> bool:
    """設定変更系の引数を反映する。何か変更したら True。"""
    changes = {}
    if args.on or args.check_on:
        changes["check_enabled"] = True
    if args.off or args.check_off:
        changes["check_enabled"] = False
    if args.on or args.download_on:
        changes["download_enabled"] = True
    if args.off or args.download_off:
        changes["download_enabled"] = False
    if args.teams_on:
        changes["teams_enabled"] = True
    if args.teams_off:
        changes["teams_enabled"] = False
    if args.set_drive_dir:
        from src.drive import validate_drive_dir

        changes["drive_dir"] = validate_drive_dir(args.set_drive_dir)
    if args.drive_on:
        if not (changes.get("drive_dir") or load_config()["drive_dir"]):
            raise ValueError("先に複製先を指定してください: --set-drive-dir フォルダ")
        changes["drive_enabled"] = True
    if args.drive_off:
        changes["drive_enabled"] = False
    if args.mail_on:
        changes["email_enabled"] = True
    if args.mail_off:
        changes["email_enabled"] = False
    if args.mail_changes_only:
        changes["email_only_on_change"] = True
    if args.jitter_on:
        changes["jitter_enabled"] = True
    if args.jitter_off:
        changes["jitter_enabled"] = False
    if args.mail_always:
        changes["email_only_on_change"] = False
    if args.set_courses is not None:
        changes["target_courses"] = args.set_courses
    if args.set_output:
        changes["output_dir"] = args.set_output
    if args.set_mail_to:
        changes["email"] = {**load_config()["email"], "to": args.set_mail_to}
    if changes:
        update_config(**changes)
    if args.set_times:
        set_daily_run_times([normalize_time(t) for t in args.set_times])
        changes["times"] = True
    return bool(changes)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        changed = apply_settings(args)
    except ValueError as e:
        parser.error(str(e))
    if changed:
        print_status()
        if args.set_output:
            print("※ 保存先の変更は次回起動から有効です。")

    headless = not args.headful
    if args.doctor:
        from src.doctor import run_doctor

        sys.exit(run_doctor())
    elif args.check_login:
        sys.exit(do_check_login())
    elif args.check_teams:
        sys.exit(do_check_teams(headless))
    elif args.test_mail:
        sys.exit(do_test_mail())
    elif args.init:
        do_init()
    elif args.install_task:
        install_task()
    elif args.uninstall_task:
        uninstall_task()
    elif args.login:
        do_login()
    elif args.list_courses:
        do_list_courses(headless)
    elif args.health:
        from src.health import run_health

        sys.exit(run_health())
    elif args.start:
        sys.exit(do_start())
    elif args.stop:
        sys.exit(do_stop())
    elif args.initial_sync:
        sys.exit(do_initial_sync(headless=headless, targets=args.course, dry_run=args.dry_run, send_mail=not args.no_mail))
    elif args.once:
        download = args.with_download or load_config()["download_enabled"]
        summary = run_once(headless=headless, download=download, targets=args.course, send_mail=not args.no_mail)
        if summary is None:
            sys.exit(1)
        _print_audit_result(summary)
    elif args.watch:
        enable_file_logging()
        watch(headless)
    elif args.status or not changed:
        print_status()
        if not args.status:
            parser.print_usage()


if __name__ == "__main__":
    try:
        main()
    except (FileNotFoundError, ValueError, TimeoutError) as e:  # 認証情報ファイルの不備など、利用者が直せるもの
        print(f"[エラー] {e}")
        sys.exit(1)
