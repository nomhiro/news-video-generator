# Implementation Tasks

## Task Overview

| Phase | Tasks | Status |
|-------|-------|--------|
| Setup | 3 | ⬜ |
| Models | 1 | ⬜ |
| Generators | 4 | ⬜ |
| Pipeline | 1 | ⬜ |
| CLI | 1 | ⬜ |
| Testing | 1 | ⬜ |
| **Total** | **11** | |

---

## Phase 1: Project Setup

### Task 1.1: Create Project Structure ⬜

**Description:** プロジェクトのディレクトリ構造を作成する

**Files to Create:**
```
news_video_generator/
├── main.py                    # 空ファイル
├── config.py                  # 空ファイル
├── requirements.txt           # 依存パッケージ
├── .env.example              # 環境変数テンプレート
├── .gitignore                # Git除外設定
├── src/
│   ├── __init__.py
│   ├── pipeline.py           # 空ファイル
│   ├── generators/
│   │   ├── __init__.py
│   │   ├── script_generator.py   # 空ファイル
│   │   ├── voice_generator.py    # 空ファイル
│   │   ├── image_generator.py    # 空ファイル
│   │   └── video_composer.py     # 空ファイル
│   ├── models/
│   │   ├── __init__.py
│   │   └── script.py            # 空ファイル
│   └── utils/
│       ├── __init__.py
│       └── logger.py            # 空ファイル
└── output/
    ├── audio/.gitkeep
    ├── images/.gitkeep
    ├── videos/.gitkeep
    └── scripts/.gitkeep
```

**Acceptance Criteria:**
- [ ] すべてのディレクトリが作成されている
- [ ] `__init__.py` が各Pythonパッケージに存在する
- [ ] `.gitkeep` がoutputサブディレクトリに存在する

---

### Task 1.2: Create requirements.txt ⬜

**Description:** 依存パッケージを定義する

**File:** `requirements.txt`

**Content:**
```
# Core Dependencies
anthropic>=0.40.0
requests>=2.31.0
python-dotenv>=1.0.0
```

**Acceptance Criteria:**
- [ ] ファイルが作成されている
- [ ] `pip install -r requirements.txt` が成功する

---

### Task 1.3: Create .env.example ⬜

**Description:** 環境変数のテンプレートを作成する

**File:** `.env.example`

**Content:**
```bash
# Required API Keys
ANTHROPIC_API_KEY=sk-ant-xxxxx
ELEVENLABS_API_KEY=xxxxx
FAL_KEY=xxxxx

# Optional: Voice IDs
ELEVENLABS_VOICE_ID_JA=EXAVITQu4vr4xnSDxMaL
ELEVENLABS_VOICE_ID_EN=21m00Tcm4TlvDq8ikWAM
```

**Acceptance Criteria:**
- [ ] ファイルが作成されている
- [ ] すべての必須変数が記載されている
- [ ] コメントで説明が記載されている

---

## Phase 2: Data Models

### Task 2.1: Implement Script Model ⬜

**Description:** 台本データを格納するdataclassを実装する

**File:** `src/models/script.py`

**Implementation:**
```python
from dataclasses import dataclass, asdict
from typing import List
from pathlib import Path
import json


@dataclass
class Script:
    """動画用台本データモデル"""
    language: str           # "ja" or "en"
    title: str              # 動画タイトル
    hook: str               # フック（冒頭5秒）
    main_points: List[str]  # メインポイント
    conclusion: str         # 結論
    full_narration: str     # 完全なナレーション台本
    image_prompts: List[str]  # 画像生成プロンプト
    estimated_duration: int   # 推定秒数

    def to_dict(self) -> dict:
        """辞書形式に変換"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'Script':
        """辞書から生成"""
        return cls(**data)

    def to_json_file(self, path: Path) -> None:
        """JSONファイルに保存"""
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def from_json_file(cls, path: Path) -> 'Script':
        """JSONファイルから読み込み"""
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return cls.from_dict(data)
```

**Acceptance Criteria:**
- [ ] dataclassとして定義されている
- [ ] to_dict() メソッドが動作する
- [ ] from_dict() メソッドが動作する
- [ ] to_json_file() メソッドが動作する
- [ ] from_json_file() メソッドが動作する
- [ ] 型ヒントが付いている
- [ ] docstringが付いている

---

## Phase 3: Generators

### Task 3.1: Implement Config ⬜

**Description:** 設定管理クラスを実装する

**File:** `config.py`

**Implementation:**
```python
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
import os
from dotenv import load_dotenv


@dataclass
class Config:
    """アプリケーション設定"""
    
    # API Keys
    anthropic_api_key: str
    elevenlabs_api_key: str
    fal_api_key: str
    
    # Voice Settings
    voice_id_ja: str = "EXAVITQu4vr4xnSDxMaL"
    voice_id_en: str = "21m00Tcm4TlvDq8ikWAM"
    
    # Output Settings
    output_dir: Path = Path("./output")
    
    @classmethod
    def from_env(cls) -> 'Config':
        """環境変数から設定を読み込み"""
        load_dotenv()
        
        return cls(
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            elevenlabs_api_key=os.getenv("ELEVENLABS_API_KEY", ""),
            fal_api_key=os.getenv("FAL_KEY", ""),
            voice_id_ja=os.getenv("ELEVENLABS_VOICE_ID_JA", "EXAVITQu4vr4xnSDxMaL"),
            voice_id_en=os.getenv("ELEVENLABS_VOICE_ID_EN", "21m00Tcm4TlvDq8ikWAM"),
        )
    
    def validate(self) -> List[str]:
        """設定の検証。エラーメッセージのリストを返す"""
        errors = []
        if not self.anthropic_api_key:
            errors.append("ANTHROPIC_API_KEY が設定されていません")
        if not self.elevenlabs_api_key:
            errors.append("ELEVENLABS_API_KEY が設定されていません")
        if not self.fal_api_key:
            errors.append("FAL_KEY が設定されていません")
        return errors
```

**Acceptance Criteria:**
- [ ] from_env() で環境変数から読み込める
- [ ] validate() でエラーを検出できる
- [ ] デフォルト値が設定されている

---

### Task 3.2: Implement Script Generator ⬜

**Description:** Claude APIを使用して台本を生成するクラスを実装する

**File:** `src/generators/script_generator.py`

**Key Points:**
1. Anthropicクライアントを初期化
2. 言語別のシステムプロンプトを構築
3. Claude APIを呼び出してJSON形式で台本を取得
4. レスポンスをパースしてScriptオブジェクトを返す

**System Prompt (日本語):**
```
あなたはYouTube ShortsやTikTok向けのニュース解説動画の台本ライターです。
与えられたニューストピックから、45秒程度の短尺動画用の台本を作成してください。

## 出力形式
以下のJSON形式で出力してください。JSON以外のテキストは含めないでください。

{
    "title": "動画タイトル（15文字以内、キャッチーに）",
    "hook": "最初の5秒で視聴者を引き付けるフック（衝撃的な事実や質問形式）",
    "main_points": [
        "ポイント1（簡潔に）",
        "ポイント2（簡潔に）",
        "ポイント3（簡潔に）"
    ],
    "conclusion": "締めの一言（CTA含む）",
    "full_narration": "ナレーション用の完全な台本（自然な話し言葉で）",
    "image_prompts": [
        "Scene 1: (英語で具体的な視覚描写)",
        "Scene 2: (英語で具体的な視覚描写)",
        "Scene 3: (英語で具体的な視覚描写)",
        "Scene 4: (英語で具体的な視覚描写)"
    ],
    "estimated_duration": 45
}

## 注意事項
- full_narrationは読み上げ用なので、自然な話し言葉で
- image_promptsは必ず英語で、Flux画像生成モデル用
- 各image_promptに "cinematic, high quality, 9:16 vertical" を含める
```

**Acceptance Criteria:**
- [ ] generate() メソッドがScriptを返す
- [ ] 日本語と英語の両方に対応している
- [ ] JSONパースエラーを適切にハンドリング
- [ ] APIエラーを適切にハンドリング

---

### Task 3.3: Implement Voice Generator ⬜

**Description:** ElevenLabs APIを使用して音声を生成するクラスを実装する

**File:** `src/generators/voice_generator.py`

**Key Points:**
1. ElevenLabs APIの呼び出し
2. 言語に応じたボイスIDの選択
3. MP3ファイルの保存

**API Call:**
```python
response = requests.post(
    f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
    headers={
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
        "xi-api-key": self.api_key
    },
    json={
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75
        }
    }
)
```

**Acceptance Criteria:**
- [ ] generate() メソッドがファイルパスを返す
- [ ] 日本語と英語の両方に対応している
- [ ] MP3ファイルが正しく保存される
- [ ] APIエラーを適切にハンドリング

---

### Task 3.4: Implement Image Generator ⬜

**Description:** fal.ai APIを使用して画像を生成するクラスを実装する

**File:** `src/generators/image_generator.py`

**Key Points:**
1. fal.ai Queue APIの呼び出し（非同期）
2. 結果のポーリング
3. 画像のダウンロードと保存
4. プロンプトの強化（high quality等を追加）

**API Flow:**
```python
# 1. リクエスト送信
response = requests.post(
    "https://queue.fal.run/fal-ai/flux/schnell",
    headers={
        "Authorization": f"Key {self.api_key}",
        "Content-Type": "application/json"
    },
    json={
        "prompt": enhanced_prompt,
        "image_size": "portrait_16_9",
        "num_inference_steps": 4,
        "num_images": 1
    }
)
request_id = response.json()["request_id"]

# 2. ポーリング
while True:
    status = requests.get(
        f"https://queue.fal.run/fal-ai/flux/schnell/requests/{request_id}/status",
        headers={"Authorization": f"Key {self.api_key}"}
    )
    if status.json()["status"] == "COMPLETED":
        break
    time.sleep(1)

# 3. 結果取得
result = requests.get(
    f"https://queue.fal.run/fal-ai/flux/schnell/requests/{request_id}",
    headers={"Authorization": f"Key {self.api_key}"}
)
image_url = result.json()["images"][0]["url"]
```

**Acceptance Criteria:**
- [ ] generate_batch() メソッドがファイルパスリストを返す
- [ ] 4枚以上の画像を生成できる
- [ ] プロンプトが強化されている
- [ ] タイムアウト処理がある
- [ ] APIエラーを適切にハンドリング

---

### Task 3.5: Implement Video Composer ⬜

**Description:** FFmpegを使用して動画を合成するクラスを実装する

**File:** `src/generators/video_composer.py`

**Key Points:**
1. ffprobeで音声ファイルの長さを取得
2. 各画像の表示時間を計算
3. concatデマクサー用のファイルリストを作成
4. FFmpegコマンドを実行

**FFmpeg Command:**
```bash
ffmpeg -y \
  -f concat -safe 0 -i filelist.txt \
  -i audio.mp3 \
  -vf "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2" \
  -c:v libx264 -preset medium -crf 23 \
  -c:a aac -b:a 192k \
  -shortest -pix_fmt yuv420p \
  output.mp4
```

**Acceptance Criteria:**
- [ ] compose() メソッドがファイルパスを返す
- [ ] 出力動画が再生可能
- [ ] 解像度が1080x1920
- [ ] 音声と画像が同期している
- [ ] FFmpegエラーを適切にハンドリング

---

## Phase 4: Pipeline

### Task 4.1: Implement Pipeline ⬜

**Description:** 全体のパイプラインを制御するクラスを実装する

**File:** `src/pipeline.py`

**Flow:**
```
1. 出力ディレクトリを作成
2. 各言語で台本を生成
3. 画像を生成（最初の言語の台本を使用）
4. 各言語で音声を生成
5. 各言語で動画を合成
6. 結果サマリーを返す
```

**Implementation:**
```python
class Pipeline:
    def __init__(self, config: Config):
        self.config = config
        self.script_generator = ScriptGenerator(config.anthropic_api_key)
        self.voice_generator = VoiceGenerator(
            config.elevenlabs_api_key,
            config.voice_id_ja,
            config.voice_id_en
        )
        self.image_generator = ImageGenerator(config.fal_api_key)
        self.video_composer = VideoComposer()

    def run(self, news_topic: str, languages: list[str] = ["ja", "en"]) -> dict:
        """パイプライン全体を実行"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # 1. Generate scripts
        scripts = {}
        for lang in languages:
            scripts[lang] = self.script_generator.generate(news_topic, lang)
            scripts[lang].to_json_file(...)
        
        # 2. Generate images (use first language's prompts)
        first_lang = languages[0]
        image_paths = self.image_generator.generate_batch(
            scripts[first_lang].image_prompts, ...
        )
        
        # 3. Generate voices
        audio_paths = {}
        for lang in languages:
            audio_paths[lang] = self.voice_generator.generate(
                scripts[lang].full_narration, lang, ...
            )
        
        # 4. Compose videos
        video_paths = {}
        for lang in languages:
            video_paths[lang] = self.video_composer.compose(
                audio_paths[lang], image_paths, ...
            )
        
        return {
            "status": "success",
            "scripts": {lang: str(path) for lang, path in ...},
            "images": [str(p) for p in image_paths],
            "audio": {lang: str(path) for lang, path in audio_paths.items()},
            "videos": {lang: str(path) for lang, path in video_paths.items()}
        }
```

**Acceptance Criteria:**
- [ ] run() メソッドが結果dictを返す
- [ ] 日本語と英語の両方の動画が生成される
- [ ] 画像は共通で使い回される
- [ ] 中間ファイルがすべて保存される
- [ ] エラー時に適切なメッセージが表示される

---

## Phase 5: CLI

### Task 5.1: Implement CLI Entry Point ⬜

**Description:** コマンドラインインターフェースを実装する

**File:** `main.py`

**Usage:**
```bash
python main.py "ニューストピック"
python main.py "ニューストピック" -l ja
python main.py "ニューストピック" -l ja en
python main.py "ニューストピック" -o ./my_videos
python main.py "ニューストピック" -v
```

**Implementation:**
```python
import argparse
from config import Config
from src.pipeline import Pipeline


def main():
    parser = argparse.ArgumentParser(
        description='ニュース動画自動生成システム'
    )
    parser.add_argument('topic', help='ニューストピック')
    parser.add_argument('-l', '--languages', nargs='+', 
                        default=['ja', 'en'], choices=['ja', 'en'])
    parser.add_argument('-o', '--output', default='./output')
    parser.add_argument('-v', '--verbose', action='store_true')
    
    args = parser.parse_args()
    
    # Load config
    config = Config.from_env()
    config.output_dir = Path(args.output)
    
    # Validate
    errors = config.validate()
    if errors:
        for error in errors:
            print(f"❌ {error}")
        return 1
    
    # Run pipeline
    pipeline = Pipeline(config)
    try:
        result = pipeline.run(args.topic, args.languages)
        print("\n🎉 完了!")
        for lang, path in result["videos"].items():
            print(f"   {lang}: {path}")
    except Exception as e:
        print(f"❌ エラー: {e}")
        return 1
    
    return 0


if __name__ == "__main__":
    exit(main())
```

**Acceptance Criteria:**
- [ ] `python main.py "トピック"` で実行できる
- [ ] `--languages` オプションが動作する
- [ ] `--output` オプションが動作する
- [ ] `--verbose` オプションが動作する
- [ ] エラー時に適切な終了コードを返す

---

## Phase 6: Testing

### Task 6.1: Run Integration Test ⬜

**Description:** 実際のAPIを使用して動画生成をテストする

**Test Topic:**
```
Google Veo 3.1 発表 - AI動画生成の新時代

Googleが最新のAI動画生成モデル「Veo 3.1」を発表しました。
主な特徴：
- 9:16縦型動画のネイティブサポート
- 4K解像度への高品質アップスケーリング
- キャラクターと背景の一貫性が向上
```

**Test Commands:**
```bash
# 日本語のみ
python main.py "Google Veo 3.1発表" -l ja

# 両言語
python main.py "Google Veo 3.1発表"
```

**Acceptance Criteria:**
- [ ] エラーなく完了する
- [ ] 日本語動画が生成される
- [ ] 英語動画が生成される
- [ ] 動画が再生可能
- [ ] 動画の長さが30-60秒

---

## Completion Checklist

### Files Created
- [ ] `main.py`
- [ ] `config.py`
- [ ] `requirements.txt`
- [ ] `.env.example`
- [ ] `.gitignore`
- [ ] `src/__init__.py`
- [ ] `src/pipeline.py`
- [ ] `src/models/__init__.py`
- [ ] `src/models/script.py`
- [ ] `src/generators/__init__.py`
- [ ] `src/generators/script_generator.py`
- [ ] `src/generators/voice_generator.py`
- [ ] `src/generators/image_generator.py`
- [ ] `src/generators/video_composer.py`
- [ ] `src/utils/__init__.py`
- [ ] `src/utils/logger.py`

### Functionality
- [ ] 台本生成が動作する
- [ ] 音声生成が動作する
- [ ] 画像生成が動作する
- [ ] 動画合成が動作する
- [ ] パイプライン全体が動作する
- [ ] CLIが動作する

### Quality
- [ ] すべての関数に型ヒントがある
- [ ] すべての関数にdocstringがある
- [ ] エラーハンドリングが実装されている
- [ ] ログ出力が実装されている
