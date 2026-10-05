import logging
import time
from urllib.parse import urlparse
from playwright.sync_api import Page

from src.config import get_credentials
from src.notifier import notify_login_required

logger = logging.getLogger(__name__)


def is_logged_in(page: Page) -> bool:
    """現在のページがYCU-Boardポータルホーム（ログイン済み）か厳密に判定"""
    try:
        parsed = urlparse(page.url)
        # Microsoft認証画面にいる間は絶対に未ログイン
        if "microsoft" in parsed.netloc.lower():
            return False
        # ドメインが ycuboard.yokohama-cu.ac.jp かつ パスが /portal/home
        if parsed.netloc == "ycuboard.yokohama-cu.ac.jp" and parsed.path.startswith("/portal/home"):
            return True
    except Exception:
        pass
    return False


def ensure_logged_in(page: Page, timeout_seconds: int = 60) -> bool:
    """
    YCU-Boardにアクセスし、ログイン状態を保証する。
    未ログインの場合は共通認証からMicrosoft SSO画面を自動で突破する（アカウント選択、KMSI等）。
    """
    email, password = get_credentials()
    logger.info("YCU-Board へのアクセスとログイン状態を確認中...")

    # 1. YCU-Board トップへ移動
    if not page.url.startswith("https://ycuboard.yokohama-cu.ac.jp"):
        page.goto("https://ycuboard.yokohama-cu.ac.jp/", wait_until="domcontentloaded")
        time.sleep(1.5)

    if is_logged_in(page):
        logger.info("[成功] 既にログイン済み（ポータルホーム）です。")
        return True

    # 2. 共通認証ボタンをクリック
    common_auth_btn = page.locator("a[href*='/oauth2/authorization'], a.login-btn:has-text('ログイン')")
    if common_auth_btn.count() > 0:
        logger.info("共通認証ボタン（/oauth2/authorization）をクリックします...")
        try:
            common_auth_btn.first.click()
            time.sleep(2.0)
        except Exception as e:
            logger.debug(f"共通認証ボタンのクリック例外: {e}")

    start_time = time.time()
    notified_2fa = False
    password_submitted = False  # 誤ったパスワードを繰り返し送るとアカウントがロックされるため、送信は1回だけ

    while time.time() - start_time < timeout_seconds:
        if is_logged_in(page):
            logger.info("[成功] ポータルホームへのアクセスを確認しました！")
            return True

        cur_url = page.url.lower()

        # Microsoft 認証画面での自動ハンドリング
        if "login.microsoftonline.com" in cur_url:
            # A. 保存済みアカウントの選択タイル
            try:
                account_tile = page.locator(
                    f"div[data-test-id*='{email}'], small:has-text('{email}'), div.table-row:has-text('{email.split('@')[0]}'), div[role='button']:has-text('{email.split('@')[0]}')"
                )
                if account_tile.count() > 0 and account_tile.first.is_visible():
                    logger.info("アカウント選択画面を検出: 保存済みアカウントをクリックします。")
                    account_tile.first.click()
                    time.sleep(2.0)
                    continue
            except Exception:
                pass

            # B. メールアドレス入力画面
            try:
                email_input = page.locator("input[type='email'], input[name='loginfmt'], #i0116")
                if email_input.count() > 0 and email_input.first.is_visible():
                    logger.info(f"メールアドレス入力画面を検出: {email[:4]}*** を入力します。")
                    email_input.first.fill(email)
                    time.sleep(0.5)
                    next_btn = page.locator("input[type='submit'], #idSIButton9, button[type='submit']")
                    if next_btn.count() > 0:
                        next_btn.first.click()
                    time.sleep(2.0)
                    continue
            except Exception:
                pass

            # C. パスワード入力画面
            try:
                passwd_input = page.locator("input[type='password'], input[name='passwd'], #i0118")
                if passwd_input.count() > 0 and passwd_input.first.is_visible():
                    if password_submitted:
                        error = page.locator("#passwordError, #i0118Error, [role='alert']")
                        if error.count() > 0 and error.first.is_visible():
                            logger.error("パスワードが違います。id_password.txt を確認してください（ロックを避けるため再送信しません）。")
                            return False
                        time.sleep(1.5)  # 画面遷移の途中。再送信はしない
                        continue
                    logger.info("パスワード入力画面を検出: パスワードを入力します。")
                    password_submitted = True
                    passwd_input.first.fill(password)
                    time.sleep(0.5)
                    signin_btn = page.locator("input[type='submit'], #idSIButton9, button[type='submit']")
                    if signin_btn.count() > 0:
                        signin_btn.first.click()
                    time.sleep(2.5)
                    continue
            except Exception:
                pass

            # D. 「サインインの状態を維持しますか？」(KMSI) 画面
            try:
                kmsi_box = page.locator("input[name='DontShowAgain'], #KmsiCheckboxField")
                yes_btn = page.locator("input[type='submit'], #idSIButton9, button:has-text('Yes'), input[value='Yes']")
                if (kmsi_box.count() > 0 and kmsi_box.first.is_visible()) or (yes_btn.count() > 0 and yes_btn.first.is_visible()):
                    logger.info("「サインイン状態を維持しますか？」を検出: 「Yes」を選択します。")
                    try:
                        if kmsi_box.count() > 0 and not kmsi_box.first.is_checked():
                            kmsi_box.first.check()
                    except Exception:
                        pass
                    if yes_btn.count() > 0:
                        yes_btn.first.click()
                    time.sleep(3.0)
                    continue
            except Exception:
                pass

            # E. Microsoft Authenticator 2段階認証 (2FA) 画面
            try:
                num_elem = page.locator("#displaySign, div[data-bind*='displaySign'], .displaySign")
                if num_elem.count() > 0 and num_elem.first.is_visible():
                    code = num_elem.first.inner_text().strip()
                    if not notified_2fa:
                        notify_login_required()
                        print("\n" + "=" * 60)
                        print("[要操作] Microsoft Authenticator によるサインイン承認が必要です。")
                        if code:
                            print(f"  >>> 画面に表示されている承認番号: [ {code} ] <<<")
                        print("  スマホのアプリで番号を入力して承認してください。")
                        print("=" * 60 + "\n")
                        notified_2fa = True
            except Exception:
                pass

        time.sleep(1.5)

    return is_logged_in(page)


def perform_full_login(page: Page, timeout_seconds: int = 180):
    """
    初回ログイン・セッション保存専用処理（対話的）
    """
    success = ensure_logged_in(page, timeout_seconds=timeout_seconds)
    if not success:
        raise TimeoutError("ログインに失敗しました。時間内に承認を完了してください。")
    logger.info("[成功] ログインとセッション保存が完了しました。")
