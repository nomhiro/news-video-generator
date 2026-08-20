"""pytest の共有設定。

このプロジェクトは `from config import Config` / `from src... import ...` の
形でリポジトリルートを import パスに前提している（Phase 2 で
src/newsvideo/ の正式パッケージに移行する予定）。
それまでは conftest.py でルートを sys.path に入れる。
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.factories import make_draft  # noqa: E402 (sys.path 設定の後に import する)


@pytest.fixture
def draft_factory():
    """検証を通る最小の下書きを作るファクトリ。

    payload の実体は `tests/factories.py::make_draft`。複数のテストファイルが
    必要とするようになったため、ここでは薄いラッパーとして提供する
    （payload をここに複製すると `factories.py` と二重管理になる）。
    """
    return make_draft
