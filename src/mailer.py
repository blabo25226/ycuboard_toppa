"""巡回・ダウンロード結果のメール送信。

学内の Microsoft 365 アカウントは SMTP の基本認証が使えないため、既定(method=auto)では
ログイン済みブラウザで Outlook on the web を開いて送信する。アプリパスワード等を用意した場合は
method=smtp でも送れる。
"""
import logging
import smtplib
import time
from datetime import datetime
from email.message import EmailMessage
from typing import List
from urllib.parse import quote

from playwright.sync_api import BrowserContext

from src.config import BASE_DIR, get_credentials, load_config

logger = logging.getLogger(__name__)

COMPOSE_URL = "https://outlook.office.com/mail/deeplink/compose?to={to}&subject={subject}"


def build_report(summary: dict) -> tuple:
    """巡回結果(summary)から (件名, 本文) を作る。"""
    lines: List[str] = []
    n_new = sum(len(c["changes"].new) for c in summary["courses"])
    n_upd = sum(len(c["changes"].updated) for c in summary["courses"])
    downloads = summary["downloads"]
    errors = summary["errors"]

    if summary.get("recovered"):
        lines.append("※ ログインできない状態でしたが、復旧しました。")
    lines.append(f"実行時刻: {summary['started']:%Y-%m-%d %H:%M}")
    lines.append(f"チェックした講義: {len(summary['courses'])} 件 / 新規資料 {n_new} 件 / 更新 {n_upd} 件")
    lines.append(f"ダウンロード: {len(downloads)} 件" if summary["download_enabled"] else "ダウンロード: OFF")
    lines.append("")

    for c in summary["courses"]:
        ch = c["changes"]
        if ch.baseline:
            lines.append(f"■ {c['name']}: 初回登録（資料 {c['count']} 件を記録。次回から差分を通知）")
        elif ch.changed:
            lines.append(f"■ {c['name']}")
            lines += [f"  + 新規: {i['material_title']} / {i['file_name']} ({i['updated_on']})" for i in ch.new]
            lines += [f"  * {i['reason']}: {i['material_title']} / {i['file_name']} ({i['updated_on']})" for i in ch.updated]
            lines += [f"  - 削除: {i['material_title']} / {i['file_name']}" for i in ch.removed]
    if not any(c["changes"].changed or c["changes"].baseline for c in summary["courses"]):
        lines.append("更新はありませんでした。")

    if downloads:
        lines += ["", "【ダウンロードした資料】"]
        lines += [f"  {d['course_name']} / {d['file_name']} ({d['reason']}) -> {d['path']}" for d in downloads]
    if errors:
        lines += ["", "【エラー】"] + [f"  {e}" for e in errors]

    flag = "初回監査完了" if summary.get("initial") else ("更新あり" if (n_new or n_upd or downloads) else "更新なし")
    if errors:
        flag += "・エラーあり"
    return f"[YCU-Board] {flag} ({summary['started']:%m/%d %H:%M})", "\n".join(lines)


def build_login_alert() -> tuple:
    """ログインできなくなったときのお知らせ (件名, 本文)。"""
    now = datetime.now()
    body = "\n".join([
        "YCU-Board に自動ログインできなかったため、定期チェックを実行できませんでした。",
        f"発生時刻: {now:%Y-%m-%d %H:%M}",
        "",
        "考えられる原因:",
        "  ・サインインの有効期限が切れた（Authenticator での承認が必要）",
        "  ・パスワードを変更した（id_password.txt を更新してください）",
        "  ・大学側のメンテナンス（毎週火曜 1:00〜6:00）",
        "",
        "対処: PC で次のコマンドを実行し、画面の指示に従ってください。",
        "  python -m src.main --login",
        "（AI エージェントなら /ycu-setup でも確認できます）",
        "",
        "※ このお知らせは、復旧するまで1回だけ送ります。復旧すると、次の結果メールでお知らせします。",
    ])
    return f"[YCU-Board] ログインできません（要対応） ({now:%m/%d %H:%M})", body


def _recipient(cfg: dict) -> str:
    return cfg["email"]["to"] or get_credentials()[0]


def _send_smtp(cfg: dict, to: str, subject: str, body: str) -> None:
    mail = cfg["email"]
    user, password = get_credentials()
    user = mail["smtp_user"] or user
    pw_file = BASE_DIR / mail["smtp_password_file"] if mail["smtp_password_file"] else None
    if pw_file and pw_file.exists():
        password = pw_file.read_text(encoding="utf-8").strip()

    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, to, subject
    msg.set_content(body)
    with smtplib.SMTP(mail["smtp_host"], mail["smtp_port"], timeout=30) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(msg)


def _send_web(context: BrowserContext, to: str, subject: str, body: str) -> None:
    """Outlook on the web の新規作成画面（宛先・件名は URL、本文はエディタへ直接入力）から送信する。"""
    page = context.new_page()
    try:
        page.goto(COMPOSE_URL.format(to=quote(to), subject=quote(subject)), wait_until="domcontentloaded")
        send = page.locator("button[aria-label='送信'], button[aria-label='Send']")
        send.first.wait_for(state="visible", timeout=60000)
        time.sleep(3)  # 作成画面の初期化待ち
        editor = page.locator("[role='textbox'][aria-label='メッセージ本文'], [role='textbox'][aria-label='Message body']").first
        editor.click()
        page.keyboard.insert_text(body)
        time.sleep(1)
        send.first.click()
        # 送信後は作成画面が閉じて送信ボタンが消える
        send.first.wait_for(state="hidden", timeout=30000)
        time.sleep(2)
    finally:
        page.close()


def send_email(subject: str, body: str, context: BrowserContext = None) -> bool:
    """メール送信。成功なら True。失敗しても例外は投げず False（巡回自体は止めない）。"""
    cfg = load_config()
    to = _recipient(cfg)
    method = cfg["email"]["method"]
    attempts = {"auto": ["web", "smtp"], "web": ["web"], "smtp": ["smtp"]}.get(method, ["web", "smtp"])

    for how in attempts:
        try:
            if how == "web":
                if context is None:
                    continue
                _send_web(context, to, subject, body)
            else:
                _send_smtp(cfg, to, subject, body)
            logger.info("結果メールを送信しました (%s -> %s)", how, to)
            return True
        except Exception as e:  # noqa: BLE001 - 送信失敗は記録して次の方法へ
            logger.warning("メール送信に失敗 (%s): %s", how, str(e).splitlines()[0][:200])
    return False


def send_report(summary: dict, context: BrowserContext = None) -> bool:
    subject, body = build_report(summary)
    return send_email(subject, body, context)


if __name__ == "__main__":  # 簡易テスト: python -m src.mailer
    from playwright.sync_api import sync_playwright

    from src.config import AUTH_PROFILE_DIR

    logging.basicConfig(level=logging.INFO)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(AUTH_PROFILE_DIR), headless=True)
        ok = send_email("[YCU-Board] テストメール", f"送信テスト {datetime.now():%Y-%m-%d %H:%M:%S}", ctx)
        ctx.close()
    print("OK" if ok else "FAILED")
