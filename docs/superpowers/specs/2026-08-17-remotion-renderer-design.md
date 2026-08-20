# Remotion レンダラの導入（動画の見た目を作り直す）

2026-08-17

## なぜやるか

いまのショート動画は「AIくさいショート」のテンプレそのものになっている。

- **静止画6枚**（`gpt-image-2`、`_enhance_prompt` が `cinematic lighting` を無条件で付与）
- **動きゼロ**（concat デマクサーで画像を尺ぶん並べるだけ。パン・ズーム・トランジション無し）
- **黄色文字＋半透明黒ボックスの `drawtext`**（`TEXT_COLOR = "yellow"` / `TEXT_BOX_COLOR = "black@0.7"`）

不満の所在を確認したところ「動きが無い」「絵柄そのもの」「字幕の見た目」の3つで、ナレーション音声は許容だった。よって **`voice_generator.py` は触らない。**

目指す方向は**図解・インフォグラフィック主体**。写実的な生成画像をやめる。
`card_visual.py` で X 用カードとして既に確立している路線を動画に持ち込む。

### 図解を選んだことの含み

図解なら**画像生成モデルに描かせる必要が無い**。LLM に構造データを出させてコード側で
レンダリングできる。副産物が3つある。

1. **文字が常に正確になる** — `gpt-image-2` の字形の運に賭けなくなる
2. **回ごとのブレが消える** — シード固定不可の問題が消える
3. **画像生成クォータの律速が動画から消える** — 「リージョン単位で上限4」「6枚で1本1分以上」
   「X のカードと共食い」がまとめて解消し、空いたクォータをカードに全振りできる

また、この選択で**生成動画モデル（Seedance / `sora-2`）は検討対象から外れる**。
図解に動く実写素材は不要で、`sora-2` が費用で否決された（$0.10/秒、8分で1本 $48）
判断を再訪する必要も無い。

## 検証で確かめたこと（2026-08-17 実測）

実装前に spike を回した。検証コードは `scratchpad/remotion-spike/` に置いたもので、
リポジトリには入れない。

Remotion 4.0.512 / Node 22 / `node:22-bookworm-slim` ベース。
1080x1920 / 30fps / 35秒（1050フレーム）の縦動画を実レンダリングした。

| 条件 | 時間 | ピーク RSS | 結果 |
|---|---|---|---|
| ローカル（20コア / concurrency 10） | 154秒 | — | 完走 |
| 2 vCPU / 4Gi / concurrency 2 / 全画面 `blur(40px)` あり | 598秒 | 1,519MB | 完走・OOM なし |
| **2 vCPU / 4Gi / concurrency 2 / blur なし** | **199秒** | **1,915MB** | 完走・OOM なし |

**Linux コンテナ内で日本語が正確に描画されることを実フレームで確認した**
（`fonts-noto-cjk` + headless Chrome + `font-family` 指定）。字形の破綻なし。

### ライセンス

**個人および3人以下は商用利用（YouTube の収益化を含む）も無料。** このプロジェクトは
個人運用なので費用はゼロ。

4人以上は Company License が必須で、自動化用途は Automators（$0.01/render・
最低 $100/月）。**受託や共同作業では相手方の人数も合算される**。
運用主体が変わったら再判定が必要。

出典: <https://www.remotion.dev/docs/license/faq> /
<https://www.remotion.dev/docs/license/pricing> /
<https://www.remotion.pro/license>

### 検証で見つかった罠

1. **`concurrency` の既定は「ホストの CPU スレッド数の半分」。**
   `os.cpu_count()` がコンテナでホストの20を返した罠と同じ構造で、
   Node の `os.cpus()` も cgroup を見ない。明示指定が必須。
2. **全画面 `filter: blur()` を使ってはいけない。** これ1つで 199秒 → 598秒（3倍）。
   実装の詳細ではなく**デザインの制約**。グロー表現はグラデーションと不透明度で作る。
3. **速くなるとメモリが増える。** blur を外して速くなった方がピーク RSS が上
   （1,519MB → 1,915MB）。フレーム生成が速いぶんエンコード待ちのバッファが溜まる。
   4GB には収まるが余裕は2倍しかない。逃げ道は `disallowParallelEncoding`
   （遅くなるがメモリ効率が上がる）。

## アーキテクチャ

### レンダラを差し替え可能にする（退路の確保）

`VideoRenderer` プロトコルを切り、実装を2つ持つ。

| 実装 | 中身 | 位置づけ |
|---|---|---|
| `FfmpegRenderer` | 現行の `VideoComposer` をそのまま包む | 既知の正常動作。**退路** |
| `RemotionRenderer` | 新規（`src/generators/remotion_renderer.py`） | 本命 |

`config.video_renderer`（`ffmpeg` / `remotion`。既定は `ffmpeg`。後述の「移行の段取り」で
人が切り替える）で `Pipeline` が選ぶ。クラウドで問題が出たら環境変数1つで
今日動いているパイプラインに戻せる。

設定を足すので **`config.py` と `.env.example` の両方を更新する**
（`tests/test_config.py` が双方向に突き合わせており、片方だけだとテストが落ちる）。
`str` なので `NoDecode` は不要（list 型の設定でしか要らない）。

この退路を機能させるため、台本スキーマは **`image_prompts` を残したまま** `scenes` を足す。
両レンダラが同じ台本から動く状態を保つ。代償は LLM が埋めるフィールドが増えることだけで、
画像クォータは `ffmpeg` を選んだときしか消費しない。

### Remotion は「無音の映像」までしか作らない

音声の多重化は既存の第2段（`-c:v copy` の ffmpeg mux）を流用する。
そのため `VideoComposer._run_ffmpeg` の第2段を**両レンダラから呼べる形に切り出す**
（`mux_audio(silent_path, audio_path, output_path)`）。コピーではなく共有にする。
2つに分かれると、片方だけ直される。

**Remotion 内で `<Audio>` を使って1発で作る形は選べない。** 「1回で音声ごと合成すると
マクサーが映像パケットを溜め込み、ピーク RSS 4,077MB で OOM（終了コード -9）」という
実測がある。検証済みの2段構えを崩さない。

Remotion 側は音声を知らない。`durationInFrames` は Python が `ffprobe` の実測値から
計算して渡す。

### props はファイル経由で渡す

`--props` にJSONを直接書くと、6セグメントぶんの日本語データで Windows の
コマンドライン長上限（8,191文字）に当たる。JSONファイルに書いてパスを渡す。

### `concurrency` は Python が決める

`video_composer._available_cpus()`（cgroup を読む既存関数）の値を `--concurrency` に渡す。

### Remotion は独立したパッケージにする

リポジトリ直下に `remotion/`（独自の `package.json`）を置き、既存の CSS 用
`package.json` とは混ぜない。あちらは `devDependencies` だけで
**「実行時に Node は不要」が前提**（`description` にそう書いてある）。Remotion は
実行時依存なので、同居させるとその前提が壊れ、イメージに Tailwind まで入る。
既存ファイルの説明文は実態に合わせて直す。

イメージは +約400MB（Node ＋ Chrome Headless Shell ＋ ネイティブ依存14個）。
`node_modules` と Chrome は**ビルド時に焼く**（`npx remotion browser ensure`）。
実行時 DL はネットワークに依存し、起動が遅くなる。

必要な apt パッケージ（Remotion 公式の一覧）:
`libnss3 libdbus-1-3 libatk1.0-0 libgbm-dev libasound2 libxrandr2 libxkbcommon-dev
libxfixes3 libxcomposite1 libxdamage1 libatk-bridge2.0-0 libpango-1.0-0 libcairo2 libcups2`

`fonts-noto-cjk` は既に本番イメージに入っている。

## データ契約

### `src/models/scene.py`（新設）

```python
class SceneLayout(StrEnum):
    STATEMENT = "statement"  # 図なし。見出しだけを大きく（フック・結論向け）
    COMPARE = "compare"      # 対比する2つ
    FLOW = "flow"            # 原因 → 結果（矢印で繋ぐ）

class SceneVisual(BaseModel):
    layout: SceneLayout
    items: list[str]   # compare/flow はちょうど2個、statement は0個。各8字以内
```

`ScriptDraft` に `scenes: list[SceneVisual]` を足し、**整合検査を4配列に拡張する**
（`segment_narrations` / `image_prompts` / `text_overlays` / `scenes`）。

### 見出しとキャプションを新設しない（既存フィールドから導出する）

画面に出る文字は3つ（見出し・図のラベル・字幕）だが、新設するのは `layout` と
`items` だけにする。

- **見出し = `text_overlays[i]`** — 既にある。定義そのものが「各画像に表示するテキスト」
- **字幕 = `segment_narrations[i]`** — 既にある

カードの `caption_ja` に相当するフィールドは**意図的に作らない。** 根拠は2つ。

1. 880c95f の教訓「キャプションが画像に描かれるなら本文で繰り返さない。同じ主張が
   2回出ても読み手の情報は増えない」
2. 検証フレームの実物 — 見出し・キャプション・字幕が3つ乗り、
   `同じ精度を1/10の計算量で出す` と `推論のコストが一桁下がる、という話です。` が
   同じことを言っていた。スマホの縦画面に文字ブロック3つは多すぎた

### `statement` は半数以下に制限する

モデルは楽な選択肢に寄る。`statement` が6個返れば図が1枚も出ず、**いまと同じ紙芝居に
戻る**。バリデータで全体の半数以下（6セグメントなら最大3個）に制限する。
「お願いする」のではなく「守らせる」（`formats.py` 冒頭の方針）。

### 数字は機械的に検査する

`items` に入る `1/10` や `50%` は、カードのときと違って**構造化データなので
突き合わせられる**。カードでは「画像側は機械的に検査できないのでスタイル文で閉じた」
（880c95f）が、Remotion では文字がデータになる。

`ScriptGenerator` が `ungrounded_numbers(items, 記事本文)` で検査し、根拠の無い数値が
あれば**理由を伝えて引き直す**（`CardVisualGenerator.generate` と同じ形）。

スキーマ側では検査できない。`ScriptDraft` は `language` を持たないのと同じ理由で
記事本文を持たない。

`grounding.py` は `src/social/` から **`src/utils/grounding.py` へ移す**
（generators → social の横方向の依存を作らないため）。純粋関数なので移動は安全。

### `stat` レイアウトは作らない

「数字1つを主役にする」レイアウトは効果的だが、直したばかりのバグ（記事に無い `¥980` が
絵に出た）を正面から誘発する。数値検査が実運用で効いていることを確認してから足す。

### 未確定: `items` の制約値

8字上限と「ちょうど2個」は**カードでの実測値の借り物**。動画は縦1920pxで面積が違う。
カードでは上限90字が正常な出力を3連続で弾いた前例があるので、
**実物を見て決め直す前提**にしておく。

## タイミング同期

`generate_with_timings` が返す秒（要素数 = セグメント数+1、単調増加を強制済み、
末尾は音声全体の終了時刻）を **Python 側でフレーム範囲に解いてから** props に入れる。
React には解決済みの `fromFrame` / `durationInFrames` だけを渡す。

単調増加の強制と「+1要素」の契約が既に Python 側にあるため。TypeScript に同じ計算を
持たせれば、必ず片方だけ直される日が来る。

**丸めの罠:** 各開始秒を独立に丸めると長さ0や負のシーンが作れてしまう。
現行が `max(end - start, 0.1)` で守っているのと同じ場所で、フレーム版では
**最低1フレームを強制する**。壊れると Remotion は例外を出さず、シーンが飛んだ動画を
黙って作る（ffmpeg が無言で壊れた動画を作るのと同じ壊れ方）。

### ワード単位の字幕は v1 ではやらない

Azure Speech には `WordBoundary` イベントがあるので技術的には可能だが、いまある
`bookmark` とは別系統になる。「Chirp 3 HD 時代に実装が3系統に分かれ、実際に効いていたのは
推定だった」という記録がある場所なので、同じことを繰り返さない。
セグメント単位で出し、見た目（黄色＋黒ボックスをやめる）で改善する。

## デザイン

### Web フォントを使わない

`fonts-noto-cjk` を `font-family` で参照する方式のままにする。検証で実物が正しく
描かれており、かつ **Remotion の既知の罠を回避できる**。`@font-face` で非同期に
読ませると、`delayRender` / `waitForFonts` で待たない限り**最初の数フレームだけ
フォールバックフォントで焼かれる**。エラーにならないので気付きにくい。

代償: ローカル（Windows / Yu Gothic）と本番（Linux / Noto Sans CJK）で字形が変わる。
**最終確認は Docker 経由で行う。**

### 改行（未解決の課題）

検証で見出しが「推論コストが桁で下 / がる」と不自然に折れた。フォントの問題ではなく
レイアウトの問題。`word-break: auto-phrase` を当てて**実フレームで確認する**。
現行の `_wrap_text` が14文字で機械的に切っているのと同じ課題が形を変えて残っている。

### 禁止事項

- 全画面 `filter: blur()`（上記の実測。3倍遅くなる）

## エラー処理

### 自動フォールバックは作らない

Remotion のレンダリングが失敗したとき `ffmpeg` レンダラへ自動的に落とす仕組みは
**作らない**。ジョブをそのまま失敗させ、既存のリース・再試行（上限3回で FAILED）に
任せる。

黙って落ちると「毎朝の自動生成が古い見た目で回り続けて誰も気付かない」状態になる。
「CD が無かった頃、マージしても反映されず旧コードで毎朝走り続けていた」のと同じ形の
失敗。切り替えは `VIDEO_RENDERER=ffmpeg` を人が明示的に打つ。

### タイムアウトと後始末

タイムアウトは **900秒**（実測199秒の4.5倍）。`FFMPEG_TIMEOUT_SEC = 1800` を流用しない
— Remotion の実測に対して緩すぎ、ハングの発覚が遅れる。

900秒はジョブのリース（既定15分）とほぼ同じ長さになるが、**問題にならないことを確認した。**
`JobWorker._start_heartbeat` が独立した daemon スレッドでリースを延ばし続けるため
（`src/jobs/worker.py:211`）、レンダリングでブロックしている間もリースは切れない。
同じジョブが別のワーカーに拾われて二重にレンダリングされることはない。
**ここを同期処理に変えるとその前提が崩れる。**

中間ファイル（無音 mp4 と props の JSON）は成功時・失敗時ともに消す
（現行の `*_silent.mp4` と同じ扱い）。

## テスト

**実レンダリングのテストは2秒（60フレーム）のコンポジションで行う。**

`.githooks/pre-push` は `-m "not live"` なので slow を含む。1050フレームを焼くテストを
置くと push が30秒→4分になり、`--no-verify` される道を作る。2秒でも経路は全部通る
（Node が呼ばれる / Chrome が動く / mp4 ができる / 音声が multiplex される /
中間ファイルが消える）。フルレンダリングの実測は手動の確認工程に置く。

| テスト | 何を見るか | 契機 |
|---|---|---|
| `test_remotion_renderer.py` | コマンド組み立て・フレーム換算・props JSON の中身 | 常時 |
| `test_scene.py` | `statement` 半数以下、`items` の個数と長さ | 常時 |
| `test_remotion_render_slow.py` | 2秒を実レンダリング。音声トラック・解像度・後始末 | `-m slow` |
| `test_container_image.py`（拡張） | Dockerfile が Node / Chrome / `remotion/` を入れているか | 常時 |
| blur 検査 | `remotion/src` に `filter: blur` が無いこと | 常時 |

フレーム換算の境界も見る: 尺0、1フレーム未満のセグメント、単調増加が崩れた timings。

**blur 検査は既知の1つを名前で狙い撃つだけ**で、遅い描画一般を防ぐものではない
（`box-shadow` を10枚重ねれば同じことが起きる）。それでも置くのは、3倍の差が実測で
出ていて、`test_deploy_workflow.py` / `test_container_image.py` と同じ
「ファイルの中身を検査する」既存の型に収まるから。

`.githooks/pre-push` の先頭に `command -v node` の検査を足す（`ffmpeg` と同じ理由 —
無いと `pytest.skip` で静かに飛ぶ）。

## 移行の段取り

`VIDEO_RENDERER` の既定を `ffmpeg` にして入れるので、**マージしても見た目は変わらない。**
切り替えを人の判断にするための段取り。

「見た目が変わらない」だけでは足りない。**新しい失敗経路も増やさない**必要がある。
シーンのラベルの数値の根拠の検査（`_ungrounded_scene_numbers`）は、最初の実装では
レンダラに関係なく例外にしていた。ラベルを描くのは Remotion だけなので、既定の
`ffmpeg` では**画面のどこにも出ない数値のために台本生成が失敗し、ジョブが3回の試行を
使い切って FAILED になる**状態だった（`Pipeline.run_from_article` が本文を
`content[:2000]` で切るため、切り捨てた先に出てくるバージョン番号は捏造に見える）。
現在は `VideoRenderer.draws_scene_text`（`ffmpeg` は False、`remotion` は True）を
`ScriptGenerator.generate(enforce_scene_grounding=...)` に渡し、描かないレンダラでは
**検査は走らせるが警告に留める**。警告を残すのは、切り替える前に捏造の頻度を知る
唯一の経路だからである。

1. **既定 `ffmpeg` で入れる。** `scenes` が台本に増えるだけで、生成される動画は今と同じ。
   この時点で両レンダラが同じ台本から動く状態になる
2. **ローカルで `VIDEO_RENDERER=remotion` にして実物を見る。** ここで
   「`items` の制約値」と「改行」を実測で決める（どちらも未確定として上に挙げたもの）
3. **Docker で確認する。** 本番と同じ Linux / Noto Sans CJK で字形と改行を見る。
   ローカル（Windows / Yu Gothic）とは変わるため、この工程は省略できない
4. **クラウドで手動1本。** 実測時間が 199秒付近に収まることを確認する
5. **Container Apps の env で既定を `remotion` に変える。** 切り戻しは env を戻すだけ

`scenes` を必須フィールドにするが、**保存済みの台本 JSON が読めなくなる経路は無い**
（`Script.from_json_file` はテストからしか呼ばれていない。台本は毎回生成される）。
`consumed` キーで踏んだ「古いイメージに切り戻せない」問題は、ここでは起きない
— 古いコードが新しい台本 JSON を読む場合も、pydantic は既定で余分なキーを無視する。

## この設計に含まれないもの

- **ナレーション音声の変更**（許容と判断された）
- **長尺（`long`）の再開** — 「どう作るか」が未決という既存の判断は変えない
- **ワード単位の字幕**（上記）
- **`stat` レイアウト**（上記）
- **生成動画モデル**（図解を選んだ時点で不要）
