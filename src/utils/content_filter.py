"""Azure のコンテンツフィルタ由来のエラーを見分ける。

画像生成（`ImageGenerator`）・台本生成（`ScriptGenerator`）・X の下書き生成
（`PostGenerator`）の3つが必要とする。以前は `image_generator.py` の中に
private な `_is_content_filter_error` として置いていたが、台本側でも同じ判定が
要るようになったので出した。

**判定を書き写してはいけない。** 写しがあると片方だけが直る
（`ARTICLE_OVERFETCH` が X 側にしか入っていなかったのと同じ形の欠陥）。

綴りが2つある
------------
Azure は API によって違う綴りを返す。

- 画像 API（`images.generate`）: ``error.code == "contentFilter"``（camelCase）
- Chat / Responses API: ``error.code == "content_filter"``（snake_case）

**両方を明示的に見る。** 画像側から持ってきた元の実装は camelCase だけを
明示判定しており、snake_case は最後の `str(exc)` の部分文字列一致で*偶然*
拾えていただけだった（台本生成が拒否された実際の応答は snake_case）。
"""

from __future__ import annotations

from typing import Any

# `error.code` に現れうる綴り。API によって違う（モジュール docstring 参照）。
FILTER_CODES = ("contentFilter", "content_filter")

# 拒否されたカテゴリが入る鍵。Azure は応答の形が揺れるので複数見る。
_RESULT_KEYS = ("content_filter_results", "content_filter_result")


def _body(exc: Exception) -> dict[str, Any] | None:
    """openai SDK の例外が持つ応答 JSON を取り出す。

    Args:
        exc: 例外

    Returns:
        dict | None: 応答 JSON（持っていなければ None）
    """
    body = getattr(exc, "body", None)
    return body if isinstance(body, dict) else None


def _error(exc: Exception) -> dict[str, Any] | None:
    """応答 JSON の `error` オブジェクトを取り出す。

    Args:
        exc: 例外

    Returns:
        dict | None: `error` オブジェクト（無ければ None）
    """
    body = _body(exc)
    if body is None:
        return None
    error = body.get("error")
    return error if isinstance(error, dict) else None


def is_content_filter_error(exc: Exception) -> bool:
    """例外がコンテンツフィルタ由来かを判定する。

    引数を `BadRequestError` ではなく `Exception` にしてあるのは、これが
    述語であって型を絞る必要がないため（呼び出し側が `except BadRequestError`
    の内側で使う）。副産物として、テストで軽い stub を渡せる。

    判定は3段。上から順に確かな根拠で、最後は保険。

    1. `exc.code`（SDK が属性として持つ）
    2. `exc.body["error"]["code"]`（応答 JSON）
    3. `str(exc)` の部分文字列（上2つが取れない形で来たとき）

    Args:
        exc: 判定する例外

    Returns:
        bool: コンテンツフィルタ由来なら True
    """
    if getattr(exc, "code", None) in FILTER_CODES:
        return True

    error = _error(exc)
    if error is not None and error.get("code") in FILTER_CODES:
        return True

    text = str(exc)
    return any(code in text for code in FILTER_CODES)


def filtered_categories(exc: Exception) -> tuple[str, ...]:
    """拒否されたカテゴリ名を拾う（best effort）。

    **絶対に例外を投げない。** これは失敗理由を人に読ませるための飾りで、
    ここで落ちると本来伝えたい「コンテンツフィルタに拒否された」という
    情報そのものが失われる。Azure の応答は形が揺れる（`innererror` の下に
    入る形もある）ので、取れなければ空を返し、呼び出し側はカテゴリ抜きの
    メッセージにする。

    Args:
        exc: 判定する例外

    Returns:
        tuple[str, ...]: `filtered` が真のカテゴリ名（順序は応答のまま）
    """
    try:
        error = _error(exc)
        if error is None:
            return ()

        names: list[str] = []
        for holder in (error, error.get("innererror")):
            if not isinstance(holder, dict):
                continue
            for key in _RESULT_KEYS:
                results = holder.get(key)
                if not isinstance(results, dict):
                    continue
                for name, detail in results.items():
                    if not isinstance(detail, dict) or not detail.get("filtered"):
                        continue
                    if name not in names:
                        names.append(str(name))
        return tuple(names)
    except Exception:
        # 形の想定が外れても、呼び出し側の失敗理由の組み立てを止めない。
        return ()


def category_suffix(exc: Exception) -> str:
    """拒否されたカテゴリを「（sexual、hate）」の形にする。

    失敗理由の文言に添えるための整形。取れなければ空文字列を返すので、
    呼び出し側はカテゴリ抜きの文になる。**台本側と投稿側で同じ整形が要る**
    ので、1行でもここに置く（写しを作ると片方だけ直る）。

    Args:
        exc: 判定する例外

    Returns:
        str: 「（...）」の形、または空文字列
    """
    categories = filtered_categories(exc)
    return f"（{'、'.join(categories)}）" if categories else ""
