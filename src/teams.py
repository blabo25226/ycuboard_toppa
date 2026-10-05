"""Teams（SharePoint）の講義資料を取得する。

履修講義ごとのチームの実体は SharePoint サイトで、チャンネル「一般」の「共有済み」は
そのサイトの Shared Documents/General フォルダ。Teams の画面は重いので使わず、
ログイン済みブラウザから SharePoint の REST API を直接呼んで一覧とダウンロードを行う。
"""
import logging
import re
import time
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
    pending_teams_downloads,
    record_teams_download,
)
from src.downloader import MAX_DIR_NAME_LENGTH, archive_old_version, resolve_save_path, sanitize_filename
from src.notifier import notify_login_required, notify_new_material
from src.state import load_state, update_state

logger = logging.getLogger(__name__)

SP_HOST = "https://yokohamacu.sharepoint.com"
LIBRARY_ROOT = "Shared Documents/General"  # チャンネル「一般」の「共有済み」
EXCLUDED_FOLDERS = {"recordings"}  # 会議の録画は大きすぎるので対象外
EXCLUDED_SUFFIXES = (".loop",)  # 会議チャットから自動で作られる Loop の部品。講義資料ではない
MAX_DEPTH = 6

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
  const enc = p => encodeURI(p.replace(/'/g, "''")).replace(/#/g, '%23').replace(/\\?/g, '%3F');
  async function walk(path, depth) {
    const q = `${site}/_api/web/GetFolderByServerRelativeUrl('${enc(path)}')?$expand=Folders,Files`
      + `&$select=Folders/Name,Folders/ServerRelativeUrl,Files/Name,Files/UniqueId,Files/TimeLastModified,Files/Length,Files/ServerRelativeUrl`;
    const r = await fetch(q, {headers: {Accept: 'application/json;odata=nometadata'}, credentials: 'include'});
    if (!r.ok) { out.push({err: r.status, path}); return; }
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
def find_team_sites(page: Page, courses: List[dict]) -> Dict[str, dict]:
    """履修講義 → Teams のチーム（SharePoint サイト）。チーム名に講義名が含まれるものを対応づける。

    一致したものは data/state.json に控え、検索結果から漏れたときの予備にする。
    """
    rows = page.evaluate(_SEARCH_SITES_JS)
    cache = load_state().get("teams_sites", {})
    if isinstance(rows, dict):
        logger.warning("チーム一覧を取得できませんでした（HTTP %s）。前回の対応表を使います。", rows.get("err"))
        rows = []
    sites = [
        {"title": r.get("Title") or "", "path": r.get("Path") or ""}
        for r in rows
        if (r.get("Path") or "").startswith(SP_HOST + "/sites/")
    ]
    found: Dict[str, dict] = {}
    for course in courses:
        key = normalize_course_name(course["name"])
        cands = [s for s in sites if key and key in normalize_course_name(s["title"])]
        if len(cands) > 1:
            # 複数あるときは、チーム名の区切り（_）が講義名と完全一致するもの、今年度らしい名前（R8 / 2026）、短い名前の順に優先する
            def rank(s):
                segments = [normalize_course_name(x) for x in s["title"].split("_")]
                return (key not in segments and normalize_course_name(s["title"].removeprefix("2026年度")) != key,
                        not re.search(r"R8|2026", s["title"]), len(s["title"]))
            cands.sort(key=rank)
            logger.info("%s: 該当するチームが複数あります。先頭を使います: %s", course["name"], [c["title"] for c in cands])
        if cands:
            found[course["id"]] = {"site": cands[0]["path"], "team": cands[0]["title"]}
        elif course["id"] in cache and not sites:
            found[course["id"]] = cache[course["id"]]
    if found:
        update_state(teams_sites={**cache, **found})
    return found


def list_files(page: Page, site: str) -> List[dict]:
    """サイトの General 配下（録画フォルダを除く）の全ファイル → [{file_id, rel_path, name, modified, size, url}]"""
    root = urlparse(site).path + "/" + LIBRARY_ROOT
    rows = page.evaluate(_LIST_FILES_JS, [site, root, MAX_DEPTH, sorted(EXCLUDED_FOLDERS)])
    errors = [r for r in rows if "err" in r]
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
    """1ファイルを保存して保存先パスを返す。失敗時は None。更新のときは旧版を退避してから保存し直す。"""
    api = f"{item['site']}/_api/web/GetFileByServerRelativeUrl('{quote(item['url'].replace(chr(39), chr(39) * 2), safe='/')}')/$value"
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
        dest = old
    else:
        dest = resolve_save_path(directory, item["name"])
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(body)
    tmp.replace(dest)
    return dest


# ---------- 1サイクル ----------
def run_teams(context: BrowserContext, courses: List[dict], target_ids: set, summary: dict, *, dry_run: bool) -> None:
    """全履修講義の Teams ファイルの差分を調べ、対象講義(target_ids)のものを保存する。結果は summary に足す。

    summary["teams"]: 講義ごとの差分 / summary["downloads"], ["pending"], ["errors"] にも反映。
    Teams 側の失敗で YCU-Board のチェックを止めない（エラーとして報告するだけ）。
    """
    summary["teams"] = []
    delay = load_config().get("request_delay_seconds", 1.5)
    page = context.new_page()
    try:
        if not open_sharepoint(page):
            summary["errors"].append("Teams: SharePoint にログインできませんでした")
            return
        sites = find_team_sites(page, courses)
        for course in courses:
            info = sites.get(course["id"])
            if not info:
                logger.info("%s: Teams にチームが見つかりません（対象外）", course["name"])
                continue
            try:
                items = list_files(page, info["site"])
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
                    _download_pending(context, course, summary, dry_run=dry_run)
                time.sleep(delay)
            except Exception as e:  # noqa: BLE001 - 1講義の失敗で全体を止めない
                logger.exception("Teams の確認に失敗: %s", course["name"])
                summary["errors"].append(f"{course['name']}（Teams）: {(str(e).splitlines() or [type(e).__name__])[0][:200]}")
    except Exception as e:  # noqa: BLE001
        logger.exception("Teams の確認に失敗しました")
        summary["errors"].append(f"Teams: {(str(e).splitlines() or [type(e).__name__])[0][:200]}")
    finally:
        page.close()


def _download_pending(context: BrowserContext, course: dict, summary: dict, *, dry_run: bool) -> None:
    pending = pending_teams_downloads(course["id"])
    if not pending:
        return
    # 初回監査・予行の一覧表示用に、YCU-Board の資料と同じ形（course_name / material_title / file_name）にそろえる
    for p in pending:
        p["material_title"] = "/".join(["Teams", *p["rel_path"].split("/")[:-1]])
        p["file_name"] = p["rel_path"].split("/")[-1]
    if dry_run:
        summary["pending"] += pending
        return
    logger.info("%s [Teams]: ダウンロード対象 %d 件", course["name"], len(pending))
    delay = load_config().get("request_delay_seconds", 1.5)
    for item in pending:
        try:
            item["name"] = item["file_name"]
            item["url"] = _server_url(item)
            item["site"] = _site_of(course["id"])
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


def _site_of(course_id: str) -> str:
    return load_state().get("teams_sites", {})[course_id]["site"]


def _server_url(item: dict) -> str:
    """DB の相対パスから、サーバー上のパスを組み立てる。"""
    site = _site_of(item["course_id"])
    return urlparse(site).path + "/" + LIBRARY_ROOT + "/" + item["rel_path"]
