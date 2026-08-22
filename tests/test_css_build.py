"""生成済み CSS（`static/css/app.css`）がテンプレートと一致していることの検証。

なぜ要るか
----------
`app.css` は Tailwind の生成物で、**テンプレートで実際に使われているクラスだけ**が
入る。クラスを足して `npm run build:css` を忘れると、そのスタイルは効かない
——しかもテンプレートは正しく描画されるので、**画面を見るまで気付かない**
（CLAUDE.md「テンプレートに Tailwind クラスを足したら CSS を再生成する」）。

この検査は走査範囲が閉じていて初めて成立する。自動検出のままだと
`CLAUDE.md` の散文を書き換えるだけで生成物が変わり、一致検査が
無関係な理由で落ちるようになる。だから2本セットにしてある。

Node が要るので `slow`（既定の `pytest` からは外れ、pre-push で走る）。
"""

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

REPO_ROOT = Path(__file__).resolve().parent.parent
TAILWIND_CLI = REPO_ROOT / "node_modules" / "@tailwindcss" / "cli" / "dist" / "index.mjs"
INPUT_CSS = REPO_ROOT / "static" / "css" / "input.css"
OUTPUT_CSS = REPO_ROOT / "static" / "css" / "app.css"


@pytest.fixture
def tailwind_cli() -> Path:
    """Tailwind の CLI。無ければ skip する。

    `.bin` のシムではなく `index.mjs` を `node` で直接叩く。Windows では
    `.bin` に `.cmd` / `.ps1` が並び、どれを呼ぶかで挙動が変わるため。
    """
    if shutil.which("node") is None:
        pytest.skip("node が PATH にありません")
    if not TAILWIND_CLI.exists():
        pytest.skip("node_modules がありません（npm install を実行してください）")
    return TAILWIND_CLI


def test_走査範囲をテンプレートに閉じている() -> None:
    """`source(none)` を外さないこと。

    Tailwind v4 の自動検出は `@source` に**加えて**プロジェクト全体を走査する。
    閉じていないと、**ドキュメントの散文に書かれたクラス名まで CSS に入る**
    ——実測で `bg-blue-600`（CLAUDE.md が「使ってはいけない例」として挙げて
    いる綴り）や `table` / `transition` / `filter` など11個の死んだ規則、
    2,709バイトぶんが入っていた。

    害は容量だけではない。`npm run build:css` の出力がドキュメントの編集で
    変わるので、**無関係な差分が出る**うえ、下の一致検査も成立しなくなる。
    """
    text = INPUT_CSS.read_text(encoding="utf-8")

    assert "source(none)" in text, "自動検出を無効にする source(none) が消えている"
    assert '@source "../../templates/**/*.html"' in text, "テンプレートの明示指定が消えている"


def test_生成済みCSSがテンプレートと一致している(tailwind_cli: Path, tmp_path: Path) -> None:
    """`app.css` が最新のテンプレートから生成されたものであること。

    落ちたときの直し方は `npm run build:css` を実行して差分をコミットする
    こと。**手で `app.css` を編集してはいけない**（生成物なので次の
    再生成で消える）。
    """
    rebuilt = tmp_path / "app.css"

    result = subprocess.run(
        [
            "node",
            str(tailwind_cli),
            "-i",
            str(INPUT_CSS),
            "-o",
            str(rebuilt),
            "--minify",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        # **`text=True` にしない。** ロケールの encoding でデコードするため、
        # Windows の cp932 では Tailwind がバナーに出す `≈` が
        # `UnicodeDecodeError` になる（実際に警告が出た）。失敗時の
        # メッセージに使うだけなので、置換しながら UTF-8 で読む。
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0, f"CSS のビルドに失敗しました: {result.stderr}"
    assert rebuilt.read_bytes() == OUTPUT_CSS.read_bytes(), (
        "static/css/app.css がテンプレートと一致しません。"
        "npm run build:css を実行して差分をコミットしてください"
    )
