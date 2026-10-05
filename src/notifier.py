"""Windows のトースト通知（PowerShell 経由）。"""
import logging
import subprocess

from src.config import load_config

logger = logging.getLogger(__name__)


def show_windows_toast(title: str, message: str) -> None:
    def esc(text: str) -> str:  # PowerShell のダブルクォート文字列と XML 用にエスケープ
        return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("`", "``").replace('"', '`"').replace("$", "`$"))

    ps_script = f"""
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
    $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
    $nodes = $template.GetElementsByTagName("text")
    $nodes.Item(0).AppendChild($template.CreateTextNode("{esc(title)}")) > $null
    $nodes.Item(1).AppendChild($template.CreateTextNode("{esc(message)}")) > $null
    $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("YCU-Board Downloader")
    $notifier.Show([Windows.UI.Notifications.ToastNotification]::new($template))
    """
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:  # noqa: BLE001 - 通知の失敗は無視してよい
        logger.debug("トースト通知に失敗: %s", e)


def notify_new_material(course_name: str, material_title: str, file_name: str) -> None:
    if load_config()["enable_notification"]:
        show_windows_toast(f"【YCU-Board 新着資料】{course_name}", f"{material_title} ({file_name})")


def notify_login_required() -> None:
    print("\n[要対応] Authenticator アプリで承認番号を入力してください。\n")
    show_windows_toast("【YCU-Board】再サインインが必要です", "Authenticatorアプリで承認番号を入力してください。")
