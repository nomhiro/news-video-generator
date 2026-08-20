"""テスト用のモデルファクトリ。

複数のテストファイルが同じ台本（`ScriptDraft` の最小の有効な payload）を
必要とするため、payload を1箇所に持つ。以前は `tests/test_script_model.py`
の `_draft` にしか無く、`scenes` を必須フィールドにした際に他のテスト
ファイルへ同じ ~25行を複製しかけたため、ここに引き上げた。
"""

from src.models.script import ScriptDraft


def make_draft(**overrides: object) -> ScriptDraft:
    """検証を通る最小の下書きを作り、必要な項目だけ差し替える。"""
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
        "illustration_concept": {
            "subject": "a router directing each input to one of several stores",
            "key_details": ["a small switch block", "several identical stores behind it"],
            "labels": ["入力", "切替"],
        },
        "segment_narrations": ["文A。", "文B。", "文C。"],
        "scenes": [
            {"layout": "compare", "items": ["従来", "新方式"], "relation": "切替"},
            {"layout": "flow", "items": ["入力", "選択"], "relation": "変換"},
            {"layout": "statement", "items": [], "relation": ""},
        ],
    }
    payload.update(overrides)
    return ScriptDraft.model_validate(payload)
