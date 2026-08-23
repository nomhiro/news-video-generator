"""引き直しのときに前回の失敗理由をモデルへ戻すことの検証（issue #61）。

なぜこれが必要か
----------------
以前は `instructions` と `news_topic` を毎回そのまま送り直していた。つまり
3回の試行は**同じ分布からの3標本**にすぎず、外しやすい制約があると3回とも
外れる。実際に #61 では 1・2回目が挿絵の文字数超過、3回目が配列長の不一致で、
`VALIDATION_RETRIES` を使い切ってその日の動画が0本になった。

同じ失敗は `PostGenerator` で先に実測されている
（`src/social/post_generator.py:372-378`「同じ入力なら同じ長さが返るので、
再生成しても結果は変わらず、投稿は毎回破棄されてアカウントが沈黙する。
…**効いているのはこのフィードバック**」）。台本側にも同じ手を入れた。

**この検査を外すと症状は静かに戻る。** 引き直し自体は動くので、失敗率が
上がったことにしか現れない。
"""

from collections.abc import Callable

import pytest

from src.generators.script_generator import ScriptGenerationError, ScriptGenerator
from src.models.script import ScriptDraft
from tests.factories import make_draft

_ARTICLE = "記事本文に数値は一つも出てこない。" * 5

# **分量の検査は2つあり、両方を同時に満たす必要がある**
# （`tests/test_script_grounding_enforcement.py` の `_draft` と同じ事情）。
# short/ja の予算は180〜240文字だが下限は緩く（`low // 2` = 90文字）見るので、
# 1セグメント36文字 × 3 = 108文字で通る。1セグメントの上限は48文字なので、
# **全体の上限を超えるにはセグメント数を増やすしかない**（制約は逆向きに働く）。
_SEGMENT = "この変更で何が起きたのかを、順を追って落ち着いた口調で説明していきます。"


def _draft(**overrides: object) -> ScriptDraft:
    """分量の検査を通る下書き（`make_draft` の既定は9文字で下限を割る）。"""
    payload: dict[str, object] = {"segment_narrations": [_SEGMENT] * 3}
    payload.update(overrides)
    return make_draft(**payload)


def _too_long_draft() -> ScriptDraft:
    """全体の分量**だけ**が上限を超える下書き。

    他の検査を同時に踏まないよう気を配る必要がある。配列長は4つとも揃え、
    `statement` は半数以下（`_validate_scenes`）に収める——踏むと
    「どの検査でフィードバックが出たか」を見ているテストが別の理由で
    赤くなり、しかもメッセージが変わるので原因が分かりにくい。
    """
    count = 8
    scenes = [
        {"layout": "compare", "items": ["従来", "新方式"], "relation": "切替"}
        if i % 2
        else {"layout": "statement", "items": [], "relation": ""}
        for i in range(count)
    ]
    return _draft(
        segment_narrations=[_SEGMENT] * count,
        image_prompts=[f"Scene {i}" for i in range(count)],
        text_overlays=[f"overlay {i}" for i in range(count)],
        scenes=scenes,
    )


def _good_draft() -> ScriptDraft:
    """すべての検査を通る下書き。"""
    return _draft()


class _Recorder:
    """`_request_script` の呼び出しを記録し、指定回数だけ失敗させる。"""

    def __init__(
        self,
        failures: int,
        bad: Callable[[], ScriptDraft] = _too_long_draft,
        good: Callable[[], ScriptDraft] = _good_draft,
    ) -> None:
        self.inputs: list[str] = []
        self._failures = failures
        self._bad = bad
        self._good = good

    def __call__(self, instructions: str, news_topic: str, draft_type: type) -> ScriptDraft:
        self.inputs.append(news_topic)
        if len(self.inputs) <= self._failures:
            return self._bad()
        return self._good()


def _generator(recorder: _Recorder, monkeypatch: pytest.MonkeyPatch) -> ScriptGenerator:
    """API を呼ばない `ScriptGenerator`（`__init__` は通信しない）。"""
    generator = ScriptGenerator("https://example.openai.azure.com", "dummy", "gpt-5.1")
    monkeypatch.setattr(generator, "_request_script", recorder)
    return generator


def test_2回目の入力には前回の失敗理由が載る(monkeypatch: pytest.MonkeyPatch) -> None:
    """1回目と同じ入力を送り直さないこと。**ここがこの修正の本体。**"""
    recorder = _Recorder(failures=1)
    _generator(recorder, monkeypatch).generate(_ARTICLE)

    assert len(recorder.inputs) == 2
    assert recorder.inputs[0] == _ARTICLE
    assert recorder.inputs[1] != _ARTICLE
    # 記事本文は残したまま、理由を足す（本文を落とすと別の記事の台本になる）
    assert _ARTICLE in recorder.inputs[1]
    assert "ナレーションが長すぎます" in recorder.inputs[1]


def test_フィードバックは累積せず元の入力から組み直す(monkeypatch: pytest.MonkeyPatch) -> None:
    """前回のフィードバックに重ねないこと。

    重ねると、既に直った制約の指示が残り続け、別の制約を壊す方向に効く。
    `PostGenerator._with_length_feedback` も元のプロンプトから組み直している。
    """
    recorder = _Recorder(failures=2)
    _generator(recorder, monkeypatch).generate(_ARTICLE)

    assert len(recorder.inputs) == 3
    assert recorder.inputs[2].count("直前の生成は") == 1


def test_挿絵の文字数超過でも理由を伝えて引き直す(monkeypatch: pytest.MonkeyPatch) -> None:
    """#61 で2回ぶんの試行を消費した失敗が、引き直しで回復すること。"""
    from src.models.scene import MAX_DETAIL_CHARS

    def _long_illustration() -> ScriptDraft:
        return _draft(
            illustration_concept={
                "subject": "a router directing each input to one of several stores",
                "key_details": ["a" * (MAX_DETAIL_CHARS + 11), "several identical stores"],
                "labels": ["入力"],
            }
        )

    recorder = _Recorder(failures=1, bad=_long_illustration)
    script = _generator(recorder, monkeypatch).generate(_ARTICLE)

    assert len(recorder.inputs) == 2
    assert "視覚要素が長すぎます" in recorder.inputs[1]
    # 引き直しで直った下書きが採用される
    assert script.illustration_concept.key_details[0] == "a small switch block"


def test_挿絵が長いままでも最終試行では動画を作る(monkeypatch: pytest.MonkeyPatch) -> None:
    """3回とも長いときは警告だけ残して採用すること。

    `key_details` は画面に一度も描かれない（画像生成プロンプトに連結される
    だけ）。挿絵の生成そのものの失敗は既に許されているのだから、その概念が
    11字長いことで動画を0本にするのは筋が通らない。
    """
    from src.models.scene import MAX_DETAIL_CHARS

    too_long = "a" * (MAX_DETAIL_CHARS + 11)

    def _long_illustration() -> ScriptDraft:
        return _draft(
            illustration_concept={
                "subject": "a router directing each input to one of several stores",
                "key_details": [too_long, "several identical stores"],
                "labels": ["入力"],
            }
        )

    recorder = _Recorder(failures=99, bad=_long_illustration)
    script = _generator(recorder, monkeypatch).generate(_ARTICLE)

    assert len(recorder.inputs) == ScriptGenerator.VALIDATION_RETRIES
    assert script.illustration_concept.key_details[0] == too_long


def test_フィードバックに出た数字は数値の根拠にならない(monkeypatch: pytest.MonkeyPatch) -> None:
    """接地の照合元は元の記事本文であること。

    フィードバック文は数字を含む（`上限240文字` / `text_overlays=7`）。
    照合元を `attempt_input` に変えると、**自分が足した数字で捏造が
    「根拠あり」になる**。リファクタで最も混ざりやすい1行なので見張る。
    """

    def _fabricated() -> ScriptDraft:
        # 240 は記事本文に無く、直前のフィードバック（上限240文字）にだけ出る
        return _draft(
            scenes=[
                {"layout": "compare", "items": ["240件", "従来"], "relation": "改善"},
                {"layout": "flow", "items": ["入力", "選択"], "relation": "変換"},
                {"layout": "statement", "items": [], "relation": ""},
            ]
        )

    recorder = _Recorder(failures=1, good=_fabricated)
    generator = _generator(recorder, monkeypatch)

    with pytest.raises(ScriptGenerationError, match="記事にない数値"):
        generator.generate(_ARTICLE)
