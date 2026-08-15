"""定期実行が画像カードの5点セットを実際に渡すことの検証（Ruling R20）。

Task 6 の当初実装は `plan_daily_posts` に `card_generator` /
`image_generator` / `artifacts` / `jobs` / `output_dir` を受け取れるように
しただけで、呼び出し元（`_build_scheduler`）がそれを渡していなかった。
その状態では `CARD` は永久に画像無しのフォールバックに留まり、
テストも実行中のアプリも「繋がっていない」ことを教えてくれない
（Dead code that looks live）。ここでは `AppContext.build` から
`plan_daily_posts` まで、実際のオブジェクトが渡ることを確認する。

実 Azure / 実 DB への接続は避ける。`Config` はローカルの SQLite / ローカルの
ファイルストアだけで完結する値に絞り、`plan_daily_posts` と
`plan_daily_batch`（動画側の計画）はフェイクに差し替えてネットワークを
一切叩かない。
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

import src.web.dependencies as dependencies
from config import Config
from src.social.card_visual import CardVisualGenerator
from tests.test_config import REQUIRED_VALUES


def _config(tmp_path: Path) -> Config:
    """実 Azure / 実ネットワークに触れないローカル設定を作る。"""
    values: dict[str, object] = {
        **REQUIRED_VALUES,
        "output_dir": tmp_path / "output",
        "news_data_dir": tmp_path / "news",
        "database_url": f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        "x_posting_switch_path": tmp_path / "x_posting.json",
        "schedule_enabled": True,
    }
    # test_config.py の `_config` と同じ理由で型検査を抑制する
    # （pydantic-settings のキーワード引数は生成された __init__ の型と
    # 静的には合わない）。
    return Config(_env_file=None, **values)  # type: ignore[arg-type,call-arg]


@pytest.fixture
def captured_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """`plan_daily_posts` の呼び出しキーワード引数を記録する。

    `plan_daily_batch`（動画の計画）もフェイクにする。実装からニュース
    取得やジョブ投入を行わせず、X 投稿側の配線だけを見るため。
    """
    calls: dict[str, Any] = {}

    def fake_plan_daily_posts(*args: Any, **kwargs: Any) -> None:
        calls.update(kwargs)

    async def fake_plan_daily_batch(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(dependencies, "plan_daily_posts", fake_plan_daily_posts)
    monkeypatch.setattr(dependencies, "plan_daily_batch", fake_plan_daily_batch)
    return calls


def test_定期実行はplan_daily_postsに画像カードの5点セットを渡す(
    tmp_path: Path, captured_kwargs: dict[str, Any]
) -> None:
    context = dependencies.AppContext.build(_config(tmp_path))
    try:
        assert context.scheduler is not None  # schedule_enabled=True にしてある

        asyncio.run(context.scheduler._task())

        # 5点すべてが None ではなく、しかも「別の何か」ではなく
        # AppContext が実際に持っているインスタンスと同一であること。
        assert captured_kwargs["card_generator"] is not None
        assert isinstance(captured_kwargs["card_generator"], CardVisualGenerator)
        # Pipeline が既に持つ ImageGenerator を再利用している
        # （同じ gpt-image-2 クォータに対して2つ目のクライアントを
        # 作っていないことの確認）。
        assert captured_kwargs["image_generator"] is context.pipeline.image_generator
        assert captured_kwargs["artifacts"] is context.artifact_store
        assert captured_kwargs["jobs"] is context.jobs
        assert captured_kwargs["output_dir"] == context.config.output_dir / "cards"
    finally:
        context.aggregator.close()
