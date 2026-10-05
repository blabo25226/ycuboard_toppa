"""YCU-Board の画面を読み取る部分（講義一覧・教材一覧の取得、ファイルのダウンロード）。"""
import logging
import re
import time
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional

from playwright.sync_api import Page

from src.config import CONFIG
from src.downloader import archive_old_version, get_material_target_dir, resolve_save_path

logger = logging.getLogger(__name__)

BASE_URL = "https://ycuboard.yokohama-cu.ac.jp"
TIMETABLE_URL = f"{BASE_URL}/lms/timetable"
COURSE_URL = f"{BASE_URL}/lms/course?idnumber={{}}"

_ROMAN = {"I": "1", "II": "2", "III": "3", "IV": "4", "V": "5"}

# 教材欄を「資料タイトル → ファイル行」の順に読み取る（ブラウザ内で実行）
_SCRAPE_MATERIALS_JS = """
() => {
  const root = document.querySelector('#materialList');
  if (!root) return [];
  const out = [];
  let title = '';
  root.querySelectorAll('.block-wide label.bold-txt, .materialCss').forEach(el => {
    if (el.matches('.materialCss')) {
      const text = sel => (el.querySelector(sel)?.textContent || '').trim();
      out.push({
        material_title: title,
        material_id: el.querySelector('#dlMaterialId')?.value || '',
        resource_id: text('.resource_Id'),
        file_name: text('.fileName') || text('label.fileDownload'),
        updated_on: text('.course-view-material-update'),
        open_end: text('.openEndDate'),
        is_file: !!el.querySelector('label.fileDownload'),
      });
    } else {
      title = el.textContent.trim();
    }
  });
  return out;
}
"""

# 講義ページの「テスト」欄(#examination)と「課題」欄(#reportList)の一覧を読み取る（ブラウザ内で実行）
_SCRAPE_COURSEWORK_JS = r"""
() => {
  const t = el => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  const param = (href, key) => {
    try { return new URL(href || '', location.href).searchParams.get(key) || ''; } catch (e) { return ''; }
  };
  const tests = [...document.querySelectorAll('#examination .contents-list .course-result-list')].map(row => {
    const name = row.querySelector('.course-view-examination-name');
    const title = t(name);
    return {
      item_id: param(name && name.getAttribute('href'), 'examinationId') || title,
      title,
      period: t(row.querySelector('.course-view-examination-period')),
    };
  });
  const reports = [...document.querySelectorAll('#reportList .sortReportBlock')].map(row => {
    const name = row.querySelector('.course-view-report-name');
    const title = t(name);
    const start = t(row.querySelector('.course-view-report-time-start'));
    const end = t(row.querySelector('.course-view-report-time-end'));
    return {
      item_id: (row.querySelector('input.reportId') || {}).value || param(name && name.getAttribute('href'), 'reportId') || title,
      title,
      period: (start || end) ? start + ' ～ ' + end : '',
    };
  });
  return { tests, reports };
}
"""


def normalize_course_name(name: str) -> str:
    """講義名の照合用に正規化（全角/半角・空白・括弧・ローマ数字と算用数字の違いを吸収）。"""
    s = unicodedata.normalize("NFKC", name).lower()
    s = re.sub(r"[\s　()（）]", "", s)
    return re.sub(r"(?<![a-z])(iv|iii|ii|i|v)(?![a-z])", lambda m: _ROMAN[m.group(1).upper()], s)


def match_courses(courses: List[dict], keywords: List[str]) -> List[dict]:
    """keywords のいずれかに部分一致する講義を返す。keywords が空なら全講義。"""
    if not keywords:
        return courses
    wanted = [normalize_course_name(k) for k in keywords]
    return [c for c in courses if any(w in normalize_course_name(c["name"]) for w in wanted)]


class YCUBoardCrawler:
    def __init__(self, page: Page):
        self.page = page
        self.delay = CONFIG.get("request_delay_seconds", 1.5)

    def sleep(self) -> None:
        time.sleep(self.delay)

    def get_enrolled_courses(self) -> List[Dict[str, str]]:
        """時間割から履修中の講義一覧を取得 → [{"id", "name", "url"}]"""
        self.page.goto(TIMETABLE_URL, wait_until="networkidle")
        try:
            self.page.wait_for_selector(".timetable-course-top-btn", timeout=10000)
        except Exception:
            logger.warning("時間割の講義ボタンが見つかりませんでした。")

        courses, seen = [], set()
        buttons = self.page.locator(".timetable-course-top-btn")
        for i in range(buttons.count()):
            btn = buttons.nth(i)
            course_id = btn.get_attribute("id") or ""
            text = btn.inner_text().strip()
            # 学部・学年の見出しボタン(例: "2026DS2026-3102")は講義セルではないので除く
            is_course_cell = "divTableCellHeader" in (btn.get_attribute("class") or "")
            if not course_id or not text or course_id in seen or not is_course_cell:
                continue
            seen.add(course_id)
            courses.append({
                "id": course_id,
                "name": text.split("\n")[0].strip(),
                "url": COURSE_URL.format(course_id),
            })
        logger.info("履修中の講義: %d 件", len(courses))
        return courses

    def open_course(self, course: dict) -> None:
        self.page.goto(course["url"], wait_until="networkidle")
        self.page.wait_for_selector("#courseContent, .contents-title", timeout=15000)

    def scrape_materials(self, course: dict) -> List[dict]:
        """講義ページの「教材」欄に載っているファイルの一覧を返す。"""
        self.open_course(course)
        rows = self.page.evaluate(_SCRAPE_MATERIALS_JS)
        items = []
        for r in rows:
            if not r["is_file"] or not r["resource_id"]:
                continue  # リンク・動画などファイル以外は対象外
            r.pop("is_file")
            items.append({**r, "course_id": course["id"], "course_name": course["name"]})
        return items

    def scrape_coursework(self, course: dict) -> Dict[str, List[dict]]:
        """直前に開いた講義ページ(scrape_materials の直後)から、テストと課題の一覧を返す → {"test": [...], "report": [...]}"""
        data = self.page.evaluate(_SCRAPE_COURSEWORK_JS)
        out = {}
        for kind, rows in (("test", data["tests"]), ("report", data["reports"])):
            out[kind] = [
                {**r, "course_id": course["id"], "course_name": course["name"]}
                for r in rows
                if r["title"]  # タイトルが読めない行は誤検出を避けて無視する
            ]
        return out

    def download(self, item: dict) -> Optional[Path]:
        """講義ページ上のファイルをクリックして保存し、保存先パスを返す。失敗時は None。"""
        target = self.page.locator(
            f"#material{item['resource_id']} label.fileDownload"
        )
        if target.count() == 0:
            logger.warning("ダウンロード要素が見つかりません: %s", item["file_name"])
            return None

        with self.page.expect_download(timeout=60000) as info:
            target.first.click()
        download = info.value
        if download.failure():
            logger.error("ダウンロード失敗: %s (%s)", item["file_name"], download.failure())
            return None

        # 更新時は、この資料の旧版（記録済みの保存先）を退避してから同じ名前で保存する。
        # 退避はダウンロード成功後に行う（失敗しても手元のファイルを失わない）。
        old = Path(item["local_path"]) if item.get("local_path") else None
        if item["reason"] == "UPDATED" and old and old.exists():
            archive_old_version(old)
        target_dir = get_material_target_dir(item["course_name"], item["material_title"])
        save_path = resolve_save_path(target_dir, item["file_name"])
        download.save_as(str(save_path))
        self.sleep()
        return save_path
