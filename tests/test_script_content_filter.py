"""台本生成がコンテンツフィルタに拒否されたときの扱い（issue #30）。

拒否には**扉が2つある**。

- 入力側: 記事のタイトルと本文が拒否され、`BadRequestError` が返る
- 出力側: 応答は返るが `output_parsed` が None で、理由が `content_filter`

どちらも記事の題材に由来する恒久的な失敗なので、引き直さず専用の型で
上へ伝える。**片方だけ閉じても症状は残る。**
"""

import httpx
import pytest
from openai import BadRequestError

from src.generators.script_generator import (
    ScriptContentFilterError,
    ScriptGenerationError,
    ScriptGenerator,
)
from src.models.formats import get_spec
from src.models.script import draft_type_for
from tests.test_content_filter import ISSUE_30_BODY

# `_request_script` は `text_format` に渡す型を引数で受ける（要素数を固定した
# 派生型。`src/models/script.py` の `draft_type_for` を参照）。ここでは何を
# 渡しても経路は同じなので、既定の形式のものを使う。
DRAFT_TYPE = draft_type_for(get_spec("short").segment_count)


def _bad_request(body: object) -> BadRequestError:
    """実物の `BadRequestError` を組み立てる。

    ここは stub で代用できない。`_request_script` は `except BadRequestError`
    で捕まえるので、`isinstance` が成立する実物が要る。
    """
    request = httpx.Request("POST", "https://example.openai.azure.com/openai/v1/responses")
    response = httpx.Response(400, request=request)
    return BadRequestError("Error code: 400", response=response, body=body)


class FakeIncompleteDetails:
    """`response.incomplete_details` のうち見る部分だけ。"""

    def __init__(self, reason: str | None):
        self.reason = reason


class FakeResponse:
    """`responses.parse` の戻りのうち `_request_script` が見る部分だけ。"""

    def __init__(self, reason: str | None = None, status: str = "incomplete"):
        self.output_parsed = None
        self.incomplete_details = FakeIncompleteDetails(reason)
        self.status = status


class FakeResponses:
    """`client.responses` の差し替え。呼ばれた回数を数える。"""

    def __init__(self, error: Exception | None = None, response: FakeResponse | None = None):
        self._error = error
        self._response = response or FakeResponse()
        self.calls = 0

    def parse(self, **kwargs: object) -> FakeResponse:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._response


class FakeClient:
    """`ScriptGenerator.client` の差し替え。"""

    def __init__(self, responses: FakeResponses):
        self.responses = responses


def _generator() -> ScriptGenerator:
    """実物の `ScriptGenerator` を作る（`__init__` は通信しない）。"""
    return ScriptGenerator("https://example.openai.azure.com", "dummy", "gpt-5.1")


def test_入力が拒否されたら専用の型になる(monkeypatch: pytest.MonkeyPatch) -> None:
    """`BadRequestError(content_filter)` を `ScriptContentFilterError` にすること。

    素の `ScriptGenerationError` のままだと、記事に印が付かず、代替も積まれず、
    翌日また同じ記事で失敗する（issue #30 の症状そのもの）。
    """
    generator = _generator()
    responses = FakeResponses(error=_bad_request(ISSUE_30_BODY))
    monkeypatch.setattr(generator, "client", FakeClient(responses))

    with pytest.raises(ScriptContentFilterError):
        generator._request_script("指示", "トピック", DRAFT_TYPE)

    # tenacity の許可リストに BadRequestError は入っていない。再試行すると
    # 拒否される入力を4回投げることになる。
    assert responses.calls == 1


def test_フィルタ以外の400はそのまま伝播する(monkeypatch: pytest.MonkeyPatch) -> None:
    """関係のない 400 をコンテンツフィルタ扱いにしないこと。

    誤ると、直せる失敗（デプロイ名の誤りなど）の記事に恒久的な印が付いて
    二度と使われなくなる。
    """
    body = {"error": {"code": "DeploymentNotFound", "message": "does not exist"}}
    generator = _generator()
    monkeypatch.setattr(generator, "client", FakeClient(FakeResponses(error=_bad_request(body))))

    with pytest.raises(BadRequestError):
        generator._request_script("指示", "トピック", DRAFT_TYPE)


def test_出力が拒否されたら専用の型になる(monkeypatch: pytest.MonkeyPatch) -> None:
    """`incomplete_details.reason == "content_filter"` も同じ扉として扱うこと。

    ここを閉じないと、同じ恒久的な失敗が別の経路から入って毎日の再試行に戻る。
    """
    generator = _generator()
    response = FakeResponse(reason="content_filter")
    monkeypatch.setattr(generator, "client", FakeClient(FakeResponses(response=response)))

    with pytest.raises(ScriptContentFilterError):
        generator._request_script("指示", "トピック", DRAFT_TYPE)


def test_打ち切りはコンテンツフィルタとして扱わない(monkeypatch: pytest.MonkeyPatch) -> None:
    """`max_output_tokens` による打ち切りを拒否と混ぜないこと。

    打ち切りは引き直しで直りうる一時的な失敗。恒久的な拒否として記事に
    印を付けると、まだ使える記事を捨てることになる。
    """
    generator = _generator()
    response = FakeResponse(reason="max_output_tokens")
    monkeypatch.setattr(generator, "client", FakeClient(FakeResponses(response=response)))

    with pytest.raises(ScriptGenerationError) as caught:
        generator._request_script("指示", "トピック", DRAFT_TYPE)

    assert not isinstance(caught.value, ScriptContentFilterError)


def test_generateは型を保ったまま伝播し引き直さない(monkeypatch: pytest.MonkeyPatch) -> None:
    """`generate` が `ScriptContentFilterError` を再ラップしないこと。

    **これが issue #30 の直しで最も壊れやすい点。**
    `ScriptContentFilterError` は `ScriptGenerationError`（つまり `Exception`）の
    サブクラスなので、引き直しループの `except Exception` に食われると素の
    `ScriptGenerationError` に化けて型が消える。型が消えると Pipeline の
    素通しも記事への印付けも代替の投入も発火せず、**症状は直す前と同じ**に
    なる（しかもテストが無ければ気付けない）。

    併せて引き直しが走らないことも見る。拒否されたのは入力なので、同じ
    プロンプトを送り直すのは API 呼び出しを捨てるだけ。
    """
    generator = _generator()
    calls: list[int] = []

    def raise_filtered(instructions: str, news_topic: str, draft_type: type) -> None:
        calls.append(1)
        raise ScriptContentFilterError("記事の題材が拒否されました")

    monkeypatch.setattr(generator, "_request_script", raise_filtered)

    with pytest.raises(ScriptContentFilterError):
        generator.generate("トピック", "ja", "short")

    assert len(calls) == 1


def test_画面に出る文言に生のJSONを入れない(monkeypatch: pytest.MonkeyPatch) -> None:
    """失敗理由が読める日本語であること。

    直す前は「パイプライン実行に失敗しました: 台本生成に失敗しました:
    Error code: 400 - {...}」という3段ラップの生 JSON が画面に出ており、
    記事の題材が原因だと読み取れなかった（issue #30 の④）。
    """
    generator = _generator()
    responses = FakeResponses(error=_bad_request(ISSUE_30_BODY))
    monkeypatch.setattr(generator, "client", FakeClient(responses))

    with pytest.raises(ScriptContentFilterError) as caught:
        generator._request_script("指示", "トピック", DRAFT_TYPE)

    message = str(caught.value)
    assert "コンテンツフィルタに拒否されました" in message
    # 拒否されたカテゴリは添える（どの種類で弾かれたかは判断の手掛かりになる）
    assert "sexual" in message
    # 生の応答の断片は出さない
    assert "content_filter_offsets" not in message
    assert "end_offset" not in message
