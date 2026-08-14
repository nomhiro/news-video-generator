"""イメージに載せ忘れやすいものの検査。

Dockerfile を読むだけの静的な検査。実際にビルドはしない
（ビルドは数分かかり、`-m slow` でも重い）。

なぜ要るか
----------
起動時に必要なファイルをローカルでは常に持っているため、
「コンテナに載せたときだけ起動しない」という形で露見する。
実際に `migrations/` を入れ忘れ、Container Apps 上で
`CommandError: Path doesn't exist: /app/migrations` で起動に失敗した。
"""

import pytest

from tests.conftest import REPO_ROOT

DOCKERFILE = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")


def _copied_sources() -> set[str]:
    """COPY 命令のコピー元を集める。

    1行に複数のコピー元を書ける（`COPY a.py b.py ./`）ので、
    行の前方一致では検査できない。

    Returns:
        set[str]: コピー元のパス
    """
    sources: set[str] = set()
    for line in DOCKERFILE.splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("COPY "):
            continue
        # フラグ（--chown=... / --from=...）と、末尾のコピー先を除く
        parts = [p for p in stripped.split()[1:] if not p.startswith("--")]
        sources.update(parts[:-1])
    return sources


COPIED = _copied_sources()


@pytest.mark.parametrize(
    ("path", "why"),
    [
        ("config.py", "設定の読み込み"),
        ("web_app.py", "エントリポイント"),
        ("src/", "アプリ本体"),
        ("templates/", "HTML"),
        ("static/", "CSS と HTMX"),
        # 起動時に alembic upgrade head を走らせるため、この2つが無いと落ちる
        ("alembic.ini", "起動時のマイグレーション"),
        ("migrations/", "起動時のマイグレーション"),
    ],
)
def test_required_paths_are_copied(path: str, why: str) -> None:
    assert path in COPIED, f"{path} をイメージに入れていない（{why}）"


def test_migrations_directory_exists() -> None:
    """COPY する対象が実在すること。

    ディレクトリを移動・改名したときに Dockerfile だけ古い状態になるのを防ぐ。
    """
    assert (REPO_ROOT / "migrations" / "versions").is_dir()
    assert list((REPO_ROOT / "migrations" / "versions").glob("*.py")), "リビジョンが1つも無い"


def test_dockerignore_does_not_exclude_migrations() -> None:
    """.dockerignore が migrations を除外していないこと。

    COPY を書いても .dockerignore に載っていれば入らない。
    """
    ignored = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    patterns = {line.strip().strip("/") for line in ignored if line.strip()}
    assert "migrations" not in patterns
    assert "alembic.ini" not in patterns


@pytest.mark.parametrize("package", ["libssl3", "libasound2"])
def test_speech_sdk_native_dependencies_are_installed(package: str) -> None:
    """Speech SDK のネイティブ依存が入っていること。

    wheel は C++ ライブラリのラッパで、これが無いと import で落ちる。
    Windows の wheel は自己完結しているため、ここもコンテナだけで露見する。
    """
    assert package in DOCKERFILE


def test_japanese_font_is_installed() -> None:
    """日本語フォントが入っていること。

    無いと drawtext がテキストオーバーレイを描画できず、動画合成が失敗する。
    """
    assert "fonts-noto-cjk" in DOCKERFILE
    assert "VIDEO_FONT_PATH=" in DOCKERFILE


def test_secrets_are_not_baked_into_the_image() -> None:
    """シークレットを COPY していないこと。

    トークンと client_secrets は TokenStore（ローカル or Blob）から読む。
    イメージに焼き込むとレジストリを読める全員に配ることになる。
    """
    for secret in ("client_secrets.json", "youtube_token.json", "tiktok_token.json", ".env"):
        assert secret not in COPIED


def test_runs_as_a_non_root_user() -> None:
    assert "USER app" in DOCKERFILE


def test_dockerfile_and_docker_ignore_agree_on_output() -> None:
    """生成物のディレクトリはイメージに入れないこと（数百MBになる）。"""
    ignored = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "output" in ignored
