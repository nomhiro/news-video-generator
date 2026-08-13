"""ローカルの OAuth トークンを Blob Storage へ移す。

なぜ要るか
----------
`TOKEN_STORE=blob` に切り替えただけでは、Blob 側は空なので未認証に
なる。YouTube の OAuth は localhost にリダイレクトする方式で
コンテナの中では完了できないため、「ローカルで認証 → 保存先に置く」
という経路が必要になる。

使い方:
    uv run python -m scripts.push_tokens          # 何を送るか表示して実行
    uv run python -m scripts.push_tokens --dry-run # 確認だけ

`.env` の AZURE_STORAGE_ACCOUNT_URL / AZURE_TOKEN_CONTAINER を見る。
TOKEN_STORE の値は見ない（local のままでも Blob に押し込めるようにする）。
"""

from __future__ import annotations

import argparse
import sys

from config import Config
from src.storage.tokens import (
    LocalFileTokenStore,
    TokenStoreError,
    build_token_store,
)


def main(argv: list[str] | None = None) -> int:
    """ローカルのトークンを Blob へコピーする。

    Args:
        argv: コマンドライン引数（テスト用）

    Returns:
        int: 終了コード
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="送らずに、対象と保存先だけを表示する",
    )
    args = parser.parse_args(argv)

    config = Config.from_env()
    if not config.azure_storage_account_url:
        print("AZURE_STORAGE_ACCOUNT_URL が設定されていません", file=sys.stderr)
        return 1

    local = LocalFileTokenStore(config.token_paths)
    present = [name for name in config.token_paths if local.exists(name)]
    missing = [name for name in config.token_paths if not local.exists(name)]

    print(f"保存先: {config.azure_storage_account_url}/{config.azure_token_container}")
    for name in present:
        print(f"  送る   : {name}  ({config.token_paths[name]})")
    for name in missing:
        # client_secrets が無いのは設定漏れ、token が無いのは未認証。
        # どちらも「ここで止める」ほどのことではない。
        print(f"  無し   : {name}  ({config.token_paths[name]})")

    if not present:
        print("送るものがありません。先にローカルで認証してください")
        return 1

    if args.dry_run:
        return 0

    remote = build_token_store(
        "blob",
        local_paths=config.token_paths,
        account_url=config.azure_storage_account_url,
        container_name=config.azure_token_container,
    )

    for name in present:
        payload = local.read(name)
        if payload is None:  # pragma: no cover - exists() の直後なので通常起きない
            continue
        try:
            remote.write(name, payload)
        except TokenStoreError as e:
            print(f"失敗: {name}: {e}", file=sys.stderr)
            return 1
        print(f"送信しました: {name}")

    print("\n完了。`.env` に TOKEN_STORE=blob を設定すると Blob 側を読みます")
    return 0


if __name__ == "__main__":
    sys.exit(main())
