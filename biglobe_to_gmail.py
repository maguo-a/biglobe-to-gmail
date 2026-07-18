# -*- coding: utf-8 -*-
"""
biglobeのメールボックスをIMAPで確認し、新着メール（未読）をGmailに
「messages.import」メソッドで投入するスクリプトです。
GitHub Actions上で定期実行されることを想定しています。

処理の流れ:
  1. biglobeにIMAP接続し、未読メールを取得
  2. 取得した各メールを、Gmail APIのmessages.importで対象Gmailアカウントに投入
     （このメソッドはGmail側の迷惑メール判定を通ります）
  3. 投入に成功したメールは、biglobe側で既読にする（重複投入を防ぐため）

必要な環境変数（GitHub Secretsから渡されます）:
  BIGLOBE_IMAP_SERVER, BIGLOBE_USERNAME, BIGLOBE_PASSWORD
  GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN, GMAIL_TARGET_ADDRESS
"""

import os
import imaplib
import email
import base64
import sys

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ===== 環境変数の読み込み =====
IMAP_SERVER = os.environ["BIGLOBE_IMAP_SERVER"]
IMAP_USERNAME = os.environ["BIGLOBE_USERNAME"]
IMAP_PASSWORD = os.environ["BIGLOBE_PASSWORD"]

GMAIL_CLIENT_ID = os.environ["GMAIL_CLIENT_ID"]
GMAIL_CLIENT_SECRET = os.environ["GMAIL_CLIENT_SECRET"]
GMAIL_REFRESH_TOKEN = os.environ["GMAIL_REFRESH_TOKEN"]
# GMAIL_TARGET_ADDRESSは今回のスクリプトでは直接使いませんが、
# 将来のログ出力や確認用に読み込んでおきます
GMAIL_TARGET_ADDRESS = os.environ.get("GMAIL_TARGET_ADDRESS", "")


def get_gmail_service():
    """リフレッシュトークンを使ってGmail APIのサービスオブジェクトを作る"""
    creds = Credentials(
        token=None,
        refresh_token=GMAIL_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GMAIL_CLIENT_ID,
        client_secret=GMAIL_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/gmail.insert"],
    )
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)


def fetch_unseen_messages():
    """biglobeから未読メールを取得する。(uid, raw_bytes)のリストを返す"""
    imap = imaplib.IMAP4_SSL(IMAP_SERVER)
    imap.login(IMAP_USERNAME, IMAP_PASSWORD)
    imap.select("INBOX")

    # UID SEARCHで未読メールのUIDを取得(UIDはメール削除・並び替えでも
    # 番号が変わらないIMAPの安定した識別子)
    status, data = imap.uid("search", None, "UNSEEN")
    if status != "OK":
        print("IMAP検索に失敗しました:", data)
        imap.logout()
        return imap, []

    uid_list = data[0].split()
    messages = []
    for uid in uid_list:
        status, msg_data = imap.uid("fetch", uid, "(RFC822)")
        if status == "OK" and msg_data and msg_data[0]:
            raw = msg_data[0][1]
            messages.append((uid, raw))
    return imap, messages


def import_to_gmail(service, raw_bytes):
    """1通のメールをGmail APIのmessages.importで投入する"""
    encoded = base64.urlsafe_b64encode(raw_bytes).decode("ascii")
    body = {"raw": encoded}
    service.users().messages().import_(
        userId="me",
        body=body,
        # neverMarkSpamは指定しない = Gmailの通常の迷惑メール判定に任せる
        internalDateSource="dateHeader",  # 元メールのDateヘッダーを受信日時として使う
        processForCalendar=False,
    ).execute()


def main():
    print("biglobeへ接続しています...")
    imap, messages = fetch_unseen_messages()

    if not messages:
        print("新着メールはありませんでした。")
        imap.logout()
        return

    print(f"{len(messages)}件の未読メールを取得しました。Gmailへ投入します。")
    service = get_gmail_service()

    success_count = 0
    for uid, raw in messages:
        try:
            import_to_gmail(service, raw)
            # 投入成功したら、biglobe側でも既読にして次回以降スキップする
            imap.uid("store", uid, "+FLAGS", "(\\Seen)")
            success_count += 1
        except Exception as e:
            # 1通失敗しても他のメール処理は続ける。既読にしないので次回また試行される
            print(f"UID {uid} の投入に失敗しました: {e}", file=sys.stderr)

    print(f"{success_count}/{len(messages)}件の投入が完了しました。")
    imap.logout()


if __name__ == "__main__":
    main()
