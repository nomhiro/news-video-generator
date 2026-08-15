"""X API v2 の薄いラッパ。

Protocol にしている理由: ワーカーのループ（掴む・状態を進める・停止する）と
「実際に X を叩く」処理を分けたい。テストではフェイクを差し込んで、
課金も公開もせずにループの挙動を検証する（既存 JobRunner と同じ方針）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

import httpx

from src.social.x_auth import XTokenExpiredError

API_BASE = "https://api.x.com/2"
# **未検証。** v1.1 の `media/upload` から v2 への移行が進行中で、公式ドキュメントの
# 該当ページが本タスクの調査時点で確認できなかった（404）。X_POSTING_ENABLED は
# 既定 false で実際に叩かれないため実害は無いが、有効化する前に
# https://docs.x.com/x-api/media/ 配下で URL とフィールド名（`media`）を
# 確認すること。
UPLOAD_URL = "https://api.x.com/2/media/upload"


class XClientError(Exception):
    """X API の呼び出しに失敗した。"""


class XSendUncertainError(XClientError):
    """送信したが結果が分からない（タイムアウトなど）。

    再送してはいけない種類の失敗。呼び出し側は NEEDS_REVIEW にする。
    """


class XClient(Protocol):
    """投稿とメディアと指標。"""

    def create_post(
        self,
        text: str,
        reply_to: str | None = None,
        media_ids: list[str] | None = None,
    ) -> str:
        """投稿して tweet_id を返す。"""
        ...

    def upload_media(self, path: Path) -> str:
        """画像をアップロードして media_id を返す。"""
        ...

    def fetch_metrics(self, tweet_ids: list[str]) -> dict[str, dict[str, int]]:
        """投稿の指標を返す（tweet_id -> 指標）。"""
        ...


class HttpXClient:
    """httpx による XClient の実装。

    **create_post は一切再試行しない。** タイムアウトや 429 の応答が
    届く前に投稿自体は通っている可能性があり、それを排除できない。
    再試行すると同じ内容が2つ並ぶ。再試行の判断は人に委ねる
    （呼び出し側が NEEDS_REVIEW にして画面に出す）。

    Attributes:
        access_token: 有効なアクセストークン（呼び出し側が ensure_fresh 済み）
    """

    def __init__(self, access_token: str, timeout: float = 30.0):
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        """接続を閉じる。"""
        self._client.close()

    def create_post(
        self,
        text: str,
        reply_to: str | None = None,
        media_ids: list[str] | None = None,
    ) -> str:
        """投稿して tweet_id を返す。

        Args:
            text: 本文
            reply_to: 返信先の tweet_id（スレッドの2件目以降）
            media_ids: 添付するメディアのID

        Returns:
            str: 作成された投稿の ID

        Raises:
            XSendUncertainError: 届いたか分からない（再送してはいけない）
            XTokenExpiredError: 再認証が必要
            XClientError: 拒否された
        """
        payload: dict[str, Any] = {"text": text}
        if reply_to:
            payload["reply"] = {"in_reply_to_tweet_id": reply_to}
        if media_ids:
            payload["media"] = {"media_ids": media_ids}

        try:
            response = self._client.post(f"{API_BASE}/tweets", json=payload)
        # 送ったが応答を受け取れなかった。投稿が通っている可能性がある
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise XSendUncertainError(f"応答を受け取れませんでした: {e}") from e

        if response.status_code == 401:
            raise XTokenExpiredError(f"認証されませんでした: {response.text}")
        # 429 も再試行しない。応答が届く前に投稿が通った場合と区別できない
        if response.status_code == 429:
            raise XSendUncertainError(f"レート制限に達しました: {response.text}")
        if response.status_code >= 500:
            # 5xx はサーバー側の状態が不明。届いた可能性を排除できない
            raise XSendUncertainError(f"サーバーエラー: {response.status_code}")
        if response.status_code >= 400:
            raise XClientError(f"投稿を拒否されました（{response.status_code}）: {response.text}")

        try:
            return str(response.json()["data"]["id"])
        except (KeyError, TypeError, ValueError) as e:
            # 2xx なのに ID が読めない。投稿は通っている
            raise XSendUncertainError(f"応答から ID を読めませんでした: {response.text}") from e

    def upload_media(self, path: Path) -> str:
        """画像をアップロードして media_id を返す。

        投稿と違い、**失敗しても再試行してよい**（アップロードは公開されない。
        重複しても未使用のメディアが残るだけ）。

        Args:
            path: PNG のパス

        Returns:
            str: media_id

        Raises:
            XClientError: アップロードに失敗した
        """
        try:
            with path.open("rb") as f:
                response = self._client.post(
                    UPLOAD_URL,
                    files={"media": (path.name, f, "image/png")},
                    # multipart なので Content-Type をヘッダーから外す
                    # （httpx は値 None のヘッダーをデフォルトの上書きとして
                    # 解釈するが、型スタブは str 以外の値を許さない）
                    headers={"Content-Type": None},  # type: ignore[arg-type]
                )
            response.raise_for_status()
            return str(response.json()["data"]["id"])
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as e:
            raise XClientError(f"メディアのアップロードに失敗しました: {e}") from e

    def fetch_metrics(self, tweet_ids: list[str]) -> dict[str, dict[str, int]]:
        """投稿の指標を返す。

        Args:
            tweet_ids: 最大100件

        Returns:
            dict[str, dict[str, int]]: tweet_id -> 指標

        Raises:
            XClientError: 取得に失敗した
        """
        if len(tweet_ids) > 100:
            raise XClientError(f"1回に問い合わせられるのは100件までです: {len(tweet_ids)}件")
        try:
            response = self._client.get(
                f"{API_BASE}/tweets",
                params={"ids": ",".join(tweet_ids), "tweet.fields": "public_metrics"},
            )
            response.raise_for_status()
            data = response.json().get("data", [])
        except (httpx.HTTPError, ValueError) as e:
            raise XClientError(f"指標の取得に失敗しました: {e}") from e

        return {str(item["id"]): dict(item.get("public_metrics", {})) for item in data}
