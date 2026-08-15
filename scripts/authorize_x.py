"""X（旧 Twitter）を初めて認証し、トークンを保存先へ書き込む。

なぜローカルで実行するか
------------------------
Authorization Code + PKCE はブラウザからのリダイレクト先が必要で、
コンテナの中には reachable な localhost もブラウザも無い（YouTube の
`InstalledAppFlow` で踏んだのと同じ制約）。運用は
「ローカルで一度だけ認証 → `uv run python -m scripts.push_tokens` で
保存先（Blob）へ送る」の2段構え。このスクリプトは前段だけを担う。

使い方:
    uv run python -m scripts.authorize_x

`.env` の X_CLIENT_ID / X_CLIENT_SECRET / X_REDIRECT_URI を見る。
"""

from __future__ import annotations

import sys
import webbrowser
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from config import Config
from src.social.x_auth import XAuthError, build_authorization_url, exchange_authorization_code
from src.storage.tokens import X_TOKEN, TokenStore, build_token_store, write_json


@dataclass(frozen=True)
class CallbackResult:
    """コールバックの解釈結果。成功時は `code`、失敗時は `error` を持つ。"""

    code: str | None
    error: str | None


def evaluate_callback(params: dict[str, list[str]], expected_state: str) -> CallbackResult:
    """リダイレクトのクエリパラメータから、認可コードを受け取れたかを判定する。

    ソケットを介した結合テストなしに検査したいので、HTTP サーバー
    （`_make_handler`）から判定ロジックだけを分離している。

    Args:
        params: `urllib.parse.parse_qs` が返すクエリパラメータ
        expected_state: `build_authorization_url` が返した state

    Returns:
        CallbackResult: 判定結果
    """
    # state を最初に見る。ここが一致しないレスポンスは、そもそも自分が
    # 発行した認可リクエストへの応答ではない（CSRF や別タブからの混入）
    # ので、code や error の中身を見る前に弾く。
    state = (params.get("state") or [""])[0]
    if state != expected_state:
        return CallbackResult(code=None, error="state が一致しません（CSRF の可能性があります）")

    # X はユーザーが認可を拒否すると code の代わりに error=access_denied を返す。
    if "error" in params:
        description = (params.get("error_description") or params.get("error") or [""])[0]
        return CallbackResult(code=None, error=f"X が認可を拒否しました: {description}")

    code = (params.get("code") or [""])[0]
    if not code:
        return CallbackResult(code=None, error="認可コードを受け取れませんでした")
    return CallbackResult(code=code, error=None)


def _make_handler(
    expected_state: str, callback_path: str, results: list[CallbackResult]
) -> type[BaseHTTPRequestHandler]:
    """1回だけコールバックを受けて判定結果を `results` に積むハンドラを作る。

    結果をクラス属性ではなく引数で渡したリストに積む。ハンドラの型は
    `HTTPServer` に渡すため `type[BaseHTTPRequestHandler]` に留めたいが、
    その型には（当然ながら）動的に追加した属性が存在せず、独自の
    クラス属性を後から読むと型チェッカが追えない。クロージャで
    キャプチャしたリストなら、呼び出し元は素の `list` として読める。
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if not parsed.path.rstrip("/").endswith(callback_path.rstrip("/")):
                self.send_error(404, "Not Found")
                return
            result = evaluate_callback(parse_qs(parsed.query), expected_state)
            results.append(result)
            self._respond(success=result.error is None)

        def _respond(self, success: bool) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            message = (
                "X の認証に成功しました。このタブは閉じて構いません。"
                if success
                else "X の認証に失敗しました。ターミナルの表示を確認してください。"
            )
            self.wfile.write(f"<html><body><p>{message}</p></body></html>".encode())

        def log_message(self, format_: str, *args: object) -> None:
            """既定のアクセスログを止める（操作者の画面を汚さないため）。"""

    return Handler


def _wait_for_callback(redirect_uri: str, expected_state: str) -> CallbackResult:
    """リダイレクト先で1回だけリクエストを待ち受け、結果を返す。

    `handle_request` は1件処理したら戻る。認可コードは一度使えば
    十分で、待ち受けを続けるとその後の別のリクエスト（ブラウザの
    favicon 取得など）にも応答してしまう loopback エンドポイントが
    残ることになる。
    """
    parsed = urlparse(redirect_uri)
    port = parsed.port or 8091
    callback_path = parsed.path or "/callback"
    results: list[CallbackResult] = []
    handler = _make_handler(expected_state, callback_path, results)
    server = HTTPServer(("127.0.0.1", port), handler)
    try:
        server.handle_request()
    finally:
        server.server_close()
    if not results:
        return CallbackResult(code=None, error="コールバックを受け取れませんでした")
    return results[-1]


def _build_store(config: Config) -> TokenStore:
    return build_token_store(
        config.token_store,
        local_paths=config.token_paths,
        account_url=config.azure_storage_account_url,
        container_name=config.azure_token_container,
    )


def main(argv: list[str] | None = None) -> int:
    """X の認可コード + PKCE フローを1回実行し、トークンを保存する。

    Args:
        argv: 未使用（他のスクリプトとの引数形式を揃えるためだけに残す）

    Returns:
        int: 終了コード（0=成功）
    """
    config = Config.from_env()

    # クライアントIDまたはシークレットが無いまま進めても、リダイレクトは
    # 返ってきても交換の段階で必ず失敗する。ブラウザを開く前に止める。
    if not config.x_client_id:
        print("X_CLIENT_ID が設定されていません。.env に設定してください", file=sys.stderr)
        return 1
    client_secret = config.x_client_secret.get_secret_value()
    if not client_secret:
        print("X_CLIENT_SECRET が設定されていません。.env に設定してください", file=sys.stderr)
        return 1

    # code_verifier は URL の組み立て時にしか生成されない。ここで受け取って
    # 保持しないと、トークン交換（PKCE の検証）が完結できない。
    url, state, code_verifier = build_authorization_url(config.x_client_id, config.x_redirect_uri)

    print("以下のURLをブラウザで開いて認証してください（自動で開かない場合はコピーしてください）:")
    print(url)
    webbrowser.open(url)

    result = _wait_for_callback(config.x_redirect_uri, state)
    if result.error is not None or result.code is None:
        print(f"認証に失敗しました: {result.error}", file=sys.stderr)
        return 1

    try:
        payload = exchange_authorization_code(
            config.x_client_id,
            client_secret,
            result.code,
            config.x_redirect_uri,
            code_verifier,
        )
    except XAuthError as e:
        print(f"トークン交換に失敗しました: {e}", file=sys.stderr)
        return 1

    try:
        access_token = str(payload["access_token"])
        refresh_token = str(payload["refresh_token"])
        expires_in = int(payload.get("expires_in", 7200))
    except (KeyError, TypeError, ValueError) as e:
        print(f"トークン交換の応答が不正です: {e}", file=sys.stderr)
        return 1

    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    store = _build_store(config)
    # `load_credentials` が読む3項目とそのまま揃える。ここで形式がずれると
    # 認証は成功したのに以降ずっと未認証扱いになる。
    write_json(
        store,
        X_TOKEN,
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at.isoformat(),
        },
    )

    # トークンの値そのものは一切表示しない（ターミナルの scrollback は
    # 秘密の保管場所ではない）。書き込み先だけを伝える。
    print(f"X のトークンを保存しました（保存先: {config.token_store}）")
    if config.token_store == "local":
        print("`uv run python -m scripts.push_tokens` でクラウドの保存先へ送ってください")
    return 0


if __name__ == "__main__":
    sys.exit(main())
