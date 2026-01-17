# Project Constitution

## Project Identity

**Project Name:** News Video Generator  
**Project Type:** CLI Application / Automation Tool  
**Primary Language:** Python 3.11+

## Project Description

ニューストピックを入力として、YouTube Shorts / TikTok向けのショート動画（30-60秒）を自動生成するシステム。
日本語版と英語版の両方を同時に生成する。

## Core Principles

### 1. Simplicity First
- 最小限の依存関係で動作する
- 設定より規約（Convention over Configuration）
- 単一責任の原則に従ったモジュール設計

### 2. Reliability
- 外部API呼び出しには適切なエラーハンドリングとリトライロジックを実装
- 中間ファイルを保存し、途中失敗からの再開を可能に
- 詳細なロギングで問題追跡を容易に

### 3. Extensibility
- 新しいニュースソースの追加が容易
- 新しい言語の追加が可能
- 各コンポーネント（TTS、画像生成等）の差し替えが可能

## Technical Constraints

### Required Technologies
- **Language:** Python 3.11+
- **LLM:** Claude API (Anthropic)
- **TTS:** ElevenLabs API
- **Image Generation:** fal.ai (Flux)
- **Video Composition:** FFmpeg (local)

### Development Standards
- Type hints required for all functions
- Docstrings required (Google style)
- Error messages in Japanese and English
- Logging with emoji prefixes for visibility

### Code Style
- Follow PEP 8
- Maximum line length: 100 characters
- Use `pathlib.Path` for file paths
- Use `dataclasses` for data models

## Project Structure

```
news_video_generator/
├── main.py                    # CLI entry point
├── config.py                  # Configuration management
├── requirements.txt
├── .env.example
├── src/
│   ├── __init__.py
│   ├── pipeline.py           # Pipeline orchestration
│   ├── generators/
│   │   ├── __init__.py
│   │   ├── script_generator.py
│   │   ├── voice_generator.py
│   │   ├── image_generator.py
│   │   └── video_composer.py
│   ├── models/
│   │   ├── __init__.py
│   │   └── script.py
│   └── utils/
│       ├── __init__.py
│       └── logger.py
└── output/
    ├── audio/
    ├── images/
    ├── videos/
    └── scripts/
```

## Quality Gates

### Before Merging
- [ ] All type hints present
- [ ] Docstrings complete
- [ ] Error handling implemented
- [ ] Logging added for key operations
- [ ] Tested with sample news topic

### Definition of Done
- Script generation works for both Japanese and English
- Voice generation produces valid MP3 files
- Image generation produces 4+ images per video
- Video composition produces valid MP4 files
- CLI provides clear feedback on progress and errors

## Dependencies Policy

### Allowed
- anthropic (Claude API)
- requests (HTTP client)
- python-dotenv (env management)

### External Tools Required
- FFmpeg (must be installed on system)

### Not Allowed
- Heavy ML frameworks (PyTorch, TensorFlow) - use APIs instead
- GUI frameworks - CLI only for MVP
