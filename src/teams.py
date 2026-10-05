"""Teams（SharePoint）の講義資料を取得する。

履修講義ごとのチームの実体は SharePoint サイトで、チャンネル「一般」の「共有済み」は
そのサイトの Shared Documents/General フォルダ。Teams の画面は重いので使わず、
ログイン済みブラウザから SharePoint の REST API を直接呼んで一覧とダウンロードを行う。
"""
import logging
import re
import time
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote, urlparse

from playwright.sync_api import BrowserContext, Page

from src.config import OUTPUT_DIR, get_credentials, load_config
from src.crawler import normalize_course_name
from src.diff_engine import (
    apply_teams_scan,
    compute_file_hash,
    has_active_teams_files,
    has_teams_records,
    pending_teams_downloads,
    record_teams_download,
    reset_teams_course,
)
from src.downloader import MAX_DIR_NAME_LENGTH, archive_old_version, resolve_save_path, sanitize_filename
from src.notifier import notify_login_required, notify_new_material, notify_teams_login_failed
from src.state import load_state, update_state

logger = logging.getLogger(__name__)

SP_HOST = "https://yokohamacu.sharepoint.com"
LIBRARY_ROOT = "Shared Documents/General"  # チャンネル「一般」の「共有済み」
EXCLUDED_FOLDERS = {"recordings"}  # 会議の録画は大きすぎるので対象外
EXCLUDED_SUFFIXES = (".loop",)  # 会議チャットから自動で作られる Loop の部品。講義資料ではない
MAX_DEPTH = 6
MAX_FILE_BYTES = 200 * 1024 * 1024  # これより大きいファイルは保存しない（メモリ・時間の都合。動画など）
LOGIN_ERROR = "Teams: SharePoint にログインできませんでした（Teams の講義資料は確認していません）"


class FolderMissing(Exception):
    """チャンネル「一般」のフォルダ（Shared Documents/General）がまだ無い。"""

# 参加しているサイト（チーム）の名前とURLの一覧。権限のあるものだけが返る。
_SEARCH_SITES_JS = """async () => {
  const q = "%s/_api/search/query?querytext='contentclass:STS_Site'&selectproperties='Title,Path'&rowlimit=500";
  const r = await fetch(q, {headers: {Accept: 'application/json;odata=nometadata'}, credentials: 'include'});
  if (!r.ok) return {err: r.status};
  const j = await r.json();
  const rows = j.PrimaryQueryResult.RelevantResults.Table.Rows || [];
  return rows.map(row => Object.fromEntries(row.Cells.map(c => [c.Key, c.Value])));
}""" % SP_HOST

# General 配下のファイルを再帰的に列挙する。
_LIST_FILES_JS = """async ([site, root, maxDepth, excluded]) => {
  const out = [];
  // decodedurl なら、ファイル名・フォルダ名の % や # もそのまま扱える
  const enc = p => encodeURIComponent(p.replace(/'/g, "''")).replace(/%2F/g, '/');
  async function walk(path, depth) {
    const q = `${site}/_api/web/GetFolderByServerRelativePath(decodedurl='${enc(path)}')?$expand=Folders,Files`
      + `&$select=Folders/Name,Folders/ServerRelativeUrl,Files/Name,Files/UniqueId,Files/TimeLastModified,Files/Length,Files/ServerRelativeUrl`;
    const r = await fetch(q, {headers: {Accept: 'application/json;odata=nometadata'}, credentials: 'include'});
    if (!r.ok) { out.push({err: r.status, path, depth}); return; }
    const j = await r.json();
    for (const f of j.Files) out.push({name: f.Name, id: f.UniqueId, modified: f.TimeLastModified, size: Number(f.Length), url: f.ServerRelativeUrl});
    if (depth < maxDepth) for (const d of j.Folders) {
      if (excluded.includes(d.Name.toLowerCase())) continue;
      await walk(d.ServerRelativeUrl, depth + 1);
    }
  }
  await walk(root, 0);
  return out;
}"""


# ---------- ログイン ----------
def _on_sharepoint(page: Page) -> bool:
    host = urlparse(page.url).netloc.lower()
    return host.endswith(".sharepoint.com") and "login" not in host


def open_sharepoint(page: Page, timeout_seconds: int = 90) -> bool:
    """SharePoint を開き、必要なら Microsoft のサインイン画面を自動で通過する。YCU-Board と同じ保存済みセッションを使う。

    パスワードは1回だけ送信する（誤ったパスワードの連続送信によるアカウントロックを避けるため）。
    """
    email, password = get_credentials()
    try:
        page.goto(SP_HOST + "/", wait_until="domcontentloaded", timeout=60000)
    except Exception as e:  # noqa: BLE001 - リダイレクト中の中断など。以降のループで状態を見る
        logger.debug("SharePoint への移動: %s", e)
    start = time.time()
    password_sent = False
    approval_notified = False
    while time.time() - start < timeout_seconds:
        if _on_sharepoint(page):
            return True
        if "login.microsoftonline.com" not in urlparse(page.url).netloc.lower():
            time.sleep(1.5)
            continue
        try:
            body = page.locator("body").inner_text(timeout=3000)
        except Exception:  # noqa: BLE001 - 遷移中
            time.sleep(1.5)
            continue
        try:
            local = email.split("@")[0]
            tile = page.locator(f"div[data-test-id*='{email}'], div[role='button']:has-text('{local}')")
            password_box = page.locator("input[name='passwd'], #i0118")
            email_box = page.locator("input[name='loginfmt'], #i0116")
            if ("アカウントを選択する" in body or "Pick an account" in body) and tile.count():
                logger.info("アカウント選択画面: 保存済みアカウントを選びます。")
                tile.first.click()
            elif ("パスワードの入力" in body or "Enter password" in body) and password_box.count() and password_box.first.is_visible():
                if password_sent:
                    error = page.locator("#passwordError, #i0118Error")
                    if error.count() and error.first.is_visible():
                        logger.error("パスワードが違います。id_password.txt を確認してください（ロックを避けるため再送信しません）。")
                        return False
                else:
                    logger.info("パスワード入力画面: パスワードを入力します。")
                    password_sent = True
                    password_box.first.fill(password)
                    password_box.first.press("Enter")
            elif "サインイン要求を承認" in body or "Approve sign in request" in body:
                if not approval_notified:
                    approval_notified = True
                    notify_login_required()
                    number = page.locator("#idRichContext_DisplaySign, #displaySign")
                    code = number.first.inner_text().strip() if number.count() else ""
                    print("\n" + "=" * 60)
                    print("[要操作] Microsoft Authenticator によるサインイン承認が必要です。")
                    if code:
                        print(f"  >>> 画面に表示されている承認番号: [ {code} ] <<<")
                    print("=" * 60 + "\n")
            elif "サインインの状態を維持しますか" in body or "Stay signed in" in body:
                logger.info("「サインイン状態を維持しますか？」: Yes を選びます。")
                page.locator("#idSIButton9").first.click()
            elif email_box.count() and email_box.first.is_visible() and not email_box.first.input_value():
                logger.info("メールアドレス入力画面: メールアドレスを入力します。")
                email_box.first.fill(email)
                email_box.first.press("Enter")
        except Exception as e:  # noqa: BLE001 - 画面遷移の途中は次の周回でやり直す
            logger.debug("サインイン処理: %s", e)
        time.sleep(2.0)
    return _on_sharepoint(page)


# ---------- サイトの特定・一覧・ダウンロード ----------
_SEPARATORS = re.compile(r"[_\s\-‐－–—・/|｜【】\[\]「」『』<>＜＞]+")
# 年度の表記: R8 / 令和8年度 / 2026 / 2026年度（NFKC・小文字化した後の文字列に使う）
_YEAR = re.compile(r"(?<![0-9a-z])(?:r|令和)(\d{1,2})(?:年度)?(?![0-9])|(?<![0-9])(20\d{2})(?:年度)?(?![0-9])")


def current_school_year(today: Optional[date] = None) -> int:
    today = today or date.today()
    return today.year if today.month >= 4 else today.year - 1


def _years(title: str) -> set:
    t = unicodedata.normalize("NFKC", title).lower()
    return {2018 + int(m.group(1)) if m.group(1) else int(m.group(2)) for m in _YEAR.finditer(t)}


def course_matches_team(course_name: str, team_title: str) -> bool:
    """チーム名が講義名を表しているか。チーム名全体か、区切り（_ や空白など）で分けた一部が、年度の表記を除いて講義名と一致すること。

    部分一致だけでは認めない（「データサイエンス1」が「データサイエンス10」や別の講義のチームに当たるのを防ぐ）。
    """
    key = normalize_course_name(course_name)
    t = unicodedata.normalize("NFKC", team_title).lower()
    return bool(key) and any(normalize_course_name(_YEAR.sub("", seg)) == key for seg in [t, *_SEPARATORS.split(t)])


def find_team_sites(page: Page, courses: List[dict], previous: Optional[Dict[str, dict]] = None) -> Dict[str, dict]:
    """履修講義 → Teams のチーム（SharePoint サイト）。保存はしない（呼び出し側が state.json に控える）。

    - チーム名が講義名と一致するもの（course_matches_team）だけを候補にする。
    - 年度の表記があって今年度でないチーム（昨年度のチームなど）は除く。
    - 前回と同じチームが候補にあれば、それを使い続ける（新しいチームが見つかっても勝手に切り替えない）。
    - 複数あれば、今年度の表記があるもの → 短い名前の順。
    - 検索に失敗したときは前回の対応表(previous)を使う。
    """
    previous = previous or {}
    rows = page.evaluate(_SEARCH_SITES_JS)
    if isinstance(rows, dict):
        logger.warning("チーム一覧を取得できませんでした（HTTP %s）。前回の対応表を使います。", rows.get("err"))
        return {c["id"]: previous[c["id"]] for c in courses if c["id"] in previous}
    sites = [
        {"title": r.get("Title") or "", "path": r.get("Path") or ""}
        for r in rows
        if (r.get("Path") or "").startswith(SP_HOST + "/sites/")
    ]
    year = current_school_year()
    found: Dict[str, dict] = {}
    for course in courses:
        key = normalize_course_name(course["name"])
        cands = [s for s in sites if course_matches_team(course["name"], s["title"])]
        old_year = [s for s in cands if _years(s["title"]) and year not in _years(s["title"])]
        cands = [s for s in cands if s not in old_year]
        if old_year:
            logger.info("%s: 今年度ではないチームは使いません: %s", course["name"], [s["title"] for s in old_year])
        partial = [s["title"] for s in sites
                   if key and key in normalize_course_name(s["title"]) and s not in cands and s not in old_year]
        if partial:
            logger.info("%s: 名前の一部だけが一致するチームは使いません: %s", course["name"], partial)
        if not cands:
            continue
        prev = previous.get(course["id"]) or {}
        keep = [s for s in cands if s["path"] == prev.get("site")]
        if keep:
            chosen = keep[0]
        else:
            cands.sort(key=lambda s: (year not in _years(s["title"]), len(s["title"])))
            chosen = cands[0]
            if len(cands) > 1:
                logger.info("%s: 該当するチームが複数あります。「%s」を使います: %s",
                            course["name"], chosen["title"], [c["title"] for c in cands])
        found[course["id"]] = {"site": chosen["path"], "team": chosen["title"]}
    return found


def list_files(page: Page, site: str) -> List[dict]:
    """サイトの General 配下（録画フォルダを除く）の全ファイル → [{file_id, rel_path, name, modified, size, url}]"""
    root = urlparse(site).path + "/" + LIBRARY_ROOT
    rows = page.evaluate(_LIST_FILES_JS, [site, root, MAX_DEPTH, sorted(EXCLUDED_FOLDERS)])
    errors = [r for r in rows if "err" in r]
    if len(errors) == 1 and errors[0]["err"] == 404 and errors[0]["depth"] == 0:
        raise FolderMissing(root)  # チャンネルのフォルダがまだ作られていない（誰もファイルを置いていないチーム）
    if errors:
        raise RuntimeError(f"Teams のファイル一覧を取得できません（HTTP {errors[0]['err']}）: {errors[0]['path']}")
    prefix = root.rstrip("/") + "/"
    items = []
    for r in rows:
        if r["name"].lower().endswith(EXCLUDED_SUFFIXES):
            continue
        url = r["url"]
        rel = url[len(prefix):] if url.startswith(prefix) else r["name"]
        items.append({
            "file_id": r["id"], "rel_path": rel, "name": r["name"],
            "modified": r["modified"], "size": r["size"], "url": url, "site": site,
        })
    return items


def save_dir(course_name: str, rel_path: str) -> Path:
    """保存先: <保存先>/<講義名>/Teams/<General からのフォルダ構成>/"""
    parts = [sanitize_filename(course_name, MAX_DIR_NAME_LENGTH), "Teams"]
    parts += [sanitize_filename(p, MAX_DIR_NAME_LENGTH) for p in rel_path.split("/")[:-1]]
    return OUTPUT_DIR.joinpath(*parts)


def download_file(context: BrowserContext, item: dict) -> Optional[Path]:
    """1ファイルを保存して保存先パスを返す。失敗時は None。

    更新のときは旧版を退避してから保存し直す。サイト上で名前変更・移動されていたら新しい名前・場所に保存する
    （旧版は元の場所に `_旧版` として残る）。
    """
    path = quote(item["url"].replace("'", "''"), safe="/")  # decodedurl なら名前の % や # もそのまま扱える
    api = f"{item['site']}/_api/web/GetFileByServerRelativePath(decodedurl='{path}')/$value"
    resp = context.request.get(api, timeout=300000)
    if not resp.ok:
        logger.error("ダウンロード失敗: %s (HTTP %s)", item["rel_path"], resp.status)
        return None
    body = resp.body()
    if item["size"] and len(body) != item["size"]:
        logger.error("ダウンロード失敗: %s（サイズ不一致 %d / %d）", item["rel_path"], len(body), item["size"])
        return None

    directory = save_dir(item["course_name"], item["rel_path"])
    directory.mkdir(parents=True, exist_ok=True)
    old = Path(item["local_path"]) if item.get("local_path") else None
    if item["reason"] == "UPDATED" and old and old.exists():
        archive_old_version(old)
        same_place = old.parent == directory and old.name == sanitize_filename(item["name"])
        dest = old if same_place else resolve_save_path(directory, item["name"])
    else:
        dest = resolve_save_path(directory, item["name"])
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(body)
    tmp.replace(dest)
    return dest


# ---------- 1サイクル ----------
def run_teams(context: BrowserContext, courses: List[dict], target_ids: set, summary: dict, *, dry_run: bool,
              scheduled: bool = False) -> None:
    """全履修講義の Teams ファイルの差分を調べ、対象講義(target_ids)のものを保存する。結果は summary に足す。

    summary["teams"]: 講義ごとの差分 / ["teams_notices"]: 対応するチームの変更 / ["teams_skipped"]: 大きくて保存しないファイル /
    ["teams_login_failed"], ["teams_recovered"]: ログインの失敗・復旧 / ["downloads"], ["pending"], ["errors"] にも反映。
    Teams 側の失敗で YCU-Board のチェックを止めない（エラーとして報告するだけ）。
    """
    summary["teams"] = []
    summary["teams_notices"] = []
    summary["teams_skipped"] = []
    delay = load_config().get("request_delay_seconds", 1.5)
    page = context.new_page()
    try:
        if not open_sharepoint(page):
            _login_failed(summary, dry_run=dry_run, scheduled=scheduled)
            return
        if not dry_run and load_state().get("teams_login_failed"):
            summary["teams_recovered"] = True
            update_state(teams_login_failed=False, teams_login_alert_sent=False)
        previous = load_state().get("teams_sites", {})
        sites = find_team_sites(page, courses, previous)
        if sites and not dry_run:
            update_state(teams_sites={**previous, **sites})
        for course in courses:
            info = sites.get(course["id"])
            if not info:
                logger.info("%s: Teams にチームが見つかりません（対象外）", course["name"])
                continue
            try:
                old = previous.get(course["id"]) or {}
                if old and old.get("site") != info["site"] and has_teams_records(course["id"]):
                    # 別のチームのファイルを「全部削除・全部新規」と通知しないよう、記録をやり直す（予行では一時DBだけが変わる）
                    logger.warning("%s: 対応するチームが変わりました: %s → %s", course["name"], old.get("team"), info["team"])
                    summary["teams_notices"].append(
                        f"{course['name']}: 対応するチームが「{old.get('team')}」から「{info['team']}」に変わりました。"
                        "新しいチームのファイルは初回登録として記録します（違っていれば --check-teams で確認してください）。")
                    reset_teams_course(course["id"])
                try:
                    items = list_files(page, info["site"])
                except FolderMissing:
                    if has_active_teams_files(course["id"]):
                        raise RuntimeError("チャンネル「一般」のフォルダが見つかりません（前回はファイルがありました）")
                    logger.info("%s [Teams]: チャンネル「一般」のフォルダがまだありません（ファイル0件）", course["name"])
                    items = []
                if not items and has_active_teams_files(course["id"]):
                    logger.warning("%s: Teams のファイルが0件として読み取られたため、読み直します。", course["name"])
                    time.sleep(delay)
                    items = list_files(page, info["site"])
                changes = apply_teams_scan(course["id"], course["name"], items)
                summary["teams"].append({"id": course["id"], "name": course["name"], "team": info["team"],
                                         "count": len(items), "changes": changes})
                logger.info("%s [Teams]: ファイル %d 件 / 新規 %d 更新 %d 削除 %d%s", course["name"], len(items),
                            len(changes.new), len(changes.updated), len(changes.removed),
                            "（初回登録）" if changes.baseline else "")
                if course["id"] in target_ids:
                    _download_pending(context, course, info["site"], summary, dry_run=dry_run)
                time.sleep(delay)
            except Exception as e:  # noqa: BLE001 - 1講義の失敗で全体を止めない
                logger.exception("Teams の確認に失敗: %s", course["name"])
                summary["errors"].append(f"{course['name']}（Teams）: {(str(e).splitlines() or [type(e).__name__])[0][:200]}")
    except Exception as e:  # noqa: BLE001
        logger.exception("Teams の確認に失敗しました")
        summary["errors"].append(f"Teams: {(str(e).splitlines() or [type(e).__name__])[0][:200]}")
    finally:
        page.close()


def _login_failed(summary: dict, *, dry_run: bool, scheduled: bool) -> None:
    """SharePoint にログインできなかった。結果メールに載せ、定期実行ならトースト通知も出す。

    「変更があったときだけメール」の設定では、復旧するまで最初の1回だけメールのきっかけにする（pipeline 側）。
    """
    summary["teams_login_failed"] = True
    summary["errors"].append(LOGIN_ERROR)
    if dry_run:
        return
    update_state(teams_login_failed=True)
    if scheduled:
        notify_teams_login_failed()


def is_large(item: dict) -> bool:
    return (item.get("size") or 0) > MAX_FILE_BYTES


def _download_pending(context: BrowserContext, course: dict, site: str, summary: dict, *, dry_run: bool) -> None:
    pending = pending_teams_downloads(course["id"])
    # 初回監査・予行の一覧表示用に、YCU-Board の資料と同じ形（course_name / material_title / file_name）にそろえる
    for p in pending:
        p["material_title"] = "/".join(["Teams", *p["rel_path"].split("/")[:-1]])
        p["file_name"] = p["rel_path"].split("/")[-1]
    for p in pending:
        if is_large(p):
            logger.info("%s [Teams]: 大きいため保存しません（%.0f MB）: %s", course["name"], p["size"] / 1e6, p["rel_path"])
            summary["teams_skipped"].append(p)
    pending = [p for p in pending if not is_large(p)]
    if not pending:
        return
    if dry_run:
        summary["pending"] += pending
        return
    logger.info("%s [Teams]: ダウンロード対象 %d 件", course["name"], len(pending))
    delay = load_config().get("request_delay_seconds", 1.5)
    for item in pending:
        try:
            item["name"] = item["file_name"]
            item["url"] = urlparse(site).path + "/" + LIBRARY_ROOT + "/" + item["rel_path"]
            item["site"] = site
            path = download_file(context, item)
            if path is None:
                summary["errors"].append(f"{course['name']} / {item['rel_path']}（Teams）: ダウンロード失敗")
                continue
            record_teams_download(course["id"], item["file_id"], str(path), compute_file_hash(path), item["modified"])
            summary["downloads"].append({**item, "path": str(path)})
            logger.info("保存: %s", path)
            notify_new_material(course["name"], item["material_title"], item["file_name"])
            time.sleep(delay)
        except Exception as e:  # noqa: BLE001
            logger.exception("ダウンロード失敗: %s", item["rel_path"])
            summary["errors"].append(f"{course['name']} / {item['rel_path']}（Teams）: {e}")


def local_date(modified: str) -> str:
    """サイトの更新日時（UTC の ISO 形式）→ この PC の時刻での日付（YYYY-MM-DD）。"""
    try:
        return datetime.fromisoformat(modified.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
    except (AttributeError, ValueError):
        return (modified or "")[:10]
