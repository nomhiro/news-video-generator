"""自動投稿の有効/無効を持つスイッチ。

なぜ DB ではなくファイルか
--------------------------
当時のジョブ表・投稿表の SQLite はコンテナのローカルディスクにあり、
リビジョン更新で消えた。スイッチをそこに置くと、画面で有効にした翌日に
マージした時点で**黙って投稿が止まる**。だから実体を Azure Files 上の
ファイルにした（記事の選択状態と同じ場所）。

DB は 2026-08-23 に共有の PostgreSQL へ移して消えなくなったが、**ここは
動かしていない**——人が画面で切り替えた意図の権威なので、移すなら移行手順と
セットで行う（`src/storage/publications.py` と同じ判断）。

環境変数 X_POSTING_ENABLED は「ファイルが無いときの初期値」でしかない。
一度画面で切り替えたら、以降はファイルが権威。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from src.utils.logger import log_error


class PostingSwitch:
    """自動投稿の有効/無効。"""

    def __init__(self, path: Path, default_enabled: bool):
        """初期化する。

        Args:
            path: スイッチの実体（Azure Files 上を想定）
            default_enabled: ファイルが無いときの値
        """
        self._path = path
        self._default = default_enabled

    def is_enabled(self) -> bool:
        """投稿してよいか。

        読めない・壊れている場合は既定値として扱う。例外にすると、
        壊れたファイルのせいで画面が開かず、止めることも直すことも
        できなくなる。
        """
        if not self._path.exists():
            return self._default
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return bool(data["enabled"])
        except (json.JSONDecodeError, KeyError, TypeError, OSError) as e:
            log_error(f"投稿スイッチを読めませんでした（既定値 {self._default} を使います）: {e}")
            return self._default

    def set_enabled(self, value: bool) -> None:
        """切り替えて保存する。

        一時ファイル + replace で原子的に書く。書き込み中に落ちると
        壊れた JSON が残り、次回の判定が既定値に戻る。
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        temp_path = Path(temp_name)
        try:
            with open(fd, "w", encoding="utf-8") as f:
                json.dump({"enabled": value}, f)
            temp_path.replace(self._path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
