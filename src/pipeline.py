"""1回ぶんの巡回サイクル: ログイン → 資料の差分チェック → (対象講義のみ)ダウンロード → 結果メール。"""
import logging
from datetime import datetime
from typing import List, Optional

from playwright.sync_api import BrowserContext

from src.auth import ensure_logged_in
from src.config import load_config
from src.crawler import YCUBoardCrawler, match_courses
from src.diff_engine import (
    apply_coursework,
    apply_scan,
    compute_file_hash,
    has_active_coursework,
    has_active_materials,
    init_db,
    pending_downloads,
    record_download,
    scratch_db,
)
from src.mailer import send_email, send_report
from src.mailer import build_login_alert
from src.notifier import notify_login_failed, notify_new_material, notify_run_failed
from src.state import load_state, now_iso, update_state
from src.teams import LOGIN_ERROR as TEAMS_LOGIN_ERROR
from src.teams import run_teams

logger = logging.getLogger(__name__)


def run_cycle(
    context: BrowserContext,
    *,
    download: bool,
    targets: Optional[List[str]] = None,
    send_mail: bool = True,
    scheduled: bool = False,
    initial: bool = False,
    dry_run: bool = False,
) -> Optional[dict]:
    """
    context   : ログイン用プロファイルで起動済みのブラウザ
    download  : True なら targets に一致する講義の新規/更新資料を保存する
    targets   : ダウンロード対象の講義名(部分一致)。None なら config の target_courses、空なら全講義
    send_mail : True なら結果をメール送信する（config の email_enabled も別途必要）
    scheduled : True（定期実行）なら、ログインできなかったときに本人へ通知する
    initial   : True なら初回監査（既存資料の取得）。結果メールの件名が変わる
    dry_run   : True ならダウンロードせず、保存対象を summary["pending"] に集めるだけ（メールも送らない）
    ログインできなかった場合は None を返す。
    """
    if dry_run:
        with scratch_db():  # 予行演習では本物の履歴DBを書き換えない
            return _run_cycle(context, download=download, targets=targets, send_mail=False, scheduled=scheduled,
                              initial=initial, dry_run=True)
    return _run_cycle(context, download=download, targets=targets, send_mail=send_mail, scheduled=scheduled,
                      initial=initial, dry_run=False)


def _run_cycle(context, *, download, targets, send_mail, scheduled, initial, dry_run):
    cfg = load_config()
    if targets is None:
        targets = cfg["target_courses"]
    init_db()

    page = context.pages[0] if context.pages else context.new_page()
    try:
        logged_in = ensure_logged_in(page, timeout_seconds=60)
    except (FileNotFoundError, ValueError):
        raise  # 認証情報ファイルの不備は呼び出し側で案内する
    except Exception as e:  # noqa: BLE001 - ネットワーク障害など。記録して本人に知らせる
        logger.exception("YCU-Board に接続できませんでした")
        if not dry_run:
            update_state(last_run=now_iso(), last_result="error", last_error=_short(e))
        if scheduled:
            notify_run_failed(_short(e))
        return None
    if not logged_in:
        logger.error("ログインできませんでした。`python -m src.main --login` で再ログインしてください。")
        update_state(last_run=now_iso(), last_result="login_failed")
        if scheduled:
            _alert_login_failed(context, send_mail)
        return None
    recovered = bool(load_state().get("login_alert_sent"))
    if recovered:
        update_state(login_alert_sent=False)

    summary = {
        "started": datetime.now(),
        "download_enabled": download,
        "courses": [],
        "downloads": [],
        "errors": [],
        "recovered": recovered,
        "initial": initial,
        "pending": [],
        "teams": [],
    }

    crawler = YCUBoardCrawler(page)
    try:
        courses = crawler.get_enrolled_courses()
        if not courses:
            raise RuntimeError("時間割から講義を1件も読み取れませんでした（読み込みの失敗、または学期の切り替わり）")
    except Exception as e:  # noqa: BLE001 - 「講義0件・更新なし」と誤って報告しないよう、エラーとして結果に載せる
        logger.exception("履修講義の取得に失敗しました")
        summary["errors"].append(f"履修講義の取得: {_short(e)}")
        courses = []
    target_ids = {c["id"] for c in match_courses(courses, targets)} if download else set()
    if download and targets and courses and not target_ids:
        logger.warning("対象講義 %s に一致する履修講義がありません。", targets)

    for course in courses:
        try:
            items = crawler.scrape_materials(course)
            if not items and has_active_materials(course["id"]):
                # 前回あった資料が全部消えた → 読み込み失敗のことが多いので、1回だけ読み直して確かめる
                logger.warning("%s: 資料が0件として読み取られたため、読み直します。", course["name"])
                crawler.sleep()
                items = crawler.scrape_materials(course)
            changes = apply_scan(course["id"], items)
            entry = {"id": course["id"], "name": course["name"], "count": len(items), "changes": changes,
                     "coursework": {}}
            summary["courses"].append(entry)
            try:  # テスト・課題の失敗で、教材のチェックとダウンロードを止めない
                entry["coursework"] = _scan_coursework(crawler, course)
            except Exception as e:  # noqa: BLE001
                logger.exception("テスト・課題の確認に失敗: %s", course["name"])
                summary["errors"].append(f"{course['name']}（テスト・課題）: {_short(e)}")
            logger.info(
                "%s: 資料 %d 件 / 新規 %d 更新 %d 削除 %d%s",
                course["name"], len(items), len(changes.new), len(changes.updated), len(changes.removed),
                "（初回登録）" if changes.baseline else "",
            )

            if course["id"] in target_ids:
                if dry_run:
                    summary["pending"] += pending_downloads(course["id"])
                else:
                    _download_pending(crawler, course, summary)
            crawler.sleep()
        except Exception as e:  # noqa: BLE001 - 1講義の失敗で全体を止めない
            logger.exception("講義の巡回に失敗: %s", course["name"])
            summary["errors"].append(f"{course['name']}: {e}")

    if cfg["teams_enabled"] and courses:
        run_teams(context, courses, target_ids, summary, dry_run=dry_run, scheduled=scheduled)

    if dry_run:
        return summary
    update_state(
        last_run=now_iso(),
        last_result="ok" if not summary["errors"] else "error",
        last_error=summary["errors"][0] if summary["errors"] else None,
        last_downloads=len(summary["downloads"]),
    )
    cfg = load_config()
    if send_mail and cfg["email_enabled"]:
        errors = summary["errors"]
        if summary.get("teams_login_failed") and load_state().get("teams_login_alert_sent"):
            # Teams のログイン失敗はお知らせ済み。復旧するまで、これだけを理由にメールは送らない（結果メールには載る）
            errors = [e for e in errors if e != TEAMS_LOGIN_ERROR]
        notable = bool(summary["downloads"] or errors or recovered or summary.get("teams_recovered")
                       or summary.get("teams_notices")) or any(
            c["changes"].changed or c["changes"].baseline or any(ch.changed for ch in c["coursework"].values())
            for c in summary["courses"]
        ) or any(t["changes"].changed or t["changes"].baseline for t in summary.get("teams", []))
        if notable or not cfg["email_only_on_change"]:
            summary["mail"] = "sent" if send_report(summary, context) else "failed"
            if summary["mail"] == "sent" and summary.get("teams_login_failed"):
                update_state(teams_login_alert_sent=True)
        else:
            summary["mail"] = "skipped"
            logger.info("更新・エラーが無いためメールは送りません（email_only_on_change）")
    else:
        summary["mail"] = "off"
    return summary


def _scan_coursework(crawler: YCUBoardCrawler, course: dict) -> dict:
    """講義ページ(直前に開いたもの)のテスト・課題を前回と比べる → {"test": Changes, "report": Changes}。通知用で、保存や提出はしない。"""
    data = crawler.scrape_coursework(course)
    if any(not data[k] and has_active_coursework(course["id"], k) for k in data):
        # 前回あったものが全部消えた → 読み込み失敗のことが多いので、1回だけ読み直して確かめる
        logger.warning("%s: テスト・課題が0件として読み取られたため、読み直します。", course["name"])
        crawler.sleep()
        crawler.open_course(course)
        data = crawler.scrape_coursework(course)
    result = {kind: apply_coursework(course["id"], kind, items) for kind, items in data.items()}
    for kind, ch in result.items():
        if ch.changed:
            logger.info("%s: %s 追加 %d 更新 %d 削除 %d", course["name"], kind, len(ch.new), len(ch.updated), len(ch.removed))
    return result


def _short(e: Exception) -> str:
    return (str(e).splitlines() or [type(e).__name__])[0][:200]


def _alert_login_failed(context: BrowserContext, send_mail: bool) -> None:
    """ログイン失敗を本人に知らせる。メールは復旧するまで1回だけ（連日の重複を避ける）。

    結果メールは Outlook on the web から送るため、Microsoft 側のサインインが切れているときは送れない。
    その場合に備えてトースト通知と state.json（--status で表示）も併用する。
    """
    notify_login_failed()
    if not (send_mail and load_config()["email_enabled"]):
        return
    if load_state().get("login_alert_sent"):
        logger.info("ログイン失敗のお知らせは送信済みです（復旧するまで再送しません）")
        return
    subject, body = build_login_alert()
    if send_email(subject, body, context):
        update_state(login_alert_sent=True)


def _download_pending(crawler: YCUBoardCrawler, course: dict, summary: dict) -> None:
    pending = pending_downloads(course["id"])
    if not pending:
        return
    logger.info("%s: ダウンロード対象 %d 件", course["name"], len(pending))
    for item in pending:
        try:
            path = crawler.download(item)
            if path is None:
                summary["errors"].append(f"{course['name']} / {item['file_name']}: ダウンロード失敗")
                continue
            record_download(course["id"], item["resource_id"], str(path), compute_file_hash(path), item["updated_on"])
            summary["downloads"].append({**item, "path": str(path)})
            logger.info("保存: %s", path)
            notify_new_material(course["name"], item["material_title"], item["file_name"])
        except Exception as e:  # noqa: BLE001
            logger.exception("ダウンロード失敗: %s", item["file_name"])
            summary["errors"].append(f"{course['name']} / {item['file_name']}: {e}")
