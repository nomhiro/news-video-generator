# 画像生成モデルを Imagen 4 Fast に置き換え

## 概要
現在の `Gemini 3 Pro Image` を `Imagen 4 Fast` に置き換え、画像生成コストを85%削減する。

## コスト削減効果
| 項目 | 現在 | 変更後 |
|------|------|--------|
| 1枚あたり | $0.134 | $0.02 |
| 6枚（ショート） | $0.804 | $0.12 |
| **削減率** | - | **85%オフ** |

## 変更対象ファイル
- `src/generators/image_generator.py`

## 実装変更内容

### 1. モデル名の変更
```python
# Before
MODEL = "gemini-3-pro-image-preview"

# After
MODEL = "imagen-4.0-fast-generate-001"
```

### 2. API呼び出し方法の変更
```python
# Before: generate_content()
response = self.client.models.generate_content(
    model=self.MODEL,
    contents=prompt,
    config=types.GenerateContentConfig(
        response_modalities=["IMAGE"],
        image_config=types.ImageConfig(
            aspect_ratio=aspect_ratio,
            image_size=self.DEFAULT_RESOLUTION,
        ),
    ),
)

# After: generate_images()
response = self.client.models.generate_images(
    model=self.MODEL,
    prompt=prompt,
    config=types.GenerateImagesConfig(
        number_of_images=1,
        aspect_ratio=aspect_ratio,
    ),
)
```

### 3. レスポンス処理の変更
```python
# Before: response.candidates[0].content.parts からinline_dataを抽出
if response.candidates and response.candidates[0].content:
    for part in response.candidates[0].content.parts:
        if hasattr(part, 'inline_data') and part.inline_data is not None:
            image = part.as_image()
            image.save(str(output_path))

# After: response.generated_images から直接取得
if response.generated_images:
    response.generated_images[0].image.save(str(output_path))
```

### 4. 解像度設定の削除
- Imagen 4 Fast は最大1408x768（1K相当）のため、`image_size`パラメータは不要
- `DEFAULT_RESOLUTION` 定数を削除または更新

### 5. docstring/コメントの更新
- クラスのdocstringを「Imagen 4 Fast」に更新

## 注意事項
- **解像度制限**: Imagen 4 Fast は最大1K解像度（現在の2Kから低下）
- **品質**: 高速優先のため若干の品質低下の可能性あり
- **レート制限**: 150 req/min（十分な余裕あり）

## 検証方法
1. 単体テスト
   ```bash
   python -c "from src.generators.image_generator import ImageGenerator; g = ImageGenerator(); print('OK')"
   ```

2. 実際の画像生成テスト
   ```bash
   python main.py "テストニュース：AIの最新動向" -l ja
   ```

3. 確認項目
   - 画像が正常に生成されること
   - 9:16（ショート）と16:9（ロング）の両方で動作すること
   - 出力ディレクトリに画像ファイルが保存されること
