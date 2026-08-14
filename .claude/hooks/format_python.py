"""編集された Python ファイルを ruff で整形し、自動修正可能な指摘を直す。

Claude Code の PostToolUse hook から呼ばれる。hook の入力は stdin に
JSON で渡され、編集対象のパスは tool_input.file_path に入っている。

なぜ hook にするか: 整形と import 並べ替えを人（と AI）の手作業から外すと、
レビューで見るべき差分が「意図のある変更」だけになる。

Windows / POSIX の両方で動くよう、シェルに依存せず Python で書いている。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _edited_path() -> Path | None:
    """hook の入力から編集対象のパスを取り出す。"""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return None

    raw = (payload.get("tool_input") or {}).get("file_path")
    if not raw:
        return None
    return Path(raw)


def _is_target(path: Path) -> bool:
    """整形対象か判定する。"""
    if path.suffix != ".py":
        return False
    if not path.is_file():
        return False
    try:
        relative = path.resolve().relative_to(REPO_ROOT)
    except ValueError:
        return False  # リポジトリ外のファイルは触らない
    # 仮想環境や生成物は対象外
    return relative.parts[0] not in {".venv", "output", "data"}


def _run(args: list[str], target: Path) -> None:
    """ruff を実行する。失敗しても hook 自体は落とさない。

    整形に失敗したこと自体で編集をブロックしたくないため、
    終了コードは無視して stderr にだけ出す。
    """
    try:
        result = subprocess.run(
            ["uv", "run", "ruff", *args, str(target)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[format_python] ruff {args[0]} をスキップしました: {e}", file=sys.stderr)
        return

    if result.returncode != 0 and result.stderr:
        print(f"[format_python] ruff {args[0]}: {result.stderr.strip()}", file=sys.stderr)


def main() -> int:
    path = _edited_path()
    if path is None or not _is_target(path):
        return 0

    _run(["format"], path)
    _run(["check", "--fix"], path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
