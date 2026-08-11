# CLAUDE.md

ニューストピックから YouTube Shorts / TikTok / 長尺向けの動画を自動生成するツール。
CLI と Web UI の2つの入口がある。

## コマンド

```bash
uv sync                          # 依存をロックファイルから同期
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

**Azure OpenAI** — 台本生成と画像生成の両方が同一のエンドポイントと API キーを共有する。
デプロイは2つ必要（`AZURE_OPENAI_DEPLOYMENT` と `AZURE_OPENAI_IMAGE_DEPLOYMENT`）。

**Google Cloud Text-to-Speech** — 音声合成。サービスアカウント JSON か ADC。

記事本文の抽出は **trafilatura**。取得は httpx で行い（User-Agent とタイムアウトを
制御するため）、抽出結果が100文字未満なら記事ページでないと判断して破棄する
（一覧ページからナビゲーションの断片が返ることがある）。

環境変数の一覧は `.env.example` を参照。`config.py` が読む変数と `.env.example` の
記載は `tests/test_config.py` が双方向に突き合わせているので、
新しい設定を足すときは両方を更新する（片方だけだとテストが落ちる）。

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

`gpt-image-2` の既定クォータは 5 images/min 程度。ショート1本で6枚使うため、
既定のままでは1本の生成に1分以上かかる。並行数は `IMAGE_MAX_CONCURRENCY` で制御し、
429 は `tenacity` でバックオフ再試行する。

異なるプロンプトの複数枚を `n` パラメータで1リクエストに畳むことはできない
（`n` は同一プロンプトからの複数枚生成）。6枚は6リクエストが必要。

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

- **`src/web/dependencies.py` がモジュールレベルの可変グローバルを DI に使っている。**
  進捗状態 `GenerationState` もプロセスメモリのみで、再起動で消え、
  レプリカを増やすと共有されない。Phase 4 でジョブテーブルに移す。
- **`data/news/*.json` を書き換え可能なデータストアとして使っている。** 排他制御がない。

### `generate_videos_task` を `async def` にしてはいけない

`src/web/routes.py` の `generate_videos_task` は**同期関数でなければならない**。
Starlette の `BackgroundTask` は非同期関数をイベントループ上で直接 await するため、
同期ブロッキングの `pipeline.run()`（数分かかる）を `async def` から呼ぶと
**生成中に Web サーバー全体が応答しなくなる**（`/status` のポーリングも止まる）。

`tests/test_web_background.py` が2つの角度から見張っている。関数が同期であることの
検査と、uvicorn を実際に起動して生成中に `/status` が返ることの検査。
後者は TestClient では書けない（TestClient はバックグラウンドタスクを
リクエスト処理内で完了させてしまい、この状況を再現できない）。

## 規約

- コメントと docstring は日本語。「何をしているか」ではなく**なぜそうしたか**を書く。
  特に、知らないと善意で元に戻されてしまう判断（上記の解像度やスキーマの話）は必ず残す。
- 例外を包むときは `from e` を付けてチェーンを保つ（ruff B904 が検査する）。
- `zip()` は長さが一致するはずの場所では `strict=True` を付ける。
