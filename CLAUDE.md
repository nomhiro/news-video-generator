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

cd remotion && npm install                        # 動画レンダラの依存（初回のみ）
cd remotion && npm run studio                     # 見た目を作り込む（ブラウザで見ながら）
```

`-f` は `short`（縦・約35秒）/ `tiktok`（縦・60〜90秒）/ `long`（横・約5分）。

**clone した直後に一度だけ hook を有効化する。** lint / 型 / テストは GitHub Actions
ではなく push 前のローカルで走る（後述の「チェックは pre-push に寄せている」）。

```bash
git config core.hooksPath .githooks
```

## 外部依存

**ffmpeg / ffprobe** が PATH に必要（`video_composer.py` が subprocess で直接呼ぶ）。

**Node 22 / Chrome Headless Shell** — `VIDEO_RENDERER=remotion` のときに必要。
`remotion/` が独立したパッケージで、CSS 用の `package.json` とは別物
（あちらは devDependencies だけで実行時に Node は不要）。

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

`ScriptDraft` は意図的に `full_narration` / `language` / `source_url` を持たない。

- `full_narration` は `segment_narrations` の連結でコード側が導出する。
  両方をモデルに出させると「連結が full_narration と一致すること」という
  冗長な制約が生まれ、モデルは一致を優先して**空のセグメントでパディング**した。
  導出にすればこの矛盾は起こりえない。
- `language` は呼び出し元が権威を持つ。
- `source_url` も呼び出し元が権威を持つ。**モデルは URL を知らない**
  （プロンプト入力は記事のタイトルと本文だけで、URL は意図的に渡していない）。
  出させれば確実に捏造する。`NewsArticle.url` を
  `PipelineJobRunner` → `Pipeline.run` → `ScriptGenerator.generate` と引数で通し、
  `to_script` が説明文に「出典: 〜」を追記する（`_with_source`）。
  CLI は自由テキストのトピックを取るので `--source-url` は任意で、
  空なら追記しない。

`segment_narrations` / `image_prompts` / `text_overlays` の要素数が一致することは
音声のタイミング同期と動画合成の前提なので、バリデータで強制している。

#### 独自解説は必須フィールドで強制する

`technical_insight`（技術的な仕組み）と `practical_impact`（実務インパクト）は
**必須**。ニュースをなぞるだけの出力は埋もれるうえ、YouTube の
「再利用されたコンテンツ」ポリシーに抵触するリスクがある。Structured Outputs では
必須フィールドをモデルが省略できないので、プロンプトでお願いするより保証が強い。

- 最低文字数（`MIN_INSIGHT_CHARS = 40`）は**言語非依存**。`ScriptDraft` が
  `language` を持たないため、バリデータの中で言語別の閾値を選べない。
- `Field(min_length=...)` だけでは足りない（全角空白を40個並べれば通る）。
  `_validate_insights` が strip 後の長さで見ている。
- 構成順序（フック → 事実 → 仕組み → インパクト → 結論）は
  `<<STRUCTURE_SPEC>>` として6種類すべてのプロンプトに差し込む。
  セグメント番号は `segment_allocation()` が `segment_count` から計算する
  （short/tiktok は6、long は10。プロンプトに番号を書くと仕様とずれる）。
  端数は解説側に寄せる（6なら仕組みが2、10なら仕組みとインパクトが3ずつ）。
- **分量の予算は変えていない。** 解説は事実のなぞりを置き換えるものであって、
  総文字数を増やすものではない。増やすと `check_length_budget` に弾かれる。
- **構成指示の中で分量の上下限を繰り返す必要がある**（`_structure_spec` の末尾）。
  パートを5つに割った直後の実測では、ショート3本すべてが予算（180〜240文字）を
  超えた（307/310/378文字、1本は63秒で上限60秒超え）。パートを増やすと
  モデルは各パートに書き足す。逆に**上限だけを書くと今度は下振れし**、
  145文字まで縮んでセグメントが文の断片（`"仕組みは、バイトコード全体でなく、"`）
  になった。セグメント境界は画像の切り替え位置なので断片は成立しない。
  上下限の両方と「各セグメントは単独で文として言い切る」をセットで書く。

**スキーマは「入っていること」しか担保できない。** 内容が薄ければ結局要約と
変わらないので、`tests/test_script_insight_live.py`（`-m live`、台本だけ生成するので
画像クォータを消費しない）で実物を出して読む工程が必要。

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
`tests/test_model_registry.py` が廃止日の90日前を過ぎたら失敗する。

この仕組みがある理由: `imagen-3.0-generate-002` が 2025-11-10 に停止していたのに
9か月気付かず、その間パイプライン全体が動作していなかった。モデルIDがアダプタ内に
散在し、廃止日をどこにも記録していなかったことが原因。

**この検査は push 契機でしか走らない。** 以前は Actions の週次 cron でも回していたが、
Actions を CD 専用にしたときに外した（Issue #15）。現時点では `ACTIVE_MODELS` の
3件すべてが `shutdown_on=None` なので日付の経過だけで失敗しうる状態ではないが、
**`shutdown_on` に日付を入れた時点で、気付く経路を作り直す必要がある**
（pre-push は作業していなければ走らない）。

### 動画のレンダラは2つある（既定は今も ffmpeg）

`VIDEO_RENDERER` で切り替える。`ffmpeg` は静止画（`gpt-image-2`）を並べる
現行の方式、`remotion` は React で図解を描く方式。

`remotion` を選ぶと**画像生成 API を1回も呼ばない**。`gpt-image-2` のクォータ
（サブスクリプション・リージョン単位で上限4）が動画の律速だったので、
これが消えると X の画像カードとの共食いも無くなる。

実測（2026-08-17、2 vCPU / 4Gi / concurrency 2、1080x1920 / 35秒 = 1050フレーム）。

| 条件 | 時間 | ピーク RSS |
|---|---|---|
| 全画面 `filter: blur(40px)` あり | 598秒 | 1,519MB |
| blur なし | **199秒** | 1,915MB |

戻すときに壊しやすい点。

- **`--concurrency` を必ず明示する。** 既定は「ホストの CPU スレッド数の半分」で
  cgroup を見ない。`os.cpu_count()` がコンテナで20を返した罠と同じ構造。
  `_available_cpus()` の値を渡している。
- **全画面 `filter: blur()` を使わない。** 上の表の3倍差。デザインの制約であって
  実装の詳細ではない。`tests/test_remotion_design_rules.py` が名前で狙い撃つ
  （`box-shadow` を重ねる等の別経路は防げない）。検査は `//` と `/* */` を
  近似的に取り除いてから grep する（トークナイザではない）。`Background.tsx` /
  `theme.ts` が禁止している構文そのもの（`blur(` / `@font-face`）をコメントで
  説明しているため、削らずに素朴に grep すると自分の説明コメントに引っかかる。
  裏を返せば、文字列リテラルの中に `blur(` を隠せば検査を通り抜ける
  （近似ゆえの穴で、直していない）。
- **速くなるとメモリが増える。** blur を外した方がピーク RSS が上（1,519 → 1,915MB）。
  フレーム生成が速いぶんエンコード待ちのバッファが溜まる。4GB に収まるが余裕は
  2倍しかない。逃げ道は `disallowParallelEncoding`。
- **Remotion は無音の映像までしか作らない。** 音声の多重化は `mux_audio()` を
  共有する。Remotion 内で `<Audio>` を使って1発で作ってはいけない
  （1段で合成していた頃、マクサーが映像パケットを溜め込んでピーク 4,077MB で
  OOM killer に殺された）。
- **Web フォントを使わない。** システムの `fonts-noto-cjk` を `font-family` で
  参照する。`@font-face` は `delayRender` / `waitForFonts` で待たない限り
  最初の数フレームだけフォールバックフォントで焼かれ、**エラーにならない**。
  代償としてローカル（Windows / Yu Gothic）と本番（Linux / Noto Sans CJK）で
  字形が変わるので、**最終確認は Docker 経由で行う**。
- **フレーム範囲は Python 側で解く**（`resolve_frame_spans`）。単調増加の強制と
  「タイミングの要素数はセグメント数+1」の契約が Python にあるため。各開始秒を
  独立に丸めると長さ0のシーンができ、Remotion は例外を出さずシーンが飛んだ
  動画を黙って作る。
- **自動フォールバックは無い。** Remotion が失敗したらジョブを失敗させ、リースと
  再試行に任せる。黙って `ffmpeg` に落ちると「毎朝の生成が古い見た目で回り続けて
  誰も気付かない」状態になる（CD が無かった頃と同じ形の失敗）。
- **Node は `node:22-trixie-slim` から取る。** 実行ステージの `python:3.13-slim` は
  Debian 13 (trixie)。`node:22-slim` は bookworm(12) なので混ぜてはいけない。
- **タイムアウトは900秒**（実測199秒の4.5倍）。`FFMPEG_TIMEOUT_SEC`（1800秒）は
  流用しない。ジョブのリース（15分）とほぼ同じ長さになるが、`_start_heartbeat` が
  独立した daemon スレッドで延ばすので切れない（`src/jobs/worker.py`）。
  **そこを同期処理に変えると前提が崩れる。**

**Remotion のライセンスは個人・3人以下なら商用（収益化含む）も無料。**
4人以上は Company License が必須で、自動化用途は $0.01/render・最低 $100/月。
**受託や共同作業では相手方の人数も合算される。** 運用主体が変わったら再判定する。

### 図解の構造は LLM に出させ、文字はコードが描く

`src/models/scene.py` の `SceneVisual` が出力契約。`layout` は3種類
（`statement` / `compare` / `flow`）の閉じた集合で、レイアウト1つに React
コンポーネントが1つ対応する。

- **見出しとキャプションのフィールドは作っていない。** 見出しは
  `text_overlays[i]`、字幕は `segment_narrations[i]` から取る。検証フレームの
  実物で、見出し・キャプション・字幕の3つを乗せるとキャプションと字幕が
  同じことを言っていた（880c95f の「同じ主張を2回出しても情報は増えない」）。
- **`statement` は半数以下に制限している。** 図を持たないレイアウトなので、
  モデルが全部これを選ぶと静止画スライドショーだった頃の紙芝居に戻る。
  実在する劣化経路で、モデルは楽な選択肢に寄る。
- **`items` の数字は記事本文と突き合わせる。** カードでは「画像側は機械的に
  検査できないのでスタイル文で閉じた」（記事に無い `¥980` が絵に描かれた）が、
  Remotion では**描く文字がデータなので検査できる**。`ScriptGenerator` が
  `ungrounded_numbers` で見て、根拠が無ければ理由を伝えて引き直す。
  **分量の超過と違い、最終試行でも通さない。**
- **`stat`（数字1つを主役にする）レイアウトは作っていない。** 効果的だが、
  直したばかりの数値捏造を正面から誘発する。数値検査が実運用で効いていることを
  確認してから足す。
- **`items` の8字上限と「ちょうど2個」はカードからの借り物。** カードは
  1024x1024、動画は 1080x1920 で面積が違う。カードでは上限90字が正常な出力を
  3回連続で弾いた前例があるので、動画でも実測で決め直す。
- `image_prompts` は `remotion` では使わないが**残してある**。
  `VIDEO_RENDERER=ffmpeg` への退路を生かすため、両レンダラが同じ台本から
  動く状態を保つ。

## 既知の設計上の負債

リファクタリング途中のため、以下は意図的に残している。


- **`data/news/*.json` を書き換え可能なデータストアとして使っている。**
  同一プロセス内はロックで守っているが、複数プロセスからは守れない。
  Phase 4 で SQLite に移す。
- **見出しの改行が不自然に折れる。未解決。** `Subtitle.tsx:34` と
  `Headline.tsx:22` に `word-break: auto-phrase` を当てたが、実物のフレームで
  確認したところ効いていない。検証で「推論コストが桁で下 / がる」と、
  「ことでした」が「こ / とでした」に割れた（2026-08-17、`remotion/out/check.png`）。
  `_wrap_text` が14文字で機械的に切っているのと同じ課題が形を変えて残っている。
  直すなら次のいずれか。テキストが props に渡る前に Python 側で分かち書きして
  改行位置を制御する、`<wbr>` / ZWSP をコード側で挿入する、BudouX のような
  文節チャンク化を挟む。`auto-phrase` 単独では足りないと分かった前提で選ぶこと。

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

### クラウド（Container Apps）で動かす

```bash
azd env set DEPLOY_APP true
azd provision      # インフラ（Container Apps 環境 / Container App / AI リソース）
azd down           # 破棄（課金を止める）
```

**アプリの反映は `azd deploy` ではなく CD で行う**（後述の「アプリの反映は
`main` へのマージで起きる」）。`azd` はインフラの払い出しと破棄にだけ使う。

`azd deploy` を打つと、CD が付けるタグ（`gh-<sha>`）とは別系列の
`azd-deploy-<timestamp>` タグと suffix 無しのリビジョンができ、
「いま動いているものがどの commit か」が追えなくなる。手で緊急に反映する必要が
あるときは CD と同じ経路（下記）を打つ。

実際に踏んだ落とし穴。

- **`migrations/` と `alembic.ini` をイメージに入れる。** 起動時に
  `alembic upgrade head` を走らせているので、無いと
  `CommandError: Path doesn't exist: /app/migrations` で**起動に失敗する**。
  ローカルには常にあるため、コンテナに載せたときだけ露見した。
  `tests/test_container_image.py` が Dockerfile の COPY を検査している。
- **初回の `azd provision` は MSI の伝播レースで失敗することがある。**
  `IdentityDoesNotExist ... No managed service identities are associated with
  resource .../containerApps/...` が出たら、そのまま再実行すれば通る。
  ユーザー割り当て ID の作成直後に Container App がそれを参照するため。
- **`AZURE_CONTAINER_REGISTRY_ENDPOINT` を output に出す**必要が以前はあった
  （無いと `azd deploy` が push 先を決められず
  `could not determine container registry endpoint` で止まった）。
  `azd deploy` を使わなくなり、レジストリも GHCR に移したので削除した。
- **1 vCPU では ffmpeg が異常終了した。** 1080x1920 / preset=medium の
  エンコードが 0.4x speed しか出ず、終了コード付きで落ちた。2 vCPU / 4Gi に
  上げてショートは完走している（長尺はまだ OOM する。上の負債を参照）。
  consumption プロファイルは CPU:メモリ = 1:2 の組み合わせしか受け付けない。
- **`os.cpu_count()` はコンテナでもホストのコア数を返す。** 2 vCPU の
  割り当てに対して 20 が返り、ffmpeg が既定でその数だけスレッドを立てる。
  `video_composer._available_cpus()` が cgroup の `cpu.max` を読んで
  `-threads` に渡している。
- **アプリのログが1行も出ていなかった。** uvicorn が起動時に
  `logging.config.dictConfig()` を呼び、既存のロガーを無効化していた
  （`logger.disabled = True`）。`_get_logger()` で毎回戻している。
  CLI では起きないので、Web だけで消えていた。
- **ログの絵文字は端末のときだけ。** クラウドのログでは
  `INFO:` / `OK:` / `ERROR:` / `WARN:` になる（`Log_s startswith "ERROR:"`
  で絞れる）。`LOG_EMOJI` で上書きできる。

  **「端末である」だけでは足りない。** Windows の日本語コンソールは TTY だが
  cp932 で、絵文字を書き込むと化けるのではなく `UnicodeEncodeError` で
  **落ちる**（CLI が起動直後に
  `'cp932' codec can't encode character '\U0001f680'` で死んだ）。
  `_can_encode()` が出力先の `encoding` で実際に試し、書けなければ
  `LOG_EMOJI=true` よりも優先して ASCII に落とす
  （絵文字が出ないより実行が落ちる方が実害が大きい）。

  **絵文字を print で直書きしない。** `main.py` は
  `src/utils/logger.prefix()` を通す。新しい絵文字を使うときは
  `_EMOJI_PROBE` にも足す（`tests/test_logger.py` が `src/` と `main.py` を
  走査して漏れを検出する。走査範囲を絞ると見逃す）。
- **リスト型の設定は `NoDecode` を付ける。** pydantic-settings は list を
  JSON として解釈しようとし、`.env` 経由では通るのに**実際の環境変数だと
  落ちる**（`SettingsError`）。Container Apps に `SCHEDULE_FORMATS=short,long`
  を env で渡した瞬間に起動しなくなった。
- **キーは `@secure()` パラメータ → Container App の secrets → env の
  `secretRef`** で渡す。env に直接書くと `az containerapp show` に平文で出る。
  `@secure()` は ARM のデプロイ履歴にも残らない。
- 台本生成の Azure OpenAI は azd の管理外（別プロジェクトの既存リソース）
  なので、`azd env set AZURE_OPENAI_ENDPOINT/...` で値を渡す必要がある。

**定期実行はアプリ内のスレッドで動く**（`src/jobs/scheduler.py`）。
Container Apps Jobs を使わない理由は、ジョブ表がコンテナのローカル
ディスク上の SQLite で、別コンテナからは書けないため。
レプリカを増やすときはこの前提が崩れる。

**minReplicas = 1 なので、動かしている間は常に課金される。**
使わないときは `azd down` で破棄する（ストレージの生成物も消えるので、
必要なものは先に取り出す）。

### アプリの反映は `main` へのマージで起きる

`.github/workflows/deploy.yml` が `main` への push で走る。Actions に置くのは
**この1本だけ**（lint / 型 / テストは後述の pre-push）。ランナーでイメージを
ビルドして **GHCR**（`ghcr.io/nomhiro/news-video-generator/web`）に push し、
`az containerapp update` でリビジョンを差し替え、新リビジョンがトラフィックを
受けるまで待つ。

この仕組みが無かったとき、マージしても反映されず、**気付かないまま毎朝
06:30 JST の自動生成が旧コードで走り続けていた**（PR #14 のマージ 14:18 UTC に対し、
稼働リビジョンの作成は 12:09 UTC）。

**`azd provision` を CD から絶対に走らせない。** `containerImage` パラメータが
`main.parameters.json` の既定（`mcr.microsoft.com/k8se/quickstart:latest`、
8080 待ち受け）に戻り、プローブが通らず Activating のままリビジョンが残る。
`tests/test_deploy_workflow.py` が workflow の中身を検査している。

**レジストリは GHCR だけ。Azure Container Registry は使わない。**
GitHub の外にもう1つレジストリを持たないため。**GHCR のパッケージは public に
してある**ので、Container Apps 側に pull 用の資格情報を持たせていない。
private にすると、ACA はリビジョン作成時とレプリカ再起動時に毎回 pull するため
短命な `GITHUB_TOKEN` では足りず、**長期の PAT を Container App の secret として
持たせることになる**（それを避けるために public にしている）。
GHCR は public パッケージのストレージが無料で、容量上限も気にしなくてよい。

push は `GITHUB_TOKEN`（この実行だけの短命なトークン）で行う。`packages: write`
権限が要る。イメージ名は `ghcr.io/${{ github.repository }}` で決まるので、
レジストリ名やリポジトリ名の設定は要らない。

**ビルドはレジストリ側ではなくランナー上の docker で行う。**
Dockerfile が `RUN --mount=type=cache` を使っており、これは BuildKit 専用の構文で、
ACR Tasks の quick build には BuildKit を有効にする口が無い（レジストリ側ビルドに
戻そうとしたときにここで詰まる）。

**生存確認は「リビジョン名 + `latestReadyRevisionName` + `trafficWeight == 100`」で
判定する。** `active == true` では判定できない。`activeRevisionsMode = Single` では
新リビジョンが ready になるまで旧リビジョンを落とさないため、移行中は新旧どちらも
active になる。イメージのタグでも判定できない（同じ commit を再デプロイすると
旧リビジョンのイメージも一致する）。

**`az containerapp update` は `--no-wait` で返させる。** az の LRO ポーリングは
サブスクリプションスコープの `containerappOperationStatuses` を読むため、権限を
リソースグループ以下に絞ると「更新は成功しているのに CLI が失敗を返す」形で
落ちうる。待つのは自前のスクリプト（`.github/scripts/wait_for_revision.sh`）。

**`--revision-suffix` には `run_attempt` も混ぜる。** GitHub の Re-run は
`run_number` を変えないため、失敗して再実行する経路で suffix が既存リビジョンと
衝突して必ず落ちる。

認証は **OIDC の federated credential**。長期シークレット（サービスプリンシパルの
パスワード）はリポジトリに置かない。一度だけ次を実行する。

```bash
APP_ID=$(az ad app create --display-name gh-newsvideo-cd --query appId -o tsv)
az ad sp create --id "$APP_ID"
az ad app federated-credential create --id "$APP_ID" --parameters '{
  "name": "github-main",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:nomhiro/news-video-generator:ref:refs/heads/main",
  "audiences": ["api://AzureADTokenExchange"]
}'
```

subject はブランチに紐づくので、**`main` 以外から `workflow_dispatch` すると
login が失敗する**。

Azure 側のロールは2つだけ。レジストリが GHCR なので ACR への権限は要らない。
**API キーは1つも渡さない**（`az containerapp update --image` は bicep パラメータを
評価しないので、キーは既存の Container App の secrets のまま）。

| ロール | スコープ | 理由 |
|---|---|---|
| `Reader` | リソースグループ | アプリとリビジョンの状態を引くため。キーは読めない |
| `Contributor` | Container App | `Container Apps Contributor` は `Microsoft.App/containerApps/*/write`（サブリソース）しか持たず、アプリ本体への `write` を持たないので使えない |

**Git Bash から `az role assignment create` を打つときは `MSYS_NO_PATHCONV=1` を付ける。**
`--scope` の `/subscriptions/...` が Windows パス（`C:/Program Files/Git/subscriptions/...`）に
変換され、`MissingSubscription: The request did not have a subscription or a valid
tenant level resource provider` という原因の分からないエラーになる（一度踏んだ）。
`gh api` も同じ理由で先頭の `/` を付けずに `gh api user/packages/...` と書く。

戻すときに壊しやすい点・運用上の注意。

- **CD 後にローカルの azd env が古くなる。** `SERVICE_WEB_IMAGE_NAME` は CD では
  更新されない。未設定のまま（新しい clone や新しい azd env）`azd provision` を
  打つと、巻き戻り先は「古いイメージ」ではなく **quickstart イメージ**になる。
  provision の前に現行イメージを取り込む。

  ```bash
  azd env set SERVICE_WEB_IMAGE_NAME "$(az containerapp show \
    -n ca-newsvideo-img-mimujd6zyifm6 -g rg-newsvideo-img \
    --query 'properties.template.containers[0].image' -o tsv)"
  ```

- **デプロイごとにジョブ表が消える。** `DATABASE_URL` はコンテナのローカル
  ディスク上の SQLite なので、リビジョン更新で実行待ちのジョブと履歴が失われる。
  マージが即デプロイになるぶん頻度が上がり、**生成中にマージするとその回の生成が
  失われる**（記事の選択状態は Azure Files なので残る）。
  イメージに入らないもの（`**.md` / `tests/` / `infra/` / `.claude/` / `.githooks/`）は
  `paths-ignore` で除外して、無駄な再起動を減らしている。
- **デプロイが失敗してもアプリは止まらない。** Single モードなので旧リビジョンが
  動き続ける。ただし `latestRevisionName` は壊れたリビジョンを指したまま残るので、
  非活性のリビジョンは適宜掃除する。
- **切り戻しはリビジョンではなくイメージタグで行う。** リビジョンは1件しか
  残らないため `az containerapp revision activate` に頼れない。GHCR にタグ
  （`gh-<短縮sha>`）が残るので、前のタグで `az containerapp update --image` を打つ。
- **ただし X 運用の導入（記事の `consumed` 化）より前のイメージには切り戻せない。**
  現行の `to_dict` は `data/news/*.json` に `consumed` を書く。導入前のコードの
  `from_dict` は `cls(**data)` なので、知らないキーを受け取って `TypeError` で落ちる。
  `_load_category` が捕まえるのは `JSONDecodeError` / `KeyError` / `OSError` だけなので、
  **記事一覧・動画の計画・投稿の計画がすべて落ちる**（実際に旧コードで再現して確認した）。
  互換のため `video_generated` も書き続けているが、それだけでは足りない。

  切り戻しを安全にしたいなら2段階で入れる。まず「知らないキーを無視する `from_dict`」を
  単体で `main` に入れて1リリース回し、そのあとで機能を入れる。いま急いで切り戻す
  必要が出た場合は、`data/news/*.json` から `consumed` キーを落とすか、
  ファイルを退避して取り直す（選択状態と生成済みの記録は失われる）。
- **最初のイメージをローカルから push してはいけない。** GHCR のパッケージと
  リポジトリの紐付けは**パッケージ作成時にしか行われない**。ローカルの docker から
  先に push すると `repository: null` の未紐付けパッケージができ、以降 Actions の
  `GITHUB_TOKEN` は `denied: permission_denied: write_package` で push できなくなる
  （`packages: write` は「そのリポジトリに紐付いたパッケージ」にしか効かない）。
  一度踏んだ。イメージに `org.opencontainers.image.source` ラベルを入れても
  **後から紐付き直さない**し、紐付けを直す REST API も無い（`PATCH` は 404、
  可視性の変更も UI 専用、削除は `delete:packages` スコープが必要）。
  復旧はパッケージを消して Actions に作らせ直すか、別の名前にして作り直すしかない。
  **パッケージは必ず Actions の初回実行で作らせる。**
- **GHCR の新規パッケージが private で作られたら、一度だけ手で public に切り替える**
  （Packages → Package settings → Change visibility）。private のままだと ACA が
  pull できず、リビジョンが Activating のまま残る。
- **テンプレートからリソースを消しても `azd provision` では削除されない。**
  ARM の incremental デプロイは「テンプレートに無いもの」を消さない。
  ACR を bicep から外したときも what-if は
  `Skip : Container Registry` と出るだけで、実体は残って課金も続いた。
  止めるには `az acr delete` を明示的に打つ必要がある（実際にそうした）。

### チェックは pre-push に寄せている

lint / 型 / テストは GitHub Actions ではなく `.githooks/pre-push` で走る。
ローカルで数十秒（実測: 全体で約58秒。Remotion 導入前は約30秒だった）で
終わるものを、push のたびに ubuntu ランナーで再実行しても遅くなるだけだった。
増えた約30秒は実際の Remotion レンダリング（2秒ぶんのフレーム）1回分で、
Node + Chrome + `mux_audio` の統合を検査する唯一の自動実行経路がこれである。

```bash
git config core.hooksPath .githooks   # clone した直後に一度だけ
```

- **`node` に加えて `remotion/node_modules` の存在も検査する。** node があっても
  `npm install` していなければ Remotion の slow テストは静かに skip される。
  ffmpeg / ffprobe と同じ理由（下記）で、hook の先頭で両方落とす。
- **`uv sync --frozen` ではなく `uv lock --check` を使う。** 見たいのは
  「lock が pyproject と一致しているか」だけで、sync は `.venv` を書き換えるため、
  Windows で開発サーバを上げたまま push すると
  `Access is denied (os error 5)` で push できなくなる。
- **`-m "not live"` を渡して slow（実 ffmpeg）を含める。** 実 ffmpeg のテストは
  ここが唯一の実行契機。ただし ffmpeg が PATH に無いと `pytest.skip` で静かに
  飛ぶので、hook の先頭で `command -v ffmpeg` を検査して落としている。
  **hook の PATH は push を起動したプロセスから継承される**ので、ターミナルでは
  通って GUI クライアントでは skip という差が出る。
- **これは門番ではない。** `--no-verify` で飛ばせるし、`core.hooksPath` の設定を
  忘れた clone や GitHub の Web エディタ経由では動かない。PR に対しても何も
  走らないので、**無検査のコードが `main` に入るとそのまま CD が走る**。
  `main` は PR 経由に強制してあるが（下記）、それは「レビューの機会を作る」
  だけで、自動検査を代替しない。

### `main` は PR 経由でしか変えられない

ruleset `protect main`（GitHub の Repository rules）で次を強制している。

| ルール | 効果 |
|---|---|
| `pull_request` | 直接 push を拒否する（`GH013: Changes must be made through a pull request.`） |
| `non_fast_forward` | force push を拒否する |
| `deletion` | ブランチの削除を拒否する |

`main` へのマージが即デプロイなので、直接 push を許すと「手元のコミットが
そのまま本番に出る」経路ができる。PR を挟めば、少なくとも差分を見る機会と
記録が残る。

- **承認必須数は 0 にしてある。** GitHub では自分の PR を自分で承認できないため、
  1 以上にすると1人しかいないこのリポジトリではマージ不能になって詰む。
- **bypass actors は空**にしてある。所有者でも直接 push できない。
  緊急時に外すなら Settings → Rules → `protect main` で
  enforcement を `Disabled` にするか、自分を bypass actor に足す。
- **必須ステータスチェックは設定していない。** PR で走るワークフローが1つも
  無いため（Actions は CD 専用）、指定できるものが無い。壊れたコードが
  `main` に入るのを止めたくなったら、PR 用の検査ワークフローを戻すのと
  セットで `required_status_checks` を足す。

### 長尺は当面作らない

定期実行の既定は `SCHEDULE_FORMATS=short`。長尺（`long`）のコードは動く
（実測: 1920x1080 / 6分4秒をクラウドで生成できている）が、**毎日作らない**。

理由は費用と、目指す映像の作り方が違うことの2点。

- 収益化の狙いは「長尺で再生時間を稼ぐ」だったが、参考にしたい映像は
  **VTuber 的なキャラクター + アニメーション付きスライド**という作りだった。
  静止画のスライドショーでは届かないし、生成動画モデルで作るものでもない。
  Azure でこれに当たるのは Text-to-Speech Avatar（アバター合成）と
  スライドのアニメーション化で、いまの構成の延長線上には無い。
- 生成動画モデルを使う案は費用が合わない。`sora-2` は既に eastus2 に
  デプロイ済み（capacity 50）で API も SDK にあるが、Retail Prices API の
  実値で **$0.10/秒**（pro は $0.30、pro high res は $0.50）。8分の全編生成は
  1本 $48（約¥7,400）、毎日なら月約¥22万。長尺の単価 0.3〜0.5円/再生に対して
  1本あたり15〜25万再生が回収ラインになる。
  加えて sora-2 は **IP と写実的なコンテンツをすべてブロック**するため、
  実在企業・製品が主題のニュースでは弾かれやすい。
  制約: 1本1〜20秒 / 同時2ジョブ / 2 job requests 分 / 音声も生成される。

長尺を再開するなら、まず「どう作るか」を決めるところから
（アバター合成か、スライドのアニメーションか）。`formats.py` の `LONG` は
10セグメント・約5〜6分のままで、8分化（16セグメント）も未実施。

### 動画の合成は2段構え（音声を同時に混ぜない）

`video_composer` は ffmpeg を**2回**呼ぶ。

1. 画像（concat デマクサー）+ 字幕 → **無音の映像**（`-an`）
2. その映像 + 音声 → 出力（`-c:v copy` で映像は再エンコードしない）

1回で済ませていた頃、長尺（1920x1080 / 341秒）が OOM killer に殺されていた
（`終了コード -9`）。ローカルで 2 vCPU / 4Gi の制限を与えて計測した数字:

| | ピーク RSS | 結果 |
|---|---|---|
| 1回で音声ごと | 4,077MB | 321秒後に -9 |
| 2段構え | **617MB** | 198秒で完走 |

エンコード速度は 1.04x 出ているのに**出力サイズが数百フレームぶん変化しない**
状態だった。マクサーが映像パケットを溜め込んでいる（stderr の
`buffers queued in out_#0:0` と一致）。第2段は再エンコードしないので
溜め込む対象が無い。

戻すときに壊しやすい点。

- 第1段に **`-t <音声の長さ>` が必要**。concat の最後の画像は尺を持たないため、
  指定しないと1フレームで終わる動画になる。
- 中間ファイル（`*_silent.mp4`）は成功時・失敗時ともに消す。残すと
  生成物が2倍になり、Blob にも余計なものが上がる。
- `tests/test_video_compose_slow.py`（`-m slow`）が実物を作って、
  音声トラックの有無・実尺・解像度・中間ファイルの後始末を検査する。
  コマンド文字列の検査では見つからない類の壊れ方なので、ここは実 ffmpeg で見る。

**`-threads` は割り当て CPU 数に絞る**（`_available_cpus()`）。既定の 0（自動）は
ホストのコア数を見るため、2 vCPU の環境で20スレッドが立つ。

### 生成物のキーに日本語が入る

キーは `videos/20260814_103904_Microsoft、画像生成AI「MAI-Image-2.6」を発表..._ja.mp4`
のように記事タイトルを含む。**Windows から `az storage blob list -o json` すると
壊れた JSON が返る**（cp932 で書き出されるため `Bad JSON escape sequence`）。
確認するときは az CLI ではなく `src/storage/artifacts.py` の
`BlobArtifactStore` を使う。

    uv run python -c "from src.storage.artifacts import BlobArtifactStore; ..."

キーをタイムスタンプ + ASCII のスラグにしてタイトルは Blob のメタデータに
持たせる方が運用は楽になる（未着手）。

### 実運用での構成（Container Apps）

公開エンドポイントは **Entra ID 認証（EasyAuth）で閉じている**。
無認証だと、URL を知っている者が `/generate` で課金を発生させ、
`/youtube/upload` でチャンネルに動画を公開できてしまう。

- アプリ登録は azd の管理外（Entra ID のオブジェクト）。`AUTH_CLIENT_ID` /
  `AUTH_CLIENT_SECRET` / `AUTH_TENANT_ID` を `azd env set` で渡す。
- **`enableIdTokenIssuance` を有効にする**。EasyAuth は
  `response_type=code id_token`（ハイブリッドフロー）で要求するので、
  無効のままだとサインインが `AADSTS700054` で失敗する。
  `az ad app create` の既定は無効（一度踏んだ）。
- 単一テナントのアプリ登録なので、`defaultAuthorizationPolicy.allowedPrincipals`
  で自分の objectId だけに絞る。指定しないと**テナント内の全員**が入れる。
- `tokenStore` は無効。有効にすると SAS URL を要求され、生成物用の
  ストレージアカウントは共有キーを無効にしてあるので SAS を作れない。
  ここでの認証は入口を閉じるためだけなので不要。

**状態の置き場所は3つに分かれている。**

| 何を | どこに | 理由 |
|---|---|---|
| 生成物（動画・画像・音声・台本） | Blob（`artifacts`） | 再起動で消えない。Entra ID 認証 |
| OAuth トークン | Blob（`tokens`） | 同上。別コンテナに分離 |
| 記事の選択状態（JSON） | Azure Files（`/app/data`） | リビジョン更新で選び直したくない |
| ジョブ表（SQLite） | コンテナのローカル（`/app/state`） | **SMB 上では動かない**（下記） |

- **SQLite を Azure Files に置くと起動しない。** `journal_mode` を DELETE に
  してもテーブル作成で固まり、リビジョンが Activating のまま終わらない。
  同じイメージで `DATABASE_URL` をローカルディスクに向けると25秒で起動する、
  という切り分けまでやった。ジョブまで永続化するなら PostgreSQL に移す。
  引き換えに、リビジョン更新で**実行待ちのジョブと履歴は消える**。
- Azure Files のマウントは SMB でアカウントキーを要求するため、
  **生成物とは別のストレージアカウント**にしている（生成物側は
  `allowSharedKeyAccess: false` を維持したい）。キーが漏れても
  記事の選択状態しか失わない。
- **`azd provision` はコンテナイメージをプレースホルダに戻しうる。**
  `containerImage` パラメータを `SERVICE_WEB_IMAGE_NAME` から受けるように
  してある。戻ると quickstart イメージ（8080 待ち受け）のリビジョンが
  作られ、プローブが通らず Activating のまま残る。
- `az containerapp update --set-env-vars` で入れた値は、次の
  `azd provision` で IaC の値に戻る（切り分け用の一時変更に使うのはよいが、
  恒久的な設定は Bicep に書く）。

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


### X の自動投稿は「二度出さない」ことを最優先に組んである

X API には**冪等キーが無い**。送信の結果が分からない状態で再送すると、同じ内容が
2回公開される。取り消しても記録は残り、フォロワーから見て最もみっともない失敗になる。
だから全体が「取りこぼす方を選ぶ」設計になっている。

- **`POSTING` の行は自動で再投稿しない。** 送信直前に `POSTING` にしてから API を呼ぶので、
  そこで落ちた行は「届いたか分からない行」。`recover_stuck_posting()` は起動時に1回だけ走り、
  それらを `NEEDS_REVIEW` に落とすだけで、キューには**戻さない**。
  `src/models/social.py` の遷移表に `POSTING → SCHEDULED` が無いのはこのため。
- **`HttpXClient.create_post` はリトライしない。** タイムアウト・429・5xx のいずれも
  `XSendUncertainError` にする。429 の応答が届く前に投稿が通っている可能性を排除できない。
  `upload_media` / `fetch_metrics` は公開しないのでリトライしてよい。
- **スレッドが途中で失敗したら残りも `NEEDS_REVIEW`。** 時間をおいて自動で続けると文脈が切れる。
- **予定時刻から `X_MAX_POST_DELAY_MINUTES`（既定60分）以上遅れた行は捨てる。**
  デプロイや停止のあと復帰した瞬間に4件が連投されるとスパムに見える。catch-up は作らない。

### 「もう投稿した」の権威は Azure Files 上の記事データ

`jobs` / `social_posts` の SQLite は**コンテナのローカルディスクにあり、リビジョン更新で
消える**。`main` へのマージが即デプロイなので、これは日常的に起きる。

「投稿済み」を SQLite だけに持つと、**デプロイ直後に同じ記事の投稿が作り直されて
二重投稿する**。だから権威は Azure Files 上の `data/news/*.json`（`NewsArticle.consumed`）に置く。
`consumed` はチャネル名 → 消費時刻の対応で、`video_generated` は
`consumed` を見る読み取り専用 property。書き込みは `mark_consumed` だけを通る。

**記事が消費済みになるのは投稿が成功した後だけ。** 計画時にマークすると、出せなかった記事を
二度と使えなくなる。

旧形式（`video_generated: true`）は `from_dict` が読み込み時に変換する。移行スクリプトを
書かないのは、クラウド上の JSON を書き換える手順を忘れたままデプロイすると記事を
全部読めなくなるから。

### 投稿スイッチもデータベースには置けない

実体は Azure Files 上の `data/x_posting.json`。SQLite に置くと、画面で有効にした翌日に
マージした時点で**黙って投稿が止まる**。`X_POSTING_ENABLED`（既定 false）は
「ファイルが無いときの初期値」でしかなく、一度画面で切り替えたら以降はファイルが権威。

**スイッチは送信段だけでなく `plan_daily_posts` も止める。** off の間に下書きを作ると、
`discard_stale` が60分後に捨て、記事は消費済みにならないので翌日また同じ記事で作り直す。
さらに `CARD` は `gpt-image-2` を呼び、そのクォータは**リージョン単位で上限4、動画生成と
共食いする**。誰も見ない出力のために動画生成を遅くすることになる。

### X のトークンは単回使用でローテートする

`offline.access` で得た refresh token は使うと無効になり、新しいものが返る。

- **更新したら保存先へ書き戻すまで何も失敗させない。** 更新成功後・保存前に落ちると、
  手元のトークンも保存先のトークンも両方死んで再認証しか道がなくなる。
- **失効を異常として扱わない。** 理由不明の失効が実際に報告されている。`XTokenExpiredError`
  を別型にしてあるのは、呼び出し側が投稿を `NEEDS_REVIEW` にして再認証を促すため。
  成功したが壊れた payload（`access_token` 欠落など）もこの型にする。素の `KeyError` を
  漏らすとその経路を素通りしてワーカーが落ちる。
- **レプリカを2つ以上にすると壊れる。** 同時 refresh で片方が無効化される。SQLite の
  ジョブ表と同じ制約の列に並ぶ既知の制約。

**認証はローカルで1回だけ。** `uv run python -m scripts.authorize_x` で PKCE を完了させ、
`uv run python -m scripts.push_tokens` で保存先へ送る。PKCE はブラウザのリダイレクトを
要求するのでコンテナ内では完了できない（YouTube の `InstalledAppFlow` と同じ理由）。
画面は認証状態と手順を出すだけで、フローは実行しない。

### 画像カードのプロンプトに動画用の装飾を付けない

`ImageGenerator.generate_batch` はカードからは **`enhance=False`** で呼ぶ。
`_enhance_prompt` は動画用の1行シーン記述を飾るためのもので、既に完成している
`CARD_STYLE_PROMPT` に重ねると同一プロンプト内で矛盾が生まれる。実際に踏んだ:
「文字を一切描くな」と「このラベルを描け」、1024x1024 を要求しながら「9:16 縦構図で」。
**矛盾したプロンプトは実 API でしか症状が出ない。**

**画像内の文字は日本語で描く。** 当初は「英大文字のみ」に限っていた
（`gpt-image-2` の CJK 描画が保証されていないという理解だった）。
**2026-08-16 に実画像で確かめ、その前提は誤りだと分かった。** 日本語ラベルと
日本語1行の説明を入れた画像を生成したところ、どちらも字形が正確で、
スマホでも読める大きさで描かれた。読み手は日本語話者なので、英語ラベルは
「読めるが分からない」状態を作るだけだった。サンプルは
`output/cards/`（git 管理外）に置いてある。

代わりに**長さ**を強制する。バリデータが見ているのは3つ。

- `labels` は各 `MAX_LABEL_CHARS`（8字）以内。名札の役割に留める
- `caption_ja` は画像の下に1行で描く。**図だけでは「何が言いたい絵か」が伝わらない**
- `key_details` は**ちょうど2個**、各 `MAX_DETAIL_CHARS`（120字）以内

`key_details` を2個に固定したのは、範囲を与えると上限まで使われて図がグループに
割れるため。実測で最も明快だったのは「2要素 + 名札 + 要点1行」で、対比する2つか
原因と結果の2つを選ぶと図として成立しやすい（`output/cards/card-sample-final.png`）。

長さの上限が効いている理由。上限を置かないと、モデルは1項目に
**パネル1枚ぶんの記述（250〜350字）**を書く。4項目あれば4コマになり、
スタイル文の `One idea only — no comic panels` は具体的な詳細指示に負けて、
スマホで読めない密度の図になる（実測）。一方まともな句は 40〜100字に収まるので、
**壊れた出力と正常な出力の実測値の間に閾値を置いている**。最初 90 にしたら
99字の正常な句を3回連続で弾き、カードを1枚も作れなかった。

`CardVisualGenerator.generate` は検証に失敗したら**理由を伝えて引き直す**
（`PostGenerator` と同じ）。引き直しが無いと、1回の逸脱でカードを作れず
`SINGLE` に降格する。同じプロンプトを送り直しても同じ応答が返る。

記事本文をそのまま画像モデルに渡さない。LLM が英語の視覚指示（`CardVisual`）を作り、
コード側が固定のスタイル文を前置する2段構え。`images.generate` に system prompt は無いので、
固定の指示はプロンプトへの前置しかない。

### 文字数は weighted length で数える

X は CJK を1文字2カウントで数え、上限は 280。**日本語は実質140字。** URL は t.co 短縮で
23カウント固定。素の `len()` で予算を組むと、出典表記を足した瞬間に投稿が弾かれる。

画面の文字数表示も `NNN/280`（weighted）で出す。日本語の文字数で見せると
「あと何文字か」は分かるが「弾かれるか」が分からない。

**組み立て後にもう一度検査する。** 本文が予算内でも、`出典: 〜` とハッシュタグを足すと
280 を超えうる（上限125字=250 + 出典16 + タグ11）。切り詰めずに例外にする。

`has_link` は `URL_PATTERN`（`src/models/social.py`）で判定する。`"http" in body` のような
別の判定を書いてはいけない。この値が **$0.015 と $0.20 で13倍違う課金区分**を決めるので、
`weighted_length` の URL 定義と食い違うと予算計算が実際の請求と合わなくなる。

### ruff は Markdown 内の Python を整形する

`pyproject.toml` の `extend-exclude` に `"*.md"` がある理由。ruff 0.16 は `.md` 内の
Python コードブロックを既定で整形し、このリポジトリの Markdown はクラスのメソッドを
インデントごと切り出した抜粋が多いため、`self` を取るメソッドがモジュール関数の形に
de-indent されてドキュメントが壊れる。加えて `ruff format --check .` が必ず失敗し、
`.githooks/pre-push` が通らず push できなくなる（両方実際に踏んだ）。

### X 運用で公開前に確認していないこと

以下は二次情報のまま実装してある。**自動投稿を有効にする前に一次情報で確認する。**
値が違えば設定の既定値と `BUDGETS` を直す。

- X API の実単価（投稿 / リンク付き投稿 / 読み取り）。2026-02-06 に従量課金へ移行しており、
  新規開発者に無料枠は無い
- weighted length の仕様（CJK = 2、上限 280）
- `media.write` スコープと `HttpXClient` の `UPLOAD_URL`（コード内にも未検証と明記してある）
- refresh token のローテーションを実トークンで1往復
- `PostGenerator._complete` の `responses.parse` が `gpt-5.1` デプロイで期待どおり動くこと
  （live テストが無い。動かなければ JSON モード + 事後検証に戻す判断になる）


## 規約

- コメントと docstring は日本語。「何をしているか」ではなく**なぜそうしたか**を書く。
  特に、知らないと善意で元に戻されてしまう判断（上記の解像度やスキーマの話）は必ず残す。
- 例外を包むときは `from e` を付けてチェーンを保つ（ruff B904 が検査する）。
- `zip()` は長さが一致するはずの場所では `strict=True` を付ける。
