"""Windows のトースト通知（PowerShell 経由）。"""
import logging

from src.config import load_config
from src.winutil import powershell

logger = logging.getLogger(__name__)


def show_windows_toast(title: str, message: str) -> None:
    def quote(text: str) -> str:  # PowerShell の単一引用符文字列（展開なし）。' は '' で表す
        return "'" + text.replace("'", "''") + "'"

    # CreateTextNode はテキストをそのまま入れるので XML のエスケープは不要
    ps_script = f"""
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
    $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
    $nodes = $template.GetElementsByTagName("text")
    $nodes.Item(0).AppendChild($template.CreateTextNode({quote(title)})) > $null
    $nodes.Item(1).AppendChild($template.CreateTextNode({quote(message)})) > $null
    $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("YCU-Board Downloader")
    $notifier.Show([Windows.UI.Notifications.ToastNotification]::new($template))
    """
    try:
        powershell(ps_script, timeout=10)
    except Exception as e:  # noqa: BLE001 - 通知の失敗は無視してよい
        logger.debug("トースト通知に失敗: %s", e)


def notify_new_material(course_name: str, material_title: str, file_name: str) -> None:
    if load_config()["enable_notification"]:
        show_windows_toast(f"【YCU-Board 新着資料】{course_name}", f"{material_title} ({file_name})")


def notify_login_failed() -> None:
    show_windows_toast("【YCU-Board】ログインできません", "定期チェックを実行できませんでした。python -m src.main --login で再ログインしてください。")


def notify_run_failed(reason: str) -> None:
    show_windows_toast("【YCU-Board】定期チェックに失敗しました", f"YCU-Board に接続できませんでした（{reason[:80]}）。次の予定時刻に再試行します。")


def notify_login_required() -> None:
    print("\n[要対応] Authenticator アプリで承認番号を入力してください。\n")
    show_windows_toast("【YCU-Board】再サインインが必要です", "Authenticatorアプリで承認番号を入力してください。")


def notify_teams_login_failed() -> None:
    show_windows_toast("【YCU-Board】Teams にログインできません",
                       "Teams の講義資料を確認できませんでした。python -m src.main --check-teams --headful で承認してください。")
