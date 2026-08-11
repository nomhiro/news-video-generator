"""設定の検証と、.env.example とのドリフト検出。

ドリフト検出を入れている理由: このプロジェクトは README・docs・
env.example がコードと乖離した状態で7か月放置され、
実装が一切使っていない API キー（Anthropic / ElevenLabs / fal.ai）を
「必要」と書き続けていた。設定の説明とコードは自動で突き合わせる。
"""

import re
from pathlib import Path

import pytest

from config import Config
from tests.conftest import REPO_ROOT


def _minimal_config(**overrides: object) -> Config:
    """検証を通る最小の設定を作る。"""
    values: dict[str, object] = {
        "azure_openai_endpoint": "https://example.openai.azure.com",
        "azure_openai_api_key": "dummy-key",
        "azure_openai_deployment": "gpt-5.1",
        "azure_openai_image_deployment": "gpt-image-2-1",
        "google_cloud_project": "dummy-project",
    }
    values.update(overrides)
    return Config(**values)  # type: ignore[arg-type]


def test_minimal_config_validates() -> None:
    assert _minimal_config().validate() == []


@pytest.mark.parametrize(
    ("field", "expected_message"),
    [
        ("azure_openai_endpoint", "AZURE_OPENAI_ENDPOINT"),
        ("azure_openai_api_key", "AZURE_OPENAI_API_KEY"),
        ("azure_openai_deployment", "AZURE_OPENAI_DEPLOYMENT"),
        ("azure_openai_image_deployment", "AZURE_OPENAI_IMAGE_DEPLOYMENT"),
        ("google_cloud_project", "GOOGLE_CLOUD_PROJECT"),
    ],
)
def test_missing_required_value_is_reported(field: str, expected_message: str) -> None:
    """必須値が欠けたら、環境変数名を含むエラーを返すこと。"""
    errors = _minimal_config(**{field: ""}).validate()
    assert any(expected_message in e for e in errors), errors


def test_image_deployment_has_no_default() -> None:
    """画像デプロイ名に既定値を置かないこと。

    デプロイ名はモデル名と一致しないことが多く（gpt-image-2 の
    デプロイ名が "gpt-image-2-1" だった）、推測した既定値は
    unknown_model という分かりにくい 400 を招く。
    """
    from dataclasses import fields

    field = next(f for f in fields(Config) if f.name == "azure_openai_image_deployment")
    assert field.default == ""


def test_rejects_zero_concurrency() -> None:
    errors = _minimal_config(image_max_concurrency=0).validate()
    assert any("IMAGE_MAX_CONCURRENCY" in e for e in errors)


def test_tiktok_is_optional() -> None:
    """TikTok 未設定でも設定検証は通ること（任意機能なので）。"""
    config = _minimal_config()
    assert config.validate() == []
    assert config.is_tiktok_configured() is False


def test_tiktok_configured_requires_both_values() -> None:
    assert _minimal_config(tiktok_client_key="k").is_tiktok_configured() is False
    assert _minimal_config(tiktok_client_secret="s").is_tiktok_configured() is False
    assert (
        _minimal_config(tiktok_client_key="k", tiktok_client_secret="s").is_tiktok_configured()
        is True
    )


def test_ensure_output_dirs_creates_all_subdirs(tmp_path: Path) -> None:
    config = _minimal_config(output_dir=tmp_path / "out")
    config.ensure_output_dirs()
    for subdir in ("audio", "images", "videos", "scripts"):
        assert (tmp_path / "out" / subdir).is_dir()


def test_ai_search_queries_have_a_default() -> None:
    assert _minimal_config().ai_search_queries


def test_ai_search_queries_parse_from_comma_separated_string() -> None:
    parsed = Config._parse_ai_search_queries(" 生成AI , ChatGPT ,, Claude ")
    assert parsed == ["生成AI", "ChatGPT", "Claude"]


def test_ai_search_queries_fall_back_when_empty() -> None:
    assert Config._parse_ai_search_queries("") == _minimal_config().ai_search_queries


# --------------------------------------------------------------------------
# .env.example とのドリフト検出
# --------------------------------------------------------------------------


def _documented_env_keys() -> set[str]:
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, flags=re.MULTILINE))


def _env_keys_read_by_config() -> set[str]:
    text = (REPO_ROOT / "config.py").read_text(encoding="utf-8")
    return set(re.findall(r'os\.getenv\(\s*"([A-Z][A-Z0-9_]*)"', text))


def test_every_env_var_config_reads_is_documented() -> None:
    """config.py が読む環境変数がすべて .env.example に載っていること。

    載っていないと、新しい設定項目の存在に誰も気付けない。
    """
    undocumented = _env_keys_read_by_config() - _documented_env_keys()
    assert not undocumented, f".env.example に記載が無い環境変数があります: {sorted(undocumented)}"


def test_env_example_documents_no_unused_keys() -> None:
    """.env.example に、コードが読まないキーが残っていないこと。

    実装が使っていない ANTHROPIC_API_KEY / ELEVENLABS_* / FAL_KEY が
    「必要」と書かれ続けていた再発を防ぐ。
    """
    stale = _documented_env_keys() - _env_keys_read_by_config()
    assert not stale, f".env.example にコードが読まないキーが残っています: {sorted(stale)}"
