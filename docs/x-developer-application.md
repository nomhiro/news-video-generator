# X 開発者アカウント申請の記載内容

アカウント: `norr-tech`
申請日: 2026-08-16

「X のデータおよび API のすべてのユースケースを説明してください」への回答。
この欄は**データ保護の審査**に使われるので、取得するデータ・用途・第三者提供の有無を
明示する構成にしている。

審査は英語で行われるため英語版を本文とし、日本語版を控えとして残す。
**実装と食い違わせないこと。** 記載内容を変えたら実装（またはその逆）も見直す。

---

## 英語版（申請フォームに貼る）

```
I operate a single account (@norr-tech) that publishes original Japanese-language
commentary on AI and IT news. I am an individual developer; this is not a service
offered to other people, and no data obtained from the X API is resold or shared
with any third party.

WHAT I POST (write access)

Each post is original commentary I produce about a news story: what happened, and
who can use it for what. A post names the source outlet in plain text (for example
"出典: TechCrunch") and does not reproduce the article's text. Roughly four posts
per day, published on a fixed schedule (around 08:00, 12:30, 19:00 and 21:30 JST).

Some posts carry one image that I generate myself: a hand-drawn-style explanatory
diagram of the concept being discussed, with short Japanese labels. The image is my
own generated content, not material taken from X or from the source article.

I plan to add two further post types that are already built but not yet enabled:
short threads that expand a single topic, and posts that link to my own YouTube
videos on the same subject.

I do not use the API to reply to other accounts, mention accounts, send direct
messages, follow or unfollow, like, or repost. I do not post identical or
near-identical content repeatedly.

WHAT I READ (read access)

Only the public metrics of my own posts: impression count, likes, reposts, replies
and bookmarks. Each of my posts is measured exactly twice — about 24 hours and
about 7 days after publication — and then never queried again. At roughly four
posts per day this is a small and predictable volume of read requests.

I use these numbers for one purpose: to see which topics and formats my own readers
respond to, so I can choose better subjects. A human reads the numbers and decides;
nothing is adjusted automatically.

WHAT I DO NOT COLLECT

I do not read, store, or analyse other users' posts, profiles, followers, timelines,
search results, or any personal data. I do not scrape. I do not build datasets about
people. The only X data that reaches my storage is the identifiers of my own posts
and the aggregate metrics of those posts.

STORAGE AND RETENTION

My own post identifiers and their metrics are stored in my own private Azure Storage
account, encrypted at rest and reachable only by me. They are kept so that I can look
back at how topics performed. OAuth tokens are stored separately in the same private
account and are never written to logs. Nothing is transmitted to any third party.

COMPLIANCE

The account posts automatically, and I follow X's automation rules: it acts only on
its own account, it does not interact with other users, and it does not spam. I can
stop all posting immediately from an operator screen, and posting is disabled by
default. Because the X API has no idempotency key, my implementation deliberately
drops a post whose outcome is unknown rather than retrying it, so the same content is
never published twice.

I understand that I may not resell anything received through the X API.
```

---

## 日本語版（控え）

```
私は個人開発者として、AI・IT ニュースについて日本語で独自の解説を発信する
単一のアカウント（@norr-tech）を運用します。他者に提供するサービスではなく、
X API から得たデータの再販や第三者提供は一切行いません。

【投稿するもの（書き込み）】
各投稿は、私が作成するニュースについての独自の解説です。何が起きたか、そして
誰がどの作業に使えるかを書きます。出典は媒体名をテキストで示し（例「出典:
TechCrunch」）、記事本文の再掲は行いません。1日約4件、固定の時刻（JST 08:00 /
12:30 / 19:00 / 21:30 前後）に投稿します。

一部の投稿には、私が自分で生成した画像を1枚添えます。話題の概念を手描き風の
説明図にしたもので、短い日本語のラベルが入ります。X や引用元から取得した素材では
なく、私の生成物です。

実装済みで未有効の投稿種別が2つあります。1つの話題を展開する短いスレッドと、
同じ主題の自分の YouTube 動画へリンクする投稿です。

API を使って他アカウントへの返信・メンション・DM・フォロー・いいね・リポストは
行いません。同一または類似の内容を繰り返し投稿することもしません。

【読み取るもの】
自分の投稿の公開指標のみです（インプレッション数、いいね、リポスト、返信、
ブックマーク）。各投稿につき、公開から約24時間後と約7日後の**2回だけ**取得し、
以降は照会しません。1日約4件なので、読み取り要求は少量かつ予測可能です。

用途は1つで、自分の読者がどの話題・形式に反応するかを見て主題の選定を改善する
ためです。数値は人が読んで判断し、自動調整は行いません。

【取得しないもの】
他のユーザーの投稿・プロフィール・フォロワー・タイムライン・検索結果、および
個人データは読み取らず、保存も分析もしません。スクレイピングは行いません。
人物に関するデータセットを作りません。保存領域に入る X のデータは、自分の投稿の
識別子とその集計指標だけです。

【保存と保持】
自分の投稿の識別子と指標は、私の非公開の Azure ストレージアカウントに保存します
（保存時暗号化、アクセスは私のみ）。過去の話題の反応を振り返るために保持します。
OAuth トークンは同じ非公開アカウント内に分離して保存し、ログには出力しません。
第三者への送信は行いません。

【遵守】
このアカウントは自動で投稿しますが、X の自動化ルールに従います。自分のアカウント
に対してのみ動作し、他ユーザーと相互作用せず、スパム行為を行いません。運用画面から
即座に全投稿を停止でき、投稿は既定で無効です。X API には冪等キーが無いため、
結果が不明な投稿は再送せず破棄する実装にしており、同じ内容が2回公開されることは
ありません。

X API を通じて受け取ったものを再販できないことを理解しています。
```

---

## 記載内容を実装と一致させるための対応表

申請文で約束したことと、それを保証しているコードの対応。**片方を変えたらもう片方も見る。**

| 申請文の記述 | 実装 |
|---|---|
| 1日約4件、固定時刻 | `X_POST_TIMES`（既定 08:00 / 12:30 / 19:00 / 21:30）、`X_POSTS_PER_DAY`（既定4） |
| 出典は媒体名のテキスト、記事本文の再掲なし | `PostGenerator._assemble` が `出典: <source>` を付ける。本文は独自解説 |
| 画像は自分の生成物 | `CardVisualGenerator` + `gpt-image-2`。引用元の素材は使わない |
| 返信・DM・フォロー等をしない | `XClient` に該当メソッドが無い（`create_post` / `upload_media` / `fetch_metrics` のみ） |
| 同一内容を繰り返さない | 記事の消費記録（`NewsArticle.consumed`）で一度使った記事を再利用しない |
| 自分の投稿の指標のみ、各2回 | `MEASUREMENT_OFFSETS = (24h, 7d)`、`collect_metrics` は自分の `tweet_id` のみ照会 |
| 他ユーザーのデータを取得しない | 読み取りは `GET /2/tweets?ids=<自分の投稿>` のみ |
| 自動調整しない | 指標を読む処理はファイルに書くだけ。型・時刻・記事選定へのフィードバックは無い |
| 非公開ストレージに保存 | Azure Blob（共有キー無効、Entra ID 認証、`metrics/x/YYYY-MM-DD.json`） |
| トークンをログに出さない | `scripts/authorize_x.py` はトークン値を表示しない。`SecretStr` を使用 |
| 即座に停止できる | `data/x_posting.json` のスイッチ。画面から切り替え |
| 既定で無効 | `X_POSTING_ENABLED = false` |
| 結果が不明なら再送しない | `XSendUncertainError` → `NEEDS_REVIEW`。`create_post` はリトライしない |

## 申請前に確認すること

- [ ] **スレッドと宣伝投稿を「実装済みで未有効」と書いてよいか。** 現状 `KIND_ROTATION` は
      単発とカードのみ（仕様書の「第1弾で配線していないもの」参照）。将来使う意図があるので
      申請には含めたが、当面使わない旨も書いてある
- [ ] **アカウントのプロフィールに自動投稿であることを書く。** X の自動化ルールは
      「欺瞞的でないこと」を求める。申請文で自動化を明示しているので、プロフィールでも
      触れておくと齟齬が無い
- [ ] リンク付き投稿（宣伝）を有効にするときは単価が13倍（$0.015 → $0.20）になる。
      `X_MONTHLY_BUDGET_USD` を見直す
