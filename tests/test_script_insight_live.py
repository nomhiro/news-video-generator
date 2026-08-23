"""独自解説を含む台本を実 API で生成する（`-m live`）。

なぜ実APIで確かめるか
--------------------
Issue #2 の受け入れ条件は「実際に生成して、ニュースのなぞりではなく
独自の解説になっていることを目視で確認する」ことである。
**スキーマは「入っていること」を担保できるが「質」は担保できない。**
フィールドを埋めるだけの薄い出力になっていないかは、実物を読むしかない。

このテストは台本だけを生成する。画像・音声・ffmpeg は動かさないので、
`gpt-image-2` のクォータ（サブスクリプション・リージョン単位で上限4）を
消費せず、パイプライン全体を回すより大幅に安い。

自動アサーションは構造だけにしてある（フィールドが埋まっている、
説明文に出典がある）。内容の良し悪しは判定しない。

実行方法:
    uv run pytest -m live -k insight

生成物は output/scripts/insight_live_*.json に残るので、それを読んで
Issue #2 にコメントする。
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from config import Config
from src.generators.script_generator import ScriptGenerator
from src.models.formats import get_spec
from src.models.script import draft_type_for

pytestmark = pytest.mark.live

# 目視確認用の3本。ニュース記事の代わりに、実務インパクトと技術的な仕組みの
# 両方を語れる題材を選んでいる（当たり障りのない話題だと、なぞりでも
# 成立してしまって差が見えない）。
TOPICS = [
    (
        "Azure OpenAI が gpt-image-2 を一般提供開始\n\n"
        "Microsoft は画像生成モデル gpt-image-2 の一般提供を開始した。"
        "従来モデルと比べて指示追従性が向上し、画像内のテキスト描画の精度も上がったとしている。"
        "料金は生成解像度と品質設定に応じた従量課金で、リージョンごとにクォータが設定される。",
        "https://example.com/news/gpt-image-2-ga",
    ),
    (
        "Python 3.14 で free-threaded ビルドが正式サポートに\n\n"
        "Python 3.14 では GIL を無効化した free-threaded ビルドが実験的位置付けから"
        "正式サポートに移行した。C拡張の互換性対応が進み、主要な数値計算ライブラリが"
        "対応版を公開している。既存の単一スレッド性能への影響は数%程度に収まったとされる。",
        "https://example.com/news/python-314-free-threading",
    ),
    (
        "Azure Container Apps が GPU ワークロードのサーバーレス課金に対応\n\n"
        "Azure Container Apps で GPU を使うワークロードを、リクエストが無い間は"
        "課金されない形で動かせるようになった。コールドスタートはモデルの読み込み時間に"
        "依存するため、イメージへのモデル同梱が推奨されている。",
        "https://example.com/news/aca-serverless-gpu",
    ),
]


@pytest.fixture(scope="module")
def generator() -> ScriptGenerator:
    """実設定から台本生成器を組み立てる。"""
    try:
        config = Config.from_env()
    except ValidationError as e:
        pytest.skip(f"Azure OpenAI の設定が揃っていません: {e}")
    return ScriptGenerator(
        endpoint=config.azure_openai_endpoint,
        api_key=config.azure_openai_api_key.get_secret_value(),
        deployment=config.azure_openai_deployment,
    )


@pytest.mark.parametrize(("topic", "source_url"), TOPICS, ids=["image", "python", "aca"])
def test_generates_script_with_insight(
    generator: ScriptGenerator, topic: str, source_url: str
) -> None:
    """独自解説と出典を含む台本が生成できること。

    質はアサートしない（スキーマでは担保できない）。目視確認用の
    JSON を output/scripts/ に残すのがこのテストの主目的。
    """
    script = generator.generate(topic, language="ja", video_format="short", source_url=source_url)

    # 構造の確認。バリデータが通っている以上ここは通るはずだが、
    # 「実 API 経由でも通る」ことを1度は見ておく
    assert script.technical_insight.strip()
    assert script.practical_impact.strip()
    assert script.source_url == source_url
    assert source_url in script.description

    # 目視確認用に残す。tmp_path ではなく output/scripts に置く
    # （テスト後に消えると受け入れ条件の確認ができない）
    out_dir = Path("output") / "scripts"
    slug = source_url.rsplit("/", 1)[-1]
    script.to_json_file(out_dir / f"insight_live_{slug}_ja.json")

    # 分量の回帰を見張る。構成を5パートに割った直後の実測では
    # 3本すべてが予算を超え、1本は63秒（上限60秒）になった
    # （パートを増やすとモデルは各パートに書き足す）。
    # generate() は最終試行でも超過していると警告だけ出して採用するので、
    # ここで見ないと静かに尺が伸びる。
    spec = get_spec("short")
    assert script.estimated_duration <= spec.max_duration_sec, (
        f"尺が上限を超えました: {len(script.full_narration)}文字 / "
        f"{script.estimated_duration}秒 (上限{spec.max_duration_sec}秒)"
    )


def test_probe_whether_the_schema_pins_the_array_count(generator: ScriptGenerator) -> None:
    """**実験**: 配列の `minItems`/`maxItems` が文法として強制されるか。

    受け入れ条件ではない。どちらの結果でも #61 の修正（フィードバック再生成 +
    要素数のスキーマ化）は成立する——これは「何が効いているか」を知るための
    観測であり、失敗させる意味が無いので `pytest.skip` で結果を報告する。

    切り分け方: **プロンプトと違う個数をスキーマで要求する。** short の
    プロンプトは「4つの配列を必ず6個ずつ」と書いてあるので、そこへ 7 を
    要求した型を渡す。

    - 7要素が返れば → Azure が `minItems`/`maxItems` を文法に落としている
    - 6要素で `ValidationError` になれば → キーワードは無視されている。
      つまり配列長の不一致から救っているのはフィードバック再生成だけ、
      という結論になる（Azure の Learn ページは 2026-08 時点で
      `minItems`/`maxItems` を unsupported と書いており、そちらが正しい）

    素の `generate()` ではなく `_request_script` を1回だけ呼ぶ。`generate()`
    は要素数が揃った下書きしか返さないので、**そこを見ても恒真の assert に
    なって何も観測できない。**
    """
    topic, _ = TOPICS[0]
    spec = get_spec("short")
    mismatched = spec.segment_count + 1
    instructions = ScriptGenerator._build_system_prompt("ja", "short")

    try:
        draft = generator._request_script(instructions, topic, draft_type_for(mismatched))
    except ValidationError as e:
        pytest.skip(
            f"minItems は強制されていない（プロンプトの{spec.segment_count}個が返った）: {e}"
        )

    counts = {
        "segment_narrations": len(draft.segment_narrations),
        "image_prompts": len(draft.image_prompts),
        "text_overlays": len(draft.text_overlays),
        "scenes": len(draft.scenes),
    }
    pytest.skip(f"minItems は強制されている（{mismatched}個を要求して {counts}）")
