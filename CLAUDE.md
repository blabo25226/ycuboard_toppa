# ycuboard_突破

YCU-Board（横浜市立大学 LMS）の講義資料を定期チェックし、更新があれば自動ダウンロードしてメールで報告するツール。Python 3.12 + Playwright。

## 構成
- `src/main.py` — CLI と常駐スケジューラ（`python -m src.main --help`）
- `src/pipeline.py` — 1回ぶんの巡回（ログイン → 差分チェック → ダウンロード → メール）
- `src/auth.py` — Microsoft SSO の自動ログイン（`id_password.txt` 使用）
- `src/crawler.py` — 時間割・講義ページの読み取り、ファイルダウンロード
- `src/diff_engine.py` — SQLite（`data/history.db`）で前回との差分とDL要否を判定
- `src/mailer.py` — 結果メール（既定は Outlook on the web 経由。SMTP 基本認証は学内アカウントで不可）
- `src/config.py` / `config.json` — 設定（ON/OFF、巡回時刻、対象講義、保存先）

## 注意
- `id_password.txt`（1行目: 見出し、2行目: メールアドレス、3行目: パスワード）、`data/`、`output/` は git 管理外。内容をログやコミットに出さない。
- YCU-Board のセッションはブラウザ再起動で切れるため、実行のたびに `ensure_logged_in` を通す。
- 講義名は「統計モデリングI」のようにローマ数字。`--course 統計モデリング1` のように算用数字でも一致する（`normalize_course_name`）。
- 毎週火曜 1:00〜6:00 はサイトのメンテナンスで巡回しない。
- 現在の対象は「教材」欄のみ。テスト・課題（提出物）は未対応。
- 動作確認: `python -m src.main --once --no-mail`（メールを送らずにチェック）。
