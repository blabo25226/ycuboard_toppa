# ycuboard — エージェント向け概要

YCU-Board（横浜市立大学 LMS）の講義資料を定期チェックし、更新があれば自動ダウンロードしてメールで報告するツール。学内の誰でも `git clone` して自分のアカウントで使う前提。Python 3.10+ / Playwright / Windows。

## 読む順番
- 利用者のセットアップ・設定を手伝う → [setup-guide.md](setup-guide.md)。スキル `/set_env`（環境設定）と `/settings`（利用設定）が本体: [skills/set_env/SKILL.md](skills/set_env/SKILL.md)、[skills/settings/SKILL.md](skills/settings/SKILL.md)
- コマンド・設定・動作の仕様 → [usage.md](usage.md)
- 実装を直す・拡張する → [architecture.md](architecture.md)

## 守ること
- `id_password.txt`（ID/パスワード）、`smtp_password.txt`、`config.json`、`data/`（ログインプロファイル・履歴DB）、`output/`、`logs/` は個人情報。git に入れない（`.gitignore` 済み）。中身をログ・チャット・コミットに出さない。読む必要があるときも、行数や文字数など中身を出さない確認にとどめる。
- 対象は **利用者本人のアカウントの履修講義だけ**。他人のアカウントや、本人が履修していない講義にアクセスしない。
- サーバー負荷を避けるため `request_delay_seconds`（既定 1.5 秒）を短くしない。巡回頻度も1日数回に留める。
- 本人にしかできない操作（パスワード入力、Authenticator での承認）は代行せず、案内する。
- 設定変更は `config.json` を直接書き換えず、`python -m src.main --set-*` / `--*-on|off` を使う（検証が入る）。
- 永続的な変更（`--install-task` によるタスク登録、`--on` による自動ダウンロード有効化など）は、利用者の了承を得てから行う。
