---
name: ycu-setup
description: YCU-Board 資料チェッカーの環境設定を、学生と対話しながら1ステップずつ行い確認する。Python、ID/パスワード、ログイン（初回/2回目）、Teams のログイン、メール、履修講義を確認し、最後に /ycu-config へ進む。「セットアップして」「環境設定」「/ycu-setup」と言われたとき、または初めて使うときに使う。
---

手順の本体は、他のエージェントと共通の `.agents/skills/ycu-setup/SKILL.md` にある。**そのファイルを最初から最後まで読み、書かれた手順と進め方のルールに従って実行すること。**

Claude Code では、選択を尋ねる場面で AskUserQuestion を使ってよい。パスワードの入力が必要な場面では、学生に `! python -m src.main --init` と入力してもらう。
