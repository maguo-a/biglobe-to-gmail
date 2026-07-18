# -*- coding: utf-8 -*-
"""
biglobeのメールボックスをIMAPで確認し、新着メールをGmail APIの
「messages.import」メソッドでGmailに投入するスクリプトです。
GitHub Actions上で定期実行されることを想定しています。

【取り込み済み判定の方式】
既読/未読フラグには一切依存しません。また「最後に処理したUID」だけを見る
単純な方式でもなく、以下の「ウィンドウ方式」を採用しています。

  1. 直近N日分(通常1日)に「サーバーへ到着した」全メールのUID一覧を取得
     (これはメールのDateヘッダーではなく、サーバー受信日時=internal dateを
      基準にするため、送信者側の時差表記には影響されません)
  2. これまでに処理済みのUID一覧(state.json)と突き合わせ、
     「まだ処理していないUID」だけを対象にする
  3. 処理できたUIDを処理済みリストに追加。ウィンドウの外に出た古いUIDは
     リストから削除し、ファイルの肥大化を防ぐ

必要な環境変数(GitHub Secretsから渡されます):
  BIGLOBE_IMAP_SERVER, BIGLOBE_USERNAME, BIGLOBE_PASSWORD
  GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN, GMAIL_TARGET_ADDRESS
任意の環境変数:
  SYNC_SINCE_DAYS   初回(state.jsonがまだ無い時)だけ使う、絞り込み日数
  SYNC_WINDOW_DAYS  2回目以降、毎回見る「窓」の日数(デフォルト1日)
"""

import os
import imaplib
import base64
import sys
import json
import datetime

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

STATE_FILE = "state.json"

# ===== 環境変数の読み込み =====
IMAP_SERVER = os.environ["BIGLOBE_IMAP_SERVER"]
IMAP_USERNAME = os.environ["BIGLOBE_USERNAME"]
IMAP_PASSWORD = os.environ["BIGLOBE_PASSWORD"]

GMAIL_CLIENT_ID = os.environ["GMAIL_CLIENT_ID"]
GMAIL_CLIENT_SECRET = os.environ["GMAIL_CLIENT_SECRET"]
GMAIL_REFRESH_TOKEN = os.environ["GMAIL_REFRESH_TOKEN"]
GMAIL_TARGET_ADDRESS = os.environ.get("GMAIL_TARGET_ADDRESS", "")

SYNC_SINCE_DAYS = int(os.environ.get("SYNC_SINCE_DAYS", "10"))
SYNC_WINDOW_DAYS = int(os.environ.get("SYNC_WINDOW_DAYS", "1"))


def load_state():
    """前回の処理状況を読み込む。無ければNoneを返す"""
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        data["processed_uids"] = set(data.get("processed_uids", []))
        return data


def save_state(uidvalidity, processed_uids):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"uidvalidity": uidvalidity, "processed_uids": sorted(processed_uids)},
            f,
        )


def get_uidvalidity(imap):
    typ, data = imap.response("UIDVALIDITY")
    if data and data[0]:
        return data[0].decode() if isinstance(data[0], bytes) else str(data[0])
    return None


def get_gmail_service():
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


def import_to_gmail(service, raw_bytes):
    encoded = base64.urlsafe_b64encode(raw_bytes).decode("ascii")
    body = {"raw": encoded}
    service.users().messages().import_(
        userId="me",
        body=body,
        internalDateSource="dateHeader",
        processForCalendar=False,
    ).execute()


def main():
    print("biglobeへ接続しています...")
    imap = imaplib.IMAP4_SSL(IMAP_SERVER)
    imap.login(IMAP_USERNAME, IMAP_PASSWORD)
    imap.select("INBOX")

    uidvalidity = get_uidvalidity(imap)
    state = load_state()

    if state is None:
        print(f"初回実行です。直近{SYNC_SINCE_DAYS}日分を対象にします。")
        window_days = SYNC_SINCE_DAYS
        processed_uids = set()
    elif state.get("uidvalidity") != uidvalidity:
        print(
            "警告: UIDVALIDITYが変化しています(メールボックスの再構築等)。"
            f"安全のため直近{SYNC_SINCE_DAYS}日分からやり直します。",
            file=sys.stderr,
        )
        window_days = SYNC_SINCE_DAYS
        processed_uids = set()
    else:
        window_days = SYNC_WINDOW_DAYS
        processed_uids = state["processed_uids"]

    # サーバー受信日時(internal date)基準で、ウィンドウ内の全メールUIDを取得
    since_date = (
        datetime.date.today() - datetime.timedelta(days=window_days)
    ).strftime("%d-%b-%Y")
    print(f"検索条件: サーバー受信日時が {since_date} 以降の全メール")

    status, data = imap.uid("search", None, f"(SINCE {since_date})")
    if status != "OK":
        print("IMAP検索に失敗しました:", data, file=sys.stderr)
        imap.logout()
        sys.exit(1)

    uids_in_window = set(int(u) for u in data[0].split()) if data[0] else set()
    new_uids = sorted(uids_in_window - processed_uids)

    if not new_uids:
        print("新着メールはありませんでした。")
        # ウィンドウの外に出た古いUIDを削除してから保存(肥大化防止)
        processed_uids &= uids_in_window
        save_state(uidvalidity, processed_uids)
        imap.logout()
        return

    print(f"{len(new_uids)}件の新着メールを取得しました。Gmailへ投入します。")
    service = get_gmail_service()

    success_count = 0
    for uid in new_uids:
        status, msg_data = imap.uid("fetch", str(uid), "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            print(f"UID {uid} の取得に失敗しました。次回また試行します。", file=sys.stderr)
            continue
        raw = msg_data[0][1]
        try:
            import_to_gmail(service, raw)
            success_count += 1
            processed_uids.add(uid)
            # 1通ごとに保存(途中で落ちても、成功した分は次回スキップされる)
            trimmed = processed_uids & uids_in_window
            save_state(uidvalidity, trimmed)
        except Exception as e:
            print(f"UID {uid} の投入に失敗しました: {e}", file=sys.stderr)
            # 失敗しても処理済みにせず、次回また試行する(他のUIDの処理は継続)

    print(f"{success_count}/{len(new_uids)}件の投入が完了しました。")

    # 最終的な状態を保存(ウィンドウ外の古いUIDは削除)
    processed_uids &= uids_in_window
    save_state(uidvalidity, processed_uids)
    imap.logout()


if __name__ == "__main__":
    main()
