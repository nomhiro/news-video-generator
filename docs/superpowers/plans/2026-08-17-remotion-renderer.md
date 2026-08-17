# Remotion レンダラ導入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 動画の見た目を「静止画スライドショー」から「コードで描く図解＋モーション」に替え、レンダラを環境変数で切り替えられる形で導入する。

**Architecture:** `VideoRenderer` プロトコルの実装を2つ持ち（現行 ffmpeg / 新規 Remotion）、`VIDEO_RENDERER` で選ぶ。Remotion は React で図解を描いて**無音の映像**までを作り、音声の多重化は既存の ffmpeg 第2段を共有する。LLM は図解の構造（`SceneVisual`）だけを出し、見出しと字幕は既存フィールドから導出する。

**Tech Stack:** Python 3.13 / pydantic / Remotion 4.0.512 / Node 22 / React 19 / ffmpeg / Docker

**Spec:** `docs/superpowers/specs/2026-08-17-remotion-renderer-design.md`

## Global Constraints

- コメントと docstring は**日本語**。「何をしているか」ではなく**なぜそうしたか**を書く
- 例外を包むときは `from e` を付ける（ruff B904）
- `zip()` は長さが一致するはずの場所で `strict=True`
- **全画面 `filter: blur()` を使わない。** 実測でレンダリングが 199秒 → 598秒（3倍）になる
- **Web フォントを使わない。** `font-family` でシステムの `fonts-noto-cjk` を参照する。`@font-face` は `delayRender` で待たない限り最初の数フレームがフォールバックフォントで焼かれる
- Remotion のタイムアウトは **900秒**（実測199秒の4.5倍）。`FFMPEG_TIMEOUT_SEC = 1800` は流用しない
- Remotion の `--concurrency` は **必ず明示指定**する。既定は「ホストの CPU スレッド数の半分」で cgroup を見ない
- `VIDEO_RENDERER` の既定は `ffmpeg`。マージしても見た目は変わらない
- 設定を足したら **`config.py` と `.env.example` の両方**を更新する（`tests/test_config.py` が双方向に突き合わせる）
- 本番ベースは `python:3.13-slim` = **Debian 13 (trixie)**。Node は `node:22-trixie-slim` から取る（`node:22-slim` は bookworm なので使わない）
- 実測済みの事実（この計画の前提。再確認は不要）:
  - 2 vCPU / 4Gi / concurrency 2 / blur なしで **199秒・ピーク1,915MB・OOM なし**
  - Linux コンテナ + `fonts-noto-cjk` + headless Chrome で**日本語は正確に描画される**
  - Remotion の apt 依存14個は trixie で**すべて解決する**
  - Remotion ライセンスは個人・3人以下なら商用でも無料

---

## File Structure

**新規（Python）**
- `src/models/scene.py` — `SceneLayout` / `SceneVisual`。LLM への出力契約
- `src/utils/grounding.py` — `src/social/grounding.py` から移動（純粋関数）
- `src/generators/video_renderer.py` — `VideoRenderer` プロトコル / `FfmpegRenderer` / `build_video_renderer`
- `src/generators/remotion_renderer.py` — `RemotionRenderer` / `resolve_frame_spans`

**新規（Remotion / TypeScript）**
- `remotion/package.json` / `remotion/tsconfig.json` / `remotion/remotion.config.ts`
- `remotion/src/index.ts` — `registerRoot`
- `remotion/src/Root.tsx` — `Composition`（`calculateMetadata` で解像度と尺を props から受ける）
- `remotion/src/Video.tsx` — シーンを並べる。props の型定義
- `remotion/src/theme.ts` — 色とフォントスタック。**単一の情報源**
- `remotion/src/Background.tsx` — 背景（blur を使わない）
- `remotion/src/Subtitle.tsx` — 画面下の字幕
- `remotion/src/scenes/Statement.tsx` / `Compare.tsx` / `Flow.tsx` — レイアウト1つに1ファイル

**変更**
- `src/models/script.py` — `scenes` フィールド、整合検査を4配列に、`statement` 上限
- `src/generators/script_generator.py` — `<<SCENES_SPEC>>` / `<<SCENES_EXAMPLE>>`、数値の根拠検査
- `src/generators/video_composer.py` — `mux_audio()` をモジュール関数に切り出す
- `src/social/post_generator.py` — grounding の import 元
- `src/pipeline.py` — レンダラを差し替え可能にし、画像生成を条件付きにする
- `config.py` / `.env.example` — `VIDEO_RENDERER`
- `Dockerfile` / `.dockerignore` / `.githooks/pre-push` / `package.json`（説明文）/ `CLAUDE.md`

**テスト**
- 新規 `tests/test_scene.py` / `tests/test_remotion_renderer.py` / `tests/test_remotion_render_slow.py` / `tests/test_remotion_design_rules.py`
- 変更 `tests/test_script_model.py` / `tests/test_grounding.py` / `tests/test_container_image.py`

---

### Task 1: `SceneVisual` モデル

**Files:**
- Create: `src/models/scene.py`
- Test: `tests/test_scene.py`

**Interfaces:**
- Consumes: なし（純粋な pydantic モデル）
- Produces:
  - `SceneLayout` (StrEnum): `STATEMENT="statement"` / `COMPARE="compare"` / `FLOW="flow"`
  - `SceneVisual(BaseModel)`: `layout: SceneLayout`, `items: list[str]`
  - `MAX_LABEL_CHARS: int = 8`
  - `ITEMS_PER_LAYOUT: dict[SceneLayout, int]`

- [ ] **Step 1: 失敗するテストを書く**

```python
# tests/test_scene.py
"""シーンの視覚指示の検証。

守っているのは2つ。レイアウトが要求する要素数を満たすこと（レンダラが
描けない形を作らせない）と、ラベルが名札の長さに収まること。
"""

import pytest
from pydantic import ValidationError

from src.models.scene import MAX_LABEL_CHARS, SceneLayout, SceneVisual


def test_statement_takes_no_items() -> None:
    scene = SceneVisual(layout=SceneLayout.STATEMENT, items=[])
    assert scene.items == []


def test_compare_takes_exactly_two_items() -> None:
    scene = SceneVisual(layout=SceneLayout.COMPARE, items=["従来", "新方式"])
    assert scene.items == ["従来", "新方式"]


def test_flow_takes_exactly_two_items() -> None:
    scene = SceneVisual(layout=SceneLayout.FLOW, items=["入力", "選択"])
    assert len(scene.items) == 2


def test_compare_rejects_three_items() -> None:
    """範囲を許すとモデルは上限まで使い、図がグループに割れる。"""
    with pytest.raises(ValidationError, match="ちょうど2個"):
        SceneVisual(layout=SceneLayout.COMPARE, items=["A", "B", "C"])


def test_statement_rejects_items() -> None:
    with pytest.raises(ValidationError, match="ちょうど0個"):
        SceneVisual(layout=SceneLayout.STATEMENT, items=["余計なラベル"])


def test_long_label_is_rejected() -> None:
    """名札に文を入れると、縦画面で図が文字に埋まる。"""
    too_long = "あ" * (MAX_LABEL_CHARS + 1)
    with pytest.raises(ValidationError, match="長すぎます"):
        SceneVisual(layout=SceneLayout.COMPARE, items=[too_long, "短い"])


def test_whitespace_only_label_is_rejected() -> None:
    """全角空白だけのラベルは長さ検査を通ってしまうので strip で見る。"""
    with pytest.raises(ValidationError, match="空です"):
        SceneVisual(layout=SceneLayout.COMPARE, items=["　　", "短い"])


def test_layout_accepts_plain_string() -> None:
    """LLM の JSON からは文字列で来るので、StrEnum に変換されること。"""
    scene = SceneVisual.model_validate({"layout": "compare", "items": ["前", "後"]})
    assert scene.layout is SceneLayout.COMPARE
```

- [ ] **Step 2: テストを実行して失敗を確認する**

Run: `uv run pytest tests/test_scene.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.models.scene'`

- [ ] **Step 3: 実装する**

```python
# src/models/scene.py
"""動画の1シーンに描くものの視覚指示。

なぜ画像生成モデルに描かせないか
--------------------------------
図解主体に振ったので、描く対象は「絵」ではなく「構造」である。LLM に構造を
出させて Remotion（React）が描けば、文字は常に正確で、回ごとのブレも無く、
`gpt-image-2` のクォータ（サブスクリプション・リージョン単位で上限4）も
消費しない。動画がクォータを使わなくなると、X の画像カードとの共食いも消える。

`src/social/card_visual.py` の `CardVisual` と役割は似ているが、意図的に
別モデルにしてある。あちらは**画像生成モデルへの英語の指示**で、こちらは
**レンダラが読む構造**。共有すると、片方の都合でもう片方が壊れる。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, model_validator

# ラベル1つの最大文字数。
#
# 名札の役割に留める。長い文を入れると縦1920pxの中で図が文字に埋まる。
# 値は `card_visual.MAX_LABEL_CHARS` と同じ8だが**借り物**である。カードは
# 1024x1024、動画は 1080x1920 で面積が違うので、実物を見て決め直す前提の
# 暫定値（設計書の「未確定」に挙げてある）。カードでは上限90字が正常な出力を
# 3回連続で弾いた前例があるので、動画でも実測で決める。
MAX_LABEL_CHARS = 8


class SceneLayout(StrEnum):
    """シーンの型。1つにつき React コンポーネントが1つ対応する。

    閉じた集合にしている理由: 自由記述を許すと、モデルは毎回違う構図を要求し、
    レンダラ側に描けないものが混ざる。`CardVisual.key_details` を
    ちょうど2個に固定したのと同じ判断。
    """

    STATEMENT = "statement"  # 図なし。見出しだけを大きく（フック・結論向け）
    COMPARE = "compare"  # 対比する2つを左右に置く
    FLOW = "flow"  # 原因 → 結果。矢印で繋ぐ


# 各レイアウトが要求する要素数。
#
# 範囲ではなく固定値にする。カードでの実測では、範囲を与えるとモデルは
# 上限まで使い、図が複数のグループに割れてスマホで読めなくなった。
ITEMS_PER_LAYOUT: dict[SceneLayout, int] = {
    SceneLayout.STATEMENT: 0,
    SceneLayout.COMPARE: 2,
    SceneLayout.FLOW: 2,
}


class SceneVisual(BaseModel):
    """1シーンに描くもの。LLM への出力契約そのもの。

    **見出しと字幕はここに持たない。** 見出しは `Script.text_overlays[i]`、
    字幕は `Script.segment_narrations[i]` から取る。同じ文字列を2箇所に
    持たせない理由は2つある。

    1. 880c95f の教訓 — キャプションが画像に描かれるなら本文で繰り返さない。
       同じ主張が2回出ても読み手の情報は増えない
    2. 検証フレームの実物 — 見出し・キャプション・字幕を3つ乗せたところ、
       キャプションと字幕が同じことを言っていた。縦画面に文字ブロック3つは多い

    Attributes:
        layout: シーンの型
        items: 図に入れる短いラベル。個数は `ITEMS_PER_LAYOUT` が決める
    """

    layout: SceneLayout
    items: list[str]

    @model_validator(mode="after")
    def _check_items(self) -> "SceneVisual":
        """レイアウトが要求する要素数と、各要素の長さを検証する。

        Returns:
            SceneVisual: 検証済みの自身

        Raises:
            ValueError: 要素数が合わない、空、または長すぎる場合
        """
        expected = ITEMS_PER_LAYOUT[self.layout]
        if len(self.items) != expected:
            raise ValueError(
                f"layout={self.layout.value} は items をちょうど{expected}個要求します"
                f"（{len(self.items)}個でした）"
            )
        for i, item in enumerate(self.items, 1):
            if not item.strip():
                raise ValueError(f"items の{i}番目が空です")
            if len(item) > MAX_LABEL_CHARS:
                raise ValueError(
                    f"items の{i}番目が長すぎます"
                    f"（{len(item)}字、最大{MAX_LABEL_CHARS}字）: {item!r}"
                )
        return self
```

- [ ] **Step 4: テストを実行して成功を確認する**

Run: `uv run pytest tests/test_scene.py -v`
Expected: PASS（8件）

- [ ] **Step 5: lint と型チェック**

Run: `uv run ruff check . && uv run ruff format . && uv run mypy`
Expected: エラーなし

- [ ] **Step 6: コミット**

```bash
git add src/models/scene.py tests/test_scene.py
git commit -m "Add a scene schema the renderer can actually draw"
```

---

### Task 2: `Script` に `scenes` を足し、整合検査を4配列にする

**Files:**
- Modify: `src/models/script.py`
- Modify: `tests/test_script_model.py`

**Interfaces:**
- Consumes: `SceneLayout` / `SceneVisual`（Task 1）
- Produces:
  - `ScriptDraft.scenes: list[SceneVisual]`（必須）
  - `Script.scenes: list[SceneVisual]`（必須）
  - `_validate_scenes(scenes: list[SceneVisual]) -> None`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_script_model.py` の末尾に追加する。

```python
def test_scenes_must_match_segment_count() -> None:
    """scenes も他の3配列と同じ数でなければならない。

    シーンの数が合わないと、レンダラが参照するインデックスが範囲外になる。
    """
    with pytest.raises(ValidationError, match="配列長の不一致"):
        _draft(scenes=[{"layout": "compare", "items": ["前", "後"]}])


def test_too_many_statement_scenes_is_rejected() -> None:
    """figure を持たない statement ばかりだと、静止画スライドショーに戻る。

    モデルは楽な選択肢に寄るので、指示ではなく検査で抑える。
    """
    with pytest.raises(ValidationError, match="statement が多すぎます"):
        _draft(
            scenes=[
                {"layout": "statement", "items": []},
                {"layout": "statement", "items": []},
                {"layout": "compare", "items": ["前", "後"]},
            ]
        )


def test_scenes_survive_to_script() -> None:
    """to_script が scenes をそのまま引き継ぐこと。"""
    script = _draft().to_script("ja")
    assert [s.layout.value for s in script.scenes] == ["compare", "flow", "statement"]
```

- [ ] **Step 2: `_draft` ヘルパーに `scenes` を足す**

`tests/test_script_model.py` の `_draft` の `payload` に追加する。既存テストは
すべてこのヘルパー経由なので、ここ1箇所で全部が通るようになる。

```python
        "scenes": [
            {"layout": "compare", "items": ["従来", "新方式"]},
            {"layout": "flow", "items": ["入力", "選択"]},
            {"layout": "statement", "items": []},
        ],
```

`_draft` は3セグメントなので `statement` の上限は `3 // 2 = 1` 個。上の並びは
`statement` が1個なので通る。

- [ ] **Step 3: テストを実行して失敗を確認する**

Run: `uv run pytest tests/test_script_model.py -v`
Expected: FAIL — `scenes` が `ScriptDraft` に存在せず `extra` として無視されるため
`test_scenes_must_match_segment_count` などが失敗する

- [ ] **Step 4: `src/models/script.py` を変更する**

import を追加する。

```python
from src.models.scene import SceneLayout, SceneVisual
```

`_HasAlignedSegments` プロトコルに `scenes` を足す。

```python
class _HasAlignedSegments(Protocol):
    """整合性検証が必要とする4フィールドだけを表す構造的な型。

    ScriptDraft と Script の両方がこれを満たす。
    """

    segment_narrations: list[str]
    image_prompts: list[str]
    text_overlays: list[str]
    scenes: list[SceneVisual]
```

`_validate_aligned_segments` の `counts` に `scenes` を足す。空要素の走査は
文字列の3配列だけを対象にしたままにする（`scenes` の中身は `SceneVisual` 側の
バリデータが見るため）。

```python
    counts = {
        "segment_narrations": len(segments),
        "image_prompts": len(model.image_prompts),
        "text_overlays": len(model.text_overlays),
        "scenes": len(model.scenes),
    }
```

`_validate_scenes` を追加する（`_validate_insights` の直後）。

```python
def _validate_scenes(scenes: list[SceneVisual]) -> None:
    """図を持たないシーンが多すぎないか検証する。

    `statement` は図を持たない。モデルが全部これを選べば図が1枚も出ず、
    **静止画スライドショーだった頃と同じ紙芝居に戻る**。これは実在する
    劣化経路で、モデルは常に楽な選択肢に寄る。`check_length_budget` と
    同じ判断で、指示ではなく検査で抑える。

    Args:
        scenes: 検証するシーン

    Raises:
        ValueError: statement が半数を超える場合
    """
    limit = len(scenes) // 2
    statements = sum(1 for scene in scenes if scene.layout is SceneLayout.STATEMENT)
    if statements > limit:
        raise ValueError(
            f"図を持たない statement が多すぎます: {statements}個"
            f"（{len(scenes)}シーン中 最大{limit}個）"
        )
```

`ScriptDraft` と `Script` の両方にフィールドを足す（`text_overlays` の直後）。

```python
    scenes: list[SceneVisual]
```

両方の `_check_content` に検証を足す。

```python
        _validate_aligned_segments(self)
        _validate_insights(self)
        _validate_scenes(self.scenes)
```

両方のクラス docstring の `Attributes` に1行足す。

```
        scenes: 各セグメントの図解の構造（レンダラが読む）
```

`ScriptDraft` の docstring の「`Script` との違い」に、`scenes` を残す理由を足す。

```
    `image_prompts` は Remotion レンダラでは使わないが**残してある**。
    `VIDEO_RENDERER=ffmpeg` への退路を生かすため、両レンダラが同じ台本から
    動く状態を保つ。
```

- [ ] **Step 5: テストを実行して成功を確認する**

Run: `uv run pytest tests/test_script_model.py -v`
Expected: PASS（既存 + 追加3件すべて）

- [ ] **Step 6: 全体のテストを走らせて壊れていないか確認する**

Run: `uv run pytest -m "not live and not slow"`
Expected: PASS

- [ ] **Step 7: コミット**

```bash
git add src/models/script.py tests/test_script_model.py
git commit -m "Require a scene structure alongside every narration segment"
```

---

### Task 3: `grounding` を `src/utils/` へ移す

**Files:**
- Move: `src/social/grounding.py` → `src/utils/grounding.py`
- Modify: `src/social/post_generator.py:46`
- Modify: `tests/test_grounding.py:6`

**Interfaces:**
- Consumes: なし
- Produces: `src.utils.grounding.ungrounded_numbers(text: str, source: str) -> set[str]`（シグネチャは不変）

`ScriptGenerator`（Task 4）がこの関数を使う。`src/generators/` から
`src/social/` を import すると横方向の依存になるため、先に中立な場所へ移す。

- [ ] **Step 1: git mv で移動する**

```bash
git mv src/social/grounding.py src/utils/grounding.py
```

- [ ] **Step 2: import 元を書き換える**

`src/social/post_generator.py:46`

```python
from src.utils.grounding import ungrounded_numbers
```

`tests/test_grounding.py:6`

```python
from src.utils.grounding import ungrounded_numbers
```

- [ ] **Step 3: 移動の理由を docstring に足す**

`src/utils/grounding.py` の module docstring の末尾に追加する。

```
`src/social/` ではなく `src/utils/` に置いている理由: 台本生成
（`src/generators/script_generator.py`）もシーンのラベルの数値検査に使う。
generators から social を import すると横方向の依存になる。
この関数は文字列しか触らないので、置き場所は中立でよい。
```

- [ ] **Step 4: テストを実行する**

Run: `uv run pytest tests/test_grounding.py tests/test_post_generator.py -v`
Expected: PASS

- [ ] **Step 5: 参照漏れが無いか確認する**

Run: `uv run pytest -m "not live and not slow" && uv run mypy`
Expected: PASS。`src.social.grounding` への参照が残っていれば ImportError で落ちる

- [ ] **Step 6: コミット**

```bash
git add -A src/social/grounding.py src/utils/grounding.py src/social/post_generator.py tests/test_grounding.py
git commit -m "Move the number grounding check somewhere both generators can reach"
```

---

### Task 4: `ScriptGenerator` にシーンの指示と数値検査を入れる

**Files:**
- Modify: `src/generators/script_generator.py`
- Test: `tests/test_script_generator.py`（既存ファイルに追加。無ければ作成）

**Interfaces:**
- Consumes: `SceneLayout` / `ITEMS_PER_LAYOUT` / `MAX_LABEL_CHARS`（Task 1）、`ungrounded_numbers`（Task 3）、`ScriptDraft.scenes`（Task 2）
- Produces:
  - `ScriptGenerator.SCENES_SPEC_TOKEN = "<<SCENES_SPEC>>"`
  - `ScriptGenerator.SCENES_EXAMPLE_TOKEN = "<<SCENES_EXAMPLE>>"`
  - `ScriptGenerator._scenes_spec(language: str, spec: FormatSpec) -> str`
  - `ScriptGenerator._scenes_example(spec: FormatSpec) -> str`
  - `ScriptGenerator._ungrounded_scene_numbers(draft: ScriptDraft, news_topic: str) -> set[str]`

- [ ] **Step 1: 失敗するテストを書く**

```python
# tests/test_script_generator.py に追加
from src.generators.script_generator import ScriptGenerator
from src.models.formats import get_spec


def test_prompt_has_no_unreplaced_tokens() -> None:
    """差し込みトークンが全部置換されていること。

    残ると `<<SCENES_SPEC>>` という文字列がそのままモデルに渡り、
    シーンの指示が一切効かないまま動く（気付きにくい）。
    """
    for video_format in ("short", "tiktok", "long"):
        for language in ("ja", "en"):
            prompt = ScriptGenerator._build_system_prompt(language, video_format)
            assert "<<" not in prompt, f"{language}/{video_format} に未置換のトークンがある"


def test_scenes_example_has_one_entry_per_segment() -> None:
    """例の要素数が形式のセグメント数と一致すること。

    プロンプトに個数を直接書くと仕様とずれる（formats.py 冒頭の教訓）。
    """
    spec = get_spec("long")
    example = ScriptGenerator._scenes_example(spec)
    assert example.count('"layout"') == spec.segment_count


def test_scenes_spec_states_the_statement_limit() -> None:
    """statement の上限が指示文に出ていること。"""
    spec = get_spec("short")
    text = ScriptGenerator._scenes_spec("ja", spec)
    assert str(spec.segment_count // 2) in text


def test_ungrounded_scene_numbers_flags_invented_figures(draft_factory) -> None:
    """記事に無い数値がラベルに入っていたら検出すること。

    カードでは記事に無い ¥980 が絵に描かれた（880c95f）。あちらは画像なので
    機械的に検査できなかったが、シーンのラベルはデータなので突き合わせられる。
    """
    draft = draft_factory(
        scenes=[
            {"layout": "compare", "items": ["50%", "従来"]},
            {"layout": "flow", "items": ["入力", "選択"]},
            {"layout": "statement", "items": []},
        ]
    )
    assert ScriptGenerator._ungrounded_scene_numbers(draft, "記事本文に数値は無い") == {"50"}


def test_grounded_scene_numbers_pass(draft_factory) -> None:
    draft = draft_factory(
        scenes=[
            {"layout": "compare", "items": ["50%", "従来"]},
            {"layout": "flow", "items": ["入力", "選択"]},
            {"layout": "statement", "items": []},
        ]
    )
    assert ScriptGenerator._ungrounded_scene_numbers(draft, "精度は50%向上した") == set()
```

`draft_factory` フィクスチャを `tests/conftest.py` に追加する
（`tests/test_script_model.py` の `_draft` と同じ内容。テスト間で共有する）。

```python
# tests/conftest.py に追加
from src.models.script import ScriptDraft


@pytest.fixture
def draft_factory():
    """検証を通る最小の下書きを作るファクトリ。

    tests/test_script_model.py の `_draft` と同じ内容。台本を必要とする
    テストが増えたので共有できる場所に出した。
    """

    def make(**overrides: object) -> ScriptDraft:
        payload: dict[str, object] = {
            "title": "テストタイトル",
            "description": "テスト説明",
            "hashtags": ["shorts", "test"],
            "hook": "冒頭のフック",
            "main_points": ["ポイント1", "ポイント2"],
            "conclusion": "締めの一言",
            "technical_insight": (
                "内部では既存モデルの推論結果をキャッシュして再利用する仕組みになっているため、"
                "2回目以降の応答が速い。"
            ),
            "practical_impact": (
                "現場では手作業だったレビュー工程を自動化でき、日次の運用コストが下がる。"
                "レビュー担当は判断だけに集中できる。"
            ),
            "image_prompts": ["Scene 1", "Scene 2", "Scene 3"],
            "text_overlays": ["overlay 1", "overlay 2", "overlay 3"],
            "estimated_duration": 35,
            "segment_narrations": ["文A。", "文B。", "文C。"],
            "scenes": [
                {"layout": "compare", "items": ["従来", "新方式"]},
                {"layout": "flow", "items": ["入力", "選択"]},
                {"layout": "statement", "items": []},
            ],
        }
        payload.update(overrides)
        return ScriptDraft.model_validate(payload)

    return make
```

- [ ] **Step 2: テストを実行して失敗を確認する**

Run: `uv run pytest tests/test_script_generator.py -v`
Expected: FAIL — `AttributeError: ... has no attribute '_scenes_example'`

- [ ] **Step 3: トークンと生成メソッドを実装する**

`ScriptGenerator` のクラス定数に追加する。

```python
    # プロンプト内でシーンの指示を差し込む位置。
    # レイアウトの種類と要素数は models/scene.py が単一の情報源なので、
    # プロンプト側には値を書かない（書くと定義とずれる）。
    SCENES_SPEC_TOKEN = "<<SCENES_SPEC>>"

    # 出力例の scenes 配列を差し込む位置。
    # 要素数は segment_count から作る（short/tiktok は6、long は10）。
    SCENES_EXAMPLE_TOKEN = "<<SCENES_EXAMPLE>>"
```

メソッドを追加する（`_structure_spec` の後）。

```python
    @staticmethod
    def _scenes_spec(language: str, spec: FormatSpec) -> str:
        """シーンの指示文を `models/scene.py` の定義から組み立てる。

        レイアウト名・要素数・statement の上限をプロンプトに直接書かない。
        書くとスキーマの定義とプロンプトがずれる（`formats.py` の冒頭に
        書いてある失敗そのもの）。

        Args:
            language: 言語コード
            spec: 形式の仕様

        Returns:
            str: プロンプトに差し込む指示
        """
        statement_limit = spec.segment_count // 2
        compare_items = ITEMS_PER_LAYOUT[SceneLayout.COMPARE]

        if language == "ja":
            return (
                f"各セグメントに対応する図解の構造を{spec.segment_count}個。"
                f"layout は次の3つから選ぶ。\n"
                f"  - compare: 対比する2つを並べる。items を{compare_items}個\n"
                f"  - flow: 原因 → 結果を矢印で繋ぐ。items を{compare_items}個\n"
                f"  - statement: 図なし。見出しだけを見せる。items は空配列\n"
                f"items は図に入れる**日本語の名札**で、各{MAX_LABEL_CHARS}文字以内。"
                f"説明文を入れてはならない（名札であって文ではない）。\n"
                f"**statement は最大{statement_limit}個まで。** 図が無いシーンばかりだと"
                f"静止画を並べただけの動画に戻る。フックと結論に使い、"
                f"本体は compare か flow にする。\n"
                f"**items に数値を書くときは、記事本文に出てくる数値だけを使うこと。**"
                f"価格・割合・日付・バージョン番号・件数を自分で作ってはならない"
                f"（検査で弾かれて再生成になる）。"
            )
        return (
            f"Provide {spec.segment_count} scene structures, one per segment. "
            f"Choose layout from exactly these three:\n"
            f"  - compare: two things side by side. Exactly {compare_items} items\n"
            f"  - flow: cause -> effect joined by an arrow. Exactly {compare_items} items\n"
            f"  - statement: no diagram, headline only. items must be an empty array\n"
            f"items are short Japanese name tags drawn inside the diagram, "
            f"each at most {MAX_LABEL_CHARS} characters. Never put a sentence there.\n"
            f"**At most {statement_limit} statement scenes.** Too many turns the video "
            f"back into a slideshow. Use them for the hook and the conclusion; "
            f"make the body compare or flow.\n"
            f"**Any number in items MUST appear in the source article.** Never invent "
            f"prices, percentages, dates, version numbers, or counts "
            f"(the check rejects them and forces a regeneration)."
        )

    @staticmethod
    def _scenes_example(spec: FormatSpec) -> str:
        """出力例の scenes 配列を組み立てる。

        要素数を `segment_count` から作る。プロンプトに固定で書くと、
        形式ごとに数が違う（short/tiktok は6、long は10）ため必ずずれる。

        Args:
            spec: 形式の仕様

        Returns:
            str: JSON 配列の文字列（`<output_format>` に差し込む）
        """
        n = spec.segment_count
        entries: list[str] = []
        for i in range(n):
            if i == 0 or i == n - 1:
                # フックと結論は図なしにするのが自然
                entries.append('        {"layout": "statement", "items": []}')
            elif i % 2 == 1:
                entries.append('        {"layout": "compare", "items": ["名札A", "名札B"]}')
            else:
                entries.append('        {"layout": "flow", "items": ["原因", "結果"]}')
        return "[\n" + ",\n".join(entries) + "\n    ]"
```

import を追加する。

```python
from src.models.scene import ITEMS_PER_LAYOUT, MAX_LABEL_CHARS, SceneLayout
from src.utils.grounding import ungrounded_numbers
```

`_build_system_prompt` の `return` に置換を2つ追加する。

```python
        return (
            template.replace(
                cls.NARRATION_SPEC_TOKEN,
                cls._narration_spec(language, spec),
            )
            .replace(
                cls.STRUCTURE_SPEC_TOKEN,
                cls._structure_spec(language, spec),
            )
            .replace(
                cls.SCENES_SPEC_TOKEN,
                cls._scenes_spec(language, spec),
            )
            .replace(
                cls.SCENES_EXAMPLE_TOKEN,
                cls._scenes_example(spec),
            )
        )
```

- [ ] **Step 4: 6つのプロンプトすべてにシーンを足す**

`SYSTEM_PROMPT_JA` / `SYSTEM_PROMPT_LONG_JA` / `SYSTEM_PROMPT_TIKTOK_JA` /
`SYSTEM_PROMPT_EN` / `SYSTEM_PROMPT_LONG_EN` / `SYSTEM_PROMPT_TIKTOK_EN` の
**6つすべて**に、次の4箇所の変更を入れる。1つでも漏らすとその形式だけ
`scenes` が空で返り、スキーマ検証で必ず失敗する。

日本語プロンプト（`<critical_constraints>`）— 「3つの配列」を「4つの配列」に、
一覧に `scenes` を足す。

```
【最重要】以下の4つの配列は必ず6個ずつ生成してください：
- image_prompts: 6個
- text_overlays: 6個
- segment_narrations: 6個
- scenes: 6個
```

`long` は「10個ずつ」「10個」にする。

日本語プロンプト（`<content_rules>` の末尾に1行追加）。

```
- scenes: <<SCENES_SPEC>>
```

日本語プロンプト（`<output_format>` の JSON、`"text_overlays"` の後に追加）。

```
    "scenes": <<SCENES_EXAMPLE>>,
```

日本語プロンプト（`<verification>` に1項目追加）。

```
5. scenes が正確に6個あること
```

英語プロンプトも同じ4箇所を英語で入れる。

```
CRITICAL: The following 4 arrays MUST have exactly 6 elements each:
- image_prompts: 6 elements
- text_overlays: 6 elements
- segment_narrations: 6 elements
- scenes: 6 elements
```

```
- scenes: <<SCENES_SPEC>>
```

```
    "scenes": <<SCENES_EXAMPLE>>,
```

```
5. scenes has exactly 6 elements
```

- [ ] **Step 5: 数値の根拠検査を実装する**

`ScriptGenerator` にメソッドを追加する。

```python
    @staticmethod
    def _ungrounded_scene_numbers(draft: ScriptDraft, news_topic: str) -> set[str]:
        """シーンのラベルに、記事に根拠が無い数値が無いか調べる。

        カードでは「画像側は機械的に検査できないのでスタイル文で閉じた」
        （880c95f。記事に無い ¥980 が絵の小物に描かれた）。Remotion では
        **描く文字がデータなので突き合わせられる**ので、検査で閉じる。

        スキーマ側（`SceneVisual`）では検査できない。`ScriptDraft` が
        `language` を持たないのと同じ理由で、記事本文を持たないため。

        Args:
            draft: 検証する下書き
            news_topic: モデルに渡した記事のテキスト（タイトル＋本文）

        Returns:
            set[str]: 根拠の無い数値。空なら合格
        """
        labels = " ".join(item for scene in draft.scenes for item in scene.items)
        if not labels.strip():
            return set()
        return ungrounded_numbers(labels, news_topic)
```

`generate()` の再試行ループに検査を追加する。**分量の検査とは扱いを分ける。**
分量は最終試行でも警告して採用するが、根拠の無い数値は採用してはいけない
（それを許すと検査の意味が無くなる）。

`draft.check_length_budget` のブロックの直後、`script = draft.to_script(...)` の
直前に挿入する。

```python
            # 数値の根拠の検査。**分量と違い、最終試行でも通さない。**
            # 記事に無い数値が画面に描かれるのは、ニュースを扱う以上
            # 最も害が大きい種類の誤りで、警告して採用する選択肢が無い。
            ungrounded = self._ungrounded_scene_numbers(draft, news_topic)
            if ungrounded:
                last_problem = (
                    f"シーンのラベルに記事にない数値があります: {sorted(ungrounded)}"
                )
                if remaining:
                    log_warning(
                        f"数値の根拠が無い（{attempt + 1}/{self.VALIDATION_RETRIES}）。"
                        f"再生成します: {last_problem}"
                    )
                    continue
                log_error(f"台本の検証に失敗: {last_problem}")
                raise ScriptGenerationError(f"生成された台本が不正です: {last_problem}")
```

- [ ] **Step 6: テストを実行して成功を確認する**

Run: `uv run pytest tests/test_script_generator.py tests/test_script_model.py -v`
Expected: PASS

- [ ] **Step 7: 全体のテストと lint**

Run: `uv run pytest -m "not live and not slow" && uv run ruff check . && uv run ruff format . && uv run mypy`
Expected: PASS

- [ ] **Step 8: コミット**

```bash
git add src/generators/script_generator.py tests/test_script_generator.py tests/conftest.py
git commit -m "Make the model design each scene, and refuse figures it invented"
```

---

### Task 5: 音声の多重化を共有できる関数に切り出す

**Files:**
- Modify: `src/generators/video_composer.py`
- Test: `tests/test_video_composer.py`（既存に追加）

**Interfaces:**
- Consumes: なし
- Produces: `src.generators.video_composer.mux_audio(silent_path: Path, audio_path: Path, output_path: Path, *, timeout_sec: int, audio_codec: str = "aac", audio_bitrate: str = "192k") -> None`

`RemotionRenderer`（Task 7）がこれを呼ぶ。**コピーではなく共有にする** — 2つに
分かれると片方だけ直される。

- [ ] **Step 1: 失敗するテストを書く**

```python
# tests/test_video_composer.py に追加
from src.generators.video_composer import VideoCompositionError, mux_audio


def test_mux_audio_builds_a_copy_command(monkeypatch, tmp_path) -> None:
    """映像は再エンコードしないこと。

    第2段が -c:v copy でなければ、1段で合成していた頃の
    「マクサーが映像パケットを溜め込んで OOM」が再発する。
    """
    recorded: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        recorded.append(cmd)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.setattr("src.generators.video_composer.subprocess.run", fake_run)
    mux_audio(
        tmp_path / "silent.mp4",
        tmp_path / "voice.mp3",
        tmp_path / "out.mp4",
        timeout_sec=900,
    )

    cmd = recorded[0]
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert "-shortest" in cmd


def test_mux_audio_reports_the_exit_code(monkeypatch, tmp_path) -> None:
    """終了コードを必ず残すこと。負の値はシグナルで殺されたことを意味する。"""
    import subprocess

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(-9, cmd, output="", stderr="killed")

    monkeypatch.setattr("src.generators.video_composer.subprocess.run", fake_run)
    with pytest.raises(VideoCompositionError, match="-9"):
        mux_audio(
            tmp_path / "silent.mp4",
            tmp_path / "voice.mp3",
            tmp_path / "out.mp4",
            timeout_sec=900,
        )
```

- [ ] **Step 2: テストを実行して失敗を確認する**

Run: `uv run pytest tests/test_video_composer.py -k mux_audio -v`
Expected: FAIL — `ImportError: cannot import name 'mux_audio'`

- [ ] **Step 3: モジュール関数として実装する**

`video_composer.py` の `_tail` の後（`class VideoComposer` の前）に追加する。

```python
def mux_audio(
    silent_path: Path,
    audio_path: Path,
    output_path: Path,
    *,
    timeout_sec: int,
    audio_codec: str = "aac",
    audio_bitrate: str = "192k",
) -> None:
    """無音の映像に音声を多重化する。**映像は再エンコードしない。**

    なぜ独立した関数か
    ------------------
    レンダラが2つ（ffmpeg / Remotion）あり、どちらも「無音の映像を作ってから
    音声を混ぜる」という同じ2段構えを取る。コピーすると片方だけ直される日が
    来るので共有する。

    なぜ2段に分けるか
    ------------------
    1回で音声ごと合成していた頃、長尺（1920x1080 / 341秒）が OOM killer に
    殺されていた（終了コード -9）。エンコード速度は 1.04x 出ているのに
    出力サイズが数百フレームぶん変化せず、子プロセスのピーク RSS が
    4,077MB に達していた。マクサーが映像パケットを溜め込んでいる。
    この段は `-c:v copy` なので溜め込む対象が無い（2段構えでピーク617MB）。

    中間ファイルの削除はここでは**行わない**。失敗時に何を残すかは
    呼び出し側の判断（テキストオーバーレイの一時ファイルなど、
    ここが知らないものも一緒に片付ける必要がある）。

    Args:
        silent_path: 音声を持たない映像
        audio_path: 混ぜる音声
        output_path: 出力先
        timeout_sec: ffmpeg を諦めるまでの秒数
        audio_codec: 音声コーデック
        audio_bitrate: 音声ビットレート

    Raises:
        VideoCompositionError: ffmpeg が失敗、またはタイムアウトした場合
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(silent_path),
        "-i",
        str(audio_path),
        "-c:v",
        "copy",
        "-c:a",
        audio_codec,
        "-b:a",
        audio_bitrate,
        "-shortest",
        str(output_path),
    ]
    try:
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=timeout_sec,
        )
    except subprocess.CalledProcessError as e:
        # 終了コードを必ず残す。負の値はシグナルで殺されたことを意味し
        # （-9 なら OOM killer の可能性が高い）、stderr の内容だけでは
        # 「エンコードの失敗」と区別できない。
        raise VideoCompositionError(
            f"音声の多重化に失敗しました (終了コード {e.returncode}):\n"
            f"stdout: {_tail(e.stdout)}\nstderr: {_tail(e.stderr)}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise VideoCompositionError(
            f"音声の多重化が {timeout_sec}秒でタイムアウトしました"
        ) from e
```

- [ ] **Step 4: `_run_ffmpeg` から使うように書き換える**

`mux_cmd` の定義と、それを実行する `subprocess.run(mux_cmd, ...)` の呼び出しを
削除し、第1段の直後で `mux_audio` を呼ぶ。第1段のエラー処理（`except` 節での
`text_files` と `silent_path` の削除）はそのまま残す。

```python
            log_step("音声を多重化中（映像は再エンコードしない）...", "🔉")
            mux_audio(
                silent_path,
                audio_path,
                output_path,
                timeout_sec=self.FFMPEG_TIMEOUT_SEC,
                audio_codec=self.AUDIO_CODEC,
                audio_bitrate=self.AUDIO_BITRATE,
            )
            silent_path.unlink(missing_ok=True)
```

`mux_audio` は `VideoCompositionError` を投げるので、既存の
`except subprocess.CalledProcessError` / `except subprocess.TimeoutExpired` では
捕まらない。中間ファイルの後始末が漏れないよう、`except VideoCompositionError` を
追加して掃除してから再送する。

```python
        except VideoCompositionError:
            # mux_audio が投げた場合。メッセージは既に整形されているので
            # 包み直さず、中間ファイルだけ片付けて上へ流す。
            for text_file in text_files:
                text_file.unlink(missing_ok=True)
            silent_path.unlink(missing_ok=True)
            raise
```

**この節は既存の2つの `except` より前に置く**（`VideoCompositionError` は
`subprocess` の例外と継承関係が無いので順序による衝突は無いが、読む順を
「自前の例外 → 外部プロセスの例外」に揃える）。

- [ ] **Step 5: テストを実行する**

Run: `uv run pytest tests/test_video_composer.py -v`
Expected: PASS

- [ ] **Step 6: 実 ffmpeg のテストで壊れていないことを確認する**

Run: `uv run pytest -m slow -v`
Expected: PASS（`tests/test_video_compose_slow.py` が音声トラックの有無・実尺・
解像度・中間ファイルの後始末を実物で検査する）

- [ ] **Step 7: コミット**

```bash
git add src/generators/video_composer.py tests/test_video_composer.py
git commit -m "Share the audio mux step between both renderers"
```

---

### Task 6: Remotion プロジェクトを作る

**Files:**
- Create: `remotion/package.json` / `remotion/tsconfig.json` / `remotion/remotion.config.ts`
- Create: `remotion/src/index.ts` / `Root.tsx` / `Video.tsx` / `theme.ts` / `Background.tsx` / `Subtitle.tsx`
- Create: `remotion/src/scenes/Statement.tsx` / `Compare.tsx` / `Flow.tsx`
- Create: `remotion/.gitignore`

**Interfaces:**
- Consumes: なし（props の形は Task 7 の Python 側と合わせる）
- Produces: コンポジション ID `NewsVideo`、エントリ `src/index.ts`、props の形は
  ```ts
  { width, height, fps, durationInFrames,
    scenes: [{ layout, items, headline, subtitle, fromFrame, durationInFrames }] }
  ```

- [ ] **Step 1: `remotion/package.json` を作る**

```json
{
  "name": "news-video-remotion",
  "private": true,
  "description": "動画のレンダラ。CSS 用の ../package.json とは別物で、こちらは実行時依存。",
  "scripts": {
    "studio": "remotion studio src/index.ts",
    "render": "remotion render src/index.ts NewsVideo out/preview.mp4"
  },
  "dependencies": {
    "@remotion/cli": "4.0.512",
    "remotion": "4.0.512",
    "react": "19.2.8",
    "react-dom": "19.2.8"
  }
}
```

バージョンを固定する（`^` を付けない）。Remotion は `remotion` と `@remotion/cli`
のバージョン一致を要求し、ずれると起動時に落ちる。

- [ ] **Step 2: `remotion/tsconfig.json` と `remotion/.gitignore` を作る**

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "skipLibCheck": true,
    "esModuleInterop": true,
    "noEmit": true
  },
  "include": ["src"]
}
```

`remotion/.gitignore`

```
node_modules/
out/
```

- [ ] **Step 3: `remotion/remotion.config.ts` を作る**

```ts
import { Config } from "@remotion/cli/config";

// Linux コンテナでの安定性・速度のために公式が推奨している設定。
Config.setChromiumMultiProcessOnLinux(true);

// concurrency はここで設定しない。**Python 側が --concurrency で渡す。**
// 既定は「ホストの CPU スレッド数の半分」で cgroup を見ないため、
// 2 vCPU の割り当てに対して10スレッドが立つ（os.cpu_count() と同じ罠）。
```

- [ ] **Step 4: `remotion/src/theme.ts` を作る**

```ts
/**
 * 色とフォントの単一の情報源。
 *
 * フォントは **Web フォントを使わない**。`fonts-noto-cjk`（本番イメージに
 * 既に入っている）を font-family で参照する。@font-face で非同期に読ませると、
 * delayRender / waitForFonts で待たない限り最初の数フレームだけ
 * フォールバックフォントで焼かれ、エラーにならないので気付きにくい。
 *
 * 代償: ローカル（Windows / Yu Gothic）と本番（Linux / Noto Sans CJK）で
 * 字形が変わる。最終確認は Docker 経由で行う。
 */
export const FONT_STACK =
  '"Noto Sans CJK JP", "Noto Sans JP", "Yu Gothic", "Meiryo", "Hiragino Sans", sans-serif';

export const COLORS = {
  bg: "#0b1020",
  text: "#ffffff",
  // 字幕は白文字。黄色＋黒ボックスをやめるのがこの作業の目的の1つ。
  subtle: "rgba(255,255,255,0.82)",
  accent: "#4cc9f0",
  accent2: "#f72585",
} as const;
```

- [ ] **Step 5: `remotion/src/Background.tsx` を作る**

```tsx
import { AbsoluteFill, useCurrentFrame, useVideoConfig } from "remotion";
import { COLORS } from "./theme";

/**
 * 背景。角度が動くグラデーションだけで作る。
 *
 * **filter: blur() を使わない。** 全画面 1080x1920 への blur(40px) を
 * 2枚重ねた版で実測したところ、2 vCPU でのレンダリングが
 * 199秒 → 598秒（3倍）になった。グローを出したいときも blur ではなく
 * グラデーションと不透明度で作る。
 */
export const Background: React.FC = () => {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  const t = durationInFrames > 0 ? frame / durationInFrames : 0;
  return (
    <AbsoluteFill
      style={{
        background: `linear-gradient(${120 + t * 60}deg, ${COLORS.bg} 0%, #16204a 55%, #241436 100%)`,
      }}
    />
  );
};
```

- [ ] **Step 6: `remotion/src/Subtitle.tsx` を作る**

```tsx
import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";
import { COLORS, FONT_STACK } from "./theme";

/**
 * 画面下の字幕。ナレーションのセグメントをそのまま出す。
 *
 * 黄色文字＋不透明な黒ボックス（drawtext 時代のスタイル）はやめ、
 * 下端のスクリム（グラデーション）に白文字を置く。ボックスの輪郭が
 * 出ないので、量産系まとめ動画の記号にならない。
 */
export const Subtitle: React.FC<{ text: string }> = ({ text }) => {
  const frame = useCurrentFrame();
  const opacity = interpolate(frame, [0, 6], [0, 1], {
    extrapolateRight: "clamp",
  });
  return (
    <AbsoluteFill style={{ justifyContent: "flex-end", opacity }}>
      <div
        style={{
          padding: "160px 72px 96px",
          background:
            "linear-gradient(to top, rgba(0,0,0,0.78) 0%, rgba(0,0,0,0.55) 55%, rgba(0,0,0,0) 100%)",
        }}
      >
        <span
          style={{
            fontFamily: FONT_STACK,
            fontSize: 54,
            fontWeight: 700,
            color: COLORS.text,
            lineHeight: 1.45,
            // 日本語を単語単位で折る。機械的に N 文字で切ると
            // 「推論コストが桁で下 / がる」のように不自然な位置で折れる。
            wordBreak: "auto-phrase",
          }}
        >
          {text}
        </span>
      </div>
    </AbsoluteFill>
  );
};
```

- [ ] **Step 7: レイアウトを3つ作る**

`remotion/src/scenes/Headline.tsx`（3つのレイアウトが共有する見出し）

```tsx
import { spring, useCurrentFrame, useVideoConfig } from "remotion";
import { COLORS, FONT_STACK } from "../theme";

/** 見出し。`Script.text_overlays[i]` が入る。 */
export const Headline: React.FC<{ text: string; size?: number }> = ({
  text,
  size = 92,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const enter = spring({ frame, fps, config: { damping: 200 } });
  return (
    <h1
      style={{
        fontFamily: FONT_STACK,
        fontSize: size,
        fontWeight: 900,
        color: COLORS.text,
        textAlign: "center",
        lineHeight: 1.28,
        margin: 0,
        wordBreak: "auto-phrase",
        transform: `translateY(${(1 - enter) * 40}px)`,
        opacity: enter,
      }}
    >
      {text}
    </h1>
  );
};
```

`remotion/src/scenes/Statement.tsx`

```tsx
import { AbsoluteFill } from "remotion";
import { Headline } from "./Headline";

/** 図なし。見出しだけを大きく見せる。フックと結論に使う。 */
export const Statement: React.FC<{ headline: string; items: string[] }> = ({
  headline,
}) => (
  <AbsoluteFill
    style={{ justifyContent: "center", alignItems: "center", padding: 72 }}
  >
    <Headline text={headline} size={112} />
  </AbsoluteFill>
);
```

`remotion/src/scenes/Compare.tsx`

```tsx
import { AbsoluteFill, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { COLORS, FONT_STACK } from "../theme";
import { Headline } from "./Headline";

/** 対比する2つを左右に並べる。要素が順に現れる。 */
export const Compare: React.FC<{ headline: string; items: string[] }> = ({
  headline,
  items,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  return (
    <AbsoluteFill
      style={{ justifyContent: "center", alignItems: "center", padding: 64 }}
    >
      <Headline text={headline} />
      <div style={{ display: "flex", gap: 40, marginTop: 88 }}>
        {items.map((item, i) => {
          // delay をずらすのが「順に出る」演出の要。
          const enter = spring({
            frame: frame - i * 8,
            fps,
            config: { damping: 200 },
          });
          return (
            <div
              key={item}
              style={{
                width: 380,
                height: 380,
                borderRadius: 32,
                backgroundColor: "rgba(255,255,255,0.06)",
                border: `4px solid ${i === 0 ? COLORS.accent : COLORS.accent2}`,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                transform: `translateY(${(1 - enter) * 80}px) scale(${0.9 + enter * 0.1})`,
                opacity: enter,
              }}
            >
              <span
                style={{
                  fontFamily: FONT_STACK,
                  fontSize: 84,
                  fontWeight: 800,
                  color: COLORS.text,
                }}
              >
                {item}
              </span>
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
```

`remotion/src/scenes/Flow.tsx`

```tsx
import { AbsoluteFill, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { COLORS, FONT_STACK } from "../theme";
import { Headline } from "./Headline";

/** 原因 → 結果を矢印で繋ぐ。上から下に流す（縦画面なので縦に並べる）。 */
export const Flow: React.FC<{ headline: string; items: string[] }> = ({
  headline,
  items,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const arrow = spring({ frame: frame - 10, fps, config: { damping: 200 } });
  return (
    <AbsoluteFill
      style={{ justifyContent: "center", alignItems: "center", padding: 64 }}
    >
      <Headline text={headline} />
      <div
        style={{
          marginTop: 80,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: 24,
        }}
      >
        <Box text={items[0]} color={COLORS.accent} frame={frame} fps={fps} delay={0} />
        <span
          style={{
            fontSize: 88,
            color: COLORS.subtle,
            opacity: arrow,
            transform: `translateY(${(1 - arrow) * -20}px)`,
          }}
        >
          ↓
        </span>
        <Box text={items[1]} color={COLORS.accent2} frame={frame} fps={fps} delay={18} />
      </div>
    </AbsoluteFill>
  );
};

const Box: React.FC<{
  text: string;
  color: string;
  frame: number;
  fps: number;
  delay: number;
}> = ({ text, color, frame, fps, delay }) => {
  const enter = spring({ frame: frame - delay, fps, config: { damping: 200 } });
  return (
    <div
      style={{
        width: 640,
        height: 220,
        borderRadius: 28,
        backgroundColor: "rgba(255,255,255,0.06)",
        border: `4px solid ${color}`,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        transform: `translateY(${(1 - enter) * 60}px)`,
        opacity: enter,
      }}
    >
      <span
        style={{
          fontFamily: FONT_STACK,
          fontSize: 76,
          fontWeight: 800,
          color: COLORS.text,
        }}
      >
        {text}
      </span>
    </div>
  );
};
```

- [ ] **Step 8: `remotion/src/Video.tsx` を作る**

```tsx
import { AbsoluteFill, Sequence } from "remotion";
import { Background } from "./Background";
import { Subtitle } from "./Subtitle";
import { Compare } from "./scenes/Compare";
import { Flow } from "./scenes/Flow";
import { Statement } from "./scenes/Statement";
import { COLORS } from "./theme";

export type SceneProps = {
  layout: "statement" | "compare" | "flow";
  items: string[];
  /** 見出し。Script.text_overlays[i] */
  headline: string;
  /** 字幕。Script.segment_narrations[i] */
  subtitle: string;
  /**
   * フレーム範囲は **Python 側で解決済み**のものを受ける。
   * 単調増加の強制と「タイミングの要素数はセグメント数+1」という契約は
   * 既に Python にあるため、同じ計算をここに持たせない。
   */
  fromFrame: number;
  durationInFrames: number;
};

export type VideoProps = {
  width: number;
  height: number;
  fps: number;
  durationInFrames: number;
  scenes: SceneProps[];
};

const LAYOUTS = {
  statement: Statement,
  compare: Compare,
  flow: Flow,
} as const;

export const NewsVideo: React.FC<VideoProps> = ({ scenes }) => (
  <AbsoluteFill style={{ backgroundColor: COLORS.bg }}>
    <Background />
    {scenes.map((scene, i) => {
      const Layout = LAYOUTS[scene.layout];
      return (
        <Sequence
          key={i}
          from={scene.fromFrame}
          durationInFrames={scene.durationInFrames}
        >
          <Layout headline={scene.headline} items={scene.items} />
          <Subtitle text={scene.subtitle} />
        </Sequence>
      );
    })}
  </AbsoluteFill>
);

/**
 * Studio を開いたときと、props を渡さずにレンダリングしたときの既定値。
 * 実運用では Python が --props でファイル経由の JSON を渡す。
 */
export const SAMPLE_PROPS: VideoProps = {
  width: 1080,
  height: 1920,
  fps: 30,
  durationInFrames: 90,
  scenes: [
    {
      layout: "statement",
      items: [],
      headline: "推論コストが桁で下がる",
      subtitle: "推論のコストが一桁下がる、という話です。",
      fromFrame: 0,
      durationInFrames: 30,
    },
    {
      layout: "compare",
      items: ["従来", "新方式"],
      headline: "何が変わったのか",
      subtitle: "変わったのは、動かす範囲を絞ったことでした。",
      fromFrame: 30,
      durationInFrames: 30,
    },
    {
      layout: "flow",
      items: ["入力", "選択"],
      headline: "仕組み",
      subtitle: "入力ごとに、使う専門家を切り替えています。",
      fromFrame: 60,
      durationInFrames: 30,
    },
  ],
};
```

- [ ] **Step 9: `remotion/src/Root.tsx` と `remotion/src/index.ts` を作る**

```tsx
// remotion/src/Root.tsx
import { Composition } from "remotion";
import { NewsVideo, SAMPLE_PROPS } from "./Video";

/**
 * コンポジションは1つだけ。
 *
 * 解像度・fps・尺は props から `calculateMetadata` で受ける。Composition に
 * 固定値を書くと形式（short / tiktok / long）ごとに定義が増え、
 * `src/models/formats.py` が単一の情報源であることが崩れる。
 */
export const RemotionRoot: React.FC = () => (
  <Composition
    id="NewsVideo"
    component={NewsVideo}
    // calculateMetadata が上書きするが、Composition は初期値を要求する
    durationInFrames={SAMPLE_PROPS.durationInFrames}
    fps={SAMPLE_PROPS.fps}
    width={SAMPLE_PROPS.width}
    height={SAMPLE_PROPS.height}
    defaultProps={SAMPLE_PROPS}
    calculateMetadata={({ props }) => ({
      durationInFrames: props.durationInFrames,
      fps: props.fps,
      width: props.width,
      height: props.height,
    })}
  />
);
```

```ts
// remotion/src/index.ts
import { registerRoot } from "remotion";
import { RemotionRoot } from "./Root";

registerRoot(RemotionRoot);
```

- [ ] **Step 10: 依存を入れて、実物が描けることを確認する**

```bash
cd remotion && npm install --no-audit --no-fund
npx remotion still src/index.ts NewsVideo out/check.png --frame=45
```

Expected: `out/check.png` ができる。**画像を開いて目で確認する** — 日本語が
正しく描かれ、見出し・図・字幕が重なっていないこと。

- [ ] **Step 11: コミット**

```bash
cd .. && git add remotion/
git commit -m "Draw the video with React instead of stitching still images"
```

`remotion/node_modules/` と `remotion/out/` は `remotion/.gitignore` で除外される。

---

### Task 7: `RemotionRenderer`（Python 側）

**Files:**
- Create: `src/generators/remotion_renderer.py`
- Test: `tests/test_remotion_renderer.py`

**Interfaces:**
- Consumes: `mux_audio`（Task 5）、`_available_cpus`（既存 `video_composer`）、`SceneVisual`（Task 1）
- Produces:
  - `resolve_frame_spans(segment_timings: list[float], audio_duration_sec: float, fps: int, count: int) -> list[tuple[int, int]]`
  - `RemotionRenderer` — `needs_images: bool = False`、`render(**kwargs) -> Path`
  - `RemotionRenderError(Exception)`

- [ ] **Step 1: フレーム換算の失敗するテストを書く**

```python
# tests/test_remotion_renderer.py
"""Remotion レンダラの Python 側。

実レンダリングは tests/test_remotion_render_slow.py が担当する。
ここではコマンドの組み立てとフレーム換算だけを見る（速い）。
"""

import json

import pytest

from src.generators.remotion_renderer import (
    RemotionRenderer,
    resolve_frame_spans,
)


def test_spans_cover_the_whole_audio_without_gaps() -> None:
    spans = resolve_frame_spans([0.0, 1.0, 2.0, 3.0], 3.0, 30, 3)
    assert spans == [(0, 30), (30, 30), (60, 30)]


def test_spans_fall_back_to_even_split_without_timings() -> None:
    """bookmark が取れなかった場合。均等割りにする。"""
    spans = resolve_frame_spans([], 3.0, 30, 3)
    assert [s[1] for s in spans] == [30, 30, 30]
    assert spans[0][0] == 0


def test_every_span_is_at_least_one_frame() -> None:
    """長さ0のシーンを作らせない。

    各開始秒を独立に丸めると、近接したタイミングで長さ0や負のシーンが
    できる。Remotion は例外を出さず、シーンが飛んだ動画を黙って作る
    （ffmpeg が無言で壊れた動画を作るのと同じ壊れ方）。
    """
    spans = resolve_frame_spans([0.0, 0.001, 0.002, 1.0], 1.0, 30, 3)
    assert all(duration >= 1 for _, duration in spans)


def test_spans_are_contiguous_and_monotonic() -> None:
    """隙間も重なりも作らない。"""
    spans = resolve_frame_spans([0.0, 0.4, 0.41, 2.0], 2.0, 30, 3)
    for i in range(len(spans) - 1):
        assert spans[i][0] + spans[i][1] == spans[i + 1][0]


def test_spans_survive_non_monotonic_timings() -> None:
    """タイミングが逆行していても増加を強制する。"""
    spans = resolve_frame_spans([0.0, 1.5, 0.5, 3.0], 3.0, 30, 3)
    starts = [start for start, _ in spans]
    assert starts == sorted(starts)
    assert len(set(starts)) == len(starts)


def test_spans_end_exactly_at_the_audio_end() -> None:
    spans = resolve_frame_spans([0.0, 1.0, 2.0, 3.0], 3.0, 30, 3)
    assert spans[-1][0] + spans[-1][1] == 90
```

- [ ] **Step 2: テストを実行して失敗を確認する**

Run: `uv run pytest tests/test_remotion_renderer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.generators.remotion_renderer'`

- [ ] **Step 3: `resolve_frame_spans` を実装する**

```python
# src/generators/remotion_renderer.py
"""Remotion（React）で動画を描くレンダラ。

なぜ ffmpeg の concat ではなくブラウザで描くか
--------------------------------------------
図解主体に振ったので、描く対象は「画像」ではなく「構造」になった。
React で描けば文字は常に正確で、回ごとのブレも無く、`gpt-image-2` の
クォータも消費しない。モーション（要素が順に現れる、次の図へ移る）も
ffmpeg のフィルタグラフより桁違いに書きやすく、**何よりデザインを詰める
反復が速い**（ブラウザで見ながら直せる）。

実測（2026-08-17、2 vCPU / 4Gi / concurrency 2）
------------------------------------------------
1080x1920 / 30fps / 35秒（1050フレーム）で **199秒・ピーク1,915MB**。
OOM は起きない。ただし全画面 `filter: blur()` を入れた版は 598秒（3倍）に
なった。**デザイン側の制約**として `remotion/src/` に書いてある。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from src.generators.video_composer import _available_cpus, _tail, mux_audio
from src.models.formats import get_spec
from src.models.scene import SceneVisual
from src.utils.logger import log_error, log_step, log_success


class RemotionRenderError(Exception):
    """Remotion のレンダリングに失敗した。"""


def resolve_frame_spans(
    segment_timings: list[float],
    audio_duration_sec: float,
    fps: int,
    count: int,
) -> list[tuple[int, int]]:
    """セグメントの開始秒を、シーンごとのフレーム範囲に解く。

    なぜ Python 側で解くか
    ----------------------
    単調増加の強制と「タイミングの要素数はセグメント数+1（末尾は音声全体の
    終了時刻）」という契約は、既に `voice_generator` / `video_composer` 側に
    ある。同じ計算を TypeScript に持たせると、必ず片方だけ直される日が来る。
    React には**解決済みのフレーム範囲だけ**を渡す。

    丸めの罠
    --------
    各開始秒を独立に丸めると、近接したタイミングで**長さ0や負のシーン**が
    できる。現行の `_calculate_durations` が `max(end - start, 0.1)` で
    守っているのと同じ場所で、ここでは境界を厳密に増加させることで
    最低1フレームを保証する。崩れると Remotion は例外を出さず、
    シーンが飛んだ動画を黙って作る。

    Args:
        segment_timings: 各セグメントの開始秒（末尾は音声全体の終了秒）。
            要素数が `count + 1` に足りなければ均等割りにフォールバックする
        audio_duration_sec: ffprobe で測った音声の長さ
        fps: フレームレート
        count: シーン数（1以上）

    Returns:
        list[tuple[int, int]]: (開始フレーム, 表示フレーム数)。要素数は count。
        隙間も重なりも無く、合計は全体のフレーム数に一致する

    Raises:
        ValueError: count が1未満の場合
    """
    if count < 1:
        raise ValueError(f"シーン数は1以上でなければなりません: {count}")

    # 全体のフレーム数。シーン数を下回らせない（各シーンに最低1フレーム要る）。
    total = max(count, round(audio_duration_sec * fps))

    if len(segment_timings) >= count + 1:
        raw = [t * fps for t in segment_timings[: count + 1]]
    else:
        # bookmark が取れなかった場合のフォールバック。均等割り。
        step = total / count
        raw = [i * step for i in range(count)] + [float(total)]

    # 境界を整数にし、**厳密に増加**させる。i 番目の境界は「後続の
    # シーンに最低1フレームずつ残せる位置」より後ろには置かない。
    bounds = [0] * (count + 1)
    bounds[count] = total
    for i in range(count):
        lower = bounds[i - 1] + 1 if i > 0 else 0
        upper = total - (count - i)
        bounds[i] = min(max(round(raw[i]), lower), upper)

    return [(bounds[i], bounds[i + 1] - bounds[i]) for i in range(count)]
```

- [ ] **Step 4: テストを実行して成功を確認する**

Run: `uv run pytest tests/test_remotion_renderer.py -v`
Expected: PASS（6件）

- [ ] **Step 5: レンダラのテストを書く**

```python
# tests/test_remotion_renderer.py に追加
from src.models.scene import SceneLayout, SceneVisual


def _scenes() -> list[SceneVisual]:
    return [
        SceneVisual(layout=SceneLayout.STATEMENT, items=[]),
        SceneVisual(layout=SceneLayout.COMPARE, items=["従来", "新方式"]),
        SceneVisual(layout=SceneLayout.FLOW, items=["入力", "選択"]),
    ]


@pytest.fixture
def captured(monkeypatch, tmp_path):
    """Remotion と ffmpeg の呼び出しを捕まえ、props を読めるようにする。"""
    calls: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        # --props=<path> の中身を読んでおく（呼び出し後に消えるため）
        for arg in cmd:
            if isinstance(arg, str) and arg.startswith("--props="):
                calls["props"] = json.loads(Path(arg.split("=", 1)[1]).read_text("utf-8"))
        # Remotion が作るはずの無音ファイルを用意する
        Path(cmd[-1]).write_bytes(b"fake")

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    def fake_mux(silent, audio, output, **kwargs):
        calls["mux"] = (silent, audio, output)
        output.write_bytes(b"muxed")

    monkeypatch.setattr("src.generators.remotion_renderer.subprocess.run", fake_run)
    monkeypatch.setattr("src.generators.remotion_renderer.mux_audio", fake_mux)
    monkeypatch.setattr(
        "src.generators.remotion_renderer.RemotionRenderer._audio_duration",
        lambda self, path: 3.0,
    )
    return calls


def test_renderer_does_not_need_images() -> None:
    """画像生成を飛ばせること。クォータの律速がここで消える。"""
    assert RemotionRenderer().needs_images is False


def test_props_carry_resolved_frame_spans(captured, tmp_path) -> None:
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"audio")
    RemotionRenderer().render(
        audio_path=audio,
        output_path=tmp_path / "out.mp4",
        image_paths=[],
        scenes=_scenes(),
        text_overlays=["見出し1", "見出し2", "見出し3"],
        segment_narrations=["字幕1", "字幕2", "字幕3"],
        segment_timings=[0.0, 1.0, 2.0, 3.0],
        language="ja",
        video_format="short",
    )
    props = captured["props"]
    assert props["width"] == 1080
    assert props["height"] == 1920
    assert props["durationInFrames"] == 90
    assert [s["fromFrame"] for s in props["scenes"]] == [0, 30, 60]
    assert [s["headline"] for s in props["scenes"]] == ["見出し1", "見出し2", "見出し3"]
    assert [s["subtitle"] for s in props["scenes"]] == ["字幕1", "字幕2", "字幕3"]
    assert props["scenes"][1]["items"] == ["従来", "新方式"]


def test_concurrency_is_always_explicit(captured, tmp_path) -> None:
    """既定に任せない。ホストのコア数の半分が立ち、コンテナで OOM を招く。"""
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"audio")
    RemotionRenderer().render(
        audio_path=audio,
        output_path=tmp_path / "out.mp4",
        image_paths=[],
        scenes=_scenes(),
        text_overlays=["a", "b", "c"],
        segment_narrations=["a", "b", "c"],
        segment_timings=[0.0, 1.0, 2.0, 3.0],
        language="ja",
        video_format="short",
    )
    cmd = captured["cmd"]
    assert any(str(a).startswith("--concurrency=") for a in cmd)


def test_props_file_is_removed(captured, tmp_path) -> None:
    """中間ファイルを残さない。残すと生成物が増え、Blob にも上がる。"""
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"audio")
    RemotionRenderer().render(
        audio_path=audio,
        output_path=tmp_path / "out.mp4",
        image_paths=[],
        scenes=_scenes(),
        text_overlays=["a", "b", "c"],
        segment_narrations=["a", "b", "c"],
        segment_timings=[0.0, 1.0, 2.0, 3.0],
        language="ja",
        video_format="short",
    )
    assert list(tmp_path.glob("*_props.json")) == []
    assert list(tmp_path.glob("*_silent.mp4")) == []


def test_mismatched_lengths_are_rejected(tmp_path) -> None:
    """配列長の不一致はここでも弾く。

    スキーマが担保しているが、レンダラは Script を経由しない呼び出しも
    受けうる。zip(strict=True) で落ちるより、原因の分かる例外にする。
    """
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"audio")
    with pytest.raises(RemotionRenderError, match="配列長"):
        RemotionRenderer().render(
            audio_path=audio,
            output_path=tmp_path / "out.mp4",
            image_paths=[],
            scenes=_scenes(),
            text_overlays=["a"],
            segment_narrations=["a", "b", "c"],
            segment_timings=[],
            language="ja",
            video_format="short",
        )
```

`RemotionRenderError` を import に足す。

- [ ] **Step 6: テストを実行して失敗を確認する**

Run: `uv run pytest tests/test_remotion_renderer.py -v`
Expected: FAIL — `RemotionRenderer` が未定義

- [ ] **Step 7: `RemotionRenderer` を実装する**

```python
# src/generators/remotion_renderer.py の末尾に追加

# Remotion プロジェクトの場所。リポジトリ直下の remotion/。
# CSS 用の ../package.json とは別パッケージ（あちらは devDependencies だけで
# 「実行時に Node は不要」が前提。混ぜるとその前提が壊れる）。
REMOTION_DIR = Path(__file__).resolve().parents[2] / "remotion"

# Remotion のエントリとコンポジション ID。remotion/src/ 側と一致させる。
ENTRY_POINT = "src/index.ts"
COMPOSITION_ID = "NewsVideo"


class RemotionRenderer:
    """Remotion で動画を描くレンダラ。

    **無音の映像までしか作らない。** 音声の多重化は `mux_audio` に任せる。
    Remotion 内で `<Audio>` を使って1発で作る形は選べない — 1回で音声ごと
    合成していた頃、マクサーが映像パケットを溜め込んでピーク RSS 4,077MB で
    OOM killer に殺された実測がある。検証済みの2段構えを崩さない。
    """

    FRAME_RATE = 30

    # Remotion を諦めるまでの秒数。
    #
    # 実測199秒（2 vCPU / blur なし / 35秒の動画）の4.5倍。
    # `VideoComposer.FFMPEG_TIMEOUT_SEC`（1800秒）を流用しない — Remotion の
    # 実測に対して緩すぎ、本当にハングしたときの発覚が遅れる。
    #
    # ジョブのリース（既定15分 = 900秒）とほぼ同じ長さになるが問題にならない。
    # `JobWorker._start_heartbeat` が独立した daemon スレッドでリースを
    # 延ばし続けるため、ここでブロックしていてもリースは切れない
    # （src/jobs/worker.py:211）。**そこを同期処理に変えると前提が崩れる。**
    TIMEOUT_SEC = 900

    # 画像生成を必要としない。これが `Pipeline` が gpt-image-2 の呼び出しを
    # 丸ごと飛ばせる根拠で、クォータの律速が消える理由。
    needs_images = False

    def render(
        self,
        *,
        audio_path: Path,
        output_path: Path,
        image_paths: list[Path],
        scenes: list[SceneVisual],
        text_overlays: list[str],
        segment_narrations: list[str],
        segment_timings: list[float],
        language: str,
        video_format: str,
    ) -> Path:
        """図解の構造から動画を作る。

        Args:
            audio_path: ナレーション音声
            output_path: 出力する動画のパス
            image_paths: 使わない（`FfmpegRenderer` と契約を揃えるため受ける）
            scenes: 各セグメントの図解の構造
            text_overlays: 各シーンの見出し
            segment_narrations: 各シーンの字幕
            segment_timings: 各セグメントの開始秒（末尾は音声の終了秒）
            language: 使わない（フォントはシステムのものを font-family で選ぶ）
            video_format: 形式名。解像度は formats.py が持つ

        Returns:
            Path: 生成された動画のパス

        Raises:
            RemotionRenderError: 配列長の不一致、または Remotion の失敗
        """
        counts = {
            "scenes": len(scenes),
            "text_overlays": len(text_overlays),
            "segment_narrations": len(segment_narrations),
        }
        if len(set(counts.values())) != 1:
            detail = ", ".join(f"{k}={v}" for k, v in counts.items())
            raise RemotionRenderError(f"配列長の不一致: {detail}")
        if not scenes:
            raise RemotionRenderError("シーンが空です")
        if not audio_path.exists():
            raise RemotionRenderError(f"音声ファイルが見つかりません: {audio_path}")

        spec = get_spec(video_format)
        audio_duration = self._audio_duration(audio_path)
        spans = resolve_frame_spans(
            segment_timings, audio_duration, self.FRAME_RATE, len(scenes)
        )
        total_frames = spans[-1][0] + spans[-1][1]

        log_step(
            f"動画を描画中... ({spec.label} {spec.output_width}x{spec.output_height}, "
            f"{len(scenes)}シーン, {total_frames}フレーム)",
            "🎬",
        )

        props = {
            "width": spec.output_width,
            "height": spec.output_height,
            "fps": self.FRAME_RATE,
            "durationInFrames": total_frames,
            "scenes": [
                {
                    "layout": scene.layout.value,
                    "items": scene.items,
                    "headline": headline,
                    "subtitle": subtitle,
                    "fromFrame": start,
                    "durationInFrames": duration,
                }
                # 長さは上で一致を確認済みなので strict=True で守る
                for scene, headline, subtitle, (start, duration) in zip(
                    scenes, text_overlays, segment_narrations, spans, strict=True
                )
            ],
        }

        output_path.parent.mkdir(parents=True, exist_ok=True)
        props_path = output_path.with_name(f"{output_path.stem}_props.json")
        silent_path = output_path.with_name(f"{output_path.stem}_silent.mp4")

        try:
            props_path.write_text(
                json.dumps(props, ensure_ascii=False), encoding="utf-8"
            )
            self._run_remotion(props_path, silent_path)
            log_step("音声を多重化中（映像は再エンコードしない）...", "🔉")
            mux_audio(
                silent_path,
                audio_path,
                output_path,
                timeout_sec=self.TIMEOUT_SEC,
            )
        finally:
            # 中間ファイルは成功時・失敗時ともに消す。残すと生成物が2倍になり、
            # Blob にも余計なものが上がる（*_silent.mp4 と同じ扱い）。
            props_path.unlink(missing_ok=True)
            silent_path.unlink(missing_ok=True)

        log_success(f"動画を描画しました ({audio_duration:.1f}秒)")
        return output_path

    def _run_remotion(self, props_path: Path, silent_path: Path) -> None:
        """Remotion の CLI を呼んで無音の映像を作る。

        props を**ファイル経由**で渡す理由: `--props` に JSON を直接書くと、
        6セグメントぶんの日本語データで Windows のコマンドライン長上限
        （8,191文字）に当たる。

        Args:
            props_path: props の JSON ファイル
            silent_path: 出力する無音の映像

        Raises:
            RemotionRenderError: Remotion が失敗、またはタイムアウトした場合
        """
        # Windows では npx は npx.cmd なので、shell=False では解決できない。
        npx = shutil.which("npx")
        if npx is None:
            raise RemotionRenderError(
                "npx が PATH にありません（Node 22 以上が必要です）"
            )

        concurrency = _available_cpus()
        cmd = [
            npx,
            "remotion",
            "render",
            ENTRY_POINT,
            COMPOSITION_ID,
            str(silent_path.resolve()),
            f"--props={props_path.resolve()}",
            # **必ず明示する。** 既定は「ホストの CPU スレッド数の半分」で
            # cgroup を見ないため、2 vCPU の割り当てに10スレッドが立つ
            # （os.cpu_count() で踏んだ罠と同じ構造）。
            f"--concurrency={concurrency}",
            "--log=info",
        ]

        log_step(f"Remotion の並行数: {concurrency}", "🧵")
        try:
            subprocess.run(
                cmd,
                cwd=REMOTION_DIR,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
                timeout=self.TIMEOUT_SEC,
            )
        except FileNotFoundError as e:
            raise RemotionRenderError(
                f"Remotion のプロジェクトが見つかりません: {REMOTION_DIR}"
            ) from e
        except subprocess.CalledProcessError as e:
            log_error(f"Remotion のレンダリングに失敗しました（終了コード {e.returncode}）")
            # 終了コードを必ず残す。負の値はシグナルで殺されたことを意味する
            # （-9 なら OOM killer の可能性が高い）。
            raise RemotionRenderError(
                f"Remotion のレンダリングに失敗しました (終了コード {e.returncode}):\n"
                f"stdout: {_tail(e.stdout)}\nstderr: {_tail(e.stderr)}"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise RemotionRenderError(
                f"Remotion のレンダリングが {self.TIMEOUT_SEC}秒でタイムアウトしました"
            ) from e

    @staticmethod
    def _audio_duration(audio_path: Path) -> float:
        """音声の長さを ffprobe で測る。

        `VideoComposer._get_media_duration` と同じことをするが、
        インスタンスを作らずに使えるよう別に持つ。テストからは
        差し替え点として使う。

        Args:
            audio_path: 音声ファイル

        Returns:
            float: 長さ（秒）

        Raises:
            RemotionRenderError: 測定に失敗した場合
        """
        from src.generators.video_composer import VideoComposer, VideoCompositionError

        try:
            return VideoComposer()._get_media_duration(audio_path)
        except VideoCompositionError as e:
            raise RemotionRenderError(f"音声の長さを測れませんでした: {e}") from e
```

`_audio_duration` は `@staticmethod` だがテストで
`RemotionRenderer._audio_duration` を `lambda self, path: 3.0` に差し替えるため、
**`staticmethod` ではなくインスタンスメソッドにする**（`self` を取る）。
上のコードの `@staticmethod` を外し、`def _audio_duration(self, audio_path: Path)`
にする。

- [ ] **Step 8: テストを実行して成功を確認する**

Run: `uv run pytest tests/test_remotion_renderer.py -v`
Expected: PASS（11件）

- [ ] **Step 9: lint と型チェック**

Run: `uv run ruff check . && uv run ruff format . && uv run mypy`
Expected: エラーなし

- [ ] **Step 10: コミット**

```bash
git add src/generators/remotion_renderer.py tests/test_remotion_renderer.py
git commit -m "Resolve scene timing in Python and hand Remotion only frames"
```

---

### Task 8: レンダラを差し替え可能にして配線する

**Files:**
- Create: `src/generators/video_renderer.py`
- Modify: `config.py` / `.env.example`
- Modify: `src/pipeline.py`
- Test: `tests/test_video_renderer.py` / `tests/test_pipeline.py`（既存に追加）

**Interfaces:**
- Consumes: `VideoComposer`（既存）、`RemotionRenderer`（Task 7）
- Produces:
  - `VideoRenderer(Protocol)` — `needs_images: bool` と `render(**kwargs) -> Path`
  - `FfmpegRenderer` — `needs_images = True`
  - `build_video_renderer(name: str) -> VideoRenderer`
  - `Config.video_renderer: Literal["ffmpeg", "remotion"]`（既定 `"ffmpeg"`）

- [ ] **Step 1: 失敗するテストを書く**

```python
# tests/test_video_renderer.py
"""レンダラの差し替え。

既定を ffmpeg にしてある理由: これは今日動いているパイプラインで、
クラウドで問題が出たときに環境変数1つで戻れる退路になる。
"""

import pytest

from src.generators.remotion_renderer import RemotionRenderer
from src.generators.video_renderer import (
    FfmpegRenderer,
    build_video_renderer,
)


def test_default_is_ffmpeg() -> None:
    """マージしても見た目が変わらないこと。"""
    assert isinstance(build_video_renderer("ffmpeg"), FfmpegRenderer)


def test_remotion_can_be_selected() -> None:
    assert isinstance(build_video_renderer("remotion"), RemotionRenderer)


def test_unknown_renderer_is_rejected() -> None:
    """未知の名前で黙って既定に落とさない。

    スケジューラの中で初めて分かると、気付くのが翌朝になる。
    """
    with pytest.raises(ValueError, match="未知のレンダラ"):
        build_video_renderer("blender")


def test_ffmpeg_renderer_needs_images() -> None:
    assert FfmpegRenderer().needs_images is True


def test_remotion_renderer_does_not_need_images() -> None:
    assert RemotionRenderer().needs_images is False
```

- [ ] **Step 2: テストを実行して失敗を確認する**

Run: `uv run pytest tests/test_video_renderer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.generators.video_renderer'`

- [ ] **Step 3: プロトコルと実装を書く**

```python
# src/generators/video_renderer.py
"""動画のレンダラを差し替え可能にする。

なぜ2つ持つか
-------------
Remotion（React で図解を描く）が本命だが、`ffmpeg`（静止画を並べる現行の
方式）は**今日動いているパイプライン**なので退路として残す。クラウドで
問題が出たら `VIDEO_RENDERER=ffmpeg` に戻すだけで復帰できる。

**自動フォールバックは作らない。** Remotion が失敗したら、そのままジョブを
失敗させて既存のリース・再試行（上限3回で FAILED）に任せる。黙って古い
レンダラに落ちると「毎朝の自動生成が古い見た目で回り続けて誰も気付かない」
状態になる（CD が無かった頃、マージしても反映されず旧コードで毎朝
走り続けていたのと同じ形の失敗）。切り替えは人が明示的に打つ。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from src.generators.remotion_renderer import RemotionRenderer
from src.generators.video_composer import VideoComposer
from src.models.scene import SceneVisual


class VideoRenderer(Protocol):
    """動画を作るものの契約。

    引数をすべてキーワード専用にしている。レンダラごとに使わない引数が
    あり（`FfmpegRenderer` は `scenes` を、`RemotionRenderer` は
    `image_paths` を使わない）、位置引数だと順序の取り違えが起きる。
    """

    @property
    def needs_images(self) -> bool:
        """画像生成が必要か。

        False なら `Pipeline` は `gpt-image-2` の呼び出しを丸ごと飛ばす。
        クォータ（リージョン単位で上限4）を消費しなくなる。
        """
        ...

    def render(
        self,
        *,
        audio_path: Path,
        output_path: Path,
        image_paths: list[Path],
        scenes: list[SceneVisual],
        text_overlays: list[str],
        segment_narrations: list[str],
        segment_timings: list[float],
        language: str,
        video_format: str,
    ) -> Path: ...


class FfmpegRenderer:
    """現行の `VideoComposer` を `VideoRenderer` の形で包む。

    振る舞いは一切変えない。これが退路として機能するには、
    「今と同じものが出る」ことが担保されている必要がある。
    """

    needs_images = True

    def __init__(self) -> None:
        self._composer = VideoComposer()

    def render(
        self,
        *,
        audio_path: Path,
        output_path: Path,
        image_paths: list[Path],
        scenes: list[SceneVisual],
        text_overlays: list[str],
        segment_narrations: list[str],
        segment_timings: list[float],
        language: str,
        video_format: str,
    ) -> Path:
        """静止画を並べて動画を合成する。

        `scenes` と `segment_narrations` は使わない（契約を揃えるために受ける）。

        Args:
            audio_path: ナレーション音声
            output_path: 出力する動画のパス
            image_paths: 並べる画像
            scenes: 使わない
            text_overlays: 各画像に載せるテキスト
            segment_narrations: 使わない
            segment_timings: 各セグメントの開始秒
            language: フォント選択用の言語コード
            video_format: 形式名

        Returns:
            Path: 生成された動画のパス
        """
        return self._composer.compose(
            audio_path,
            image_paths,
            output_path,
            text_overlays=text_overlays,
            language=language,
            segment_timings=segment_timings,
            video_format=video_format,
        )


def build_video_renderer(name: str) -> VideoRenderer:
    """名前からレンダラを組み立てる。

    未知の名前は**黙って既定に落とさない**。定期実行の中で初めて分かると、
    気付くのが翌朝になる（`SCHEDULE_FORMATS` の検証と同じ判断）。

    Args:
        name: "ffmpeg" または "remotion"

    Returns:
        VideoRenderer: レンダラ

    Raises:
        ValueError: 未知の名前の場合
    """
    if name == "ffmpeg":
        return FfmpegRenderer()
    if name == "remotion":
        return RemotionRenderer()
    raise ValueError(f"未知のレンダラです: {name!r}（'ffmpeg' または 'remotion'）")
```

- [ ] **Step 4: テストを実行して成功を確認する**

Run: `uv run pytest tests/test_video_renderer.py -v`
Expected: PASS（5件）

- [ ] **Step 5: 設定を足す**

`config.py` の `# --- 出力 ---` の直前に追加する。

```python
    # --- 動画のレンダラ ---
    #
    # ffmpeg: 静止画（gpt-image-2）を並べる現行の方式。
    # remotion: React で図解を描く方式。画像生成 API を使わない。
    #
    # **既定は ffmpeg。** これは今日動いているパイプラインで、クラウドで
    # 問題が出たときの退路になる。切り替えは人が明示的に行う
    # （自動フォールバックは作っていない。理由は
    # src/generators/video_renderer.py の docstring）。
    #
    # remotion には Node 22 と Chrome Headless Shell が必要。
    # ローカルでは `cd remotion && npm install` を一度実行する。
    video_renderer: Literal["ffmpeg", "remotion"] = Field(default="ffmpeg")
```

`.env.example` に追加する（`tests/test_config.py` が双方向に突き合わせるので
**必ず両方**を更新する）。

```
# 動画のレンダラ。ffmpeg（静止画を並べる）または remotion（React で図解を描く）。
# remotion には Node 22 と `cd remotion && npm install` が必要。
VIDEO_RENDERER=ffmpeg
```

- [ ] **Step 6: 設定のテストを実行する**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS（`.env.example` と `Config` の項目が一致すること）

- [ ] **Step 7: `Pipeline` を配線する**

`src/pipeline.py` の import を差し替える。

```python
from src.generators.video_renderer import VideoRenderer, build_video_renderer
```

`VideoComposer` の直接 import は不要になる（`FfmpegRenderer` が持つ）。

`__init__` の `self.video_composer = VideoComposer()` を差し替える。

```python
        # レンダラは設定で差し替える。既定は ffmpeg（今日動いている方式）。
        self.video_renderer: VideoRenderer = build_video_renderer(config.video_renderer)
```

`run()` の「2. Generate images」を条件付きにする。

```python
            # 2. 画像を生成する（レンダラが必要とする場合のみ）
            #
            # Remotion レンダラは図解を React で描くので画像を使わない。
            # 飛ばすと gpt-image-2 のクォータ（リージョン単位で上限4、
            # 1本6枚で1分以上）を一切消費しなくなり、X の画像カードとの
            # 共食いも消える。
            image_paths: list[Path] = []
            if self.video_renderer.needs_images:
                log_step("画像を生成中...", "🎨")
                first_lang = languages[0]
                image_dir = self.config.output_dir / "images" / base_name
                image_paths = self.image_generator.generate_batch(
                    scripts[first_lang].image_prompts,
                    image_dir,
                    language=first_lang,
                    video_format=video_format,
                )
            else:
                log_step("画像生成は不要です（レンダラが図解を描きます）", "🎨")
```

「4. Compose videos」をレンダラ経由にする。

```python
            for lang in languages:
                video_path = self.config.output_dir / "videos" / f"{base_name}_{lang}.mp4"
                self.video_renderer.render(
                    audio_path=audio_paths[lang],
                    output_path=video_path,
                    image_paths=image_paths,
                    scenes=scripts[lang].scenes,
                    text_overlays=scripts[lang].text_overlays,
                    segment_narrations=scripts[lang].segment_narrations,
                    segment_timings=segment_timings.get(lang, []),
                    language=lang,
                    video_format=video_format,
                )
                video_paths[lang] = video_path
```

- [ ] **Step 8: パイプラインのテストを足す**

```python
# tests/test_pipeline.py に追加
def test_pipeline_skips_image_generation_for_remotion(monkeypatch) -> None:
    """Remotion では gpt-image-2 を1回も呼ばないこと。

    クォータの律速が消えるのがこの作業の副産物なので、
    呼ばれていないことを検査で固定する。
    """
    # 既存のテストが使っているフェイクの組み立て方に合わせる。
    # image_generator.generate_batch が呼ばれたら失敗させる。
    ...
```

既存 `tests/test_pipeline.py` のフェイクの作り方に合わせて実装する。
`generate_batch` を「呼ばれたら `AssertionError` を投げる関数」に差し替え、
`VIDEO_RENDERER=remotion` の設定で `Pipeline.run` を回して例外が出ないことを見る。

- [ ] **Step 9: 全体のテストと型チェック**

Run: `uv run pytest -m "not live" && uv run ruff check . && uv run ruff format . && uv run mypy`
Expected: PASS

- [ ] **Step 10: コミット**

```bash
git add src/generators/video_renderer.py tests/test_video_renderer.py config.py .env.example src/pipeline.py tests/test_pipeline.py
git commit -m "Let one env var choose the renderer, and skip images when unused"
```

---

### Task 9: イメージに Node と Chrome を載せる

**Files:**
- Modify: `Dockerfile` / `.dockerignore`
- Modify: `tests/test_container_image.py`
- Modify: `.githooks/pre-push`
- Modify: `package.json`（説明文）

**Interfaces:**
- Consumes: `remotion/`（Task 6）
- Produces: `remotion` レンダラが動くコンテナイメージ

- [ ] **Step 1: 失敗するテストを書く**

```python
# tests/test_container_image.py に追加
def test_dockerfile_installs_node() -> None:
    """Node が無いと remotion レンダラは起動時ではなく**生成時**に落ちる。

    ローカルには常にあるため、コンテナに載せたときだけ露見する
    （migrations/ を入れ忘れて起動できなかったのと同じ種類の失敗）。
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    # 本番ベースは python:3.13-slim = Debian 13 (trixie)。
    # node:22-slim は bookworm なので glibc とライブラリ名が合わない。
    assert "node:22-trixie-slim" in text


def test_dockerfile_bakes_chrome_and_node_modules() -> None:
    """実行時に取得させない。ネットワークに依存し、初回生成が遅くなる。"""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "remotion browser ensure" in text
    assert "remotion/node_modules" in text


def test_dockerfile_copies_the_remotion_project() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "remotion/src" in text


def test_dockerfile_installs_chrome_native_deps() -> None:
    """Chrome Headless Shell のネイティブ依存。1つ欠けても起動しない。"""
    text = DOCKERFILE.read_text(encoding="utf-8")
    for package in ("libnss3", "libgbm-dev", "libatk-bridge2.0-0", "libcups2"):
        assert package in text, f"{package} が Dockerfile にない"
```

- [ ] **Step 2: テストを実行して失敗を確認する**

Run: `uv run pytest tests/test_container_image.py -v`
Expected: FAIL — Dockerfile に Node が無い

- [ ] **Step 3: `Dockerfile` を変更する**

ビルドステージを1つ足す（`FROM python:3.13-slim AS builder` の後）。

```dockerfile
# ---- Remotion ステージ: node_modules と Chrome を用意する ----
#
# **node:22-trixie-slim を使う。** 実行ステージの python:3.13-slim は
# Debian 13 (trixie) で、node:22-slim は bookworm(12)。混ぜると glibc と
# ライブラリ名（libasound2 → libasound2t64 など）が食い違う。
FROM node:22-trixie-slim AS remotion

WORKDIR /remotion

# 依存だけを先に入れる。src の変更でこのレイヤーを無効化しない。
COPY remotion/package.json remotion/tsconfig.json remotion/remotion.config.ts ./
RUN npm install --no-audit --no-fund

COPY remotion/src ./src

# Chrome Headless Shell を焼き込む（約92MB）。実行時に取得させると
# ネットワークに依存し、初回の動画生成が数十秒遅くなる。
RUN npx remotion browser ensure
```

実行ステージの apt に Chrome のネイティブ依存を足す。既存の
`RUN apt-get update && apt-get install ...` のパッケージ一覧に追加する
（**この14個は trixie で解決することを実測で確認済み**）。

```dockerfile
        libnss3 \
        libdbus-1-3 \
        libatk1.0-0 \
        libgbm-dev \
        libxrandr2 \
        libxkbcommon-dev \
        libxfixes3 \
        libxcomposite1 \
        libxdamage1 \
        libatk-bridge2.0-0 \
        libpango-1.0-0 \
        libcairo2 \
        libcups2 \
```

`libasound2` は Azure Speech SDK 用に既に入っているので重ねない。

Node の実体を持ってくる（`useradd` の前）。

```dockerfile
# Node の実体。node / npm / npx がすべて /usr/local の下にある。
# 実行ステージと同じ Debian リリース（trixie）のイメージから取るので、
# glibc の食い違いは起きない。
COPY --from=remotion /usr/local/bin/node /usr/local/bin/node
COPY --from=remotion /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
```

Remotion プロジェクトを持ってくる（アプリ本体の COPY 群の後）。

```dockerfile
# Remotion のレンダラ。node_modules と Chrome はビルドステージで
# 用意したものをそのまま持ってくる（実行時 npm install はしない）。
COPY --from=remotion --chown=app:app /remotion /app/remotion
```

- [ ] **Step 4: `.dockerignore` を調整する**

`remotion/node_modules` と `remotion/out` を除外する（ステージ側で入れるため、
ホストのものを持ち込まない。Windows でビルドしたものは Linux で動かない）。

```
remotion/node_modules
remotion/out
```

`remotion/` 自体は除外しない。

- [ ] **Step 5: テストを実行して成功を確認する**

Run: `uv run pytest tests/test_container_image.py -v`
Expected: PASS

- [ ] **Step 6: 実際にイメージをビルドして Node が動くことを確認する**

```bash
docker build -t newsvideo-remotion .
docker run --rm newsvideo-remotion sh -c "node --version && npx --version && ls /app/remotion/node_modules/.bin/remotion"
```

Expected: `v22.x` と npx のバージョンが出て、remotion の実体が見つかる。
**ここが通らなければ先に進まない** — Node の入れ方が間違っている。

- [ ] **Step 7: `.githooks/pre-push` に Node の検査を足す**

`ffprobe` の検査の後に追加する。

```sh
# Remotion の slow テストは Node が無いと pytest.skip で静かに飛ぶ。
# ffmpeg と同じ理由で、先にここで落とす。
command -v node >/dev/null 2>&1 || {
	echo "pre-push: node が PATH にありません（-m slow が静かに skip されます）" >&2
	exit 1
}
```

- [ ] **Step 8: CSS 用 `package.json` の説明文を直す**

「実行時に Node は不要」が事実でなくなったので実態に合わせる。

```json
  "description": "CSS のビルドにのみ使う。動画のレンダラは別パッケージ（remotion/）で、そちらは実行時に Node が必要。",
```

- [ ] **Step 9: コミット**

```bash
git add Dockerfile .dockerignore tests/test_container_image.py .githooks/pre-push package.json
git commit -m "Put Node and Chrome in the image so Remotion can run there"
```

---

### Task 10: 実レンダリングのテストとデザイン規約の検査

**Files:**
- Create: `tests/test_remotion_render_slow.py`
- Create: `tests/test_remotion_design_rules.py`

**Interfaces:**
- Consumes: `RemotionRenderer`（Task 7）、`remotion/`（Task 6）
- Produces: なし（テストのみ）

- [ ] **Step 1: デザイン規約の検査を書く**

```python
# tests/test_remotion_design_rules.py
"""Remotion のデザイン側の規約を検査する。

**これは既知の1つを名前で狙い撃つだけ**で、遅い描画一般を防ぐものではない
（box-shadow を10枚重ねれば同じことが起きる）。それでも置くのは、実測で
3倍の差が出ていて、tests/test_deploy_workflow.py や
tests/test_container_image.py と同じ「ファイルの中身を検査する」型に
収まるから。
"""

from pathlib import Path

REMOTION_SRC = Path(__file__).resolve().parents[1] / "remotion" / "src"


def test_no_blur_filter_anywhere() -> None:
    """全画面 blur は 199秒 → 598秒（3倍）にする。実測（2026-08-17）。

    グローを出したいときは blur ではなくグラデーションと不透明度で作る。
    """
    offenders = [
        path.relative_to(REMOTION_SRC).as_posix()
        for path in REMOTION_SRC.rglob("*.tsx")
        if "blur(" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"filter: blur() を使っているファイル: {offenders}"


def test_no_web_fonts() -> None:
    """@font-face / @remotion/google-fonts を使わないこと。

    非同期に読ませると、delayRender / waitForFonts で待たない限り最初の
    数フレームだけフォールバックフォントで焼かれる。エラーにならないので
    気付きにくい。システムの fonts-noto-cjk を font-family で参照する。
    """
    for path in REMOTION_SRC.rglob("*.ts*"):
        text = path.read_text(encoding="utf-8")
        assert "@font-face" not in text, f"{path.name} が @font-face を使っている"
        assert "google-fonts" not in text, f"{path.name} が google-fonts を使っている"
```

- [ ] **Step 2: 検査を実行する**

Run: `uv run pytest tests/test_remotion_design_rules.py -v`
Expected: PASS（Task 6 のコードは blur も Web フォントも使っていない）

- [ ] **Step 3: 実レンダリングのテストを書く**

```python
# tests/test_remotion_render_slow.py
"""Remotion を実際に動かして、経路全体が通ることを確認する。

**2秒（60フレーム）のコンポジションで測る。**
.githooks/pre-push は `-m "not live"` なので slow を含む。実運用と同じ
1050フレームを焼くと push が30秒から4分になり、--no-verify される道を
作ってしまう。2秒でも通る経路は同じ（Node が呼ばれる / Chrome が動く /
mp4 ができる / 音声が多重化される / 中間ファイルが消える）。
フル尺の実測は移行時の手動確認で行う。
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from src.generators.remotion_renderer import RemotionRenderer
from src.models.scene import SceneLayout, SceneVisual

pytestmark = pytest.mark.slow

REMOTION_DIR = Path(__file__).resolve().parents[1] / "remotion"


@pytest.fixture
def toolchain_available() -> None:
    """Node / ffmpeg / node_modules が揃っていること。

    揃っていなければ skip する。**.githooks/pre-push が node と ffmpeg の
    存在を先に検査している**ので、push 経路では skip されない。
    """
    if shutil.which("node") is None:
        pytest.skip("node が PATH にない")
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg / ffprobe が PATH にない")
    if not (REMOTION_DIR / "node_modules").is_dir():
        pytest.skip("remotion/node_modules が無い（cd remotion && npm install）")


@pytest.fixture
def two_second_audio(tmp_path: Path) -> Path:
    """2秒の無音の MP3 を作る。ffmpeg で生成するので外部素材が要らない。"""
    audio = tmp_path / "silence.mp3"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "anullsrc=r=24000:cl=mono",
            "-t", "2", str(audio),
        ],
        capture_output=True,
        check=True,
    )
    return audio


def _probe(path: Path, stream: str) -> str:
    """ffprobe で指定した種類のストリームの codec_type を返す（無ければ空）。"""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", stream,
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_render_produces_a_playable_video(
    toolchain_available: None, two_second_audio: Path, tmp_path: Path
) -> None:
    output = tmp_path / "out.mp4"
    RemotionRenderer().render(
        audio_path=two_second_audio,
        output_path=output,
        image_paths=[],
        scenes=[
            SceneVisual(layout=SceneLayout.STATEMENT, items=[]),
            SceneVisual(layout=SceneLayout.COMPARE, items=["従来", "新方式"]),
            SceneVisual(layout=SceneLayout.FLOW, items=["入力", "選択"]),
        ],
        text_overlays=["見出し1", "見出し2", "見出し3"],
        segment_narrations=["字幕1です。", "字幕2です。", "字幕3です。"],
        segment_timings=[0.0, 0.7, 1.4, 2.0],
        language="ja",
        video_format="short",
    )

    assert output.exists()
    # 音声トラックがあること。無ければ多重化が抜けている
    assert _probe(output, "a:0") == "audio"
    assert _probe(output, "v:0") == "video"
    # 中間ファイルを残さないこと
    assert list(tmp_path.glob("*_silent.mp4")) == []
    assert list(tmp_path.glob("*_props.json")) == []


def test_render_uses_the_format_resolution(
    toolchain_available: None, two_second_audio: Path, tmp_path: Path
) -> None:
    """解像度は formats.py が決める。short は 1080x1920。"""
    output = tmp_path / "out.mp4"
    RemotionRenderer().render(
        audio_path=two_second_audio,
        output_path=output,
        image_paths=[],
        scenes=[SceneVisual(layout=SceneLayout.COMPARE, items=["A", "B"])],
        text_overlays=["見出し"],
        segment_narrations=["字幕です。"],
        segment_timings=[0.0, 2.0],
        language="ja",
        video_format="short",
    )
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "1080,1920"
```

- [ ] **Step 4: 実レンダリングのテストを走らせる**

```bash
cd remotion && npm install --no-audit --no-fund && cd ..
uv run pytest tests/test_remotion_render_slow.py -v -s
```

Expected: PASS。**時間を記録する** — pre-push 全体が1分を大きく超えるなら、
コンポジションをさらに短くするか、シーン数を減らす。

- [ ] **Step 5: pre-push 全体を通す**

Run: `uv run pytest -m "not live"`
Expected: PASS

- [ ] **Step 6: コミット**

```bash
git add tests/test_remotion_render_slow.py tests/test_remotion_design_rules.py
git commit -m "Render two real seconds on every push, and forbid the slow filter"
```

---

### Task 11: `CLAUDE.md` に運用知識を書く

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: Task 1〜10 のすべて
- Produces: なし（ドキュメント）

CLAUDE.md は「知らないと善意で元に戻されてしまう判断」を残す場所なので、
実測値と罠を必ず書く。

- [ ] **Step 1: 「コマンド」節に Remotion の手順を足す**

```markdown
cd remotion && npm install                        # 動画レンダラの依存（初回のみ）
cd remotion && npm run studio                     # 見た目を作り込む（ブラウザで見ながら）
```

- [ ] **Step 2: 「外部依存」節に Node を足す**

```markdown
**Node 22 / Chrome Headless Shell** — `VIDEO_RENDERER=remotion` のときに必要。
`remotion/` が独立したパッケージで、CSS 用の `package.json` とは別物
（あちらは devDependencies だけで実行時に Node は不要）。
```

- [ ] **Step 3: 「触るときに知っておくべきこと」に新しい節を足す**

```markdown
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
  （`box-shadow` を重ねる等の別経路は防げない）。
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
```

- [ ] **Step 4: 「既知の設計上の負債」に未解決の課題を足す**

```markdown
- **見出しの改行が不自然に折れることがある。** 検証で「推論コストが桁で下 /
  がる」となった。`word-break: auto-phrase` を当てているが、実運用の見出しで
  十分かは未確認。現行の `_wrap_text` が14文字で機械的に切っているのと同じ
  課題が形を変えて残っている。
```

- [ ] **Step 5: 書いた内容が事実か確認する**

Run: `uv run pytest -m "not live"`
Expected: PASS。CLAUDE.md に書いた検査（design rules / container image /
frame spans）が実在すること

- [ ] **Step 6: コミット**

```bash
git add CLAUDE.md
git commit -m "Record why the renderer avoids blur, web fonts, and silent fallbacks"
```

---

## 移行（実装完了後、別の作業として行う）

設計書の「移行の段取り」に沿って人が判断する。**この計画の完了時点では
`VIDEO_RENDERER=ffmpeg` のままなので、見た目は変わらない。**

1. ローカルで `VIDEO_RENDERER=remotion` にして実物を見る。`cd remotion && npm run studio`
   で見ながら詰める。**ここで「`items` の8字上限・ちょうど2個」と「改行」を実測で
   決め直す**（どちらも未確定として設計書に挙げてある）
2. Docker で確認する（本番と同じ Linux / Noto Sans CJK。ローカルの Windows とは
   字形が変わるので省略できない）
3. クラウドで手動1本。実測時間が199秒付近に収まることを確認する
4. Container Apps の env で既定を `remotion` に変える。切り戻しは env を戻すだけ

---

## Self-Review

**1. Spec coverage**

| 設計書の項目 | 実装するタスク |
|---|---|
| `VideoRenderer` プロトコルと2実装 | Task 8 |
| `config.video_renderer` / `.env.example` | Task 8 |
| Remotion は無音まで / `mux_audio` を共有 | Task 5, 7 |
| props はファイル経由 | Task 7 |
| `concurrency` は Python が決める | Task 7 |
| `remotion/` を独立パッケージに | Task 6, 9 |
| `SceneVisual`（`layout` / `items`） | Task 1 |
| `scenes` を4配列の整合検査に | Task 2 |
| 見出し・キャプションを新設しない | Task 1（docstring）, 6（props）, 7 |
| `statement` 半数以下 | Task 2 |
| 数値の機械的検査 / `grounding` の移動 | Task 3, 4 |
| `stat` を作らない | Task 1（3種類のみ） |
| フレーム換算を Python 側で | Task 7 |
| ワード単位字幕はやらない | 該当タスクなし（作らないもの） |
| Web フォントを使わない | Task 6, 10 |
| 改行（`word-break: auto-phrase`） | Task 6 |
| blur 禁止 | Task 6, 10 |
| 自動フォールバックを作らない | Task 8 |
| タイムアウト900秒 / 後始末 | Task 7 |
| テスト5種類 | Task 1, 2, 7, 8, 9, 10 |
| `pre-push` に `node` 検査 | Task 9 |
| 移行の段取り | 計画末尾（実装外） |
| 画像生成を飛ばす | Task 8 |

**2. Placeholder scan**

Task 8 Step 8 の `test_pipeline_skips_image_generation_for_remotion` に `...` を
残している。既存 `tests/test_pipeline.py` のフェイクの組み立て方に合わせる必要が
あり、その形をこの計画の外から確定できないため。**実装者は既存ファイルを読んで
同じ形で書く**こと。他のステップに未確定の箇所は無い。

**3. Type consistency**

- `SceneVisual.layout` は `SceneLayout`。props に入れるときは `.value`
  （Task 7 の `scene.layout.value`、Task 6 の TS 側は文字列リテラル型）で一致
- `resolve_frame_spans` は `list[tuple[int, int]]` を返し、Task 7 が
  `(start, duration)` で展開する
- `mux_audio` はキーワード専用の `timeout_sec` を要求する。Task 5 の定義と
  Task 7 の呼び出しで一致
- `VideoRenderer.render` は全キーワード専用。`FfmpegRenderer` / `RemotionRenderer` /
  `Pipeline` の3箇所で引数名が一致
- `needs_images` は `FfmpegRenderer` で `True`、`RemotionRenderer` で `False`
- `RemotionRenderer._audio_duration` は**インスタンスメソッド**（Task 7 Step 7 の
  末尾に明記。テストが `lambda self, path` で差し替えるため）
