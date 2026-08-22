"""要約から HTML を落とす処理の検証。

これが無いと、記事一覧の要約に `<h2 id="dc8acd71-...">` のようなタグが
文字として出る（2026-08-22 に実際にそうなっていた。note.com のフィードは
本文の先頭を要素ごと渡してくる）。
"""

from src.models.news import NewsArticle, NewsCategory
from src.utils.html_text import strip_html


def test_タグを落として中の文字だけを残す() -> None:
    assert strip_html("<p>推論コストが10分の1になった</p>") == "推論コストが10分の1になった"


def test_実測で出ていた_note_の要約が読める形になる() -> None:
    """画面に出ていた実物。属性の UUID が残らないこと。"""
    raw = (
        '<br /><h2 id="dc8acd71-3043-4e7e-971c-a7295764839d" '
        'name="dc8acd71-3043-4e7e-971c-a7295764839d">内部統制の考え方</h2>'
        "<p>AIエージェントが業務に入ると監査の単位が変わる。</p>"
    )

    result = strip_html(raw)

    assert result == "内部統制の考え方 AIエージェントが業務に入ると監査の単位が変わる。"
    assert "<" not in result
    assert "dc8acd71" not in result


def test_段落の境界は空白になる() -> None:
    """消すと語が繋がる。`<p>A</p><p>B</p>` が「AB」になってはいけない。"""
    assert strip_html("<p>前半</p><p>後半</p>") == "前半 後半"
    assert strip_html("一行目<br>二行目") == "一行目 二行目"


def test_実体参照を解く() -> None:
    assert strip_html("A&amp;B") == "A&B"
    assert strip_html("続きは&#8230;") == "続きは…"
    assert strip_html("空白&nbsp;を畳む") == "空白 を畳む"


def test_文字としての山括弧を消さない() -> None:
    """実体参照を先に解くと、この文の `<b>` がタグとして消える。

    除去 → unescape の順序を守っていることの検査。逆にすると
    「山括弧を含む文章」が黙って削られる。
    """
    assert strip_html("タグは &lt;b&gt; と書く") == "タグは <b> と書く"


def test_script_と_style_は要素ごと落とす() -> None:
    """タグだけ消すと中身が本文として残る。"""
    assert strip_html("<style>.a{color:red}</style>本文") == "本文"
    assert strip_html("<script>var a=1;</script>本文") == "本文"


def test_中身が実質空なら空文字を返す() -> None:
    """記号だけの要約は「要約なし」として扱う（空の枠を出さないため）。"""
    assert strip_html("<br /><br />") == ""
    assert strip_html("<hr /> — ") == ""
    assert strip_html("") == ""


def test_連続する空白を1つに畳む() -> None:
    assert strip_html("A   \n\t B") == "A B"


def test_NewsArticle_は構築の時点で要約を正規化する() -> None:
    """情報源ごとに落とすと片方だけが腐るので、モデルが権威。"""
    article = NewsArticle(
        id="x",
        title="t",
        url="https://example.com/a",
        source="note",
        category=NewsCategory.AI,
        summary='<p id="u">要約の本文</p>',
    )

    assert article.summary == "要約の本文"


def test_保存済みの汚れた要約も読み込みで直る() -> None:
    """すでに JSON に入っている HTML は、次の取得を待たずに読める形になる。"""
    restored = NewsArticle.from_dict(
        {
            "id": "x",
            "title": "t",
            "url": "https://example.com/a",
            "source": "note",
            "category": "ai",
            "summary": "<h2>見出し</h2><p>本文</p>",
        }
    )

    assert restored.summary == "見出し 本文"
