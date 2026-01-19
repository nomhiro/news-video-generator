# TikTok収益化対応 & Long版改善: 動画生成機能更新

## 概要
1. TikTokの収益化要件（60秒以上の動画）に対応するため、新しい「tiktok」動画形式を追加
2. Long版のセグメント数を可変(15-25)から**10個固定**に変更

## フォーマット比較

| 属性 | short (現在) | tiktok (新規) | long (変更) |
|------|-------------|---------------|-------------|
| **想定時間** | ~35秒 | 60-90秒 | ~5分 |
| **ナレーション(日本語)** | 250-300文字 | 500-650文字 | 2000-2500文字 |
| **ナレーション(英語)** | 120-150語 | 250-350語 | 750-900語 |
| **セグメント数** | 6個(固定) | 6個(固定) | **10個(固定)** |
| **画像枚数** | 6枚 | 6枚 | **10枚** |
| **1セグメントあたり** | ~5-6秒 | ~10-15秒 | ~30秒 |
| **話速** | 1.25x | 1.15x | 1.1x |
| **アスペクト比** | 9:16 (縦) | 9:16 (縦) | 16:9 (横) |
| **解像度** | 1080x1920 | 1080x1920 | 1920x1080 |

## 変更ファイル一覧

### 1. src/generators/script_generator.py
**変更内容:**
- TikTok用システムプロンプト追加（日本語/英語）
- Long用システムプロンプト修正: 15-25個 → **10個固定**
- `_build_system_prompt`メソッドに"tiktok"分岐追加

**主な変更箇所:**
- 行107-173 (`SYSTEM_PROMPT_LONG_JA`): セグメント数を10個固定に
- 行251-317 (`SYSTEM_PROMPT_LONG_EN`): セグメント数を10個固定に
- 行249以降: `SYSTEM_PROMPT_TIKTOK_JA`と`SYSTEM_PROMPT_TIKTOK_EN`追加
- 行389-407: `_build_system_prompt`に"tiktok"条件追加

### 2. src/pipeline.py
**変更内容:**
- 話速マッピングにtiktok追加（1.15x）

**主な変更箇所:**
- 行104-105:
```python
speaking_rates = {"long": 1.1, "tiktok": 1.15, "short": 1.25}
speaking_rate = speaking_rates.get(video_format, 1.25)
```

### 3. src/generators/image_generator.py
**変更内容:**
- tiktokフォーマット用のラベル追加

**主な変更箇所:**
- 行64-66: フォーマットラベルの辞書化
- 行150-156: tiktokをshortと同じ縦長処理に

### 4. src/generators/video_composer.py
**変更内容:**
- tiktokフォーマット用のラベル追加

**主な変更箇所:**
- 行157-165: tiktok用のフォーマットラベル追加

### 5. main.py (CLI)
**変更内容:**
- `--format`/`-f`引数追加

**主な変更箇所:**
- 行53以降:
```python
parser.add_argument(
    "-f", "--format",
    default="short",
    choices=["short", "tiktok", "long"],
    help="動画形式: short(35秒), tiktok(60-90秒), long(5分)"
)
```
- 行84: `pipeline.run(args.topic, args.languages, video_format=args.format)`

### 6. templates/partials/selected_panel.html (Web UI)
**変更内容:**
- TikTokオプションをフォーマットセレクタに追加
- グリッドを2列から3列に変更

---

## TikTok用システムプロンプト（日本語）

```python
SYSTEM_PROMPT_TIKTOK_JA = """<role>
あなたはTikTok向けのニュース解説動画の台本ライターです。
</role>

<task>
与えられたニューストピックから、60〜90秒程度のTikTok動画用の台本をJSON形式で作成してください。
TikTokの収益化には60秒以上の動画が必要です。
</task>

<critical_constraints>
【最重要】以下の3つの配列は必ず6個ずつ生成してください：
- image_prompts: 6個
- text_overlays: 6個
- segment_narrations: 6個

配列の要素数が1つでも異なると動画生成が失敗します。
各要素は空文字列("")にしないでください。
</critical_constraints>

<content_rules>
- ナレーション(full_narration): 500〜650文字の自然な話し言葉（60〜90秒に相当）
- segment_narrations: full_narrationを6個に分割（各セグメント80-110文字程度）
- image_prompts: 必ず英語で記述、"cinematic, high quality" を含める
- text_overlays: 各画像に対応する短文（15-25文字）
- title: 40文字程度
- hashtags: 5〜8個（"TikTok"と"ニュース"は必須）
- estimated_duration: 75
</content_rules>
...(省略)"""
```

## Long用システムプロンプト修正（日本語）

```python
# 変更前: 15〜25枚（可変）
# 変更後: 10枚（固定）

<critical_constraints>
【最重要】以下の3つの配列は必ず10個ずつ生成してください：
- image_prompts: 10個
- text_overlays: 10個
- segment_narrations: 10個
</critical_constraints>

<content_rules>
- ナレーション(full_narration): 2000〜2500文字
- segment_narrations: full_narrationを10個に分割（各セグメント200-250文字程度）
- image_prompts: 10個（英語、cinematic, high quality含む）
- text_overlays: 10個（各20-30文字）
- estimated_duration: 300
</content_rules>
```

---

## 実装順序

1. **script_generator.py** - TikTokプロンプト追加 + Longプロンプト修正
2. **pipeline.py** - 話速設定
3. **image_generator.py** - ラベル追加
4. **video_composer.py** - ラベル追加
5. **main.py** - CLI引数追加
6. **selected_panel.html** - Web UIオプション追加

---

## 検証方法

### CLIテスト
```bash
# TikTok形式（60-90秒、6セグメント）
python main.py "ニューストピック" -f tiktok -l ja

# Long形式（5分、10セグメント）
python main.py "ニューストピック" -f long -l ja

# 動画の長さ確認
ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 output/[ファイル名].mp4
```

### 期待される結果

| フォーマット | 動画長 | セグメント | 画像 | アスペクト比 |
|-------------|--------|-----------|------|-------------|
| short | ~35秒 | 6個 | 6枚 | 9:16 (縦) |
| tiktok | 60-90秒 | 6個 | 6枚 | 9:16 (縦) |
| long | ~5分 | **10個** | **10枚** | 16:9 (横) |
