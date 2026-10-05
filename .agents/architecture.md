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
| `src/mailer.py` | 結果メール(Outlook on the web / SMTP) |
| `src/notifier.py` | Windows トースト通知 |
| `src/config.py` | `config.json` の読み書き、認証情報の読み取り、メンテナンス判定 |

## サイトの構造（2026年10月時点で確認）
- 時間割: `/lms/timetable`。講義セルは `.timetable-course-top-btn.divTableCellHeader`、`id` が講義ID(`idnumber`)。学年見出しのボタン(`2026DS2026-3102` など)は `divTableCellHeader` が無いので除外している。
- 講義ページ: `/lms/course?idnumber=<講義ID>`。教材欄は `#materialList`。資料タイトルは `.block-wide label.bold-txt`、ファイル行は `.materialCss`（`id="material<resource_id>"`、`label.fileDownload` をクリックでダウンロード、`#dlMaterialId` が資料ID、`.course-view-material-update` が登録日）。
- YCU-Board のセッション Cookie はブラウザ再起動で消える。Microsoft 側のセッションは永続プロファイルに残るので、毎回 SSO を通れば承認なしで入れる。
- 画面構造が変わったらまず `crawler.py` の `_SCRAPE_MATERIALS_JS` とセレクタを疑う。

## 実装上の注意
- DB 接続は `diff_engine.get_connection()`（コミットして必ず閉じる）を使う。`sqlite3.connect` の `with` だけでは閉じず、Windows ではファイルがロックされる。
- 差し替え（ID だけ変わった再アップロード）は、旧行の `local_path` を新しい行に引き継ぎ `downloaded_updated_on` を NULL にして「更新」として再取得させ、旧行は `removed=1` にする。
- `config.json` と `state.json` は `config.write_atomic` で書く（常駐が書き込み途中を読まないように）。
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
- テスト・課題（提出物）は未対応。
- Windows 専用（トースト通知、タスクスケジューラ）。
- 学部・学年の違いでページ構造が異なる場合は未検証。
