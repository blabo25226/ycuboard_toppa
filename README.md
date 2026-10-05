# YCU-Board 資料チェッカー

YCU-Board（横浜市立大学 LMS）に自動でログインし、履修している講義の「教材」を決まった時刻にチェックします。更新があれば資料を自動で保存し、結果を自分のメールに送ります。

- 対象は **自分のアカウントの履修講義だけ**です。履修講義は時間割から自動で取得するので、講義ごとの設定は要りません。
- 認証情報（ID・パスワード）は **自分のPCの中だけ**に保存されます。外部には送りません。
- 非公式のツールです。自分のアカウントで、自己責任で使ってください。大学のサーバーに負荷をかけないよう、アクセスの間隔をあけています。

## 必要なもの

- Windows 10 / 11
- [Python 3.10 以上](https://www.python.org/downloads/)（インストール時に **Add python.exe to PATH** にチェック）
- [Git](https://git-scm.com/)

## セットアップ

PowerShell を開いて、順番に実行します。

### 1. ダウンロードして準備する

```powershell
git clone <このリポジトリのURL>
cd <クローンしたフォルダ>
pip install -r requirements.txt
playwright install chromium
```

### 2. ID とパスワードを登録する

```powershell
python -m src.main --init
```

聞かれたら次を入力します。

- **ID**: メールアドレスの `@` より前の部分（例: `d123456a`）。`@yokohama-cu.ac.jp` は自動で補われます。
- **パスワード**: YCU のパスワード（入力は画面に表示されません）。

`id_password.txt` が作られます。手で作る場合は、次の4行で書きます。

```text
id
d123456a
password
あなたのパスワード
```

このファイルは `.gitignore` に入っているので commit されません。**他人に見せたり、共有したりしないでください。**

### 3. ログインする（初回だけ）

```powershell
python -m src.main --login
```

ブラウザが開いて、自動でサインインします。Microsoft Authenticator の承認を求められたら、画面の番号をスマホのアプリに入力してください。サインイン状態は保存され、次回からは承認なしで自動ログインされます。

### 4. 動作を確認する

```powershell
python -m src.main --list-courses    # 履修講義が表示されればOK
python -m src.main --once            # 今すぐ1回チェック（ダウンロードはしない）
```

`--once` を実行すると、結果のメールが自分のアドレスに届きます。初回は各講義の資料を記録するだけで、「新規」とは扱われません。

### 5. 自動で動かす

```powershell
python -m src.main --on              # 定期チェックと自動ダウンロードを有効にする
python -m src.main --install-task    # Windows にログインしたら自動で常駐するように登録する
```

これで、設定した時刻（初期は 11:00 と 17:00）に自動でチェックされます。次回の Windows ログオンから常駐します。**今すぐ始めたい場合は**、次を実行してください。

```powershell
Start-ScheduledTask -TaskName YCUBoardWatcher
```

PC の電源が入っていてログオンしている間だけ動きます。スリープ中は動きません。

## 設定を変える

```powershell
python -m src.main --status                                  # 現在の設定を見る
python -m src.main --set-times 9:00 13:00 21:00              # チェックする時刻（回数も自由）
python -m src.main --set-courses 機械学習 統計モデリング1      # 資料を保存する講義を絞る（部分一致）
python -m src.main --set-courses                             # 絞り込みを解除して全講義にする
python -m src.main --set-output D:\講義資料                   # 保存先フォルダ（初期は output/）
python -m src.main --download-off                            # 資料の保存だけ止める（チェックは続ける）
python -m src.main --mail-off                                # メールを止める
python -m src.main --off                                     # チェックと保存をまとめて止める
```

**資料を保存する講義を絞らないと、全講義の資料が保存されます。** 履修講義が多い場合は `--set-courses` で絞るのがおすすめです。

## 困ったとき

| 症状 | 対処 |
|---|---|
| 「ログインできませんでした」と出る | `python -m src.main --login` をもう一度実行する |
| 履修講義が表示されない | `--login` でログインし直す。時間割に講義が登録されているかも確認する |
| 自動実行されていない | `logs/ycuboard.log` を確認する。`--status` で「定期チェック」が ON か確認する |
| 自動起動をやめたい | `python -m src.main --uninstall-task` |
| 毎週火曜の深夜に動かない | YCU-Board のメンテナンス時間（火曜 1:00〜6:00）のため、チェックしません |

## AI エージェントに任せる場合

Claude Code などのエージェントに「`.agents/setup-guide.md` を読んで、私のセットアップを手伝って」と頼むこともできます。パスワードの入力など、本人が行う手順はエージェントが案内します。

詳しい仕様・コマンド・構成は [`.agents/`](.agents/) にあります。
