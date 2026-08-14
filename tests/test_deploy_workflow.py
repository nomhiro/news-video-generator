"""CD とローカル hooks の配線の検査。

ファイルを読むだけの静的な検査。実際に Actions を回すことはしない。

なぜ要るか
----------
`main` にマージしても反映されない状態が実際に起きていた
（PR #14 のマージが 14:18 UTC、稼働リビジョンの作成が 12:09 UTC。
反映は人が `azd deploy` を打つまで起きなかった）。

直したあとの壊れ方は「静かに元に戻る」形になる。

- ワークフローに `azd provision` を足す → `containerImage` がプレースホルダに
  戻り、8080 待ち受けの quickstart イメージのリビジョンが Activating のまま残る
- 生存確認の待ちを外す → 起動していないのにジョブが緑になる
- `pre-push` から `uv lock --check` を外す → ロックファイルの更新漏れを
  検出する経路が無くなる（ローカルは同期済みなので手元では露見しない）

いずれも動くコードのまま成立してしまうので、ここで押さえる。
"""

import pathlib
import re
import shutil
import subprocess

import pytest

from tests.conftest import REPO_ROOT

WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
DEPLOY_WORKFLOW = WORKFLOW_DIR / "deploy.yml"
WAIT_SCRIPT = REPO_ROOT / ".github" / "scripts" / "wait_for_revision.sh"
PRE_PUSH = REPO_ROOT / ".githooks" / "pre-push"


def _without_comments(path: pathlib.Path) -> str:
    """`#` から始まる行を落とす。

    「なぜそうしたか」を書いたコメントに禁止語（`azd provision` など）が
    現れるため、本文をそのまま検査すると自分のコメントに引っかかる。
    """
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Actions は CD だけに使う
# --------------------------------------------------------------------------


def test_only_the_cd_workflow_exists() -> None:
    """`.github/workflows/` に CD の1本だけがあること。

    lint / 型 / テストをランナーで再実行すると、ローカルで数十秒で終わるものに
    毎 push 分の待ちが乗る。チェックは pre-push に寄せてある。
    """
    workflows = sorted(p.name for p in WORKFLOW_DIR.glob("*.y*ml"))

    assert workflows == ["deploy.yml"], f"CD 以外のワークフローがある: {workflows}"


def test_deploy_workflow_never_provisions() -> None:
    """デプロイのワークフローが provision を走らせないこと。

    `azd provision` は `containerImage` を `main.parameters.json` の既定
    （mcr.microsoft.com/k8se/quickstart:latest）に戻しうる。8080 を待ち受ける
    イメージなのでプローブが通らず、Activating のままリビジョンが残る。
    """
    workflow = _without_comments(DEPLOY_WORKFLOW)

    # `azd provision` の2語だけでは azd up / azd env / az deployment group create
    # などがすり抜ける。azd はそもそも呼ばない前提なので語として弾く。
    for pattern, why in [
        (r"\bazd\b", "azd は CD から呼ばない"),
        (r"az\s+deployment\b", "ARM デプロイを走らせない"),
        (r"--template-file\b", "bicep を当てない"),
    ]:
        assert re.search(pattern, workflow) is None, f"{pattern} が現れている（{why}）"


def test_deploy_workflow_does_not_build_in_acr_tasks() -> None:
    """ビルドを ACR Tasks（`az acr build`）に投げないこと。

    Dockerfile が `RUN --mount=type=cache` を使っている。これは BuildKit 専用の
    構文で、ACR Tasks の quick build には BuildKit を有効にする口が無いため
    `the --mount option requires BuildKit` で落ちる。
    ランナー上の docker（BuildKit 既定で有効）でビルドする。
    """
    workflow = _without_comments(DEPLOY_WORKFLOW)
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    # 前提が変わったら（cache mount を止めたら）この検査も見直す
    assert "RUN --mount=" in dockerfile, "BuildKit 専用構文が消えている。この検査を見直す"
    assert "az acr build" not in workflow
    assert "docker build" in workflow


# --------------------------------------------------------------------------
# デプロイの安全装置
# --------------------------------------------------------------------------


def test_deploy_workflow_uses_oidc_without_long_lived_secrets() -> None:
    """OIDC で認証すること（サービスプリンシパルのパスワードを置かない）。"""
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    assert "id-token: write" in workflow
    assert "azure/login@v2" in workflow
    # 長期シークレットの置き場所として使われがちな名前が現れないこと
    for forbidden in ("AZURE_CREDENTIALS", "client-secret", "--password"):
        assert forbidden not in workflow, f"{forbidden} は使わない"


def test_deploy_workflow_serializes_deployments() -> None:
    """同時デプロイを1本に絞り、進行中のものを打ち切らないこと。

    途中で切ると Container App が Activating のまま残る。
    """
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    assert "concurrency:" in workflow
    assert "cancel-in-progress: false" in workflow


def test_deploy_workflow_has_a_job_timeout() -> None:
    """ジョブ全体のタイムアウトがあること。

    生存確認のポーリングや `logs show` が詰まると、既定の6時間まで回り続ける。
    """
    assert "timeout-minutes:" in DEPLOY_WORKFLOW.read_text(encoding="utf-8")


def test_deploy_workflow_does_not_use_the_cli_long_running_poller() -> None:
    """`az containerapp update` は `--no-wait` で返させること。

    az の LRO ポーリングはサブスクリプションスコープの
    containerappOperationStatuses を読むため、権限をリソースグループ以下に
    絞ると更新自体は成功しているのに CLI が失敗を返しうる。
    待つのは自前のスクリプトの仕事。
    """
    workflow = _without_comments(DEPLOY_WORKFLOW)

    assert "--no-wait" in workflow


def test_deploy_workflow_makes_the_revision_suffix_unique_per_attempt() -> None:
    """再実行でも suffix が衝突しないこと。

    GitHub の Re-run では `run_number` が変わらない（変わるのは `run_attempt`）。
    既存リビジョンと同じ suffix は ACA に拒否されるため、失敗して再実行する
    という最も起こりやすい経路で2回目が必ず落ちる。
    """
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    assert "github.run_attempt" in workflow


def test_deploy_workflow_installs_the_containerapp_extension() -> None:
    """`az containerapp` の拡張機能を明示的に入れること。

    拡張機能なのでランナーに入っておらず、入れないと動的インストールの
    確認プロンプトに当たって落ちる。
    """
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    assert "az extension add --name containerapp" in workflow


def test_deploy_workflow_waits_for_the_revision() -> None:
    """イメージを差し替えたあと、生存確認まで行うこと。

    `az containerapp update` はリビジョンの作成を要求した時点で成功として返る。
    待たないと、起動していないのにジョブが緑になる。
    """
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    assert "wait_for_revision.sh" in workflow
    assert WAIT_SCRIPT.is_file(), "ワークフローが参照するスクリプトが無い"


@pytest.mark.parametrize(
    ("needle", "why"),
    [
        ("latestReadyRevisionName", "Single モードで ready を表すのはこの値"),
        ("trafficWeight", "実際にトラフィックを受けていることの確認"),
        ("Provisioned", "プロビジョニングの完了"),
        ("Healthy", "ヘルス状態"),
        ("EXPECTED_REVISION", "古いリビジョンを見て緑になるのを防ぐ"),
        ("exit 1", "満たさなければジョブを失敗させる"),
    ],
)
def test_wait_script_checks_the_right_things(needle: str, why: str) -> None:
    script = WAIT_SCRIPT.read_text(encoding="utf-8")

    assert needle in script, f"{needle} を見ていない（{why}）"


def test_wait_script_does_not_treat_active_as_ready() -> None:
    """`active` を成功条件に使わないこと。

    Single モードでは新リビジョンが ready になるまで旧リビジョンを落とさない。
    移行中は新旧どちらも active なので、`active == true` は生成直後から真になり、
    準備完了の証拠にならない（偽陽性の材料になる）。
    """
    script = _without_comments(WAIT_SCRIPT)

    assert "active" not in script


@pytest.mark.parametrize("log_type", ["console", "system"])
def test_wait_script_dumps_both_log_types_on_failure(log_type: str) -> None:
    """失敗時に console と system の両方を出すこと。

    アプリ側の例外（実際に踏んだ alembic の
    `CommandError: Path doesn't exist: /app/migrations` など）は console にしか
    出ない。一方でプローブ失敗や pull 失敗は system にしか出ない。
    クラッシュループで replica が消えると console は空になるため、両方要る。
    """
    script = WAIT_SCRIPT.read_text(encoding="utf-8")

    assert f"--type {log_type}" in script


# --------------------------------------------------------------------------
# チェックはローカルの pre-push で走る
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "why"),
    [
        ("uv lock --check", "ロックファイルの更新漏れを検出する唯一の経路"),
        ("uv run ruff check .", "lint"),
        ("uv run ruff format --check .", "整形"),
        ("uv run mypy", "型"),
        ('uv run pytest -m "not live"', "slow（実 ffmpeg）を含み、課金する live だけ外す"),
    ],
)
def test_pre_push_runs_the_checks(command: str, why: str) -> None:
    hook = PRE_PUSH.read_text(encoding="utf-8")

    assert command in hook, f"{command} が pre-push に無い（{why}）"


def test_pre_push_fails_when_ffmpeg_is_missing() -> None:
    """ffmpeg が無いときに落ちること。

    slow テストは ffmpeg が無いと pytest.skip で静かに飛ぶ。
    「走ったつもりで skip されていた」を防ぐため、hook 側で先に落とす。
    """
    hook = PRE_PUSH.read_text(encoding="utf-8")

    assert "command -v ffmpeg" in hook
    assert "command -v ffprobe" in hook


def test_pre_push_stops_at_the_first_failure() -> None:
    """`set -e` があること（無いと最後のコマンドの結果だけで判定される）。"""
    assert "set -e" in PRE_PUSH.read_text(encoding="utf-8")


@pytest.mark.parametrize("script", [PRE_PUSH, WAIT_SCRIPT])
def test_shell_scripts_use_lf(script: pathlib.Path) -> None:
    """LF かつ shebang があること。

    CRLF だと Linux 側で `bash\\r` を探して `not found` になる。
    改行は .gitattributes（`* text=auto eol=lf`）で固定しているので、
    ここは固定が効いていることの確認になる。
    """
    raw = script.read_bytes()

    assert b"\r\n" not in raw, "CRLF が混じっている"
    assert raw.startswith(b"#!"), "shebang が無い"


@pytest.mark.parametrize("script", [PRE_PUSH, WAIT_SCRIPT])
def test_shell_scripts_are_executable_in_git(script: pathlib.Path) -> None:
    """git 側の実行ビットが立っていること。

    Windows のチェックアウトはファイルシステム上の実行ビットを持たないため、
    ファイルの stat では判定できない。立っていないと core.hooksPath を
    設定しても hook が動かず、ランナー上でも `Permission denied` になる。
    """
    if shutil.which("git") is None:
        pytest.skip("git が PATH にありません")

    relative = script.relative_to(REPO_ROOT).as_posix()
    entry = subprocess.run(
        ["git", "ls-files", "--stage", "--", relative],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout

    assert entry, f"{relative} が git に追加されていない"
    assert entry.split()[0] == "100755", (
        f"{relative} に実行ビットが無い"
        f"（`git update-index --chmod=+x {relative}` で付ける）: {entry.split()[0]}"
    )
