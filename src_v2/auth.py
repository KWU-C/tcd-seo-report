"""Search Console API / Gmail API向けOAuth2認証（デスクトップアプリフロー）。

既存 generate_report.py と同じ credentials.json / token.pickle を再利用する
（サービスアカウントは使用しない）。token.pickle は既に webmasters.readonly と
gmail.send の両スコープで発行済みのため、SCOPESをこの2つにしても再認証は
発生しない（部分集合チェックを満たすため）。新規環境でtoken.pickleが
存在しない場合は、この2スコープで初回認証が行われる。
"""
from __future__ import annotations

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
            flow = InstalledAppFlow.from_client_secrets_file(str(oauth_file), GA4_SCOPES)
            creds = flow.run_local_server(port=0)
        GA4_TOKEN_PATH.write_bytes(pickle.dumps(creds))

    return creds
