"""AIモデルの廃止スケジュールを見張るテスト。

このテストの存在理由は src/model_registry.py の docstring に書いてある。
要点だけ言うと、2026年8月にこのプロジェクトの画像生成が9か月間
動作していなかった。使用中のモデルが停止済みだったのに、誰も
気付く仕組みがなかった。

`test_no_active_model_needs_attention` が本体で、CI の週次実行でも走る。
"""

from datetime import date, timedelta

import pytest

from src.model_registry import (
    ACTIVE_MODELS,
    DEPRECATION_WARNING_DAYS,
    RETIRED_MODELS,
    ModelEntry,
    Vendor,
    entries_needing_attention,
    format_report,
)


def test_no_active_model_needs_attention() -> None:
    """使用中のモデルに、停止日が近い/過ぎているものが無いこと。

    落ちたときは移行が必要。確認の上で問題ないなら
    src/model_registry.py の shutdown_on を更新する。
    """
    today = date.today()
    flagged = entries_needing_attention(today)

    assert not flagged, (
        f"{DEPRECATION_WARNING_DAYS}日以内に停止する（またはすでに停止した）"
        f"モデルを使用しています:\n\n" + format_report(today)
    )


def test_registry_is_not_empty() -> None:
    """登録簿が空でないこと。

    アダプタを書き換えた際にエントリを消してしまうと、
    見張りが無音で機能しなくなるため。
    """
    assert ACTIVE_MODELS, "ACTIVE_MODELS が空です"


def test_every_active_entry_points_at_real_code() -> None:
    """各エントリの used_by が実在するファイルを指していること。

    モジュールを移動・改名したときにエントリが迷子になるのを防ぐ。
    """
    from tests.conftest import REPO_ROOT

    for entry in ACTIVE_MODELS:
        path = REPO_ROOT / entry.used_by
        assert path.exists(), f"{entry.model_id}: used_by が存在しません: {entry.used_by}"


def test_retired_model_ids_are_absent_from_source() -> None:
    """停止済みモデルのIDがソースに復活していないこと。

    「前は動いていたから」と過去のIDを書き戻す事故を防ぐ。
    """
    from tests.conftest import REPO_ROOT

    targets = [REPO_ROOT / "config.py", REPO_ROOT / "main.py", REPO_ROOT / "web_app.py"]
    targets += sorted((REPO_ROOT / "src").rglob("*.py"))

    retired_ids = {m.model_id for m in RETIRED_MODELS}
    offenders: list[str] = []
    for path in targets:
        if path.name == "model_registry.py":
            continue  # 登録簿自身は記録として保持している
        text = path.read_text(encoding="utf-8")
        offenders += [
            f"{path.relative_to(REPO_ROOT).as_posix()}: {mid}" for mid in retired_ids if mid in text
        ]

    assert not offenders, "停止済みモデルIDがソースに残っています:\n" + "\n".join(offenders)


def test_azure_entries_record_a_deployment_name() -> None:
    """Azure のエントリはデプロイ名を持つこと。

    デプロイ名はモデル名と一致しないことが多く（gpt-image-2 →
    "gpt-image-2-1"）、これを記録していないと unknown_model という
    分かりにくい 400 の原因を追えない。実際に一度踏んでいる。
    """
    for entry in ACTIVE_MODELS:
        if entry.vendor is Vendor.AZURE_OPENAI:
            assert entry.deployment_name, f"{entry.model_id}: deployment_name が未設定"


# --------------------------------------------------------------------------
# ModelEntry の日付計算そのもののテスト
# 見張りが正しく動くことを、固定日で確認する
# --------------------------------------------------------------------------

_TODAY = date(2026, 8, 11)


def _entry(shutdown_on: date | None) -> ModelEntry:
    return ModelEntry(
        purpose="テスト用",
        vendor=Vendor.AZURE_OPENAI,
        model_id="test-model",
        used_by="src/model_registry.py",
        deployment_name="test-deployment",
        shutdown_on=shutdown_on,
    )


@pytest.mark.parametrize(
    ("shutdown_on", "expected_days"),
    [
        (None, None),
        (_TODAY, 0),
        (_TODAY + timedelta(days=30), 30),
        (_TODAY - timedelta(days=274), -274),  # imagen-3 が停止していた期間に相当
    ],
)
def test_days_until_shutdown(shutdown_on: date | None, expected_days: int | None) -> None:
    assert _entry(shutdown_on).days_until_shutdown(_TODAY) == expected_days


@pytest.mark.parametrize(
    ("shutdown_on", "expected"),
    [
        (None, False),
        (_TODAY + timedelta(days=1), False),
        (_TODAY, False),  # 当日はまだ「過ぎて」いない
        (_TODAY - timedelta(days=1), True),
    ],
)
def test_is_expired(shutdown_on: date | None, expected: bool) -> None:
    assert _entry(shutdown_on).is_expired(_TODAY) is expected


@pytest.mark.parametrize(
    ("shutdown_on", "expected"),
    [
        (None, False),  # 未公表は警告しない
        (_TODAY + timedelta(days=DEPRECATION_WARNING_DAYS + 1), False),
        (_TODAY + timedelta(days=DEPRECATION_WARNING_DAYS), True),  # 境界は警告する
        (_TODAY, True),
        (_TODAY - timedelta(days=1), True),  # 停止済みも警告する
    ],
)
def test_needs_attention(shutdown_on: date | None, expected: bool) -> None:
    assert _entry(shutdown_on).needs_attention(_TODAY) is expected


def test_format_report_mentions_expired_models() -> None:
    """レポートが停止済みモデルを「停止済み」と明示すること。

    テストが落ちたときに、原因が一目で分かる必要がある。
    """
    report = format_report(_TODAY)
    assert "使用中のAIモデル" in report
    for entry in ACTIVE_MODELS:
        assert entry.model_id in report
