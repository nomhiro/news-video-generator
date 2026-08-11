"""使用中のAIモデルと、その廃止スケジュールの登録簿。

なぜこのモジュールがあるか
--------------------------
2026年8月、このプロジェクトの画像生成は9か月にわたって動作していなかった。
使っていた ``imagen-3.0-generate-002`` は 2025-11-10 にシャットダウンされて
いたが、誰も気付かなかった。原因は2つある。

1. モデルIDがアダプタの実装内に散在していて、「今どのモデルに依存して
   いるか」を一覧できる場所がなかった。
2. 廃止日をどこにも記録していなかったため、期限が近づいても警告が出なかった。

このモジュールは両方を塞ぐ。使用中の全モデルをここに集約し、
``tests/test_model_registry.py`` が「廃止日が近い、または過ぎている」
エントリを検出して失敗する。CI の週次実行でも同じテストが走る。

運用ルール
----------
- 新しいモデルを使い始めたら、必ずここにエントリを追加する
- アダプタ側ではモデルIDをハードコードせず、ここを参照する
- 廃止日が判明したら ``shutdown_on`` を埋める（未公表なら ``None``）
- テストが落ちたら、移行するか、確認した上で日付を更新する

廃止日はベンダー側の告知でしか分からない。Azure のデプロイ一覧
（``az cognitiveservices account deployment list``）には廃止日が含まれない
ため、以下の一次情報を人が確認して転記する。

- Azure OpenAI: https://learn.microsoft.com/azure/foundry/openai/concepts/model-retirement-schedule
- Gemini API: https://ai.google.dev/gemini-api/docs/deprecations
- Google Cloud TTS: https://cloud.google.com/text-to-speech/docs/release-notes
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

# 廃止日の何日前から警告するか。
# 90日あれば、移行先の調査・実装・実物での検証を落ち着いて回せる。
DEPRECATION_WARNING_DAYS = 90


class Vendor(StrEnum):
    """モデルの提供元。廃止情報の参照先が変わるため区別する。"""

    AZURE_OPENAI = "azure-openai"
    GOOGLE_CLOUD = "google-cloud"


@dataclass(frozen=True)
class ModelEntry:
    """使用中のモデル1件。

    Attributes:
        purpose: このモデルが担う役割（例: "台本生成"）
        vendor: 提供元
        model_id: ベンダー側のモデルID
        model_version: モデルのバージョン（無い場合は None）
        deployment_name: Azure のデプロイ名。モデルIDと一致しないことが多い
            （例: モデル gpt-image-2 のデプロイ名が "gpt-image-2-1"）。
            デプロイの概念が無い提供元では None
        used_by: 実装しているモジュール（grep の起点）
        shutdown_on: 停止日。未公表なら None
        notes: 補足
    """

    purpose: str
    vendor: Vendor
    model_id: str
    used_by: str
    model_version: str | None = None
    deployment_name: str | None = None
    shutdown_on: date | None = None
    notes: str = ""

    def days_until_shutdown(self, today: date) -> int | None:
        """停止日までの残り日数を返す。

        Args:
            today: 基準日

        Returns:
            残り日数。停止日が未公表なら None。過去なら負の値
        """
        if self.shutdown_on is None:
            return None
        return (self.shutdown_on - today).days

    def is_expired(self, today: date) -> bool:
        """すでに停止日を過ぎているか。"""
        remaining = self.days_until_shutdown(today)
        return remaining is not None and remaining < 0

    def needs_attention(self, today: date, warning_days: int = DEPRECATION_WARNING_DAYS) -> bool:
        """停止日が近い、または過ぎているか。"""
        remaining = self.days_until_shutdown(today)
        return remaining is not None and remaining <= warning_days


# --------------------------------------------------------------------------
# 使用中のモデル
#
# 実際のデプロイ名は環境変数で上書きできる（AZURE_OPENAI_DEPLOYMENT /
# AZURE_OPENAI_IMAGE_DEPLOYMENT）。ここに書くのは既定の想定値であり、
# 「どのモデル世代に依存しているか」を記録することが目的。
# --------------------------------------------------------------------------
ACTIVE_MODELS: tuple[ModelEntry, ...] = (
    ModelEntry(
        purpose="台本生成 (Responses API + Structured Outputs)",
        vendor=Vendor.AZURE_OPENAI,
        model_id="gpt-5.1",
        model_version="2025-11-13",
        deployment_name="gpt-5.1",
        used_by="src/generators/script_generator.py",
        shutdown_on=None,
        notes=(
            "gpt-4o からの置換先。廃止日は未公表。"
            "現行世代は gpt-5.4 (2026-03-05) / gpt-5.6-* (2026-07-09)。"
        ),
    ),
    ModelEntry(
        purpose="画像生成",
        vendor=Vendor.AZURE_OPENAI,
        model_id="gpt-image-2",
        model_version="2026-04-21",
        deployment_name="gpt-image-2-1",
        used_by="src/generators/image_generator.py",
        shutdown_on=None,
        notes=(
            "GA。imagen-3.0-generate-002 (2025-11-10 停止) の後継として採用。"
            "任意解像度に対応する唯一の gpt-image 系で、両辺16の倍数・"
            "長辺3840以下・総ピクセル数 655,360〜8,294,400 の制約がある。"
            "既定クォータは 5 images/min 程度で、これが生成速度の律速になる。"
        ),
    ),
    ModelEntry(
        purpose="音声合成 (Chirp 3 HD)",
        vendor=Vendor.GOOGLE_CLOUD,
        model_id="ja-JP-Chirp3-HD-Zephyr",
        deployment_name=None,
        used_by="src/generators/voice_generator.py",
        shutdown_on=None,
        notes=(
            "en-US-Chirp3-HD-Zephyr も併用。SSML の <mark> をサポートしないため"
            "セグメント境界のタイミングは個別生成＋実測で求めている。"
            "Phase 3 で Azure AI Speech（SSML <bookmark> 対応）へ移行予定。"
        ),
    ),
)


# --------------------------------------------------------------------------
# 過去に停止されたモデル
#
# 「この教訓を忘れない」ための記録。ここに載っているIDがコード中に
# 復活していないかをテストで検査する。
# --------------------------------------------------------------------------
RETIRED_MODELS: tuple[ModelEntry, ...] = (
    ModelEntry(
        purpose="画像生成（旧）",
        vendor=Vendor.GOOGLE_CLOUD,
        model_id="imagen-3.0-generate-002",
        used_by="(削除済み)",
        shutdown_on=date(2025, 11, 10),
        notes=(
            "9か月にわたり停止に気付かず、パイプライン全体が動作しない状態だった。"
            "この登録簿を作った直接の理由。"
        ),
    ),
    ModelEntry(
        purpose="画像生成（旧・後継も停止）",
        vendor=Vendor.GOOGLE_CLOUD,
        model_id="imagen-4.0-generate-001",
        used_by="(未使用)",
        shutdown_on=date(2026, 8, 17),
        notes="Imagen 4 系。Imagen に留まる選択肢が無いことの根拠。",
    ),
)


def entries_needing_attention(
    today: date, warning_days: int = DEPRECATION_WARNING_DAYS
) -> list[ModelEntry]:
    """停止日が近い、または過ぎている使用中モデルを返す。

    Args:
        today: 基準日
        warning_days: 何日前から警告するか

    Returns:
        該当するエントリ（無ければ空リスト）
    """
    return [m for m in ACTIVE_MODELS if m.needs_attention(today, warning_days)]


def format_report(today: date) -> str:
    """使用中モデルの一覧を人が読める形で返す。

    CLI やテストの失敗メッセージで使う。

    Args:
        today: 基準日

    Returns:
        整形済みの複数行テキスト
    """
    lines = [f"使用中のAIモデル ({today.isoformat()} 時点)", ""]
    for m in ACTIVE_MODELS:
        remaining = m.days_until_shutdown(today)
        if remaining is None:
            status = "停止日: 未公表"
        elif remaining < 0:
            status = f"停止済み ({m.shutdown_on} / {-remaining}日前)"
        else:
            status = f"停止まで {remaining}日 ({m.shutdown_on})"
        deployment = f" [デプロイ名: {m.deployment_name}]" if m.deployment_name else ""
        lines.append(f"  {m.purpose}")
        lines.append(f"    {m.vendor}: {m.model_id}{deployment}")
        lines.append(f"    {status}")
        lines.append(f"    実装: {m.used_by}")
        lines.append("")
    return "\n".join(lines)
