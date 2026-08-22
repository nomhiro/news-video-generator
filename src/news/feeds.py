"""AI カテゴリの情報源（RSS / Atom フィード）。

なぜ検索エンジンの集約をやめたか
--------------------------------
以前は Google News RSS の検索クエリ（`生成AI` / `ChatGPT` など）で AI
カテゴリを埋めていた。実物の投稿を読んで否決された。実測（2026-08-22、
本文が取れた3件）で選ばれたのは:

- 「タレントが体重減少を ChatGPT に相談して病気発覚」（Yahoo!ニュース）
- 「Claude 活用術の本が発売」（`oita-press.co.jp` = 地方紙の PR 転載）
- 「Pixel 11 は何が進化したか」（`ｄメニューニュース`）

3件のうち技術ニュースは1件だけだった。検索クエリは**語が一致するだけの
記事**を拾うので、AI が話題に出た芸能ニュースと、AI の一次情報を区別できない。

もう1つの理由は URL。Google News RSS の `link` は
`https://news.google.com/rss/articles/CBMi...` というリダイレクタで、
記事の実 URL ではない。投稿にリンクを載せるとカードに出るのは
**Google News** で、媒体名も記事タイトルも出ない。

だから情報源を「誰が書いたか」で選ぶ形に変えた。ここに並ぶのは
発信元そのもののフィードなので、`link` は媒体の実 URL になる。

**すべて実測で生きていることを確認してから載せている**（2026-08-22）。
死んだ URL を既定値に置くと、そのフィードだけ静かに0件になり、
記事が減った理由が分からなくなる（`RssSource` は1本の失敗で
全体を落とさない）。フィードを足すときも実際に叩いて確認する。

**Anthropic には公式 RSS が無い。** 4パターン試して全て 404
（`/rss.xml` / `/news/rss.xml` / `/news/feed.xml` / `/engineering/rss.xml`）。
URL を推測して足さないこと。Claude 関連は Zenn の `claude` トピックと
海外ニュース経由で入る。

**除いたもの**（実測して意図的に外した）:

- `zenn.dev/feed`（Zenn 全体の新着）: トピックを問わないため
  「或るログ研究者」のような無関係な記事が入る。トピック別フィードを使う
- `b.hatena.ne.jp/hotentry/it.rss`: 集約サイトであり、ここでやめた
  Google News と同じ性質を持つ
- `blogs.nvidia.com/feed/`: AI 専用ではなく、GeForce NOW のゲーム配信
  告知が上位に来る
- `blog.cloudflare.com/rss/` / `engineering.fb.com/feed/`: 同じ理由で
  AI 以外の比率が高い
- `huggingface.co/papers/feed`（401）、`ai.meta.com/blog/rss/`（404）
"""

from __future__ import annotations

from typing import NamedTuple


class Feed(NamedTuple):
    """1本のフィード。

    Attributes:
        source: 記事の `source` に入る発信元名。**投稿本文には出ない**
            （リンクだけを載せる方針なので）。画面とログのための表示名
        url: RSS / Atom の URL
    """

    source: str
    url: str


# 一次情報。開発元・研究所が自分で出しているもの。
PRIMARY_FEEDS: tuple[Feed, ...] = (
    Feed("OpenAI", "https://openai.com/news/rss.xml"),
    Feed("Google Research", "https://research.google/blog/rss/"),
    Feed("Google DeepMind", "https://deepmind.google/blog/rss.xml"),
    Feed("Google Developers", "https://developers.googleblog.com/feeds/posts/default"),
    Feed("Microsoft AI", "https://news.microsoft.com/source/topics/ai/feed/"),
    # Azure Blog は AI 専用ではないが、このプロジェクトの土台
    # （Azure OpenAI / AI Foundry）の告知がここに出るので残す。
    Feed("Azure", "https://azure.microsoft.com/en-us/blog/feed/"),
    Feed("AWS ML", "https://aws.amazon.com/blogs/machine-learning/feed/"),
    Feed("Hugging Face", "https://huggingface.co/blog/feed.xml"),
    Feed("GitHub", "https://github.blog/ai-and-ml/feed/"),
)

# 日本語の技術記事。書き手が実際に触った話が入るので、
# 「誰がどの作業でどう使えるか」を書く投稿と相性が良い。
JA_COMMUNITY_FEEDS: tuple[Feed, ...] = (
    Feed("Zenn", "https://zenn.dev/topics/ai/feed"),
    Feed("Zenn", "https://zenn.dev/topics/llm/feed"),
    Feed("Zenn", "https://zenn.dev/topics/generativeai/feed"),
    Feed("Zenn", "https://zenn.dev/topics/claude/feed"),
    Feed("Zenn", "https://zenn.dev/topics/azureopenai/feed"),
    Feed("Qiita", "https://qiita.com/tags/ai/feed"),
    Feed("Qiita", "https://qiita.com/tags/llm/feed"),
    Feed("Qiita", "https://qiita.com/tags/%E7%94%9F%E6%88%90ai/feed"),
    Feed("note", "https://note.com/hashtag/AI/rss"),
    Feed("Publickey", "https://www.publickey1.jp/atom.xml"),
)

# 海外の動向。日本語media を経由しないぶん早い。
# 本文が英語なので、投稿は日本語で書かせる必要がある
# （`PostGenerator` の共通ルールに明記してある）。
OVERSEAS_FEEDS: tuple[Feed, ...] = (
    Feed("TechCrunch", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    Feed("VentureBeat", "https://venturebeat.com/category/ai/feed/"),
    Feed("Ars Technica", "https://arstechnica.com/ai/feed/"),
    Feed("The Verge", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    Feed(
        "MIT Technology Review",
        "https://www.technologyreview.com/topic/artificial-intelligence/feed",
    ),
    Feed("Simon Willison", "https://simonwillison.net/atom/everything/"),
)

# 論文。
#
# **1日あたりの件数が桁違いに多い**（実測: cs.AI 267件 / cs.LG 200件 /
# cs.CL 109件）。`limit_per_feed` で上から数件を取るだけなので、
# 「新しい順の先頭数件」であって「読む価値のある順」ではない。
# 関連度で絞る仕組みは無い（既知の負債）。
PAPER_FEEDS: tuple[Feed, ...] = (
    Feed("arXiv cs.AI", "https://rss.arxiv.org/rss/cs.AI"),
    Feed("arXiv cs.CL", "https://rss.arxiv.org/rss/cs.CL"),
    Feed("arXiv cs.LG", "https://rss.arxiv.org/rss/cs.LG"),
)

AI_FEEDS: tuple[Feed, ...] = (
    *PRIMARY_FEEDS,
    *JA_COMMUNITY_FEEDS,
    *OVERSEAS_FEEDS,
    *PAPER_FEEDS,
)
