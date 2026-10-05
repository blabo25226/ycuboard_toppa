# YCU-Board 資料チェック＆自動ダウンロード

YCU-Board（横浜市立大学 LMS）に自動ログインして履修講義の「教材」を定期チェックし、更新があれば指定した講義の資料を保存、結果をメールで報告します。

## 準備

1. `id_password.txt` を作成する（`.gitignore` 済み）。

   ```
   id
   a111111a@yokohama-cu.ac.jp
   パスワード
   ```
   1行目は見出し、2行目がメールアドレス（結果メールの宛先にも使います）、3行目がパスワードです。
2. 依存をインストールする: `pip install playwright` と `playwright install chromium`
3. 初回ログイン: `python -m src.main --login`
   ブラウザが開き、ID・パスワードが自動入力されます。Authenticator の承認が求められたらスマホで承認してください。以降は Microsoft のセッションが残るので承認なしで自動ログインされます（切れたら再度 `--login`）。

## 使い方

| やりたいこと | コマンド |
|---|---|
| 今すぐ1回チェックする | `python -m src.main --once` |
| 今すぐチェック＋対象講義の資料を保存 | `python -m src.main --once --with-download` |
| 講義を指定して実行 | `python -m src.main --once --with-download --course 統計モデリング1` |
| メールを送らず試す | `... --no-mail` |
| 設定時刻に自動実行（常駐） | `python -m src.main --watch` |
| 履修講義の一覧 | `python -m src.main --list-courses` |
| 現在の設定 | `python -m src.main --status` |

`--once` は ON/OFF 設定に関係なく実行されます。`--watch` は設定を毎回読み直すので、常駐中に ON/OFF や時刻を変えても反映されます。

### ON / OFF（初期値: チェック OFF・ダウンロード OFF・メール ON）

```
python -m src.main --check-on / --check-off       # 定期チェック
python -m src.main --download-on / --download-off # 更新資料の自動ダウンロード
python -m src.main --mail-on / --mail-off         # 結果メール
python -m src.main --on / --off                   # チェックとダウンロードをまとめて
```
ダウンロードはチェック結果の差分を見て動くため、定期実行では「チェック ON」が前提です。

### 巡回時刻・対象講義・保存先

```
python -m src.main --set-times 11:00 17:00 21:30   # 時刻と回数を自由に設定（初期: 11:00, 17:00）
python -m src.main --set-courses 統計モデリング1 機械学習   # ダウンロード対象（部分一致。指定なしで全講義）
python -m src.main --set-output D:\講義資料          # 保存先（初期: output/）
python -m src.main --set-mail-to someone@example.com # メール宛先（初期: id_password.txt のアドレス）
```
講義名は「統計モデリングI」のようにローマ数字でも `1` でも一致します。設定は `config.json` に保存され、直接編集もできます。

## 動作

1. 時間割から履修講義を取得し、各講義の「教材」欄のファイル一覧（資料タイトル・ファイル名・登録日・ID）を読み取る。
2. 前回の記録（`data/history.db`）と比較して差分を判定する。
   - **新規**: 新しいファイルが追加された
   - **更新/差し替え**: 登録日やファイル名が変わった、または同じ資料でファイルが差し替えられた
   - **削除**: サイトから消えた（ローカルのファイルは残す）
   - 講義を初めて見たときは「初回登録」として記録だけ行い、差分通知はしません。
3. 対象講義で未保存・更新された資料を `<保存先>/<講義名>/<資料タイトル>/` に保存。更新時は旧版を `_旧版日時` を付けて残します。
4. 結果（差分・保存したファイル・エラー）をメールで送信。Windows のトースト通知も出ます。

毎週火曜 1:00〜6:00 はサイトのメンテナンスのため巡回しません。PC が起動していない時刻の巡回は、起動後 2 時間以内なら実行されます。

## メール送信について

学内の Microsoft 365 アカウントは SMTP の基本認証が使えないため、既定ではログイン済みブラウザで Outlook on the web を開いて送信します（追加設定不要）。アプリパスワード等を使う場合は `config.json` の `email.method` を `smtp` にし、`smtp_password.txt` にパスワードを書いてください。

## 構成

`src/main.py`（CLI・スケジューラ）/ `pipeline.py`（1サイクル）/ `auth.py`（自動ログイン）/ `crawler.py`（画面の読み取り）/ `diff_engine.py`（差分判定）/ `downloader.py`（保存先）/ `mailer.py`（メール）/ `notifier.py`（トースト）/ `config.py`（設定）

現在の対象は「教材」のみです。テストと課題（提出物）は未対応です。
