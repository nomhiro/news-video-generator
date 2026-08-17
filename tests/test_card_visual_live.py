"""画像カードの実物確認。

`gpt-image-2` の実 API を叩き、コンテンツフィルタ・スキーマの通り・
実際の解像度は完全にはフェイクで検証できない。1枚だけ生成して確かめる。

**画像クォータを消費するので既定では走らせない。**
`uv run pytest -m live -k card_visual` で明示的に実行する。
"""

import io

import pytest
from PIL import Image

from config import Config
from src.generators.image_generator import ImageGenerator
from src.social.card_visual import CARD_IMAGE_SIZE, CardVisual, build_card_prompt


@pytest.mark.live
def test_画像カードを実際に1枚生成する(tmp_path) -> None:
    config = Config()  # type: ignore[call-arg]  # .env から必須項目を読む
    generator = ImageGenerator(
        endpoint=config.image_endpoint,
        api_key=config.image_api_key.get_secret_value(),
        deployment=config.azure_openai_image_deployment,
        max_concurrency=1,
    )
    visual = CardVisual(
        subject="A cache that reuses previous model inputs to cut cost.",
        key_details=["a funnel narrowing", "two arrows returning to a store"],
        labels=["CACHE", "REUSED"],
        caption_ja="同じ入力を使い回すことで推論コストが下がる。",
    )
    prompt = build_card_prompt(visual)

    paths = generator.generate_batch([prompt], tmp_path, size=CARD_IMAGE_SIZE)

    assert len(paths) == 1
    image = Image.open(io.BytesIO(paths[0].read_bytes()))
    assert image.format == "PNG"
    assert image.size == (1024, 1024)
