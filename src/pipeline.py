"""1回ぶんの巡回サイクル: ログイン → 資料の差分チェック → (対象講義のみ)ダウンロード → 結果メール。"""
import logging
from datetime import datetime
from typing import List, Optional

from playwright.sync_api import BrowserContext

from src.auth import ensure_logged_in
from src.config import load_config
from src.crawler import YCUBoardCrawler, match_courses
from src.diff_engine import apply_scan, compute_file_hash, init_db, pending_downloads, record_download
from src.mailer import send_report
from src.notifier import notify_new_material

logger = logging.getLogger(__name__)


def run_cycle(
    context: BrowserContext,
    *,
    download: bool,
    targets: Optional[List[str]] = None,
    send_mail: bool = True,
) -> Optional[dict]:
    """
    context   : ログイン用プロファイルで起動済みのブラウザ
    download  : True なら targets に一致する講義の新規/更新資料を保存する
    targets   : ダウンロード対象の講義名(部分一致)。None なら config の target_courses、空なら全講義
    send_mail : True なら結果をメール送信する（config の email_enabled も別途必要）
    ログインできなかった場合は None を返す。
    """
    cfg = load_config()
    if targets is None:
        targets = cfg["target_courses"]
    init_db()

    page = context.pages[0] if context.pages else context.new_page()
    if not ensure_logged_in(page, timeout_seconds=60):
        logger.error("ログインできませんでした。`python -m src.main --login` で再ログインしてください。")
        return None

    crawler = YCUBoardCrawler(page)
    courses = crawler.get_enrolled_courses()
    target_ids = {c["id"] for c in match_courses(courses, targets)} if download else set()
    if download and targets and not target_ids:
        logger.warning("対象講義 %s に一致する履修講義がありません。", targets)

    summary = {
        "started": datetime.now(),
        "download_enabled": download,
        "courses": [],
        "downloads": [],
        "errors": [],
    }

    for course in courses:
        try:
            items = crawler.scrape_materials(course)
            changes = apply_scan(course["id"], items)
            summary["courses"].append(
                {"id": course["id"], "name": course["name"], "count": len(items), "changes": changes}
            )
            logger.info(
                "%s: 資料 %d 件 / 新規 %d 更新 %d 削除 %d%s",
                course["name"], len(items), len(changes.new), len(changes.updated), len(changes.removed),
                "（初回登録）" if changes.baseline else "",
            )

            if course["id"] in target_ids:
                _download_pending(crawler, course, summary)
            crawler.sleep()
        except Exception as e:  # noqa: BLE001 - 1講義の失敗で全体を止めない
            logger.exception("講義の巡回に失敗: %s", course["name"])
            summary["errors"].append(f"{course['name']}: {e}")

    cfg = load_config()
    if send_mail and cfg["email_enabled"]:
        send_report(summary, context)
    return summary


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
