# 利用者のセットアップを手伝う

セットアップと運用は3つのスキルで進める。**本体は `skills/` にある。** 利用者に「セットアップして」「設定したい」と頼まれたら、該当するスキルを読んで、その手順どおりに進める。

| スキル | 内容 | ファイル |
|---|---|---|
| `/ycu-setup` | 環境設定: Python・依存、ID/パスワード、ログイン（初回/2回目）、メール、履修講義の確認 | [skills/ycu-setup/SKILL.md](skills/ycu-setup/SKILL.md) |
| `/ycu-resume` | 復旧: 止まった定期監査の診断、ログイン復旧、常駐の起動、取りこぼしの補完 | [skills/ycu-resume/SKILL.md](skills/ycu-resume/SKILL.md) |
| `/ycu-config` | 利用設定: 定期監査の ON/OFF と回数・時刻、講義ごとの自動ダウンロード、結果メール | [skills/ycu-config/SKILL.md](skills/ycu-config/SKILL.md) |

- 定期監査が止まった／動いていない → `/ycu-resume`（診断 → ログイン復旧・常駐の起動・取りこぼしの補完）。
- 初めて使う利用者 → `/ycu-setup`（最後に `/ycu-config` へ続く）。
- 設定だけ変えたい → `/ycu-config`。
- Claude Code では `.claude/skills/` から同名のスキルとして呼べる。他のエージェントは `skills/*/SKILL.md` を直接読む。

## 診断・確認用コマンド（スキルが使う）
| コマンド | 内容 |
|---|---|
| `--doctor` | OS / Python / Playwright / 認証情報 / ログイン履歴 / 自動起動の有無を診断（秘密の値は出さない） |
| `--check-login` | 保存済みセッションだけで自動ログインできるか（ブラウザ非表示・人の操作なし） |
| `--login` | ブラウザを表示してログイン（初回・セッション切れ。Authenticator の承認は本人が行う） |
| `--test-mail` | 自分宛にテストメールを1通送る（ログイン済みが前提） |
| `--list-courses` | 履修講義の一覧 |
| `--status` | 現在の設定 |

## よくある失敗
| 症状 | 原因と対処 |
|---|---|
| `--login` が承認待ちで止まる | 利用者が Authenticator で承認していない。画面の番号を伝える |
| `--check-login` が NG | セッション切れ。`--login` を実行してから再確認 |
| ログイン後すぐ `/login` に戻される | YCU-Board のセッションはブラウザ再起動で切れるが、通常は毎回 SSO を通る。繰り返すなら `data/auth_profile` を削除して `--login` |
| 講義が0件 / 足りない | 時間割が空、反映待ち、またはページ構造の変更。`crawler.py` の `.timetable-course-top-btn` を確認 |
| テストメールが届かない | ログイン済みか（`--check-login`）、迷惑メール・「その他」タブ、ログ（`logs/ycuboard.log`）を確認 |
| `playwright` が無い | `pip install -r requirements.txt` と `playwright install chromium` |
