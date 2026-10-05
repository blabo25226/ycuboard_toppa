# 利用者のセットアップを手伝う手順

利用者に「セットアップして」と頼まれたら、この順で進める。各ステップの結果を確認してから次へ。リポジトリのルートで実行する。
パスワードは利用者本人が入力する（チャットに書かせない）。

## 0. 前提の確認
- `python --version` が 3.10 以上、`git --version` が通ること。無ければインストールを案内する（Python は PATH に追加するオプションが必要）。
- OS は Windows。それ以外は未対応（トースト通知・タスク登録が Windows 専用）。

## 1. 依存のインストール
```powershell
pip install -r requirements.txt
playwright install chromium
```

## 2. 認証情報
- 既に `id_password.txt` があるか確認する（中身は表示しない。`Test-Path` と行数程度）。
- 無ければ、利用者に **自分のターミナルで** `python -m src.main --init` を実行してもらう（ID は `@yokohama-cu.ac.jp` より前だけでよい。パスワードは非表示入力）。エージェントのシェルは入力できないことが多い。
- 形式は4行: `id` / ID / `password` / パスワード。ID にドメイン付きのメールアドレスを書いても可。

## 3. ログイン（初回）
```powershell
python -m src.main --login
```
ブラウザが開き自動サインインする。Authenticator の承認画面が出たら、画面に出ている番号を **利用者がスマホで入力** する。終了時に「ログインに成功しました」と出る。失敗したら、タイムアウト（3分）か ID/パスワード違いを疑う。

## 4. 動作確認
```powershell
python -m src.main --list-courses
python -m src.main --once --no-mail
```
履修講義が出ること、エラーが無いことを確認する。講義が0件なら、時間割に講義が登録されているか利用者に確認する（時間割ページは現在の学期を表示する）。

## 5. 利用者に決めてもらうこと
- **チェック時刻**: 初期は 11:00 / 17:00。変えるなら `--set-times 9:00 13:00 21:00`。
- **ダウンロードする講義**: `--list-courses` の一覧から選んでもらい `--set-courses 講義名1 講義名2`。**未指定のまま `--download-on` にすると全講義を保存する**ので、必ず確認する。
- **保存先**: 初期は `output/`。変えるなら `--set-output フォルダ`。
- **結果メール**: 初期 ON、宛先は本人の学内アドレス。不要なら `--mail-off`。

## 6. 自動実行を有効にする（了承を得てから）
```powershell
python -m src.main --on             # チェック+自動ダウンロードを ON
python -m src.main --install-task   # ログオン時に --watch を自動起動
Start-ScheduledTask -TaskName YCUBoardWatcher   # 今すぐ開始
python -m src.main --status
```
- `--on` は定期チェックと自動ダウンロードの両方を ON にする。チェックだけなら `--check-on`。
- 常駐は PC がオンでログオン中のみ。ログは `logs/ycuboard.log`。解除は `--uninstall-task`。

## 7. 最終確認
`--status` の内容を利用者に伝え、次を案内して終わる: 設定の変え方（README 参照）、`--login` のやり直し方、`logs/ycuboard.log` の場所。

## よくある失敗
| 症状 | 原因と対処 |
|---|---|
| `--login` が承認待ちで止まる | 利用者が Authenticator で承認していない。番号を伝える |
| ログイン後すぐ `/login` に戻される | YCU-Board のセッションはブラウザ再起動で切れる。通常は `ensure_logged_in` が毎回 SSO を通る。繰り返すなら `data/auth_profile` を削除して `--login` |
| 講義が0件 | 時間割が空、またはページ構造の変更。`crawler.py` の `.timetable-course-top-btn` を確認 |
| メールが届かない | `logs/ycuboard.log` の「メール送信に失敗」を確認。Outlook on the web に入れるか（`--login` 済みか）を確認 |
| `playwright` が無い | `pip install -r requirements.txt` と `playwright install chromium` |
