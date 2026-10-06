# 構成と実装メモ

## ファイル
| ファイル | 役割 |
|---|---|
| `src/main.py` | CLI、常駐ループ(`watch`)、`--init`、タスク登録 |
| `src/pipeline.py` | 1サイクル: ログイン → 講義ごとにスクレイプ・差分・ダウンロード → メール |
| `src/auth.py` | Microsoft SSO の自動ログイン(`ensure_logged_in`) |
| `src/crawler.py` | 時間割・講義ページの読み取り、ダウンロード、講義名の照合 |
| `src/diff_engine.py` | SQLite(`materials`)で差分判定とダウンロード要否の判定 |
| `src/downloader.py` | 保存先パスの決定、ファイル名のサニタイズ、旧版の退避 |
| `src/teams.py` | Teams（SharePoint）: サイトの特定・ファイル一覧・ダウンロード・Teams 分の1サイクル(`run_teams`) |
| `src/drive.py` | `output/` を別フォルダ（Google Drive の同期フォルダなど）へ複製（`mirror_to_drive`、サイクルの最後に `pipeline` が呼ぶ） |
| `src/mailer.py` | 結果メール(Outlook on the web / SMTP) |
| `src/notifier.py` | Windows トースト通知 |
| `src/config.py` | `config.json` の読み書き、認証情報の読み取り、メンテナンス判定 |

## サイトの構造（2026年10月時点で確認）
- 時間割: `/lms/timetable`。講義セルは `.timetable-course-top-btn.divTableCellHeader`、`id` が講義ID(`idnumber`)。学年見出しのボタン(`2026DS2026-3102` など)は `divTableCellHeader` が無いので除外している。
- 講義ページ: `/lms/course?idnumber=<講義ID>`。教材欄は `#materialList`。資料タイトルは `.block-wide label.bold-txt`、ファイル行は `.materialCss`（`id="material<resource_id>"`、`label.fileDownload` をクリックでダウンロード、`#dlMaterialId` が資料ID、`.course-view-material-update` が登録日）。
- テスト欄: `#examination .course-result-list`（タイトル `.course-view-examination-name` の href に `examinationId`、期間 `.course-view-examination-period`）。課題欄: `#reportList .sortReportBlock`（`input.reportId`、`.course-view-report-name`、`.course-view-report-time-start/end`、提出状況 `.course-view-report-status`）。読み取りは `crawler._SCRAPE_COURSEWORK_JS`、差分は `diff_engine.apply_coursework`（`coursework` テーブル）。
- YCU-Board のセッション Cookie はブラウザ再起動で消える。Microsoft 側のセッションは永続プロファイルに残るので、毎回 SSO を通れば承認なしで入れる。
- 画面構造が変わったらまず `crawler.py` の `_SCRAPE_MATERIALS_JS` とセレクタを疑う。

## Teams（SharePoint）の構造（2026年10月時点で確認）
- チームの実体は `https://yokohamacu.sharepoint.com/sites/<サイト>`。チャンネル「一般」の「共有済み」＝ `Shared Documents/General`。Teams の画面（`teams.cloud.microsoft`）は重く、サイトの表示まで数十秒〜2分かかるので使わない。
- サイトの特定: SharePoint の検索 API（`/_api/search/query?querytext='contentclass:STS_Site'`）で、権限のあるサイトの名前(Title)とURL(Path)が一覧で取れる。この一覧には参加していない**公開チーム**や昨年度のチームも含まれるので、`teams.course_matches_team`（区切りで分けた一部が年度表記を除いて講義名と完全一致）と年度の判定（`_years` / `current_school_year`）で絞る。前回のチームが候補にあれば使い続ける（勝手に切り替えない）。`find_team_sites` は保存せず、`run_teams` が予行以外のときに `data/state.json` の `teams_sites` に控える（検索が失敗したときの予備・前回のチームの判定に使う）。チームが変わったら `diff_engine.reset_teams_course` で記録をやり直す。
- 一覧とダウンロード: ログイン済みブラウザから REST API（`GetFolderByServerRelativePath(decodedurl=...)?$expand=Folders,Files`、`GetFileByServerRelativePath(decodedurl=...)/$value`）を直接呼ぶ。`decodedurl` 版は名前の `%`・`#` も扱える（`...ByServerRelativeUrl` は扱えない）。`General` 自体の 404 は `FolderMissing`（フォルダ未作成）。ファイルのキーは `UniqueId`、更新判定は `TimeLastModified` と `Length`、名前変更・移動は `rel_path` の変化。ダウンロードはサイズ照合のうえ `.part` に書いてから置き換える。`MAX_FILE_BYTES`（200 MB）超は保存しない（全体をメモリに読むため）。
- 録画は `Recordings` フォルダ（除外）。`*.loop` も除外。
- ログイン: SharePoint を開き、Microsoft のサインイン画面を `teams.open_sharepoint` が通す（アカウント選択 → メール → パスワード1回 → 承認待ち → 維持）。YCU-Board と同じ `data/auth_profile` を使うので、通常は承認不要。新しいサインイン画面は「次へ」ボタンのクリックでは進まず、Enter で送る必要があった。メール欄はパスワード画面でも DOM に残るので、画面の文言で段階を判定する。入れなかったときは `teams._login_failed`（`summary["teams_login_failed"]`、state の `teams_login_failed`、定期実行ならトースト）。メールを出すかの判定は `pipeline` 側で、お知らせ済み（`teams_login_alert_sent`）ならこのエラーを数えない。
- 差分は `diff_engine.apply_teams_scan`（`teams_files` テーブル。講義の初回は `scans` の `kind='teams'` で判断）。

## 実装上の注意
- 実行時刻の誤差は `schedule.planned_for`（予定ごとに1回だけ `draw_offset` で引いて state.json の `planned` に保存）。`last_due_slot` / `next_slot` / `--health` は誤差込みの予定時刻で判定する。起動時に「すでに予定を過ぎた枠」は処理済みとして扱い、補完との二重実行を避ける。
- DB 接続は `diff_engine.get_connection()`（コミットして必ず閉じる）を使う。`sqlite3.connect` の `with` だけでは閉じず、Windows ではファイルがロックされる。
- 差し替え（ID だけ変わった再アップロード）は、旧行の `local_path` を新しい行に引き継ぎ `downloaded_updated_on` を NULL にして「更新」として再取得させ、旧行は `removed=1` にする。
- `config.json` と `state.json` は `config.write_atomic` で書く（常駐が書き込み途中を読まないように）。
- 「その講義の教材／テスト／課題を初めて確認したか」は `scans` テーブルで判断する（行の有無では、最初は0件だった講義に後から出た最初の1件を見逃すため）。
- 差分のキーは `(course_id, resource_id)`。指紋は `(file_name, updated_on, material_title)`。
- ダウンロード要否は `pending_downloads`（DB の `local_path` / `downloaded_updated_on` と実ファイルの有無）で決める。差分検知とは独立しているので、途中で失敗しても次回に再試行される。
- `run_cycle` は講義ごとに例外を捕まえて続行し、エラーはレポートに載せる。
- 設定は `load_config()` で毎回読み直す。ただし `OUTPUT_DIR` などのパス定数は import 時に決まる（保存先の変更は次回起動から有効）。
- PowerShell に日本語パスを渡すときは `-EncodedCommand`（UTF-16LE の Base64）を使う。`-Command` だと文字化けする。
- Windows の cp932 コンソールで落ちないよう `main.py` で stdout/stderr を UTF-8 に再設定している。

## 動作確認の方法
- 画面を出さずにチェック: `python -m src.main --once --no-mail`
- 差分判定のテスト: `diff_engine.DB_PATH` を一時ファイルに差し替えて `apply_scan` / `pending_downloads` を呼ぶ（`diff_engine.scratch_db()` でも可。`--dry-run` はこれで本物の DB を守っている）。
- メール単体: `python -m src.mailer`（自分宛にテストメールが1通届く）
- 実運用の確認はアカウントに触れるので、利用者の了承を得てから行う。

## 既知の制約 / 今後
- テスト・課題は通知のみ（ダウンロード・提出はしない）。お知らせ・アンケート・掲示板は未対応。
- Windows 専用（トースト通知、タスクスケジューラ）。
- 学部・学年の違いでページ構造が異なる場合は未検証。
