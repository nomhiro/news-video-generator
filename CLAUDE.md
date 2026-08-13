# CLAUDE.md

ニューストピックから YouTube Shorts / TikTok / 長尺向けの動画を自動生成するツール。
CLI と Web UI の2つの入口がある。

## コマンド

```bash
uv sync                          # 依存をロックファイルから同期
uv run alembic upgrade head       # DB スキーマを当てる（Web 起動時に自動でも走る）
uv run python -m scripts.push_tokens              # ローカルの OAuth トークンを Blob へ
uv run python main.py "トピック" -l ja -f short     # 動画を1本生成
uv run python web_app.py                          # Web UI (http://127.0.0.1:8000)

uv run ruff check . && uv run ruff format .        # lint と整形
uv run mypy                                       # 型チェック
uv run pytest                                     # テスト（slow/live は既定で除外）
uv run pytest -m slow                             # ffmpeg を実際に起動するテスト
uv run pytest -m live                             # 実APIを叩く（課金あり）

npm run build:css                                 # テンプレートのクラスを変えたとき
```

`-f` は `short`（縦・約35秒）/ `tiktok`（縦・60〜90秒）/ `long`（横・約5分）。

## 外部依存

**ffmpeg / ffprobe** が PATH に必要（`video_composer.py` が subprocess で直接呼ぶ）。

**Azure OpenAI** — 台本生成（`AZURE_OPENAI_DEPLOYMENT`）と画像生成
（`AZURE_OPENAI_IMAGE_DEPLOYMENT`）の2つのデプロイを使う。

画像生成は `infra/` の azd テンプレートで払い出す**専用の Foundry プロジェクト**
（`rg-newsvideo-img` / westus3）に置いており、台本生成とは別リソース。
`AZURE_OPENAI_IMAGE_ENDPOINT` / `AZURE_OPENAI_IMAGE_API_KEY` で指定する。
未設定なら台本生成と同じものを流用するので、単一リソース構成でも動く。

**Azure AI Speech** — 音声合成。キーとリージョンのみ（`AZURE_SPEECH_API_KEY` /
`AZURE_SPEECH_REGION`）。台本・画像の Azure OpenAI とは別リソース
（`rg-newsvideo-speech` / japaneast）。

記事本文の抽出は **trafilatura**。取得は httpx で行い（User-Agent とタイムアウトを
制御するため）、抽出結果が100文字未満なら記事ページでないと判断して破棄する
（一覧ページからナビゲーションの断片が返ることがある）。

環境変数の一覧は `.env.example` を参照。設定は `config.py` の
pydantic-settings モデルで、**フィールド名の大文字がそのまま環境変数名**になる
（`azure_openai_endpoint` ← `AZURE_OPENAI_ENDPOINT`）。
必須項目が欠けていると起動時に `ValidationError` で落ちる。

`tests/test_config.py` がモデルの項目と `.env.example` の記載を双方向に
突き合わせているので、設定を足すときは両方を更新する（片方だけだとテストが落ちる）。

APIキーは `SecretStr` なので、使うときは `.get_secret_value()` が必要。
ログや例外に設定オブジェクトが丸ごと出ても平文が漏れないようにしている。

## 触るときに知っておくべきこと

### デプロイ名とモデル名は違う

Azure の「デプロイ名」はモデル名と一致しないことが多い。このプロジェクトでは
モデル `gpt-image-2` のデプロイ名が `gpt-image-2-1` になっている。
モデル名を推測して既定値に置くと `unknown_model` という分かりにくい 400 になる
（一度踏んだ）。そのため画像デプロイ名には既定値を置かず、必須設定にしている。

デプロイ名は次で確認できる。

```bash
az cognitiveservices account deployment list -n <resource> -g <resource-group> -o table
```

### 画像の生成解像度は動画の出力解像度と違う

`gpt-image-2` は両辺が16の倍数であることを要求するため、**動画の出力解像度
1080x1920 をそのまま指定できない**（1080 が16の倍数でない）。
実際には 1152x2048（厳密な 9:16）で生成し、ffmpeg 側で 1080x1920 に縮小する。
アスペクト比が一致しているのでクロップは発生しない。長尺は 2048x1152 → 1920x1080。

サイズ定数を変えるときは `validate_size()` が制約（16の倍数 / 長辺3840以下 /
アスペクト比3:1以下 / 総ピクセル数 655,360〜8,294,400）を検証する。

### 画像生成のクォータが速度の律速

`gpt-image-2` のクォータは**サブスクリプション単位・リージョン単位で上限 4**。
リソースを増やしてもリージョンが同じなら増えない。引き上げには Azure ポータルからの
申請が必要（自動化できない）。

現在は westus3 に capacity 4。ショート1本で6枚使うため、1本の生成に1分以上かかる。
並行数は `IMAGE_MAX_CONCURRENCY` で制御し、429 は `tenacity` でバックオフ再試行する。

画像生成を別リージョンに置いている理由がこれ。台本生成のある eastus2 は
既存デプロイ（別プロジェクトの `gpt-image-2-1`）が 4/4 を使い切っていた。

異なるプロンプトの複数枚を `n` パラメータで1リクエストに畳むことはできない
（`n` は同一プロンプトからの複数枚生成）。6枚は6リクエストが必要。

### インフラは azd で管理する

```bash
azd provision          # 払い出し（AI リソースのみ）
azd provision --preview # what-if。作成されるものを確認する
azd down               # 破棄
```

`infra/main.bicep` がサブスクリプションスコープでリソースグループから作る。
`deployApp` パラメータは既定 false で、Container Apps は払い出さない。
Dockerfile は検証済み。クラウドで動かすには生成物の保存先が未解決
（`infra/core/app-hosting.bicep` の冒頭を参照）。

**API キーは Bicep の output にしていない。** ARM の output はデプロイ履歴に
平文で残り、リソースグループの閲覧権限があれば読めてしまう。
`infra/hooks/postprovision.*` が az CLI で取得して表示する。

Foundry プロジェクトとモデルデプロイには `dependsOn` を入れて直列化してある。
どちらも親アカウントを変更する操作なので、並列に走らせると
`Another operation is in progress on the resource` で片方が失敗する（一度踏んだ）。

### セグメントのタイミングは bookmark で取る（推定しない）

`voice_generator.py` はセグメントごとに `<bookmark mark="seg_{i}"/>` を
**先頭**に置いた SSML を1回だけ合成し、`bookmark_reached` イベントで
各セグメントの開始オフセットを受け取る。これが `video_composer` の
画像切り替え時刻になる。

Google Cloud TTS の Chirp 3 HD から移行した理由がここにある。Chirp 3 HD は
SSML の `<mark>` に対応せず、実装が3系統（個別合成して結合 / 文字数按分で推定 /
`<mark>` を試みて必ず按分にフォールバック）に分かれていた。実際に効いていたのは
按分の推定で、ナレーションと画像の切り替えがずれていた。
1系統に統合した副産物として `pydub` / `mutagen` / `audioop-lts` /
`google-cloud-texttospeech` の4依存が消えた。

戻すときに壊しやすい点。

- **bookmark はセグメントの先頭**に置く。後ろに置くと得られるのが終了時刻に
  なり、画像切り替えが1セグメントぶんずれる。
- 返り値の要素数は**セグメント数 + 1**。末尾は音声全体の終了時刻で、
  最後のセグメントの表示時間を決めるのに必要（`_calculate_durations` の前提）。
- オフセットは**単調増加を強制**する。bookmark が欠けたら直前の値を使う。
  崩れると duration が負になり、ffmpeg が無言で壊れた動画を作る。
- テキストは XML エスケープする。記事タイトルに `&` や `<` が実際に混じる。
- ボイスは**標準 Neural**（`ja-JP-NanamiNeural` / `en-US-AvaNeural`）。
  Dragon HD 系（`*:MAI-Voice-*`）は音質が上だが `<prosody>` 非対応で、
  形式別の話速（1.1〜1.25）を指定できず機能が退行する。

オフセットは 100ナノ秒刻み（tick）で来るので `10_000_000` で割る。

### 台本の構造は Structured Outputs のスキーマで強制されている

`src/models/script.py` の `ScriptDraft` が LLM への出力契約そのもの。
フィールドを増やすと生成される JSON スキーマが変わる。

`ScriptDraft` は意図的に `full_narration` と `language` を持たない。

- `full_narration` は `segment_narrations` の連結でコード側が導出する。
  両方をモデルに出させると「連結が full_narration と一致すること」という
  冗長な制約が生まれ、モデルは一致を優先して**空のセグメントでパディング**した。
  導出にすればこの矛盾は起こりえない。
- `language` は呼び出し元が権威を持つ。

`segment_narrations` / `image_prompts` / `text_overlays` の要素数が一致することは
音声のタイミング同期と動画合成の前提なので、バリデータで強制している。

### テンプレートに Tailwind クラスを足したら CSS を再生成する

`static/css/app.css` は Tailwind v4 の生成物で、**テンプレートで実際に使われている
クラスだけ**が入っている。新しいユーティリティクラスを使ったら
`npm run build:css` を実行しないと、そのスタイルは効かない。

CDN（`cdn.tailwindcss.com`）は使わない。公式が本番非推奨としており、
ブラウザ側で JIT コンパイルするため初回描画が遅く、オフラインで動かない。
HTMX も `static/vendor/htmx.min.js` にベンダリングしてある（2.0.4）。

Node は CSS のビルドにだけ必要で、アプリの実行には不要。

### AIモデルを変えたら登録簿を更新する

`src/model_registry.py` に使用中の全モデルと廃止日を集約している。
`tests/test_model_registry.py` が廃止日の90日前を過ぎたら失敗し、CI の週次 cron でも走る。

この仕組みがある理由: `imagen-3.0-generate-002` が 2025-11-10 に停止していたのに
9か月気付かず、その間パイプライン全体が動作していなかった。モデルIDがアダプタ内に
散在し、廃止日をどこにも記録していなかったことが原因。

## 既知の設計上の負債

リファクタリング途中のため、以下は意図的に残している。

- **`data/news/*.json` を書き換え可能なデータストアとして使っている。**
  同一プロセス内はロックで守っているが、複数プロセスからは守れない。
  Phase 4 で SQLite に移す。

### OAuth トークンも保存先を差し替える

YouTube / TikTok のトークンと `client_secrets.json` は
`src/storage/tokens.py` の `TokenStore` 経由で読み書きする。
`TOKEN_STORE=local`（既定）はファイル、`blob` は Blob Storage の
専用コンテナ（`tokens`）。

コンテナで local だと、再起動でトークンが消えて毎回ブラウザ認証が必要になる。
YouTube の OAuth は `InstalledAppFlow`（localhost にリダイレクト）なので
コンテナの中では実質的に完了できない。**認証はローカルで1回行い、
`uv run python -m scripts.push_tokens` で保存先に送る**運用にする。

- **`Credentials.from_authorized_user_file` は使えない。** 保存先が Blob の
  ときローカルにファイルが無いので、`from_authorized_user_info` /
  `InstalledAppFlow.from_client_config`（どちらも dict を受ける）を使う。
- **壊れた値は「無い」として扱う**（`read_json`）。トークン更新が中断されて
  壊れた JSON が残った場合、例外にすると認証フローにも入れず画面から
  復帰できない。
- **保存先に到達できないときは未認証として返す。** 例外を投げると、
  画面を開くだけで 500 になる。未認証なら認証ボタンが出る。
- ローカル保存は一時ファイル + `replace` で原子的に書く。更新中に落ちると
  壊れた JSON が残り、次回の起動で再認証になる。
- 名前（`youtube_token` / `youtube_client_secrets` / `tiktok_token`）は
  ローカルと Blob で共通。`config.token_paths` の対応表とずれると、
  local ↔ blob を行き来したときに別のものを指す。

Key Vault ではなく Blob にしている。生成物用に**すでに Entra ID 専用の
ストレージアカウントがある**（共有キー認証は無効、匿名アクセス不可、
7日のソフトデリート）ため、認証経路とコードを流用できる。
トークンが利用者ごとに増える、監査が要件になる段階で Key Vault に移す
（`TokenStore` の実装を1つ足すだけで済む）。

### 生成の進捗はジョブ表に持つ（プロセスメモリではない）

`/generate` は**生成しない**。`jobs` テーブルに行を作って即座に返り、
実行は `JobWorker` のスレッドが担う。`/status` は行を読むだけ。

以前は `GenerationState`（プロセスメモリ上のシングルトン）に持っていた。
再起動で消え、レプリカ間で共有されず、失敗した1件を再実行する手段も
無かった。実測で確認済み: 生成中にサーバーを kill しても行は RUNNING で
残り、再起動後の `/status` に同じ進捗が出る。

- **同じジョブを2人が実行しないための仕組みはリース**。掴むときに
  `lease_expires_at` を入れ、実行中は heartbeat で延ばす。ワーカーが
  落ちれば期限が切れ、他のワーカーが `requeue_expired()` で QUEUED に
  戻す。試行回数が上限（既定3回）を超えたら FAILED で打ち切る
  （毎回落ちる記事で画像生成のクォータを食い潰さないため）。
- **SQLite には `FOR UPDATE SKIP LOCKED` が無い。** 掴む操作は
  「status が QUEUED のまま」を条件にした UPDATE の影響行数で競合を
  検出している。PostgreSQL のときだけ `SKIP LOCKED` を使う。
- **本文はジョブ行に持たせない。** `article_id` だけを持ち、実行時に
  ニュースストアから読み直す。以前は記事オブジェクトを background task の
  引数でメモリ渡ししていたので、落ちると本文ごと消えていた。
- **SQLite は `journal_mode=WAL` が必須。** ワーカーが書いている最中に
  `/status` が読むので、WAL でないと `database is locked` になる。
- **時刻は読み出し時に UTC を付け直す。** SQLite は
  `DateTime(timezone=True)` でもタイムゾーンを保存しないため、
  naive で返ってくる。付け直さないとリースの比較が
  `can't compare offset-naive and offset-aware` で落ちる
  （`src/storage/jobs.py` の `_as_utc`）。
- **スキーマは Alembic**。起動時に `upgrade head` を自動で当てている。
  `alembic.ini` は**ロケールの encoding で読まれる**ので日本語コメントを
  書くと cp932 で `UnicodeDecodeError` になる（一度踏んだ）。
  接続先は ini に書かず `migrations/env.py` がアプリ設定から取る。

**レプリカを2つ以上にするには DB を差し替える必要がある。** SQLite の
ファイルは1台のファイルシステム上にしかないので、行にしただけでは
共有されない。`DATABASE_URL` を Azure Database for PostgreSQL に向ければ
コードはそのまま動く（SQLAlchemy を挟んでいる理由がこれ）。

### 生成物は「作る場所」と「置く場所」を分ける

生成は必ず `output_dir`（ローカル）で行う。ffmpeg は subprocess で動く
外部プロセスで、パスしか受け取れないため変えられない。

保存先は `src/storage/artifacts.py` の `ArtifactStore` で差し替える。
`ARTIFACT_STORE=local`（既定）ならローカルのまま、`blob` なら
Azure Blob Storage に publish する。コンテナのファイルシステムは再起動で
消え、レプリカ間でも共有されないので、クラウドで動かすなら blob が前提。

- **キーは posix 形式の相対パス**（`videos/20260814_005245_ja.mp4`）。
  Windows の `\` をそのまま Blob 名にするとローカルとキーが一致せず、
  アップロードした動画を一覧から引けなくなる。`normalize_key` が正規化し、
  `..` を含むキーは弾く（キーは HTML 経由でフォームから戻ってくる値）。
- **読み出しは `fetch()` でローカルパスを借りる。** ローカル保存なら実体を
  そのまま渡し、blob なら一時ファイルに落として `with` を抜けたら消す。
  アップローダは `with` の内側で呼ぶ（外に出すとファイルが消えた後になる）。
- **保存の失敗で生成を失敗させない。** 動画はローカルに残っているので、
  publish の例外は記録するだけにする（`Pipeline._publish_artifacts`）。
  ここで投げると成功した生成物ごと失敗扱いになる。

認証は**アカウントキーを使わない**。ストレージアカウント側で
`allowSharedKeyAccess: false` にしてあり、`DefaultAzureCredential` で
接続する（ローカルは `az login`、Container Apps はマネージド ID）。
ユーザー割り当て ID では `AZURE_CLIENT_ID` の指定が必須で、
省略するとシステム割り当てを探して認証に失敗する。

`tests/test_artifacts_blob_live.py` が実 Blob で往復を確認する
（`uv run pytest -m live -k blob`）。Entra ID 認証・キーの階層化・
バイト列の復元はフェイクでは検証できない。

### ニュースストアの更新はロックで囲む

`data/news/{category}.json` の更新は read-modify-write（読み込み → 変更 →
全件保存）。動画生成は threadpool のスレッドから `mark_as_generated` を呼び、
イベントループ側は同時に `toggle_selection` を処理しうるため、
排他しないと更新が失われる（選択状態や生成済みフラグが消える）。

`NewsAggregator._category_lock` がカテゴリ単位で直列化する。
新しい更新メソッドを足すときは `_update_article` を通すか、
読み込みから保存までを同じロックで囲む。

ロックは `RLock`。`_load_category` / `_save_category` も内部でロックを取るため、
外側で取っている区間から呼ぶと通常の `Lock` ではデッドロックする。

保存は一時ファイル + `Path.replace` で原子的に行う。直接上書きすると、
書き込み中に落ちたときに壊れた JSON が残って全記事を失う。
なお Windows では置換の瞬間に読み手が `PermissionError` を受けるため、
読み取りもロックで守っている（実測で発生した）。

### Web の依存は lifespan で組み立てる

`src/web/dependencies.py` の `AppContext` が依存をまとめ、`lifespan` が
起動時に組み立てて `app.state.context` に置く。ルートは
`Depends(get_aggregator)` のように受け取る。

テストでは `app.dependency_overrides[get_pipeline] = ...` で差し替える。
以前はモジュールのグローバル変数を monkeypatch で書き換えるしかなかった。

`TestClient` は `with TestClient(app) as client:` の形で使う。
`with` を使わないと lifespan が走らず、依存が未初期化のまま
`RuntimeError` になる。

### `generate_videos_task` を `async def` にしてはいけない

`src/web/routes.py` の `generate_videos_task` は**同期関数でなければならない**。
Starlette の `BackgroundTask` は非同期関数をイベントループ上で直接 await するため、
同期ブロッキングの `pipeline.run()`（数分かかる）を `async def` から呼ぶと
**生成中に Web サーバー全体が応答しなくなる**（`/status` のポーリングも止まる）。

`tests/test_web_background.py` が2つの角度から見張っている。関数が同期であることの
検査と、uvicorn を実際に起動して生成中に `/status` が返ることの検査。
後者は TestClient では書けない（TestClient はバックグラウンドタスクを
リクエスト処理内で完了させてしまい、この状況を再現できない）。

### コンテナで動かすときの前提

```bash
docker build -t newsvideo .
docker run --rm -p 8000:8000 --env-file .env \
  -e WEB_HOST=0.0.0.0 \
  -e GOOGLE_APPLICATION_CREDENTIALS=/secrets/gcp-sa.json \
  -v "/path/to/service-account.json:/secrets/gcp-sa.json:ro" \
  newsvideo
```

Git Bash から実行するときは `MSYS_NO_PATHCONV=1` を付ける。付けないと
`-v` の右辺（`/secrets/...`）を Windows パスに変換されてマウントが壊れる。

**日本語フォントの同梱が必須。** `fonts-noto-cjk` を入れないと `drawtext` が
描画できず動画合成が失敗する。イメージは
`/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc` を `VIDEO_FONT_PATH` で
明示している。フォント探索は `VideoComposer.JAPANESE_FONT_CANDIDATES` で
Windows / Linux / macOS のパスを並べており、`VIDEO_FONT_PATH` が最優先。


## 規約

- コメントと docstring は日本語。「何をしているか」ではなく**なぜそうしたか**を書く。
  特に、知らないと善意で元に戻されてしまう判断（上記の解像度やスキーマの話）は必ず残す。
- 例外を包むときは `from e` を付けてチェーンを保つ（ruff B904 が検査する）。
- `zip()` は長さが一致するはずの場所では `strict=True` を付ける。
