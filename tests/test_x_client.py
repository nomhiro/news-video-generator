"""X API クライアントが組み立てる実際のリクエストの検証。

なぜ httpx.MockTransport を使うか
---------------------------------
このクラスの不具合は「送る直前の組み立て」に出る。フェイクの `XClient` を
差し込むテスト（`tests/test_post_worker.py`）は、そこを一切通らない。

実際に踏んだ例が2つある。どちらも実 API を叩くまで誰も気付かなかった。

1. 既定ヘッダーに `Content-Type: application/json` を固定し、`upload_media`
   側で `None` を渡して打ち消そうとしていた。httpx は None のヘッダー値を
   受け付けないので、メディアアップロードは**必ず**
   `Header value must be str or bytes` で失敗していた。
2. `/2/media/upload` は `media_category` を必須で要求する。省略すると 400 で
   `Missing media_category field` が返る。

`MockTransport` なら、ネットワークも課金も無しに「httpx が最終的に組み立てた
リクエスト」を検査できる。中身は解析されないので、画像の内容は何でもよい。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from src.social.x_client import (
    MEDIA_CATEGORY_IMAGE,
    HttpXClient,
    XClientError,
    XSendUncertainError,
)

Handler = Callable[[httpx.Request], httpx.Response]


def _client_with(handler: Handler, access_token: str = "token") -> HttpXClient:
    """MockTransport を差し込んだクライアントを作る。

    既定ヘッダーは実物から引き継ぐ。ヘッダーの組み立てそのものが
    検査対象なので、ここで作り直してはいけない。
    """
    client = HttpXClient(access_token)
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers=dict(client._client.headers),
    )
    return client


def _image(tmp_path: Path) -> Path:
    """アップロード用のダミーファイル。

    送信の組み立てを見るだけなので、PNG として妥当である必要はない。
    """
    path = tmp_path / "card.png"
    path.write_bytes(b"dummy-image-bytes" * 4)
    return path


def test_メディアアップロードは_multipart_で送る(tmp_path: Path) -> None:
    """既定ヘッダーに Content-Type を固定すると、ここが壊れる。

    以前は application/json を固定していたため、httpx が組み立てる
    multipart の境界文字列と食い違い、そもそも送信前に例外になっていた。
    """
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers.get("content-type", "")
        seen["body_len"] = len(request.content)
        return httpx.Response(200, json={"data": {"id": "media-1"}})

    client = _client_with(handler)
    try:
        media_id = client.upload_media(_image(tmp_path))
    finally:
        client.close()

    assert media_id == "media-1"
    content_type = str(seen["content_type"])
    assert content_type.startswith("multipart/form-data"), content_type
    # 境界文字列が入っていること（httpx に組み立てさせている証拠）
    assert "boundary=" in content_type
    # 画像の中身が本文に載っていること
    assert isinstance(seen["body_len"], int)
    assert seen["body_len"] > 68


def test_メディアアップロードは_media_category_を送る(tmp_path: Path) -> None:
    """必須項目。省略すると 400 `Missing media_category field` になる（実 API で確認）。"""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode("utf-8", "replace")
        return httpx.Response(200, json={"data": {"id": "media-1"}})

    client = _client_with(handler)
    try:
        client.upload_media(_image(tmp_path))
    finally:
        client.close()

    assert 'name="media_category"' in seen["body"]
    assert MEDIA_CATEGORY_IMAGE in seen["body"]


def test_アップロード失敗の例外に応答本文が入る(tmp_path: Path) -> None:
    """ステータスと URL だけでは理由が分からない。

    `media_category` の欠落に気付くまで、本文を見るために API を手で
    叩き直す必要があった。次に別の必須項目が増えたときも同じことになる。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"errors": [{"message": "Missing media_category field"}]})

    client = _client_with(handler)
    try:
        with pytest.raises(XClientError) as excinfo:
            client.upload_media(_image(tmp_path))
    finally:
        client.close()

    assert "Missing media_category field" in str(excinfo.value)


def test_投稿は_JSON_で送る() -> None:
    """`json=` を使うので Content-Type は httpx が付ける。

    既定ヘッダーから外した副作用でここが壊れないことを見張る。
    """
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers.get("content-type", "")
        seen["body"] = request.content.decode()
        return httpx.Response(201, json={"data": {"id": "tweet-1"}})

    client = _client_with(handler)
    try:
        tweet_id = client.create_post("本文", reply_to="tweet-0", media_ids=["media-1"])
    finally:
        client.close()

    assert tweet_id == "tweet-1"
    assert seen["content_type"].startswith("application/json")
    assert "tweet-0" in seen["body"]
    assert "media-1" in seen["body"]


def test_認可ヘッダーが付く() -> None:
    """トークンが載らなければ全ての呼び出しが 401 になる。"""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(201, json={"data": {"id": "tweet-1"}})

    client = _client_with(handler, access_token="abc123")
    try:
        client.create_post("本文")
    finally:
        client.close()

    assert seen["auth"] == "Bearer abc123"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_投稿は_429_と_5xx_を_送信結果不明として扱う(status: int) -> None:
    """届いたか分からないものは再送しない。

    X API に冪等キーが無いため、再送は同じ内容を2回公開する。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="rate limited or server error")

    client = _client_with(handler)
    try:
        with pytest.raises(XSendUncertainError):
            client.create_post("本文")
    finally:
        client.close()


def test_投稿は_タイムアウトを_送信結果不明として扱う() -> None:
    """応答が来なかっただけで、投稿が通っている可能性は残る。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = _client_with(handler)
    try:
        with pytest.raises(XSendUncertainError):
            client.create_post("本文")
    finally:
        client.close()


def test_投稿は_4xx_を_拒否として扱う() -> None:
    """400 は届いた上で拒否された状態なので、送信結果は不明ではない。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="invalid request")

    client = _client_with(handler)
    try:
        with pytest.raises(XClientError):
            client.create_post("本文")
    finally:
        client.close()


def test_指標は101件以上を受け付けない() -> None:
    """X の上限は100件。サーバーに言わせず自前で守る。"""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 到達しない
        raise AssertionError("上限超過なのに送信した")

    client = _client_with(handler)
    try:
        with pytest.raises(XClientError):
            client.fetch_metrics([str(i) for i in range(101)])
    finally:
        client.close()
