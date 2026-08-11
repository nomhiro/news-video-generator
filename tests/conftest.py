"""pytest の共有設定。

このプロジェクトは `from config import Config` / `from src... import ...` の
形でリポジトリルートを import パスに前提している（Phase 2 で
src/newsvideo/ の正式パッケージに移行する予定）。
それまでは conftest.py でルートを sys.path に入れる。
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
