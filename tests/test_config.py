"""設定の検証と、.env.example とのドリフト検出。

ドリフト検出を入れている理由: このプロジェクトは README・docs・
env.example がコードと乖離した状態で7か月放置され、
実装が一切使っていない API キー（Anthropic / ElevenLabs / fal.ai）を
「必要」と書き続けていた。設定の説明とコードは自動で突き合わせる。
"""

import re
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from config import DEFAULT_AI_SEARCH_QUERIES, Config
from tests.conftest import REPO_ROOT

REQUIRED_VALUES: dict[str, object] = {
    "azure_openai_endpoint": "https://example.openai.azure.com",
    "azure_openai_api_key": "dummy-key",
    "azure_openai_deployment": "gpt-5.1",
    "azure_openai_image_deployment": "gpt-image-2-1",
    "azure_speech_api_key": "dummy-speech-key",
}


def _config(**overrides: object) -> Config:
    """検証を通る最小の設定を作り、必要な項目だけ差し替える。

    `_env_file=None` で `.env` を読ませない。読ませると開発者の
    ローカル設定でテスト結果が変わる。
    """
    values = {**REQUIRED_VALUES, **overrides}
    # _env_file は pydantic-settings が実行時に解釈する引数で、
    # 生成された __init__ の型には現れないため型検査を抑制する。
    return Config(_env_file=None, **values)  # type: ignore[arg-type,call-arg]


def test_minimal_config_is_valid() -> None:
    config = _config()
    assert config.azure_openai_deployment == "gpt-5.1"


@pytest.mark.parametrize("field", sorted(REQUIRED_VALUES))
def test_required_field_missing_raises(field: str) -> None:
    """必須項目が欠けたら起動時に失敗すること。

    使う直前に None を踏むより、起動時に落ちた方が原因が分かりやすい。
    """
    values = {k: v for k, v in REQUIRED_VALUES.items() if k != field}
    with pytest.raises(ValidationError) as exc_info:
        Config(_env_file=None, **values)  # type: ignore[arg-type,call-arg]
    assert field in str(exc_info.value)


# --------------------------------------------------------------------------
# シークレットの取り扱い
# --------------------------------------------------------------------------


def test_api_key_is_a_secret() -> None:
    """APIキーが SecretStr であること。"""
    assert isinstance(_config().azure_openai_api_key, SecretStr)


def test_secrets_do_not_leak_into_repr() -> None:
    """設定を文字列化してもキーの平文が出ないこと。

    設定オブジェクトは例外やログに丸ごと出力されることがあるため、
    ここが漏れると実害がある。
    """
    config = _config(
        azure_openai_api_key="super-secret-key",
        tiktok_client_secret="tiktok-secret-value",
    )
    for rendered in (repr(config), str(config), str(config.model_dump())):
        assert "super-secret-key" not in rendered
        assert "tiktok-secret-value" not in rendered


def test_secret_value_is_retrievable() -> None:
    """必要な場所では平文を取り出せること。"""
    config = _config(azure_openai_api_key="the-key")
    assert config.azure_openai_api_key.get_secret_value() == "the-key"


# --------------------------------------------------------------------------
# 検証ルール
# --------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", ["example.openai.azure.com", "my-resource", "ftp://x"])
def test_endpoint_must_be_a_url(endpoint: str) -> None:
    """リソース名だけを入れる間違いを起動時に弾くこと。"""
    with pytest.raises(ValidationError, match="http"):
        _config(azure_openai_endpoint=endpoint)


def test_endpoint_trailing_slash_is_normalized() -> None:
    """末尾のスラッシュを落とすこと。

    落とさないと base_url が `.../openai/v1` を二重に持つ形になる。
    """
    assert (
        _config(azure_openai_endpoint="https://example.openai.azure.com/").azure_openai_endpoint
        == "https://example.openai.azure.com"
    )


@pytest.mark.parametrize("value", [0, -1, 21])
def test_concurrency_out_of_range_is_rejected(value: int) -> None:
    with pytest.raises(ValidationError):
        _config(image_max_concurrency=value)


@pytest.mark.parametrize("privacy", ["public", "private", "unlisted"])
def test_youtube_privacy_accepts_api_values(privacy: str) -> None:
    assert _config(youtube_default_privacy=privacy).youtube_default_privacy == privacy


@pytest.mark.parametrize("privacy", ["PUBLIC", "hidden", "friends", ""])
def test_youtube_privacy_rejects_other_values(privacy: str) -> None:
    """不正な値はアップロード時ではなく起動時に弾くこと。

    アップロード時に落ちると、そこまでの生成（画像6枚＋音声）が無駄になる。
    """
    with pytest.raises(ValidationError, match="YOUTUBE_DEFAULT_PRIVACY"):
        _config(youtube_default_privacy=privacy)


@pytest.mark.parametrize(
    "privacy",
    ["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY"],
)
def test_tiktok_privacy_accepts_api_values(privacy: str) -> None:
    assert _config(tiktok_default_privacy=privacy).tiktok_default_privacy == privacy


@pytest.mark.parametrize("privacy", ["PRIVATE", "public", "everyone"])
def test_tiktok_privacy_rejects_other_values(privacy: str) -> None:
    with pytest.raises(ValidationError, match="TIKTOK_DEFAULT_PRIVACY"):
        _config(tiktok_default_privacy=privacy)


def test_web_port_out_of_range_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _config(web_port=70000)


# --------------------------------------------------------------------------
# TikTok は任意機能
# --------------------------------------------------------------------------


def test_tiktok_is_optional() -> None:
    config = _config()
    assert config.is_tiktok_configured() is False


def test_tiktok_needs_both_values() -> None:
    assert _config(tiktok_client_key="k").is_tiktok_configured() is False
    assert _config(tiktok_client_secret="s").is_tiktok_configured() is False
    assert _config(tiktok_client_key="k", tiktok_client_secret="s").is_tiktok_configured() is True


# --------------------------------------------------------------------------
# AI検索クエリのパース
# --------------------------------------------------------------------------


def test_ai_search_queries_have_a_default() -> None:
    assert _config().ai_search_queries == list(DEFAULT_AI_SEARCH_QUERIES)


def test_ai_search_queries_parse_from_comma_separated_string() -> None:
    """環境変数にはカンマ区切りで書けること。

    pydantic は list 型を JSON として解釈しようとするため、
    素直な書き方を通すには変換が必要になる。
    """
    config = _config(ai_search_queries=" 生成AI , ChatGPT ,, Claude ")
    assert config.ai_search_queries == ["生成AI", "ChatGPT", "Claude"]


def test_empty_ai_search_queries_falls_back_to_default() -> None:
    assert _config(ai_search_queries="").ai_search_queries == list(DEFAULT_AI_SEARCH_QUERIES)


def test_ai_search_queries_accept_a_list() -> None:
    assert _config(ai_search_queries=["a", "b"]).ai_search_queries == ["a", "b"]


# --------------------------------------------------------------------------
# 呼び出し側の名前に合わせるプロパティ
# --------------------------------------------------------------------------


def test_voice_name_properties_mirror_the_env_fields() -> None:
    config = _config(azure_speech_voice_ja="ja-JP-X", azure_speech_voice_en="en-US-Y")
    assert config.voice_name_ja == "ja-JP-X"
    assert config.voice_name_en == "en-US-Y"


def test_speech_region_has_a_default() -> None:
    """リージョンは既定値を持つこと。

    日本語ナレーションが主用途なので japaneast を既定にしている。
    キーと違い、これを毎回書かせる意味がない。
    """
    assert _config().azure_speech_region == "japaneast"


def test_speech_key_is_a_secret() -> None:
    """Speech のキーも平文で漏れないこと。"""
    config = _config(azure_speech_api_key="speech-secret-value")
    for rendered in (repr(config), str(config), str(config.model_dump())):
        assert "speech-secret-value" not in rendered
    assert config.azure_speech_api_key.get_secret_value() == "speech-secret-value"


# --------------------------------------------------------------------------
# ディレクトリ作成
# --------------------------------------------------------------------------


def test_ensure_output_dirs_creates_all_subdirs(tmp_path: Path) -> None:
    config = _config(output_dir=tmp_path / "out")
    config.ensure_output_dirs()
    for subdir in ("audio", "images", "videos", "scripts"):
        assert (tmp_path / "out" / subdir).is_dir()


def test_ensure_news_dirs_creates_the_directory(tmp_path: Path) -> None:
    config = _config(news_data_dir=tmp_path / "news")
    config.ensure_news_dirs()
    assert (tmp_path / "news").is_dir()


# --------------------------------------------------------------------------
# 環境変数から読めること
# --------------------------------------------------------------------------


def test_reads_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """フィールド名の大文字がそのまま環境変数名になること。"""
    for field, value in REQUIRED_VALUES.items():
        monkeypatch.setenv(field.upper(), str(value))
    monkeypatch.setenv("IMAGE_MAX_CONCURRENCY", "7")

    config = Config(_env_file=None)  # type: ignore[call-arg]
    assert config.azure_openai_deployment == "gpt-5.1"
    assert config.image_max_concurrency == 7


# --------------------------------------------------------------------------
# .env.example とのドリフト検出
# --------------------------------------------------------------------------


def _documented_env_keys() -> set[str]:
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, flags=re.MULTILINE))


def _settings_env_keys() -> set[str]:
    """Config が読む環境変数名（フィールド名の大文字）。"""
    return {name.upper() for name in Config.model_fields}


def test_every_setting_is_documented() -> None:
    """Config の全項目が .env.example に載っていること。

    載っていないと、新しい設定項目の存在に誰も気付けない。
    """
    undocumented = _settings_env_keys() - _documented_env_keys()
    assert not undocumented, f".env.example に記載が無い設定があります: {sorted(undocumented)}"


def test_env_example_documents_no_unknown_keys() -> None:
    """.env.example に、Config が読まないキーが残っていないこと。

    実装が使っていない ANTHROPIC_API_KEY / ELEVENLABS_* / FAL_KEY が
    「必要」と書かれ続けていた再発を防ぐ。
    """
    stale = _documented_env_keys() - _settings_env_keys()
    assert not stale, f".env.example に Config が読まないキーが残っています: {sorted(stale)}"
