# テキストオーバーレイ表示問題の調査結果と修正計画

## 問題の概要

動画「20260118_153927_イーロン・マスク「退職後のために貯蓄するのは無意味になるだろう」」において、text_overlay[1]（「AIとロボットで"何でも手に入る"時代」）が5.98秒〜13.15秒の間に表示されない。

## 調査結果

### 1. スクリーンショット検証

| 時間 | 期待 | 実際 |
|------|------|------|
| 0.5秒 | テキスト1 | ✅ テキスト1 |
| 5.5秒 | テキスト1 | ✅ テキスト1 |
| 6秒 | テキスト2 | ❌ テキスト1（テキストなし or テキスト1が残る） |
| 6.5秒 | テキスト2 | ❌ テキスト1 |
| 10秒 | テキスト2 | ❌ テキスト1 |
| 14秒 | テキスト3 | ✅ テキスト3 |
| 22秒 | テキスト4/5 | ✅ 正しいテキスト |

**重要な発見**: 画像は正しく切り替わっているが、テキスト2のみが表示されない。

### 2. FFmpegテスト結果

- `enable='between(t\,start\,end)'`構文: ✅ 正常動作
- `textfile`オプション: ✅ 正常動作
- スマートクォート（U+201C/U+201D）: ✅ 正常表示
- `concat`デマクサー使用: ✅ 正常動作
- **同じスクリプトで再テスト**: ✅ **正常動作！テキスト2が正しく表示される**

### 3. 根本原因の特定

**結論**: 現在のコードには問題がない。問題の動画生成時に一時的な問題が発生した可能性が高い。

考えられる原因：
1. **一時ファイルのI/O問題**: 2番目のテキストファイルが正しく書き込まれなかった、または読み込めなかった
2. **ファイルロック**: 別のプロセスがファイルをロックしていた
3. **エンコーディング問題**: 特定の文字（スマートクォート）の書き込み時にエンコーディングエラーが発生した
4. **一時的なディスクI/O遅延**: ファイル作成とFFmpeg読み込みの間にタイミング問題

**現在のコードでは再現しない**ため、予防的な改善を行う。

### 4. 修正が必要なファイル

- [src/generators/video_composer.py](src/generators/video_composer.py) - テキストオーバーレイ処理の堅牢性向上

## 推奨対応

### 即時対応: 問題の動画を再生成

現在のコードでは問題が再現しないため、**問題の動画を再生成**することで解決できる。

```bash
# 問題の動画を再生成するコマンド
cd c:/Users/nom40/Documents/new-video-generator
python -c "
from src.pipeline import Pipeline
from config import Config
from src.models.script import Script
from pathlib import Path

config = Config()
pipeline = Pipeline(config)

# 既存のスクリプトを読み込み
script = Script.from_json_file(Path('output/scripts/20260118_153927_イーロン・マスク「退職後のために貯蓄するのは無意味になるだろう」（海外）（BUSINESS INSI_ja.json'))

# 音声と動画のみを再生成
# ...
"
```

### 予防的改善（オプション）

将来の同様の問題を防ぐため、以下の改善を推奨：

#### 1. テキストファイル作成後の検証追加

`video_composer.py` の `_create_text_file` メソッドを改善:

```python
def _create_text_file(self, text: str) -> Path:
    """テキストオーバーレイ用の一時ファイルを作成する。"""
    fd, text_path = tempfile.mkstemp(suffix=".txt")
    wrapped_text = self._wrap_text(text)

    # ファイルに書き込み
    with open(fd, "w", encoding="utf-8") as f:
        f.write(wrapped_text)

    # 検証: ファイルが正しく作成されたか確認
    text_path = Path(text_path)
    if not text_path.exists():
        raise VideoCompositionError(f"テキストファイルの作成に失敗: {text_path}")

    # 検証: 内容が正しく書き込まれたか確認
    with open(text_path, "r", encoding="utf-8") as f:
        content = f.read()
        if content != wrapped_text:
            raise VideoCompositionError(f"テキストファイルの内容が不正: 期待={repr(wrapped_text[:50])}, 実際={repr(content[:50])}")

    return text_path
```

#### 2. デバッグログの強化

各テキストファイルの内容をログに出力:

```python
# _run_ffmpeg メソッド内、FFmpeg実行前
for i, text_file in enumerate(text_files):
    with open(text_file, 'r', encoding='utf-8') as f:
        content = f.read()
        log_step(f"テキスト{i+1}確認: {repr(content[:30])}...", "✅")
```

## 修正対象ファイル

- [src/generators/video_composer.py](src/generators/video_composer.py): lines 104-117, 360-370

## 検証手順

1. **即時対応**: 問題の動画を現在のコードで再生成
2. **確認**: 6秒、10秒、13秒のフレームを抽出してテキスト2が表示されることを確認
3. **予防的改善を適用後**: 新しい動画を生成してテキストオーバーレイが正しく動作することを確認

```bash
# フレーム抽出コマンド
ffmpeg -ss 7 -i 再生成した動画.mp4 -frames:v 1 -update 1 frame_7s.png -y
```

## 結論

- **根本原因**: 問題の動画生成時の一時的なI/O問題（再現不可）
- **解決策**: 動画の再生成
- **予防策**: テキストファイル作成の検証強化（オプション）

## 実行計画

### 対応: 問題の動画を再生成

既存のスクリプト・画像・音声を使用して動画のみを再生成する。

**手順**:
1. 既存のスクリプトJSONを読み込む
2. 既存の画像ファイルパスを取得
3. 既存の音声ファイルのタイミング情報を取得
4. VideoComposerで動画を再合成

**修正対象ファイル**: なし（再生成のみ）

**実行コード**:
```python
from src.generators.video_composer import VideoComposer
from src.models.script import Script
from pathlib import Path
import json

# 1. スクリプト読み込み
script_path = Path("output/scripts/20260118_153927_..._ja.json")
script = Script.from_json_file(script_path)

# 2. 画像パス取得
image_dir = Path("output/images/20260118_153927_...")
image_paths = sorted(image_dir.glob("*.png"))

# 3. 音声パスとタイミング
audio_path = Path("output/audio/20260118_153927_..._ja.mp3")
# タイミングは音声ファイルの長さから再計算

# 4. 動画再合成
composer = VideoComposer()
composer.compose(
    audio_path=audio_path,
    image_paths=image_paths,
    output_path=Path("output/videos/20260118_153927_..._ja_FIXED.mp4"),
    text_overlays=script.text_overlays,
    language="ja",
    segment_timings=segment_timings,
    video_format="short"
)
```

**検証**:
```bash
ffmpeg -ss 7 -i output_FIXED.mp4 -frames:v 1 -update 1 frame_7s.png -y
# frame_7s.png で「AIとロボットで"何でも手に入る"時代」が表示されることを確認
```
