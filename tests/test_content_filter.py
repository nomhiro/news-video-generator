"""コンテンツフィルタ由来のエラーの判定。

**綴りが2つあることがこのテストの主眼。** 画像 API は `contentFilter`
（camelCase）、Chat / Responses API は `content_filter`（snake_case）を返す。
移す前の実装は camelCase だけを明示判定しており、snake_case は最後の
`str(exc)` の部分文字列一致で*偶然*拾えていただけだった。

実 `BadRequestError` を組み立てないのはこのリポジトリの前例に沿っている
（`tests/test_post_planner.py` もドメインの例外を注入する）。判定関数は
述語で `Exception` を受けるので、属性を持つ軽い stub で足りる。
"""

from src.utils.content_filter import filtered_categories, is_content_filter_error


class FakeBadRequest(Exception):
    """`BadRequestError` のうち、判定が見る部分だけを持つ stub。"""

    def __init__(
        self,
        message: str = "Error code: 400",
        code: str | None = None,
        body: object | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.body = body


# issue #30 で実際に返ってきた応答の形（メッセージは要約してある）。
# **snake_case で、`content_filter_results` にカテゴリが入る。**
ISSUE_30_BODY = {
    "error": {
        "message": (
            "The response was filtered due to the prompt triggering Azure OpenAI's "
            "content management policy."
        ),
        "code": "content_filter",
        "content_filter_results": {
            "sexual": {"filtered": True, "severity": "medium"},
            "hate": {"filtered": False, "severity": "safe"},
            "violence": {"filtered": False, "severity": "safe"},
        },
        "content_filter_offsets": {"start_offset": 0, "end_offset": 19473, "check_offset": 0},
    }
}


def test_台本生成が実際に受けた応答をコンテンツフィルタとして判定する() -> None:
    """issue #30 の実ペイロード（snake_case）を判定できること。

    `code` 属性を持たない形で来ても body から判定する。ここが通らないと、
    台本生成の拒否が恒久的な失敗として扱われず毎日の再試行に戻る。
    """
    exc = FakeBadRequest(message="Error code: 400 - filtered", body=ISSUE_30_BODY)

    assert is_content_filter_error(exc) is True


def test_拒否されたカテゴリだけを拾う() -> None:
    """`filtered` が真のものだけを返すこと。

    severity が safe のカテゴリまで並べると、画面の文言が
    「sexual、hate、violence に拒否されました」になって嘘になる。
    """
    assert filtered_categories(FakeBadRequest(body=ISSUE_30_BODY)) == ("sexual",)


def test_SDKがcode属性に載せた場合も判定する() -> None:
    """body を持たず `code` だけの形でも判定できること。"""
    assert is_content_filter_error(FakeBadRequest(code="content_filter")) is True


def test_画像APIのcamelCaseも判定する() -> None:
    """画像側の綴り（`contentFilter`）を落としていないこと。

    共通化のときにこちらを落とすと、X の画像カードが SINGLE に降格せず
    投稿の計画ごと落ちる。
    """
    body = {"error": {"code": "contentFilter", "message": "filtered"}}

    assert is_content_filter_error(FakeBadRequest(body=body)) is True
    assert is_content_filter_error(FakeBadRequest(code="contentFilter")) is True


def test_フィルタ以外の400は判定しない() -> None:
    """関係のない 400 をコンテンツフィルタと誤認しないこと。

    誤認すると、直せる失敗（デプロイ名の誤りなど）の記事に恒久的な印が
    付いて二度と使われなくなる。
    """
    body = {
        "error": {
            "code": "DeploymentNotFound",
            "message": "The API deployment for this resource does not exist.",
        }
    }
    exc = FakeBadRequest(message="Error code: 400 - deployment not found", body=body)

    assert is_content_filter_error(exc) is False
    assert filtered_categories(exc) == ()


def test_innererrorの下にあるカテゴリも拾う() -> None:
    """Azure が `innererror` に入れてくる形にも答えること。"""
    body = {
        "error": {
            "code": "content_filter",
            "message": "filtered",
            "innererror": {
                "content_filter_result": {
                    "self_harm": {"filtered": True, "severity": "high"},
                }
            },
        }
    }

    assert filtered_categories(FakeBadRequest(body=body)) == ("self_harm",)


def test_カテゴリが読めない形でも例外を出さない() -> None:
    """形の想定が外れても落ちないこと。

    ここで落とすと、本来伝えたい「コンテンツフィルタに拒否された」という
    情報そのものが失われ、画面は理由の分からない失敗に戻る。
    """
    broken = [
        FakeBadRequest(body=None),
        FakeBadRequest(body="not a dict"),
        FakeBadRequest(body={"error": "not a dict"}),
        FakeBadRequest(body={"error": {"content_filter_results": "not a dict"}}),
        FakeBadRequest(body={"error": {"content_filter_results": {"sexual": "not a dict"}}}),
        FakeBadRequest(body={"error": {"innererror": 42}}),
    ]

    for exc in broken:
        assert filtered_categories(exc) == ()


def test_属性も本文も無いときは文字列で判定する() -> None:
    """最後の砦（`str(exc)` の部分文字列）が効いていること。"""
    assert is_content_filter_error(Exception("400 content_filter triggered")) is True
    assert is_content_filter_error(Exception("400 rate limit")) is False
