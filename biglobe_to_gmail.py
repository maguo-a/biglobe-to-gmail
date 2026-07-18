# -*- coding: utf-8 -*-
"""
biglobeのメールボックスをIMAPで確認し、新着メールをGmail APIの
「messages.import」メソッドでGmailに投入するスクリプトです。
GitHub Actions上で定期実行されることを想定しています。

【取り込み済み判定の方式(ウィンドウ方式)】
既読/未読フラグには一切依存しません。
  1. 直近WINDOW_DAYS日分(既定14日)に「サーバーへ到着した」全メールの
     UID一覧を取得する(メールのDateヘッダーではなく、サーバー受信日時=
     internal dateを基準にするため、送信者側の時差表記には影響されません)
  2. これまでに処理済みのUID一覧(state.json)と突き合わせ、
     「まだ処理していないUID」だけを対象にする
  3. 処理できたUIDを処理済みリストに追加。ウィンドウの外に出た古いUIDは
     リストから削除し、ファイルの肥大化を防ぐ
  4. 1通の投入に失敗しても他のメールの処理は継続し、失敗したものは
     次回また自動的に再試行される(ウィンドウが十分広いので取りこぼさない)

必要な環境変数(GitHub Secretsから渡されます):
  BIGLOBE_IMAP_SERVER, BIGLOBE_USERNAME, BIGLOBE_PASSWORD
  GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN, GMAIL_TARGET_ADDRESS
任意の環境変数:
  SYNC_WINDOW_DAYS  毎回見る「窓」の日数(既定14日。初回も含め常にこの値を使う)
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

# 窓の日数。初回か2回目以降かに関わらず、常にこの日数を使う。
WINDOW_DAYS = int(os.environ.get("SYNC_WINDOW_DAYS", "14"))


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
    """現在選択中メールボックスのUIDVALIDITY値を取得する"""
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
    # labelIdsを明示的に指定しないと「すべてのメール」にしか入らず、
    # 受信トレイに表示されない・通知も来ない状態になるため、
    # INBOX(受信トレイ表示)とUNREAD(未読扱い、通知のトリガーになる)を付与する
    body = {"raw": encoded, "labelIds": ["INBOX", "UNREAD"]}
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
        print(f"初回実行です。直近{WINDOW_DAYS}日分を対象にします。")
        processed_uids = set()
    elif state.get("uidvalidity") != uidvalidity:
        print(
            "警告: UIDVALIDITYが変化しています(メールボックスの再構築等)。"
            "処理済み記録をリセットします。",
            file=sys.stderr,
        )
        processed_uids = set()
    else:
        processed_uids = state["processed_uids"]

    # サーバー受信日時(internal date)基準で、ウィンドウ内の全メールUIDを取得
    since_date = (
        datetime.date.today() - datetime.timedelta(days=WINDOW_DAYS)
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
        processed_uids &= uids_in_window
        save_state(uidvalidity, processed_uids)
        imap.logout()
        return

    print(f"{len(new_uids)}件の新着メールを取得しました。Gmailへ投入します。")
    service = get_gmail_service()

    success_count = 0
    for uid in new_uids:
        status, msg_data = imap.uid("fetch", str(uid), "(RFC822)")
        if status != "OK" or not msg_data:
            print(f"UID {uid} の取得に失敗しました。次回また試行します。", file=sys.stderr)
            continue

        # msg_dataの構造はメールによって要素の並びが変わることがあるため、
        # 実際にbytes型を持つ部分を探して取り出す(決め打ちのインデックス指定はしない)
        raw = None
        for part in msg_data:
            if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
                raw = part[1]
                break
        if raw is None:
            print(f"UID {uid} のメール本体を取得できませんでした。次回また試行します。", file=sys.stderr)
            continue

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

    processed_uids &= uids_in_window
    save_state(uidvalidity, processed_uids)
    imap.logout()


if __name__ == "__main__":
    main()
