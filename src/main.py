"""YCU-Board 資料チェック＆自動ダウンロードの CLI。 `python -m src.main --help` 参照。"""
import argparse
import base64
import logging
import subprocess
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
    load_config,
    is_in_maintenance,
    normalize_time,
    set_daily_run_times,
    update_config,
    write_credentials,
)
from src.crawler import YCUBoardCrawler
from src.pipeline import run_cycle

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


def run_once(*, headless: bool, download: bool, targets=None, send_mail: bool = True):
    with sync_playwright() as p:
        context = open_context(p, headless)
        try:
            return run_cycle(context, download=download, targets=targets, send_mail=send_mail)
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


def _powershell(script: str) -> subprocess.CompletedProcess:
    # 日本語パスが文字化けしないよう UTF-16LE の Base64 で渡す
    script = "$ProgressPreference = 'SilentlyContinue'\n" + script
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", encoded], capture_output=True, text=True, errors="replace")


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
    when = "更新があったときだけ" if cfg["email_only_on_change"] else "毎回"
    print(f"  結果メール        : {onoff(cfg['email_enabled'])}   (--mail-on / --mail-off)  宛先: {mail} / {when} (--mail-changes-only / --mail-always)")
    print(f"  巡回時刻          : {', '.join(cfg['daily_run_times'])}  (1日{len(cfg['daily_run_times'])}回, --set-times)")
    print(f"  ダウンロード対象  : {targets}  (--set-courses)")
    print(f"  保存先            : {cfg['output_dir']}  (--set-output)\n")


def watch(headless: bool) -> None:
    """設定した時刻に巡回する常駐ループ。設定は毎回読み直す（ON/OFFや時刻の変更が即反映される）。"""
    logger.info("定期監視を開始します（Ctrl+C で終了）")
    print_status()
    done = set()  # (日付, 時刻) 実行済みスロット
    started = datetime.now()

    while True:
        now = datetime.now()
        cfg = load_config()
        if cfg["check_enabled"]:
            for hm in cfg["daily_run_times"]:
                slot = datetime.combine(now.date(), datetime.strptime(hm, "%H:%M").time())
                key = (now.date(), hm)
                late = now - slot
                # 起動前に過ぎていた時刻は実行しない。起動後に過ぎた時刻は一定時間内なら実行する。
                if key in done or late < timedelta(0) or slot < started - timedelta(minutes=1):
                    continue
                done.add(key)
                if late > timedelta(minutes=CATCH_UP_MINUTES):
                    continue
                if is_in_maintenance():
                    logger.warning("メンテナンス時間帯のため %s の巡回をスキップします。", hm)
                    continue
                logger.info("定期巡回を開始します (%s)", hm)
                try:
                    run_once(headless=headless, download=cfg["download_enabled"])
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
    setup.add_argument("--test-mail", action="store_true", help="自分宛にテストメールを送信")

    run = p.add_argument_group("実行")
    run.add_argument("--login", action="store_true", help="ブラウザを表示してログイン（初回・セッション切れ時）")
    run.add_argument("--once", action="store_true", help="今すぐ1回チェックする（ON/OFF設定に関係なく実行）")
    run.add_argument("--watch", action="store_true", help="設定した時刻に自動チェックする常駐モード")
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
    sw.add_argument("--mail-on", action="store_true")
    sw.add_argument("--mail-off", action="store_true")
    sw.add_argument("--mail-changes-only", action="store_true", help="更新・保存・エラーがあったときだけメールする")
    sw.add_argument("--mail-always", action="store_true", help="毎回メールする（初期値）")
    sw.add_argument("--on", action="store_true", help="チェックとダウンロードをまとめてON")
    sw.add_argument("--off", action="store_true", help="チェックとダウンロードをまとめてOFF")

    st = p.add_argument_group("設定変更")
    st.add_argument("--set-times", nargs="+", metavar="HH:MM", help="巡回時刻（例: --set-times 11:00 17:00 21:30）")
    st.add_argument("--set-courses", nargs="*", metavar="講義名", help="ダウンロード対象の講義（指定なしで全講義）")
    st.add_argument("--set-output", metavar="フォルダ", help="資料の保存先フォルダ")
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
    if args.mail_on:
        changes["email_enabled"] = True
    if args.mail_off:
        changes["email_enabled"] = False
    if args.mail_changes_only:
        changes["email_only_on_change"] = True
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
    elif args.once:
        download = args.with_download or load_config()["download_enabled"]
        summary = run_once(headless=headless, download=download, targets=args.course, send_mail=not args.no_mail)
        if summary is None:
            sys.exit(1)
        print(f"\n完了: 講義 {len(summary['courses'])} 件をチェック / ダウンロード {len(summary['downloads'])} 件")
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
