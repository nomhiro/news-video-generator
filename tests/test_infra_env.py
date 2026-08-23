"""Container App に設定が届いているかの検査。

bicep を読むだけの静的な検査。実際に provision はしない。

なぜ要るか
----------
`X_CLIENT_ID` / `X_CLIENT_SECRET` を渡す配線が `infra/` に1件も無いまま、
アプリは正常に起動し、画面は「X 未認証」としか言わなかった（issue #28）。
`config.py` の既定が空文字（任意）なので、**設定の欠落は起動失敗ではなく
「静かに壊れる」形で現れる**——動画生成も Web も動き続けるので、実際に
投稿しようとするまで気付けない。bicep の env を検査する経路が1つも
無かったことが原因。

検査は「文字列があること」ではなく**対応関係**を見る。単に
`"X_CLIENT_ID" in text` だと、コメントに書いただけで通り、シークレット名の
綴りミスも `main.parameters.json` の欠落も見逃す（どちらも azd が空文字を
渡す結果になり、同じ壊れ方が再発する）。
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

import pytest

from tests.conftest import REPO_ROOT

APP_HOSTING_PATH = REPO_ROOT / "infra" / "core" / "app-hosting.bicep"
DATABASE_PATH = REPO_ROOT / "infra" / "core" / "database.bicep"
MAIN_PATH = REPO_ROOT / "infra" / "main.bicep"
PARAMETERS_PATH = REPO_ROOT / "infra" / "main.parameters.json"


def _without_comments(path: pathlib.Path) -> str:
    """`//` から始まる行を落とす。

    「なぜ渡さないか」を書いたコメントに、渡していない env の名前
    （`X_POSTING_ENABLED` / `X_REDIRECT_URI`）が現れる。本文をそのまま
    検査すると自分のコメントに引っかかる（`tests/test_deploy_workflow.py`
    と同じ理由・同じ実装）。
    """
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("//")
    ]
    return "\n".join(lines)


APP_HOSTING = _without_comments(APP_HOSTING_PATH)
DATABASE = _without_comments(DATABASE_PATH)
MAIN = _without_comments(MAIN_PATH)


def _parameters() -> dict[str, Any]:
    """`main.parameters.json` の parameters を読む。"""
    document: dict[str, Any] = json.loads(PARAMETERS_PATH.read_text(encoding="utf-8"))
    parameters: dict[str, Any] = document["parameters"]
    return parameters


PARAMETERS = _parameters()


def _block(text: str, anchor: str) -> str:
    """`anchor` の開き括弧から、対応する閉じ括弧までを切り出す。

    env と secrets は入れ子の配列・オブジェクトを含むので、行の前方一致では
    範囲を決められない。

    Args:
        text: 対象のテキスト
        anchor: ブロックの開始（例 `"env: concat("`）。末尾が開き括弧であること

    Returns:
        str: ブロックの中身
    """
    start = text.index(anchor) + len(anchor)
    depth = 1
    for offset, char in enumerate(text[start:]):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return text[start : start + offset]
    raise AssertionError(f"{anchor} の閉じ括弧が見つからない")


def _declared_secrets() -> set[str]:
    """Container App が宣言しているシークレット名。"""
    return set(re.findall(r"name: '([a-z0-9-]+)'", _block(APP_HOSTING, "secrets: concat(")))


def _secret_references() -> set[str]:
    """シークレットを参照している名前。

    `secretRef`（env から）と `clientSecretSettingName`（EasyAuth から）の
    2経路がある。`auth-client-secret` は env に出てこないので、後者を
    含めないと「宣言したのに参照されていない」と誤検出する。
    """
    return set(re.findall(r"(?:secretRef|clientSecretSettingName): '([a-z0-9-]+)'", APP_HOSTING))


def _env_entries() -> dict[str, tuple[str, str]]:
    """env の名前 → (`value` か `secretRef` か, その式)。

    `name:` の次の行が値だという並びに依存している。並べ替えたら落ちるが、
    それは意図（bicep 側の書き方を1つに固定する）。
    """
    entries: dict[str, tuple[str, str]] = {}
    lines = _block(APP_HOSTING, "env: concat(").splitlines()
    for index, line in enumerate(lines):
        matched = re.search(r"name: '([A-Z0-9_]+)'", line)
        if matched is None:
            continue
        following = re.search(r"(value|secretRef): (.+)$", lines[index + 1].strip())
        assert following is not None, f"{matched.group(1)} の値が次の行に無い"
        entries[matched.group(1)] = (following.group(1), following.group(2).strip())
    return entries


def _conditional_branches(text: str) -> list[tuple[str, str]]:
    """`<式> ? [ … ] : []` と `<式> ? [] : [ … ]` を (式, 中身) で返す。"""
    branches: list[tuple[str, str]] = []
    for matched in re.finditer(r"(\S[^\n?]*?)\s*\n?\s*\?\s*", text):
        gate = matched.group(1).strip()
        rest = text[matched.end() :]
        if rest.lstrip().startswith("[]"):
            # 条件が偽のときに中身が来る形（empty(...) ? [] : [ … ]）
            tail = rest[rest.index("[]") + 2 :]
            if ":" not in tail:
                continue
            body = tail[tail.index(":") + 1 :]
            branches.append((gate, _block(body, "[")))
        elif rest.lstrip().startswith("["):
            branches.append((gate, _block(rest, "[")))
    return branches


def _secure_params(text: str) -> set[str]:
    """`@secure()` が付いた param の名前。"""
    return set(re.findall(r"@secure\(\)(?:\s*@\w+\([^)]*\))*\s*param (\w+)", text, re.DOTALL))


# --------------------------------------------------------------------------
# シークレットの宣言と参照が食い違わないこと
#
# 参照だけ残すとリビジョンが作られず、宣言だけ残すと死んだシークレットに
# なる。どちらも「片方だけ直した」ときに起きる。
# --------------------------------------------------------------------------


def test_every_secret_ref_is_declared() -> None:
    """参照しているシークレットがすべて宣言されていること。

    存在しないシークレットを指すと、Container Apps はリビジョンの作成を
    拒否する（アプリは旧リビジョンで動き続けるので、デプロイが失敗した
    ことにしか見えない）。
    """
    missing = _secret_references() - _declared_secrets()
    assert not missing, f"宣言されていないシークレットを参照している: {sorted(missing)}"


def test_every_declared_secret_is_referenced() -> None:
    """宣言したシークレットがすべて参照されていること。"""
    unused = _declared_secrets() - _secret_references()
    assert not unused, f"参照されていないシークレットが残っている: {sorted(unused)}"


def test_the_x_secret_and_its_env_share_one_gate() -> None:
    """`x-client-secret` の宣言と `secretRef` が同じ条件式で出ること。

    ここが今回の変更でいちばん壊しやすい。条件がずれると、
    シークレットが無いのに env が参照する（リビジョンが作られない）か、
    参照されないシークレットだけが残る。
    """
    gates = {
        marker: [gate for gate, body in _conditional_branches(APP_HOSTING) if marker in body]
        for marker in ("name: 'x-client-secret'", "secretRef: 'x-client-secret'")
    }
    for marker, found in gates.items():
        assert len(found) == 1, f"{marker} を含む条件ブランチが1つではない: {found}"
    declaration, reference = (found[0] for found in gates.values())
    assert declaration == reference, (
        f"シークレットの宣言（{declaration}）と参照（{reference}）の条件が違う"
    )


# --------------------------------------------------------------------------
# X の資格情報が届いていること
#
# 無いとトークンだけ送っても投稿できない。更新（refresh）が
# client_id / client_secret の Basic 認証を要求する。
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "why"),
    [
        ("X_CLIENT_ID", "トークン更新の Basic 認証"),
        ("X_CLIENT_SECRET", "トークン更新の Basic 認証"),
        ("TOKEN_STORE", "トークンをコンテナのファイルシステムに置けない"),
        ("AZURE_TOKEN_CONTAINER", "同上"),
        ("AZURE_CLIENT_ID", "DefaultAzureCredential にどの ID を使うか教える"),
    ],
)
def test_required_env_is_passed_to_the_container(name: str, why: str) -> None:
    assert name in _env_entries(), f"{name} を Container App に渡していない（{why}）"


def test_x_client_secret_is_passed_by_secret_ref() -> None:
    """シークレットは `secretRef` で渡すこと。

    `value:` に書くと `az containerapp show` の出力に平文で出る。
    """
    kind, expression = _env_entries()["X_CLIENT_SECRET"]
    assert kind == "secretRef", "X_CLIENT_SECRET を value で渡している（平文で読めてしまう）"
    assert expression == "'x-client-secret'"


def test_x_client_id_comes_from_the_parameter() -> None:
    """`X_CLIENT_ID` はパラメータの値で渡すこと（機密ではない）。"""
    kind, expression = _env_entries()["X_CLIENT_ID"]
    assert kind == "value"
    assert expression == "xClientId", "リテラルを埋め込んでいる"


@pytest.mark.parametrize(
    ("name", "why"),
    [
        (
            "X_POSTING_ENABLED",
            "投稿スイッチの権威は Azure Files 上の data/x_posting.json。"
            "env はファイルが無いときの初期値でしかなく、有効化は画面から行う",
        ),
        (
            "X_REDIRECT_URI",
            "参照するのは scripts/authorize_x.py（ローカルの PKCE）だけで、"
            "refresh_token グラントは redirect_uri を送らない",
        ),
    ],
)
def test_local_only_settings_are_not_passed(name: str, why: str) -> None:
    assert name not in _env_entries(), f"{name} は渡さない（{why}）"


# --------------------------------------------------------------------------
# パラメータが端から端まで繋がっていること
#
# issue #28 と同型の再発を止める不変条件。app-hosting に param を足しても、
# main.bicep で素通ししなければ既定の空文字が使われ、main.parameters.json に
# 書かなければ azd の値が渡らない。**どちらも起動は成功する。**
# --------------------------------------------------------------------------


def _module_params() -> str:
    return _block(MAIN, "module appHosting 'core/app-hosting.bicep' = if (deployApp) {")


@pytest.mark.parametrize("name", sorted(_secure_params(_without_comments(APP_HOSTING_PATH))))
def test_every_secure_param_is_passed_by_the_root_template(name: str) -> None:
    """app-hosting の `@secure()` param が main.bicep から渡されていること。"""
    assert f"{name}: {name}" in _module_params(), f"main.bicep が {name} を渡していない"


@pytest.mark.parametrize("name", sorted(_secure_params(_without_comments(MAIN_PATH))))
def test_every_secure_param_is_bound_to_an_azd_variable(name: str) -> None:
    """main.bicep の `@secure()` param が azd の変数に紐付いていること。

    `main.parameters.json` に無いと、azd は既定値（空文字）を渡す。
    """
    assert name in PARAMETERS, f"main.parameters.json に {name} が無い"


@pytest.mark.parametrize(
    ("parameter", "variable"),
    [("xClientId", "X_CLIENT_ID"), ("xClientSecret", "X_CLIENT_SECRET")],
)
def test_x_parameters_read_the_env_names_the_app_uses(parameter: str, variable: str) -> None:
    """azd の置換名がアプリの読む環境変数名と一致すること。

    打ち間違えると、`azd env set` した値が届かないまま空文字になる。
    """
    assert parameter in PARAMETERS, f"main.parameters.json に {parameter} が無い"
    assert PARAMETERS[parameter]["value"] == f"${{{variable}=}}"
    assert f"{parameter}: {parameter}" in _module_params()


def test_no_output_exposes_a_secure_parameter() -> None:
    """`@secure()` の値を output に出さないこと。

    ARM の output はデプロイ履歴に平文で残り、リソースグループの閲覧権限が
    あれば読めてしまう。
    """
    for text in (APP_HOSTING, MAIN):
        outputs = [line for line in text.splitlines() if line.startswith("output ")]
        for name in _secure_params(text):
            leaked = [line for line in outputs if name in line]
            assert not leaked, f"{name} を output に出している: {leaked}"


# --------------------------------------------------------------------------
# ジョブ表と投稿表が共有 DB にあること（Issue #56 / #3）
#
# コンテナのローカルディスク上の SQLite に戻すと、X 投稿キューがデプロイの
# たびに消え、その日の残りの投稿が出ないまま終わる。**起動は成功し、画面は
# 「まだ何も予定が無い朝」と完全に同じ見た目になる**ので、気付く手段が
# 画面にもログにも無い。だから文面で見張る。
# --------------------------------------------------------------------------


def test_database_url_points_at_the_shared_postgres() -> None:
    """`DATABASE_URL` が共有 DB を指していること。"""
    kind, expression = _env_entries()["DATABASE_URL"]
    assert kind == "value"
    assert "postgresql+psycopg://" in expression, f"共有 DB を指していない: {expression}"
    assert "sqlite" not in expression, "コンテナのローカルディスクに戻っている"
    assert "database.outputs.fqdn" in expression, "ホスト名をリテラルで埋めている"


def test_database_url_carries_no_password() -> None:
    """接続情報にパスワードを書かないこと。

    env は `az containerapp show` に平文で出る。そもそもサーバー側は
    パスワード認証を無効にしてあり、接続は Entra のトークンで行う。
    """
    _, expression = _env_entries()["DATABASE_URL"]
    userinfo = expression.split("://", 1)[1].split("@", 1)[0]
    assert ":" not in userinfo, f"URL にパスワードが入っている: {userinfo}"


def test_the_connection_user_is_the_identity_name() -> None:
    """URL の user と PostgreSQL のロール名が同じ出所であること。

    ロール名は `administrators` の `principalName`（= マネージド ID の名前）に
    なる。ずれると起動時に
    `password authentication failed for user "..."` で落ちる。
    """
    _, expression = _env_entries()["DATABASE_URL"]
    assert "database.outputs.loginName" in expression
    assert "output loginName string = identityName" in DATABASE
    assert "principalName: identityName" in DATABASE


def test_the_managed_identity_is_the_entra_administrator() -> None:
    """マネージド ID がサーバーの Entra 管理者であること。

    管理者でないと `alembic upgrade head` がテーブルを作れず、
    `pgaadauth_create_principal` を手で流す工程が復活する。
    """
    assert "Microsoft.DBforPostgreSQL/flexibleServers/administrators" in DATABASE
    assert "principalType: 'ServicePrincipal'" in DATABASE
    assert "identityPrincipalId: identity.properties.principalId" in APP_HOSTING


def test_postgres_accepts_only_entra_authentication() -> None:
    """パスワード認証を無効にしてあること。

    公開エンドポイント + 「Azure から許可」のファイアウォール規則なので、
    パスワード認証を残すと総当たりの的が1つ増える。
    """
    assert "activeDirectoryAuth: 'Enabled'" in DATABASE
    assert "passwordAuth: 'Disabled'" in DATABASE
    assert "administratorLogin" not in DATABASE, "パスワード認証の管理者を作っている"


def test_postgres_stays_on_the_cheapest_sku() -> None:
    """一番安い構成のままであること（B1MS + 32GB で月約$16）。

    上げるのは構わないが、**気付かずに上がる**ことを防ぐ。ここは
    収益化前のプロジェクトで、X の投稿予算（月$30）と同じ桁の固定費。
    """
    assert "name: 'Standard_B1ms'" in DATABASE
    assert "tier: 'Burstable'" in DATABASE
    assert "storageSizeGB: 32" in DATABASE
    assert "autoGrow: 'Disabled'" in DATABASE
    assert "geoRedundantBackup: 'Disabled'" in DATABASE


def test_the_app_stays_on_one_replica() -> None:
    """DB が共有になってもレプリカは1のままであること。

    定期実行がアプリ内の daemon スレッドで動くので、2以上にすると
    **全レプリカが同時刻に走る**（毎朝の生成が二重に走り、X の下書きも
    二重に積まれる）。増やすにはリーダー選出か Container Apps Jobs への
    切り出しが先に必要。
    """
    assert "minReplicas: 1" in APP_HOSTING
    assert "maxReplicas: 1" in APP_HOSTING
