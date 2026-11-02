"""Search Console API / Gmail API向けOAuth2認証（デスクトップアプリフロー）。

既存 generate_report.py と同じ credentials.json / token.pickle を再利用する
（サービスアカウントは使用しない）。token.pickle は既に webmasters.readonly と
gmail.send の両スコープで発行済みのため、SCOPESをこの2つにしても再認証は
発生しない（部分集合チェックを満たすため）。新規環境でtoken.pickleが
存在しない場合は、この2スコープで初回認証が行われる。
"""
from __future__ import annotations

import os
import pickle
import sys
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]
TOKEN_PATH = Path("token.pickle")

GA4_SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]
GA4_TOKEN_PATH = Path("token_ga4.pickle")

# 問い合わせ集計スプレッドシートの読み取り（2026-11-02〜）。既存の credentials.json（TCD Workspace の
# OAuthクライアント）を使い、トークンは既存の token.pickle に足さず別ファイルにする
# （GSC・送信の認証を作り直さずに済み、読み取り権限だけを分けて管理できる）。
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
SHEETS_TOKEN_PATH = Path("token_sheets.pickle")


class AuthorizationRequired(RuntimeError):
    """無人実行中に、ブラウザでの認可が必要になった（トークンが無い・失効した）。"""


def _interactive_allowed() -> bool:
    """launchd などの無人実行では TCD_NONINTERACTIVE=1 を設定する。
    その場合はブラウザ認可を開始せず例外にする（待ち続けて止まるのを防ぐ）。"""
    return os.environ.get("TCD_NONINTERACTIVE") != "1"


def get_oauth_credentials(credentials_file: str = "credentials.json") -> Any:
    creds = None

    if TOKEN_PATH.exists():
        creds = pickle.loads(TOKEN_PATH.read_bytes())
        if creds and creds.scopes is not None and not set(SCOPES).issubset(set(creds.scopes)):
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            oauth_file = Path(credentials_file)
            if not oauth_file.exists():
                print(
                    f"エラー: {credentials_file} が見つかりません。"
                    "OAuth2クライアント（デスクトップアプリ）のJSONを配置してください。",
                    file=sys.stderr,
                )
                sys.exit(1)
            if not _interactive_allowed():
                raise AuthorizationRequired(f"{TOKEN_PATH} の再認可が必要（無人実行のためブラウザ認可は行わない）")
            flow = InstalledAppFlow.from_client_secrets_file(str(oauth_file), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_bytes(pickle.dumps(creds))

    return creds


def get_search_console_service(credentials_file: str = "credentials.json") -> Any:
    creds = get_oauth_credentials(credentials_file)
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def get_gmail_service(credentials_file: str = "credentials.json") -> Any:
    creds = get_oauth_credentials(credentials_file)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def get_ga4_credentials(credentials_file: str = "credentials.json") -> Any:
    """GA4 Data API向け認証（token_ga4.pickle、fetch_ga4_referrers.pyと同じ資格情報を再利用）。"""
    creds = None

    if GA4_TOKEN_PATH.exists():
        creds = pickle.loads(GA4_TOKEN_PATH.read_bytes())
        if creds and creds.scopes is not None and not set(GA4_SCOPES).issubset(set(creds.scopes)):
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            oauth_file = Path(credentials_file)
            if not oauth_file.exists():
                print(
                    f"エラー: {credentials_file} が見つかりません。"
                    "OAuth2クライアント（デスクトップアプリ）のJSONを配置してください。",
                    file=sys.stderr,
                )
                sys.exit(1)
            if not _interactive_allowed():
                raise AuthorizationRequired(f"{GA4_TOKEN_PATH} の再認可が必要（無人実行のためブラウザ認可は行わない）")
            flow = InstalledAppFlow.from_client_secrets_file(str(oauth_file), GA4_SCOPES)
            creds = flow.run_local_server(port=0)
        GA4_TOKEN_PATH.write_bytes(pickle.dumps(creds))

    return creds


def get_sheets_credentials(credentials_file: str = "credentials.json", interactive: bool = False) -> Any:
    """スプレッドシート読み取り用の認証。interactive=False（既定）ではブラウザ認可を行わず、
    トークンが無い・更新できない場合は AuthorizationRequired を送出する。
    初回の認可は `python -m src_v2.fetch_inquiries --authorize` で行う。"""
    creds = None
    if SHEETS_TOKEN_PATH.exists():
        creds = pickle.loads(SHEETS_TOKEN_PATH.read_bytes())
        if creds and creds.scopes is not None and not set(SHEETS_SCOPES).issubset(set(creds.scopes)):
            creds = None

    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif interactive and _interactive_allowed():
        oauth_file = Path(credentials_file)
        if not oauth_file.exists():
            raise AuthorizationRequired(f"{credentials_file} が見つからない")
        flow = InstalledAppFlow.from_client_secrets_file(str(oauth_file), SHEETS_SCOPES)
        creds = flow.run_local_server(port=0)
    else:
        raise AuthorizationRequired(f"{SHEETS_TOKEN_PATH} が無い（`python -m src_v2.fetch_inquiries --authorize` で認可する）")
    SHEETS_TOKEN_PATH.write_bytes(pickle.dumps(creds))
    return creds
