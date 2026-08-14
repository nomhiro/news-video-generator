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

import pathlib

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


# --------------------------------------------------------------------------
# コンテナの CPU 割り当てを尊重すること
#
# ffmpeg の既定（-threads 0）はホストのコア数だけスレッドを立てる。
# コンテナの割り当てを見ないので、2 vCPU の環境で 20 スレッドが動き、
# スレッドごとのフレームバッファでメモリを食い潰して OOM killer に
# 殺された（長尺 1920x1080 / 307秒 が 終了コード -9）。
# --------------------------------------------------------------------------


def test_thread_count_follows_the_cgroup_v2_quota(tmp_path: pathlib.Path) -> None:
    """cgroup v2 の割り当てからスレッド数を決めること。"""
    from src.generators.video_composer import _available_cpus

    cpu_max = tmp_path / "cpu.max"
    cpu_max.write_text("200000 100000", encoding="utf-8")  # = 2 CPU

    assert _available_cpus(cpu_max=cpu_max) == 2


def test_thread_count_follows_the_cgroup_v1_quota(tmp_path: pathlib.Path) -> None:
    """cgroup v1 でも読めること。"""
    from src.generators.video_composer import _available_cpus

    quota = tmp_path / "quota"
    period = tmp_path / "period"
    quota.write_text("400000", encoding="utf-8")
    period.write_text("100000", encoding="utf-8")

    assert _available_cpus(cpu_max=tmp_path / "missing", quota_path=quota, period_path=period) == 4


def test_unlimited_quota_falls_back_to_cpu_count(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """制限が無い（ローカル実行）なら os.cpu_count() を使うこと。"""
    from src.generators import video_composer

    cpu_max = tmp_path / "cpu.max"
    cpu_max.write_text("max 100000", encoding="utf-8")
    monkeypatch.setattr(video_composer.os, "cpu_count", lambda: 8)

    assert video_composer._available_cpus(cpu_max=cpu_max) == 8


def test_at_least_one_cpu(tmp_path: pathlib.Path) -> None:
    """0 にならないこと（-threads 0 は「自動」を意味してしまう）。"""
    from src.generators.video_composer import _available_cpus

    cpu_max = tmp_path / "cpu.max"
    cpu_max.write_text("50000 100000", encoding="utf-8")  # 0.5 CPU

    assert _available_cpus(cpu_max=cpu_max) == 1


def test_composer_passes_a_thread_limit() -> None:
    """ffmpeg のコマンドに -threads を渡していること。

    渡さないとホストのコア数で動き、コンテナのメモリ制限を超える。
    """
    source = (REPO_ROOT / "src" / "generators" / "video_composer.py").read_text(encoding="utf-8")
    assert '"-threads",' in source
    assert "_available_cpus()" in source
