# 動画テキストオーバーレイの行間修正プラン

## 問題
動画に配置されるテキストで、改行がある場合に行間に無駄なスペースがある。

## 現状分析

### 現在の設定 ([video_composer.py:42](src/generators/video_composer.py#L42))
```python
TEXT_LINE_SPACING = 8  # 行間（ピクセル）
```

### FFmpegの動作
- `line_spacing`パラメータは「最大グリフ高さ」に追加されるピクセル値
- デフォルト値は0
- 現在は8ピクセルが追加されている

### 関連設定
- `TEXT_FONT_SIZE = 72` (フォントサイズ)
- `TEXT_BOX_BORDER = 15` (背景ボックスの余白)

## 修正方針

`TEXT_LINE_SPACING`を0に変更し、FFmpegデフォルトの行間（グリフ高さのみ）にする。

### 変更箇所
- ファイル: [src/generators/video_composer.py](src/generators/video_composer.py)
- 行: 42
- 変更: `TEXT_LINE_SPACING = 8` → `TEXT_LINE_SPACING = 0`

## 検証方法
1. 動画を生成してテキストオーバーレイの行間を視覚的に確認
2. 必要に応じて値を微調整
