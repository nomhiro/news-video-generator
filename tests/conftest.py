"""pytest の共有設定。

このプロジェクトは `from config import Config` / `from src... import ...` の
形でリポジトリルートを import パスに前提している（Phase 2 で
src/newsvideo/ の正式パッケージに移行する予定）。
それまでは conftest.py でルートを sys.path に入れる。
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.storage.publications import PublicationStore  # noqa: E402
from tests.factories import make_draft  # noqa: E402 (sys.path 設定の後に import する)


@pytest.fixture
def draft_factory():
    """検証を通る最小の下書きを作るファクトリ。

    payload の実体は `tests/factories.py::make_draft`。複数のテストファイルが
    必要とするようになったため、ここでは薄いラッパーとして提供する
    （payload をここに複製すると `factories.py` と二重管理になる）。
    """
    return make_draft


class FakePostQueue:
    """投稿表のうち、記事カードが使う部分だけのフェイク。

    `X:予定` のバッジは投稿表（SQLite）を見る——下書きは記事データではなく
    あちらにあるため。**カードを描くルートすべてがこの依存を持つ**ので、
    DB を立てないテストでも渡す必要がある。
    """

    def __init__(self, article_ids: list[str] | None = None):
        self.article_ids = article_ids or []

    def list_upcoming(self, limit: int = 20) -> list[SimpleNamespace]:
        return [SimpleNamespace(article_id=aid) for aid in self.article_ids[:limit]]


@pytest.fixture
def post_queue() -> FakePostQueue:
    """空の投稿キュー。`X:予定` を出したいテストは記事 ID を足す。"""
    return FakePostQueue()


@pytest.fixture
def publications(tmp_path: Path) -> PublicationStore:
    """公開の記録。ただのファイルなのでフェイクにしない。"""
    return PublicationStore(tmp_path / "publications.json")
