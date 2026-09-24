# 人間側

`https://ycuboard.yokohama-cu.ac.jp/`へログインするために必要なメールアドレス(a111111a@yokohama-cu.ac.jp)とそのパスワードを`id_password.txt`を作成し、記入する。

# AI側

## YCU-Board の初回ログイン（共有認証）

1. `https://ycuboard.yokohama-cu.ac.jp/` を開くと `/login` に移る。入口は二つある。上の「Click here for shared authentication」の **Login** が共有認証。下のゲストログインは使わない。
2. Login は `/oauth2/authorization` へ飛び、Microsoft のサインイン画面（`login.microsoftonline.com`）に着く。
3. メール欄に `id_password.txt` の ID（`d244006a@yokohama-cu.ac.jp`）を入れて **Next** を押す。
4. パスワード欄に同じファイルのパスワードを入れて **Sign in** を押す。
5. 「Approve sign in request」が出る。Outlook モバイルアプリを開き、画面の番号を入力して承認する。ここは人が操作する。
6. 「Stay signed in?」では「Don't show this again」にチェックを入れ、**Yes** を押す。次回から同じブラウザではサインインを聞かれにくくするため。
7. `https://ycuboard.yokohama-cu.ac.jp/portal/home` のホームが開けば完了。お知らせ、LMS、課題・テスト、講義検索などが使える。

## YCU-Board の二回目以降

初回で「Stay signed in?」に **Yes** 済みで、同じブラウザにサインインが残っている場合。

1. 開いていた YCU-Board のタブを閉じ、タブが残っていないことを確認する。
2. `https://ycuboard.yokohama-cu.ac.jp/` を再度開く。
3. ログイン画面や Microsoft のサインインは出ず、`https://ycuboard.yokohama-cu.ac.jp/portal/home` のホームがそのまま開く。
4. ホームが開かずログイン画面に戻ったときは、サインインが切れている。初回ログインの手順からやり直す。
