"""gpt-image-2 の解像度制約の検証。

なぜ重要か: gpt-image-2 は両辺が16の倍数であることを要求するため、
動画の最終解像度である 1080x1920 をそのまま指定できない。
1080 は 16 の倍数ではない。この事実を知らずに「素直な値」を書くと
実行時に 400 が返る。validate_size() はそれを API 呼び出し前に止める。
"""

import pytest

from src.generators.image_generator import ImageGenerator, validate_size
from src.models.formats import SPECS, VideoFormat


def test_vertical_constant_is_valid() -> None:
    """ショート/TikTok 用の定数が制約を満たすこと。"""
    assert validate_size(SPECS[VideoFormat.SHORT].image_size) == (1152, 2048)


def test_horizontal_constant_is_valid() -> None:
    """ロング用の定数が制約を満たすこと。"""
    assert validate_size(SPECS[VideoFormat.LONG].image_size) == (2048, 1152)


def test_constants_are_exact_video_aspect_ratios() -> None:
    """生成サイズが動画の縦横比と厳密に一致すること。

    一致していれば ffmpeg は単純な縮小だけで済み、
    クロップやレターボックスが入らない。
    """
    w, h = validate_size(SPECS[VideoFormat.SHORT].image_size)
    assert w / h == pytest.approx(1080 / 1920)  # 9:16

    w, h = validate_size(SPECS[VideoFormat.LONG].image_size)
    assert w / h == pytest.approx(1920 / 1080)  # 16:9


def test_video_output_resolution_is_not_a_valid_request() -> None:
    """1080x1920 が指定できないことを明示的に記録する。

    「なぜ生成解像度と出力解像度が違うのか」を後から読む人に伝える。
    """
    with pytest.raises(ValueError, match="16の倍数"):
        validate_size("1080x1920")


@pytest.mark.parametrize(
    "size",
    [
        "720x1280",  # 9:16 の最小実用サイズ (921,600px)
        "864x1536",
        "1008x1792",
        "1152x2048",
        "2160x3840",  # 縦4K。総ピクセル数の上限ぴったり
        "1024x1024",
        "2048x1152",
    ],
)
def test_accepts_valid_sizes(size: str) -> None:
    width, height = validate_size(size)
    assert width % 16 == 0
    assert height % 16 == 0


@pytest.mark.parametrize(
    ("size", "reason"),
    [
        ("1080x1920", "16の倍数"),  # 1080 が 16 の倍数でない
        ("1152x2050", "16の倍数"),  # 2050 が 16 の倍数でない
        ("576x1024", "総ピクセル数"),  # 589,824px は下限 655,360 未満
        ("2160x4096", "長辺"),  # 4096 > 3840
        ("512x2048", "アスペクト比"),  # 4:1 は 3:1 を超える
        ("0x1024", "正の値"),
    ],
)
def test_rejects_invalid_sizes(size: str, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        validate_size(size)


@pytest.mark.parametrize("size", ["1152", "1152*2048", "axb", "", "1152x2048x1"])
def test_rejects_malformed_input(size: str) -> None:
    with pytest.raises(ValueError, match="形式が不正"):
        validate_size(size)


def test_upper_case_x_is_accepted() -> None:
    """ "1152X2048" のような表記も受け付けること。"""
    assert validate_size("1152X2048") == (1152, 2048)


@pytest.mark.parametrize(
    ("video_format", "expected"),
    [
        ("short", SPECS[VideoFormat.SHORT].image_size),
        ("tiktok", SPECS[VideoFormat.SHORT].image_size),
        ("long", SPECS[VideoFormat.LONG].image_size),
        ("unknown", SPECS[VideoFormat.SHORT].image_size),  # 不明な形式は縦を既定にする
    ],
)
def test_size_for_format(video_format: str, expected: str) -> None:
    generator = ImageGenerator.__new__(ImageGenerator)  # API 接続なしでメソッドだけ検証
    assert generator._size_for_format(video_format) == expected


def _stub_generator(monkeypatch: pytest.MonkeyPatch) -> tuple[ImageGenerator, list[str]]:
    """`_generate_single` を差し替えて、渡された size だけを記録する。

    `generate_batch` は ThreadPoolExecutor 経由で `_generate_single` を呼ぶため、
    実 API を呼ばずに「どの size が使われたか」だけを確かめられる。
    """
    generator = ImageGenerator.__new__(ImageGenerator)  # API 接続なしで組み立てる
    generator.max_concurrency = 1
    captured: list[str] = []

    def fake_generate_single(self: ImageGenerator, prompt: str, output_path, size: str, index: int):
        captured.append(size)
        output_path.write_bytes(b"fake")
        return output_path

    monkeypatch.setattr(ImageGenerator, "_generate_single", fake_generate_single)
    return generator, captured


def test_generate_batch_は_video_format_からサイズを導出する(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """既定動作（size 省略時）を変えていないことを確かめる。"""
    generator, captured = _stub_generator(monkeypatch)

    generator.generate_batch(["p"], tmp_path, video_format="long")

    assert captured == [SPECS[VideoFormat.LONG].image_size]


def test_generate_batch_は_size_指定を_video_format_より優先する(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """画像カードは video_format という概念を持たないため、
    直接サイズを渡せる必要がある（video_format の既定値 "short" が
    有効になってはいけない）。
    """
    generator, captured = _stub_generator(monkeypatch)

    generator.generate_batch(["p"], tmp_path, size="1024x1024")

    assert captured == ["1024x1024"]


def _stub_generator_capturing_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ImageGenerator, list[str]]:
    """`_request_image` を差し替えて、実際に API へ渡る最終文字列を記録する。

    `_enhance_prompt` は `_generate_single` の手前（`task()` クロージャの中）で
    適用されるため、装飾後の文字列を見るには `_generate_single` ではなく
    `_request_image` の境界で捕まえる必要がある。
    """
    generator = ImageGenerator.__new__(ImageGenerator)  # API 接続なしで組み立てる
    generator.max_concurrency = 1
    captured: list[str] = []

    def fake_request_image(self: ImageGenerator, prompt: str, size: str, index: int) -> bytes:
        captured.append(prompt)
        return b"fake"

    monkeypatch.setattr(ImageGenerator, "_request_image", fake_request_image)
    return generator, captured


def test_generate_batch_は_enhance_False_で動画用の装飾を付けない(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """画像カード（`CARD_STYLE_PROMPT`）は既に完結した指示を持つ。

    `_enhance_prompt` は動画用の1行シーン記述を飾るためのもので、
    完結済みのプロンプトに重ねると矛盾した指示が1つの文字列に混ざる
    （縦長構図の指示 vs 1024x1024、「ラベルの文字を描け」vs
    「テキストは描くな」）。`enhance=False` はこの重ね書きを止める。
    """
    generator, captured = _stub_generator_capturing_prompt(monkeypatch)
    card_prompt = 'Labels: render exactly these words, "CACHE", in a small hand-lettered font.'

    generator.generate_batch([card_prompt], tmp_path, size="1024x1024", enhance=False)

    assert captured == [card_prompt]
    assert "Do not render any text" not in captured[0]
    assert "Vertical portrait" not in captured[0]
    assert "Horizontal landscape" not in captured[0]


def test_generate_batch_は_既定で動画用の装飾を付ける(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """既定動作（`enhance` 省略時）を変えていないことの確認。"""
    generator, captured = _stub_generator_capturing_prompt(monkeypatch)

    generator.generate_batch(["a scene"], tmp_path, video_format="short")

    assert "Do not render any text" in captured[0]
    assert "Vertical portrait" in captured[0]
