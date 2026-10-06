---
name: ycu-cloud
description: 保存した資料を、PC につないだクラウドドライブ（Google Drive for Desktop など）の指定フォルダにも、監査のたびに複製する設定を、学生と対話しながら行う。つながっているドライブを確認し、複製先フォルダを聞き、書き込みテストをして ON/OFF を反映する。「クラウドに保存したい」「Google Drive にも入れて」「Drive に保存」「/ycu-cloud」と言われたとき、または /ycu-config のクラウド保存の項目で使う。
---

手順の本体は、他のエージェントと共通の `.agents/skills/ycu-cloud/SKILL.md` にある。**そのファイルを最初から最後まで読み、書かれた手順と進め方のルールに従って実行すること。**

Claude Code では、選択を尋ねる場面で AskUserQuestion を使ってよい。承認操作が必要な場面では、学生にその操作を案内する。
