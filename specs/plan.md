# Technical Plan

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              CLI (main.py)                               │
│                         argparse → Config → Pipeline                     │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         Pipeline (pipeline.py)                           │
│                    Orchestrates all generators                           │
└─────────────────────────────────────────────────────────────────────────┘
         │              │              │              │
         ▼              ▼              ▼              ▼
┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐
│   Script    │ │   Voice     │ │   Image     │ │   Video     │
│  Generator  │ │  Generator  │ │  Generator  │ │  Composer   │
│             │ │             │ │             │ │             │
│ Claude API  │ │ ElevenLabs  │ │  fal.ai     │ │   FFmpeg    │
└─────────────┘ └─────────────┘ └─────────────┘ └─────────────┘
```

---

## Technology Stack

| Layer | Technology | Rationale |
|-------|------------|-----------|
| Language | Python 3.11+ | 豊富なライブラリ、型ヒントサポート |
| LLM | Claude API | 高品質な日本語生成、JSON出力の安定性 |
| TTS | ElevenLabs | 多言語対応、高品質な音声合成 |
| Image | fal.ai (Flux) | 高速生成、高品質、API形式で利用可能 |
| Video | FFmpeg | 業界標準、無料、高機能 |
| Config | python-dotenv | シンプルな環境変数管理 |
| HTTP | requests | 標準的なHTTPクライアント |

---

## Module Design

### 1. config.py - 設定管理

```python
@dataclass
class Config:
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
        """Load from environment variables"""
        
    def validate(self) -> list[str]:
        """Return list of validation errors"""
```

### 2. src/models/script.py - データモデル

```python
@dataclass
class Script:
    language: str           # "ja" or "en"
    title: str
    hook: str
    main_points: list[str]
    conclusion: str
    full_narration: str
    image_prompts: list[str]
    estimated_duration: int
    
    def to_dict(self) -> dict:
        """Convert to dictionary"""
        
    @classmethod
    def from_dict(cls, data: dict) -> 'Script':
        """Create from dictionary"""
        
    def to_json_file(self, path: Path) -> None:
        """Save to JSON file"""
```

### 3. src/generators/script_generator.py - 台本生成

```python
class ScriptGenerator:
    def __init__(self, api_key: str):
        self.client = Anthropic(api_key=api_key)
        self.model = "claude-sonnet-4-20250514"
    
    def generate(self, news_topic: str, language: str = "ja") -> Script:
        """Generate script from news topic"""
        
    def _build_system_prompt(self, language: str) -> str:
        """Build language-specific system prompt"""
        
    def _parse_response(self, response_text: str, language: str) -> Script:
        """Parse API response to Script object"""
```

**プロンプト設計:**
- JSON出力を強制（```json で囲む）
- 言語別のトーン指示
- image_promptsは英語固定
- 具体例を含める

### 4. src/generators/voice_generator.py - 音声生成

```python
class VoiceGenerator:
    BASE_URL = "https://api.elevenlabs.io/v1"
    
    def __init__(self, api_key: str, voice_id_ja: str, voice_id_en: str):
        self.api_key = api_key
        self.voice_ids = {"ja": voice_id_ja, "en": voice_id_en}
    
    def generate(self, text: str, language: str, output_path: Path) -> Path:
        """Generate voice from text"""
        
    def _get_voice_settings(self) -> dict:
        """Return voice settings"""
```

**API仕様:**
- Endpoint: `POST /v1/text-to-speech/{voice_id}`
- Model: `eleven_multilingual_v2`
- Response: `audio/mpeg` binary

### 5. src/generators/image_generator.py - 画像生成

```python
class ImageGenerator:
    BASE_URL = "https://queue.fal.run"
    MODEL = "fal-ai/flux/schnell"
    
    def __init__(self, api_key: str):
        self.api_key = api_key
    
    def generate_batch(self, prompts: list[str], output_dir: Path) -> list[Path]:
        """Generate images from multiple prompts"""
        
    def _submit_request(self, prompt: str) -> str:
        """Submit generation request, return request_id"""
        
    def _poll_result(self, request_id: str, timeout: int = 120) -> dict:
        """Poll for result with timeout"""
        
    def _enhance_prompt(self, prompt: str) -> str:
        """Add quality enhancement to prompt"""
```

**API仕様:**
- Submit: `POST /fal-ai/flux/schnell`
- Status: `GET /fal-ai/flux/schnell/requests/{id}/status`
- Result: `GET /fal-ai/flux/schnell/requests/{id}`

### 6. src/generators/video_composer.py - 動画合成

```python
class VideoComposer:
    def compose(
        self,
        audio_path: Path,
        image_paths: list[Path],
        output_path: Path
    ) -> Path:
        """Compose video from audio and images"""
        
    def _get_audio_duration(self, audio_path: Path) -> float:
        """Get audio duration using ffprobe"""
        
    def _create_filelist(self, image_paths: list[Path], durations: list[float]) -> Path:
        """Create FFmpeg concat filelist"""
        
    def _run_ffmpeg(self, filelist_path: Path, audio_path: Path, output_path: Path) -> None:
        """Run FFmpeg command"""
```

**FFmpegコマンド:**
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

### 7. src/pipeline.py - パイプライン制御

```python
class Pipeline:
    def __init__(self, config: Config):
        self.config = config
        self.script_generator = ScriptGenerator(config.anthropic_api_key)
        self.voice_generator = VoiceGenerator(...)
        self.image_generator = ImageGenerator(config.fal_api_key)
        self.video_composer = VideoComposer()
    
    def run(self, news_topic: str, languages: list[str]) -> dict:
        """Run full pipeline"""
        # 1. Generate scripts
        # 2. Generate images (using first language's prompts)
        # 3. Generate voices for each language
        # 4. Compose videos for each language
        # 5. Return summary
```

### 8. src/utils/logger.py - ロギング

```python
def setup_logger(name: str, verbose: bool = False) -> logging.Logger:
    """Setup logger with emoji prefixes"""
    
def log_step(message: str, emoji: str = "📌") -> None:
    """Log a step with emoji"""
    
def log_success(message: str) -> None:
    """Log success with ✅"""
    
def log_error(message: str) -> None:
    """Log error with ❌"""
```

---

## Error Handling Strategy

### Retry Logic

```python
def retry_with_backoff(func, max_retries=3, base_delay=1.0):
    """Retry with exponential backoff"""
    for attempt in range(max_retries):
        try:
            return func()
        except (RequestException, APIError) as e:
            if attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            time.sleep(delay)
```

### Custom Exceptions

```python
class NewsVideoError(Exception):
    """Base exception"""

class ConfigError(NewsVideoError):
    """Configuration error"""

class ScriptGenerationError(NewsVideoError):
    """Script generation failed"""

class VoiceGenerationError(NewsVideoError):
    """Voice generation failed"""

class ImageGenerationError(NewsVideoError):
    """Image generation failed"""

class VideoCompositionError(NewsVideoError):
    """Video composition failed"""
```

---

## File Structure

### Output Directory Structure

```
output/
├── scripts/
│   ├── 20250115_123456_ja.json
│   └── 20250115_123456_en.json
├── audio/
│   ├── 20250115_123456_ja.mp3
│   └── 20250115_123456_en.mp3
├── images/
│   ├── 20250115_123456_001.png
│   ├── 20250115_123456_002.png
│   ├── 20250115_123456_003.png
│   └── 20250115_123456_004.png
└── videos/
    ├── 20250115_123456_ja.mp4
    └── 20250115_123456_en.mp4
```

### Naming Convention

- Timestamp: `YYYYMMDD_HHMMSS`
- Language suffix: `_ja`, `_en`
- Image index: `_001`, `_002`, etc.

---

## Testing Strategy

### Unit Tests

```
tests/
├── test_config.py
├── test_script_generator.py
├── test_voice_generator.py
├── test_image_generator.py
├── test_video_composer.py
└── test_pipeline.py
```

### Integration Test

```python
def test_full_pipeline():
    """Test complete pipeline with sample topic"""
    config = Config.from_env()
    pipeline = Pipeline(config)
    result = pipeline.run("テストニュース", ["ja"])
    
    assert result["status"] == "success"
    assert Path(result["video_ja"]).exists()
```

---

## Dependencies

### requirements.txt

```
anthropic>=0.40.0
requests>=2.31.0
python-dotenv>=1.0.0
```

### System Requirements

- Python 3.11+
- FFmpeg (installed and in PATH)

---

## Security Considerations

1. **API Keys**: 環境変数のみ、コードにハードコードしない
2. **.gitignore**: `.env`, `output/` を除外
3. **Input Validation**: ユーザー入力をサニタイズ
4. **Error Messages**: APIキーをエラーメッセージに含めない
