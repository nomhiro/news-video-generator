# ニュース動画自動生成システム - 技術仕様書

## 1. システムアーキテクチャ

### 1.1 全体構成図

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              User Interface                              │
│                         (CLI / Web UI - 将来)                            │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                           Pipeline Orchestrator                          │
│                              (main.py)                                   │
└─────────────────────────────────────────────────────────────────────────┘
         │              │              │              │              │
         ▼              ▼              ▼              ▼              ▼
┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐
│   News      │ │   Script    │ │   Voice     │ │   Image     │ │   Video     │
│  Collector  │ │  Generator  │ │  Generator  │ │  Generator  │ │  Composer   │
│             │ │             │ │             │ │             │ │             │
│ (RSS,API)   │ │ (Claude)    │ │(ElevenLabs) │ │ (Flux/fal)  │ │(Creatomate) │
└─────────────┘ └─────────────┘ └─────────────┘ └─────────────┘ └─────────────┘
         │              │              │              │              │
         ▼              ▼              ▼              ▼              ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                            Storage Layer                                 │
│                    (Local Files / Cloud Storage)                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### 1.2 ディレクトリ構成

```
news_video_generator/
├── main.py                    # エントリーポイント
├── config.py                  # 設定管理
├── requirements.txt           # 依存パッケージ
├── .env.example              # 環境変数テンプレート
├── .env                      # 環境変数（gitignore）
│
├── src/
│   ├── __init__.py
│   ├── pipeline.py           # パイプライン制御
│   │
│   ├── collectors/           # ニュース収集モジュール
│   │   ├── __init__.py
│   │   ├── base.py          # 基底クラス
│   │   ├── rss_collector.py # RSS収集
│   │   └── newsapi_collector.py # NewsAPI収集
│   │
│   ├── generators/           # 生成モジュール
│   │   ├── __init__.py
│   │   ├── script_generator.py  # 台本生成
│   │   ├── voice_generator.py   # 音声生成
│   │   ├── image_generator.py   # 画像生成
│   │   └── video_composer.py    # 動画合成
│   │
│   ├── models/               # データモデル
│   │   ├── __init__.py
│   │   ├── news_item.py     # ニュースアイテム
│   │   ├── script.py        # 台本
│   │   └── video_project.py # 動画プロジェクト
│   │
│   └── utils/                # ユーティリティ
│       ├── __init__.py
│       ├── logger.py        # ロギング
│       ├── file_utils.py    # ファイル操作
│       └── api_utils.py     # API共通処理
│
├── templates/                # 動画テンプレート
│   └── short_video.json     # Creatomate用
│
├── output/                   # 出力ディレクトリ
│   ├── audio/
│   ├── images/
│   ├── videos/
│   └── scripts/
│
├── docs/                     # ドキュメント
│   ├── REQUIREMENTS.md
│   └── SPECIFICATION.md
│
└── tests/                    # テスト
    ├── __init__.py
    ├── test_script_generator.py
    └── test_voice_generator.py
```

---

## 2. データモデル

### 2.1 NewsItem（ニュースアイテム）

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

@dataclass
class NewsItem:
    """収集されたニュースアイテム"""
    id: str                          # ユニークID（UUID）
    title: str                       # タイトル
    summary: str                     # 要約（AI生成）
    source: str                      # ソース名
    url: str                         # 元記事URL
    published_at: datetime           # 公開日時
    viral_score: int                 # バイラルスコア（1-10）
    category: str                    # カテゴリ
    raw_content: Optional[str]       # 元記事本文
    
    def to_dict(self) -> dict:
        """辞書形式に変換"""
        pass
    
    @classmethod
    def from_dict(cls, data: dict) -> 'NewsItem':
        """辞書から生成"""
        pass
```

### 2.2 Script（台本）

```python
from dataclasses import dataclass
from typing import List

@dataclass
class Script:
    """動画用台本"""
    language: str                    # "ja" or "en"
    title: str                       # 動画タイトル
    hook: str                        # フック（冒頭5秒）
    main_points: List[str]           # メインポイント（3つ程度）
    conclusion: str                  # 結論
    full_narration: str              # 完全なナレーション台本
    image_prompts: List[str]         # 画像生成プロンプト（英語）
    estimated_duration: int          # 推定秒数
    
    def to_dict(self) -> dict:
        pass
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Script':
        pass
    
    def to_json(self, filepath: str) -> None:
        """JSONファイルに保存"""
        pass
```

### 2.3 VideoProject（動画プロジェクト）

```python
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
from datetime import datetime

@dataclass
class VideoProject:
    """動画生成プロジェクト"""
    id: str                              # プロジェクトID
    news_item: NewsItem                  # 元ニュース
    created_at: datetime                 # 作成日時
    
    # 各言語のスクリプト
    script_ja: Optional[Script] = None
    script_en: Optional[Script] = None
    
    # 生成アセット
    audio_path_ja: Optional[Path] = None
    audio_path_en: Optional[Path] = None
    image_paths: List[Path] = field(default_factory=list)
    
    # 出力動画
    video_path_ja: Optional[Path] = None
    video_path_en: Optional[Path] = None
    
    # ステータス
    status: str = "created"              # created, processing, completed, failed
    error_message: Optional[str] = None
    
    def get_output_dir(self) -> Path:
        """プロジェクト出力ディレクトリを取得"""
        pass
```

---

## 3. モジュール仕様

### 3.1 ScriptGenerator（台本生成）

```python
class ScriptGenerator:
    """Claude APIを使用した台本生成"""
    
    def __init__(self, api_key: str):
        """
        Args:
            api_key: Anthropic API Key
        """
        self.client = Anthropic(api_key=api_key)
        self.model = "claude-sonnet-4-20250514"
    
    def generate(
        self,
        news_topic: str,
        language: str = "ja",
        target_duration: int = 45
    ) -> Script:
        """
        ニューストピックから台本を生成
        
        Args:
            news_topic: ニューストピック（テキスト）
            language: 出力言語 ("ja" or "en")
            target_duration: 目標秒数
        
        Returns:
            Script: 生成された台本
        
        Raises:
            ScriptGenerationError: 生成失敗時
        """
        pass
    
    def _build_prompt(self, language: str, target_duration: int) -> str:
        """言語に応じたプロンプトを構築"""
        pass
    
    def _parse_response(self, response_text: str, language: str) -> Script:
        """APIレスポンスをScriptオブジェクトにパース"""
        pass
```

**プロンプト仕様（日本語）:**

```
あなたはYouTube ShortsやTikTok向けのニュース解説動画の台本ライターです。
与えられたニューストピックから、{target_duration}秒の短尺動画用の台本を作成してください。

## 出力形式
以下のJSON形式で出力してください：

{
    "title": "動画タイトル（15文字以内、キャッチーに）",
    "hook": "最初の5秒で視聴者を引き付けるフック（衝撃的な事実や質問形式）",
    "main_points": [
        "ポイント1（簡潔に）",
        "ポイント2（簡潔に）",
        "ポイント3（簡潔に）"
    ],
    "conclusion": "締めの一言（CTA含む、例：フォローで最新情報をチェック！）",
    "full_narration": "ナレーション用の完全な台本（自然な話し言葉で）",
    "image_prompts": [
        "シーン1の画像プロンプト（英語、具体的な視覚描写）",
        "シーン2の画像プロンプト（英語）",
        "シーン3の画像プロンプト（英語）",
        "シーン4の画像プロンプト（英語）"
    ],
    "estimated_duration": {推定秒数}
}

## 注意事項
- full_narrationは読み上げ用なので、自然な話し言葉で書く
- 「えー」「あのー」などの口語は使わない
- image_promptsは必ず英語で、Flux画像生成用
- 各画像プロンプトは「cinematic, high quality, 9:16 aspect ratio」を含める
- ニュース系なので信頼感のあるトーンで
```

---

### 3.2 VoiceGenerator（音声生成）

```python
class VoiceGenerator:
    """ElevenLabs APIを使用した音声生成"""
    
    # 推奨ボイスID
    VOICE_IDS = {
        "ja": {
            "male": "pNInz6obpgDQGcFmaJgB",    # Adam（日本語対応）
            "female": "EXAVITQu4vr4xnSDxMaL",  # Bella
        },
        "en": {
            "male": "ErXwobaYiN019PkySvjV",    # Antoni
            "female": "21m00Tcm4TlvDq8ikWAM",  # Rachel
        }
    }
    
    def __init__(self, api_key: str):
        """
        Args:
            api_key: ElevenLabs API Key
        """
        self.api_key = api_key
        self.base_url = "https://api.elevenlabs.io/v1"
    
    def generate(
        self,
        text: str,
        language: str = "ja",
        voice_id: Optional[str] = None,
        output_path: Optional[Path] = None
    ) -> Path:
        """
        テキストから音声を生成
        
        Args:
            text: 読み上げるテキスト
            language: 言語 ("ja" or "en")
            voice_id: ボイスID（Noneの場合はデフォルト使用）
            output_path: 出力パス（Noneの場合は自動生成）
        
        Returns:
            Path: 生成された音声ファイルのパス
        
        Raises:
            VoiceGenerationError: 生成失敗時
        """
        pass
    
    def list_voices(self) -> List[dict]:
        """利用可能なボイス一覧を取得"""
        pass
    
    def get_voice_settings(self) -> dict:
        """音声設定を取得"""
        return {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.5,
            "use_speaker_boost": True
        }
```

**API呼び出し仕様:**

```python
# エンドポイント
POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}

# ヘッダー
{
    "Accept": "audio/mpeg",
    "Content-Type": "application/json",
    "xi-api-key": "{API_KEY}"
}

# ボディ
{
    "text": "{読み上げテキスト}",
    "model_id": "eleven_multilingual_v2",
    "voice_settings": {
        "stability": 0.5,
        "similarity_boost": 0.75,
        "style": 0.5,
        "use_speaker_boost": true
    }
}

# レスポンス
audio/mpeg バイナリデータ
```

---

### 3.3 ImageGenerator（画像生成）

```python
class ImageGenerator:
    """fal.ai (Flux)を使用した画像生成"""
    
    def __init__(self, api_key: str):
        """
        Args:
            api_key: fal.ai API Key
        """
        self.api_key = api_key
        self.base_url = "https://queue.fal.run"
        self.model = "fal-ai/flux/schnell"  # 高速版
    
    def generate(
        self,
        prompt: str,
        output_path: Optional[Path] = None,
        aspect_ratio: str = "9:16",
        num_images: int = 1
    ) -> List[Path]:
        """
        プロンプトから画像を生成
        
        Args:
            prompt: 画像生成プロンプト（英語）
            output_path: 出力パス
            aspect_ratio: アスペクト比
            num_images: 生成枚数
        
        Returns:
            List[Path]: 生成された画像ファイルのパスリスト
        """
        pass
    
    def generate_batch(
        self,
        prompts: List[str],
        output_dir: Path
    ) -> List[Path]:
        """
        複数プロンプトから画像をバッチ生成
        
        Args:
            prompts: プロンプトリスト
            output_dir: 出力ディレクトリ
        
        Returns:
            List[Path]: 生成された画像ファイルのパスリスト
        """
        pass
    
    def _enhance_prompt(self, prompt: str) -> str:
        """プロンプトを強化"""
        enhancements = [
            "high quality",
            "detailed",
            "professional",
            "cinematic lighting",
            "8k resolution",
            "9:16 vertical aspect ratio"
        ]
        return f"{prompt}, {', '.join(enhancements)}"
    
    def _poll_result(self, request_id: str, timeout: int = 120) -> dict:
        """結果をポーリング"""
        pass
```

**API呼び出し仕様:**

```python
# エンドポイント（リクエスト送信）
POST https://queue.fal.run/fal-ai/flux/schnell

# ヘッダー
{
    "Authorization": "Key {FAL_KEY}",
    "Content-Type": "application/json"
}

# ボディ
{
    "prompt": "{画像プロンプト}",
    "image_size": "portrait_16_9",
    "num_inference_steps": 4,
    "num_images": 1,
    "enable_safety_checker": true
}

# レスポンス（キュー投入時）
{
    "request_id": "xxx-xxx-xxx"
}

# ステータス確認
GET https://queue.fal.run/fal-ai/flux/schnell/requests/{request_id}/status

# 結果取得
GET https://queue.fal.run/fal-ai/flux/schnell/requests/{request_id}

# 結果レスポンス
{
    "images": [
        {
            "url": "https://...",
            "content_type": "image/png"
        }
    ]
}
```

---

### 3.4 VideoComposer（動画合成）

```python
class VideoComposer:
    """動画合成"""
    
    def __init__(
        self,
        creatomate_api_key: Optional[str] = None,
        use_ffmpeg_fallback: bool = True
    ):
        """
        Args:
            creatomate_api_key: Creatomate API Key
            use_ffmpeg_fallback: FFmpegフォールバックを使用するか
        """
        self.creatomate_api_key = creatomate_api_key
        self.use_ffmpeg_fallback = use_ffmpeg_fallback
    
    def compose(
        self,
        audio_path: Path,
        image_paths: List[Path],
        output_path: Path,
        title: Optional[str] = None,
        subtitle_path: Optional[Path] = None
    ) -> Path:
        """
        音声と画像から動画を生成
        
        Args:
            audio_path: 音声ファイルパス
            image_paths: 画像ファイルパスリスト
            output_path: 出力動画パス
            title: 動画タイトル（オーバーレイ用）
            subtitle_path: 字幕ファイルパス（SRT形式）
        
        Returns:
            Path: 生成された動画ファイルのパス
        """
        if self.creatomate_api_key:
            return self._compose_with_creatomate(...)
        elif self.use_ffmpeg_fallback:
            return self._compose_with_ffmpeg(...)
        else:
            raise VideoCompositionError("No composition method available")
    
    def _compose_with_creatomate(self, ...) -> Path:
        """Creatomate APIで動画合成"""
        pass
    
    def _compose_with_ffmpeg(
        self,
        audio_path: Path,
        image_paths: List[Path],
        output_path: Path
    ) -> Path:
        """FFmpegで動画合成"""
        pass
    
    def _get_audio_duration(self, audio_path: Path) -> float:
        """音声ファイルの長さを取得"""
        pass
```

**FFmpegコマンド仕様:**

```bash
# 1. 画像リストファイル作成（filelist.txt）
file '/path/to/image1.png'
duration 5.0
file '/path/to/image2.png'
duration 5.0
file '/path/to/image3.png'
duration 5.0
file '/path/to/image4.png'
duration 5.0
file '/path/to/image4.png'  # 最後の画像を再度追加（ffmpeg仕様）

# 2. 動画生成コマンド
ffmpeg -y \
  -f concat -safe 0 -i filelist.txt \
  -i audio.mp3 \
  -vf "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,zoompan=z='min(zoom+0.001,1.2)':d=1:s=1080x1920" \
  -c:v libx264 \
  -preset medium \
  -crf 23 \
  -c:a aac \
  -b:a 192k \
  -shortest \
  -pix_fmt yuv420p \
  output.mp4
```

---

## 4. パイプライン制御

### 4.1 Pipeline クラス

```python
class Pipeline:
    """動画生成パイプラインの制御"""
    
    def __init__(self, config: Config):
        """
        Args:
            config: 設定オブジェクト
        """
        self.config = config
        self.script_generator = ScriptGenerator(config.anthropic_api_key)
        self.voice_generator = VoiceGenerator(config.elevenlabs_api_key)
        self.image_generator = ImageGenerator(config.fal_api_key)
        self.video_composer = VideoComposer(config.creatomate_api_key)
        self.logger = get_logger(__name__)
    
    def run(
        self,
        news_topic: str,
        languages: List[str] = ["ja", "en"],
        skip_existing: bool = True
    ) -> VideoProject:
        """
        パイプライン全体を実行
        
        Args:
            news_topic: ニューストピック
            languages: 生成する言語リスト
            skip_existing: 既存ファイルをスキップするか
        
        Returns:
            VideoProject: 生成結果
        """
        project = VideoProject(
            id=str(uuid.uuid4()),
            news_item=NewsItem(title=news_topic, ...),
            created_at=datetime.now()
        )
        
        try:
            # Step 1: 台本生成
            self._generate_scripts(project, languages)
            
            # Step 2: 画像生成（共通）
            self._generate_images(project)
            
            # Step 3: 音声生成
            self._generate_voices(project, languages)
            
            # Step 4: 動画合成
            self._compose_videos(project, languages)
            
            project.status = "completed"
            
        except Exception as e:
            project.status = "failed"
            project.error_message = str(e)
            self.logger.error(f"Pipeline failed: {e}")
            raise
        
        return project
    
    def _generate_scripts(self, project: VideoProject, languages: List[str]):
        """台本生成ステップ"""
        pass
    
    def _generate_images(self, project: VideoProject):
        """画像生成ステップ"""
        pass
    
    def _generate_voices(self, project: VideoProject, languages: List[str]):
        """音声生成ステップ"""
        pass
    
    def _compose_videos(self, project: VideoProject, languages: List[str]):
        """動画合成ステップ"""
        pass
```

---

## 5. 設定管理

### 5.1 Config クラス

```python
from dataclasses import dataclass
from pathlib import Path
import os
from dotenv import load_dotenv

@dataclass
class Config:
    """アプリケーション設定"""
    
    # API Keys
    anthropic_api_key: str
    elevenlabs_api_key: str
    fal_api_key: str
    creatomate_api_key: Optional[str] = None
    
    # Voice Settings
    voice_id_ja: str = "EXAVITQu4vr4xnSDxMaL"
    voice_id_en: str = "21m00Tcm4TlvDq8ikWAM"
    
    # Output Settings
    output_dir: Path = Path("./output")
    video_resolution: tuple = (1080, 1920)
    video_fps: int = 30
    target_duration: int = 45
    
    # Processing Settings
    max_retries: int = 3
    retry_delay: float = 1.0
    
    @classmethod
    def from_env(cls) -> 'Config':
        """環境変数から設定を読み込み"""
        load_dotenv()
        
        return cls(
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            elevenlabs_api_key=os.getenv("ELEVENLABS_API_KEY"),
            fal_api_key=os.getenv("FAL_KEY"),
            creatomate_api_key=os.getenv("CREATOMATE_API_KEY"),
            voice_id_ja=os.getenv("ELEVENLABS_VOICE_ID_JA", "EXAVITQu4vr4xnSDxMaL"),
            voice_id_en=os.getenv("ELEVENLABS_VOICE_ID_EN", "21m00Tcm4TlvDq8ikWAM"),
        )
    
    def validate(self) -> List[str]:
        """設定の検証。エラーメッセージのリストを返す"""
        errors = []
        if not self.anthropic_api_key:
            errors.append("ANTHROPIC_API_KEY is required")
        if not self.elevenlabs_api_key:
            errors.append("ELEVENLABS_API_KEY is required")
        if not self.fal_api_key:
            errors.append("FAL_KEY is required")
        return errors
```

### 5.2 環境変数（.env）

```bash
# Required
ANTHROPIC_API_KEY=sk-ant-xxxxx
ELEVENLABS_API_KEY=xxxxx
FAL_KEY=xxxxx

# Optional
CREATOMATE_API_KEY=xxxxx
ELEVENLABS_VOICE_ID_JA=EXAVITQu4vr4xnSDxMaL
ELEVENLABS_VOICE_ID_EN=21m00Tcm4TlvDq8ikWAM
```

---

## 6. エラーハンドリング

### 6.1 カスタム例外

```python
class NewsVideoGeneratorError(Exception):
    """基底例外クラス"""
    pass

class ConfigurationError(NewsVideoGeneratorError):
    """設定エラー"""
    pass

class ScriptGenerationError(NewsVideoGeneratorError):
    """台本生成エラー"""
    pass

class VoiceGenerationError(NewsVideoGeneratorError):
    """音声生成エラー"""
    pass

class ImageGenerationError(NewsVideoGeneratorError):
    """画像生成エラー"""
    pass

class VideoCompositionError(NewsVideoGeneratorError):
    """動画合成エラー"""
    pass

class APIError(NewsVideoGeneratorError):
    """API呼び出しエラー"""
    def __init__(self, service: str, status_code: int, message: str):
        self.service = service
        self.status_code = status_code
        super().__init__(f"{service} API Error ({status_code}): {message}")
```

### 6.2 リトライ処理

```python
from functools import wraps
import time

def retry(max_attempts: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """リトライデコレータ"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except (APIError, requests.RequestException) as e:
                    last_exception = e
                    if attempt < max_attempts - 1:
                        sleep_time = delay * (backoff ** attempt)
                        time.sleep(sleep_time)
            raise last_exception
        return wrapper
    return decorator
```

---

## 7. ロギング

### 7.1 Logger設定

```python
import logging
from pathlib import Path
from datetime import datetime

def setup_logger(
    name: str,
    log_dir: Path = Path("./logs"),
    level: int = logging.INFO
) -> logging.Logger:
    """ロガーをセットアップ"""
    
    log_dir.mkdir(parents=True, exist_ok=True)
    
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    # コンソールハンドラ
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_format = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    console_handler.setFormatter(console_format)
    
    # ファイルハンドラ
    log_file = log_dir / f"{datetime.now().strftime('%Y%m%d')}.log"
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(level)
    file_format = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    file_handler.setFormatter(file_format)
    
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    
    return logger
```

---

## 8. CLI インターフェース

### 8.1 main.py

```python
import argparse
from config import Config
from src.pipeline import Pipeline

def main():
    parser = argparse.ArgumentParser(
        description='ニュース動画自動生成システム'
    )
    
    parser.add_argument(
        'topic',
        nargs='?',
        help='ニューストピック（指定しない場合は対話モード）'
    )
    
    parser.add_argument(
        '--languages', '-l',
        nargs='+',
        default=['ja', 'en'],
        choices=['ja', 'en'],
        help='生成する言語（デフォルト: ja en）'
    )
    
    parser.add_argument(
        '--output', '-o',
        type=str,
        default='./output',
        help='出力ディレクトリ'
    )
    
    parser.add_argument(
        '--skip-voice',
        action='store_true',
        help='音声生成をスキップ'
    )
    
    parser.add_argument(
        '--skip-video',
        action='store_true',
        help='動画合成をスキップ'
    )
    
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='詳細ログを出力'
    )
    
    args = parser.parse_args()
    
    # 設定読み込み
    config = Config.from_env()
    errors = config.validate()
    if errors:
        for error in errors:
            print(f"❌ {error}")
        print("\n.envファイルを確認してください。")
        return 1
    
    # トピック取得
    if args.topic:
        topic = args.topic
    else:
        print("ニューストピックを入力してください（Ctrl+Dで終了）:")
        topic = input().strip()
    
    if not topic:
        print("トピックが指定されていません。")
        return 1
    
    # パイプライン実行
    pipeline = Pipeline(config)
    
    try:
        result = pipeline.run(
            news_topic=topic,
            languages=args.languages
        )
        
        print("\n✅ 生成完了!")
        print(f"   出力: {result.get_output_dir()}")
        
    except Exception as e:
        print(f"\n❌ エラー: {e}")
        return 1
    
    return 0

if __name__ == "__main__":
    exit(main())
```

---

## 9. テスト仕様

### 9.1 ユニットテスト

```python
# tests/test_script_generator.py
import pytest
from src.generators.script_generator import ScriptGenerator

class TestScriptGenerator:
    
    @pytest.fixture
    def generator(self):
        return ScriptGenerator(api_key="test_key")
    
    def test_generate_japanese_script(self, generator, mocker):
        """日本語台本生成テスト"""
        # Claude APIをモック
        mock_response = mocker.Mock()
        mock_response.content = [mocker.Mock(text='{"title": "テスト", ...}')]
        mocker.patch.object(generator.client.messages, 'create', return_value=mock_response)
        
        script = generator.generate("テストニュース", language="ja")
        
        assert script.language == "ja"
        assert script.title is not None
        assert len(script.image_prompts) >= 4
    
    def test_generate_english_script(self, generator, mocker):
        """英語台本生成テスト"""
        pass
    
    def test_invalid_response_handling(self, generator, mocker):
        """不正なレスポンスのハンドリングテスト"""
        pass
```

### 9.2 統合テスト

```python
# tests/test_pipeline.py
import pytest
from src.pipeline import Pipeline
from config import Config

class TestPipeline:
    
    @pytest.fixture
    def pipeline(self):
        config = Config.from_env()
        return Pipeline(config)
    
    @pytest.mark.integration
    def test_full_pipeline(self, pipeline, tmp_path):
        """パイプライン全体のテスト"""
        result = pipeline.run(
            news_topic="テストニュース",
            languages=["ja"]
        )
        
        assert result.status == "completed"
        assert result.video_path_ja.exists()
```

---

## 10. 実装優先順位

### Phase 1（MVP）- 必須
1. `config.py` - 設定管理
2. `src/models/` - データモデル
3. `src/generators/script_generator.py` - 台本生成
4. `src/generators/voice_generator.py` - 音声生成
5. `src/generators/image_generator.py` - 画像生成
6. `src/generators/video_composer.py` - FFmpegでの動画合成
7. `src/pipeline.py` - パイプライン制御
8. `main.py` - CLIエントリーポイント

### Phase 2 - 機能拡張
9. `src/collectors/` - ニュース収集
10. Creatomate連携
11. 字幕生成

### Phase 3 - 運用
12. エラー通知
13. Web UI
14. n8n連携
