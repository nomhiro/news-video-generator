# Implementation Tasks

## Task Overview

| Phase | Tasks | Status |
|-------|-------|--------|
| Setup | 3 | ✅ |
| Models | 1 | ✅ |
| Generators | 5 | ✅ |
| Pipeline | 1 | ✅ |
| CLI | 1 | ✅ |
| Testing | 1 | ⬜ |
| **Total** | **12** | |

---

## Phase 1: Project Setup

### Task 1.1: Create Project Structure ✅

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
- [X] すべてのディレクトリが作成されている
- [X] `__init__.py` が各Pythonパッケージに存在する
- [X] `.gitkeep` がoutputサブディレクトリに存在する

---

### Task 1.2: Create requirements.txt ✅

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
- [X] ファイルが作成されている
- [X] `pip install -r requirements.txt` が成功する

---

### Task 1.3: Create .env.example ✅

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
- [X] ファイルが作成されている
- [X] すべての必須変数が記載されている
- [X] コメントで説明が記載されている

---

## Phase 2: Data Models

### Task 2.1: Implement Script Model ✅

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
- [X] dataclassとして定義されている
- [X] to_dict() メソッドが動作する
- [X] from_dict() メソッドが動作する
- [X] to_json_file() メソッドが動作する
- [X] from_json_file() メソッドが動作する
- [X] 型ヒントが付いている
- [X] docstringが付いている

---

## Phase 3: Generators

### Task 3.1: Implement Config ✅

**Description:** 設定管理クラスを実装する

**File:** `config.py`

**Acceptance Criteria:**
- [X] from_env() で環境変数から読み込める
- [X] validate() でエラーを検出できる
- [X] デフォルト値が設定されている

---

### Task 3.2: Implement Script Generator ✅

**Description:** Claude APIを使用して台本を生成するクラスを実装する

**File:** `src/generators/script_generator.py`

**Acceptance Criteria:**
- [X] generate() メソッドがScriptを返す
- [X] 日本語と英語の両方に対応している
- [X] JSONパースエラーを適切にハンドリング
- [X] APIエラーを適切にハンドリング

---

### Task 3.3: Implement Voice Generator ✅

**Description:** ElevenLabs APIを使用して音声を生成するクラスを実装する

**File:** `src/generators/voice_generator.py`

**Acceptance Criteria:**
- [X] generate() メソッドがファイルパスを返す
- [X] 日本語と英語の両方に対応している
- [X] MP3ファイルが正しく保存される
- [X] APIエラーを適切にハンドリング

---

### Task 3.4: Implement Image Generator ✅

**Description:** fal.ai APIを使用して画像を生成するクラスを実装する

**File:** `src/generators/image_generator.py`

**Acceptance Criteria:**
- [X] generate_batch() メソッドがファイルパスリストを返す
- [X] 4枚以上の画像を生成できる
- [X] プロンプトが強化されている
- [X] タイムアウト処理がある
- [X] APIエラーを適切にハンドリング

---

### Task 3.5: Implement Video Composer ✅

**Description:** FFmpegを使用して動画を合成するクラスを実装する

**File:** `src/generators/video_composer.py`

**Acceptance Criteria:**
- [X] compose() メソッドがファイルパスを返す
- [X] 出力動画が再生可能
- [X] 解像度が1080x1920
- [X] 音声と画像が同期している
- [X] FFmpegエラーを適切にハンドリング

---

## Phase 4: Pipeline

### Task 4.1: Implement Pipeline ✅

**Description:** 全体のパイプラインを制御するクラスを実装する

**File:** `src/pipeline.py`

**Acceptance Criteria:**
- [X] run() メソッドが結果dictを返す
- [X] 日本語と英語の両方の動画が生成される
- [X] 画像は共通で使い回される
- [X] 中間ファイルがすべて保存される
- [X] エラー時に適切なメッセージが表示される

---

## Phase 5: CLI

### Task 5.1: Implement CLI Entry Point ✅

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

**Acceptance Criteria:**
- [X] `python main.py "トピック"` で実行できる
- [X] `--languages` オプションが動作する
- [X] `--output` オプションが動作する
- [X] `--verbose` オプションが動作する
- [X] エラー時に適切な終了コードを返す

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
- [X] `main.py`
- [X] `config.py`
- [X] `requirements.txt`
- [X] `.env.example`
- [X] `.gitignore`
- [X] `src/__init__.py`
- [X] `src/pipeline.py`
- [X] `src/models/__init__.py`
- [X] `src/models/script.py`
- [X] `src/generators/__init__.py`
- [X] `src/generators/script_generator.py`
- [X] `src/generators/voice_generator.py`
- [X] `src/generators/image_generator.py`
- [X] `src/generators/video_composer.py`
- [X] `src/utils/__init__.py`
- [X] `src/utils/logger.py`

### Functionality
- [X] 台本生成が動作する
- [X] 音声生成が動作する
- [X] 画像生成が動作する
- [X] 動画合成が動作する
- [X] パイプライン全体が動作する
- [X] CLIが動作する

### Quality
- [X] すべての関数に型ヒントがある
- [X] すべての関数にdocstringがある
- [X] エラーハンドリングが実装されている
- [X] ログ出力が実装されている
