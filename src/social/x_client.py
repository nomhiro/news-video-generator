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
# 実 API で確認済み（2026-08-17、@NORRTechLab のアプリで 200 を確認）。
# multipart のフィールド名は `media`、`media_category` が必須。
# 応答は `data.id`（= media_id）と `media_key` を返し、PNG を送っても X 側で
# JPEG に再エンコードされる（1.5MB の PNG が 153KB の JPEG になった）。
# media_id の有効期限は 86,400 秒。
UPLOAD_URL = "https://api.x.com/2/media/upload"

# メディアの用途。`/2/media/upload` は必須項目として要求する
# （省略すると 400 "Missing media_category field"。実 API で確認済み）。
MEDIA_CATEGORY_IMAGE = "tweet_image"


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

    def close(self) -> None:
        """接続を閉じる。

        `PostWorker` は `client_factory` をポーリングごとに呼んで新しい
        クライアントを作る（アクセストークンの更新に追随するため）。
        作ったら必ずこれを呼んで閉じる責務は呼び出し側にあり、Protocol に
        含めているのはその契約を型で強制するため。忘れると
        `httpx.Client` の接続プールがポーリングごとに（既定30秒に1回）
        漏れ続ける。
        """
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
        # **Content-Type を既定ヘッダーに入れない。**
        # httpx は `json=` のとき application/json、`files=` のとき
        # multipart/form-data（境界文字列付き）を自動で付ける。既定に
        # application/json を固定すると、メディアアップロードの multipart が
        # 壊れる。以前はここで固定し、`upload_media` 側で `None` を渡して
        # 打ち消そうとしていたが、httpx は None のヘッダー値を受け付けず
        # `Header value must be str or bytes` で必ず失敗していた
        # （実際に呼ぶテストが無かったため気付かなかった）。
        self._client = httpx.Client(
            timeout=timeout,
            headers={"Authorization": f"Bearer {access_token}"},
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
        response: httpx.Response | None = None
        try:
            with path.open("rb") as f:
                # Content-Type は指定しない。httpx が multipart の境界文字列を
                # 含めて組み立てる（手で書くと境界が合わず本文が壊れる）。
                #
                # `media_category` は必須。省略すると 400 で
                # "Missing media_category field" が返る（実 API で確認）。
                response = self._client.post(
                    UPLOAD_URL,
                    files={"media": (path.name, f, "image/png")},
                    data={"media_category": MEDIA_CATEGORY_IMAGE},
                )
            response.raise_for_status()
            return str(response.json()["data"]["id"])
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as e:
            # **応答本文を添える。** `raise_for_status()` の文言はステータスと
            # URL だけで、X が返す理由（どのフィールドが足りないか）は本文にある。
            # 本文を捨てていたため `media_category` の欠落に気付くまで
            # 実 API を手で叩き直す必要があった。
            detail = f" 応答: {response.text[:300]}" if response is not None else ""
            raise XClientError(f"メディアのアップロードに失敗しました: {e}{detail}") from e

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
