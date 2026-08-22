"""数値の根拠の検査を、描くレンダラのときだけ強制することの検証。

なぜこの区別が必要か
--------------------
シーンのラベルを実際に**描くのは Remotion レンダラだけ**である。既定は
`VIDEO_RENDERER=ffmpeg` なので、既定の経路では捏造された数値は画面のどこにも
出ない。それでも例外にすると、このブランチの前提（「マージしても毎朝の自動生成の
振る舞いは変わらない」）が崩れ、**新しい失敗経路だけが増える**。しかも
`Pipeline.run_from_article` は本文を `content[:2000]` で切るため、切り捨てた後ろに
出てくるバージョン番号は「記事に無い数値」に見える。

強制しないときも検査自体は走らせて警告に残す。切り替える前に気付く唯一の経路。
"""

from typing import Any

import pytest

from src.generators.script_generator import ScriptGenerationError, ScriptGenerator
from tests.factories import make_draft

# 記事本文に無い数値をラベルに含む下書き
_UNGROUNDED_SCENES = [
    {"layout": "compare", "items": ["50%", "従来"], "relation": "改善"},
    {"layout": "flow", "items": ["入力", "選択"], "relation": "変換"},
    {"layout": "statement", "items": [], "relation": ""},
]

_ARTICLE = "記事本文に数値は一つも出てこない。" * 5


def _draft():
    """数値の根拠**以外**は全部通る下書きを作る。

    分量の検査は2つあり、**両方を同時に満たす必要がある**。どちらかで
    再生成が走ると、「根拠の検査が再試行を使ったか」を見ているテストが
    別の理由で赤くなる。

    - `check_length_budget`（全体）: 下限を大きく割らないこと。short/ja の
      予算は180〜240文字だが、下限は緩く（`low // 2` = 90文字）見るので
      3セグメント108文字で通る
    - `check_segment_budget`（1セグメント）: short/ja の上限は48文字。
      以前はここを `* 2`（72文字）にしていて、セグメント単位の検査を
      入れた時点で再生成が走るようになった

    2つの制約は逆方向に働く（全体は長くしたい、セグメントは短くしたい）ので、
    **セグメント数を増やさずに全体を伸ばすことはできない**。全体の下限が
    緩いことに乗って、1セグメント36文字に収めている。
    """
    segment = "この変更で何が起きたのかを、順を追って落ち着いた口調で説明していきます。"
    return make_draft(
        scenes=_UNGROUNDED_SCENES,
        segment_narrations=[segment, segment, segment],
    )


def _generator(monkeypatch: pytest.MonkeyPatch) -> ScriptGenerator:
    """API を呼ばない `ScriptGenerator` を作る。

    `__init__` は OpenAI クライアントを組み立てるだけで通信しない。
    `_request_script` を差し替えて、常に同じ下書きを返させる。
    """
    generator = ScriptGenerator("https://example.openai.azure.com", "dummy", "gpt-5.1")
    monkeypatch.setattr(
        generator,
        "_request_script",
        lambda *args, **kwargs: _draft(),
    )
    return generator


def test_grounding_is_enforced_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """既定は厳格。引数を忘れた呼び出し元が安全側に落ちること。"""
    generator = _generator(monkeypatch)
    with pytest.raises(ScriptGenerationError, match="記事にない数値"):
        generator.generate(_ARTICLE)


def test_grounding_can_be_relaxed_for_renderers_that_do_not_draw_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """描かないレンダラのときは台本生成を失敗させないこと。

    ラベルが画面に出ないのに3回の試行を使い切って FAILED になるのは、
    既定の経路に新しい失敗を持ち込むだけである。
    """
    generator = _generator(monkeypatch)
    script = generator.generate(_ARTICLE, enforce_scene_grounding=False)
    assert [scene.items for scene in script.scenes] == [["50%", "従来"], ["入力", "選択"], []]


def test_relaxed_grounding_still_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """強制しないときも検査は走り、警告に残ること。

    切り替える前に「捏造がどれくらい起きているか」を知る唯一の経路。
    """
    warnings: list[str] = []
    monkeypatch.setattr(
        "src.generators.script_generator.log_warning",
        lambda message, *args: warnings.append(message),
    )
    generator = _generator(monkeypatch)
    generator.generate(_ARTICLE, enforce_scene_grounding=False)
    assert any("50" in message for message in warnings), warnings


def test_relaxed_grounding_does_not_consume_a_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """再生成もしないこと。引き直せば1本ぶんの課金と時間が増える。"""
    calls: list[Any] = []
    generator = ScriptGenerator("https://example.openai.azure.com", "dummy", "gpt-5.1")

    def _request(*args: object, **kwargs: object):
        calls.append(args)
        return _draft()

    monkeypatch.setattr(generator, "_request_script", _request)
    generator.generate(_ARTICLE, enforce_scene_grounding=False)
    assert len(calls) == 1
