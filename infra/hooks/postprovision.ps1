# 払い出し後に、アプリが必要とする値を表示する。
#
# API キーを Bicep の output にしていない理由: Bicep/ARM の output は
# デプロイ履歴に平文で保存され、リソースグループの閲覧権限があれば
# 誰でも読める。キーはここで az CLI 経由で取得する。
#
# .env を自動で書き換えることはしない。利用者の設定を勝手に壊したくないので、
# 貼り付ける内容を表示するだけにしている。

$ErrorActionPreference = 'Stop'

$accountName = azd env get-value AZURE_OPENAI_IMAGE_ACCOUNT_NAME
$resourceGroup = azd env get-value AZURE_RESOURCE_GROUP
$endpoint = azd env get-value AZURE_OPENAI_IMAGE_ENDPOINT
$deployment = azd env get-value AZURE_OPENAI_IMAGE_DEPLOYMENT
$capacity = azd env get-value AZURE_OPENAI_IMAGE_CAPACITY

Write-Host ''
Write-Host '=== 画像生成リソースの払い出しが完了しました ===' -ForegroundColor Green
Write-Host ("  リソースグループ : {0}" -f $resourceGroup)
Write-Host ("  アカウント       : {0}" -f $accountName)
Write-Host ("  エンドポイント   : {0}" -f $endpoint)
Write-Host ("  デプロイ名       : {0}" -f $deployment)
Write-Host ("  capacity         : {0} (≒ {0} images/min)" -f $capacity)
Write-Host ''

$key = az cognitiveservices account keys list `
    --name $accountName `
    --resource-group $resourceGroup `
    --query key1 `
    --output tsv

if (-not $key) {
    Write-Host 'キーの取得に失敗しました。az login の状態を確認してください。' -ForegroundColor Red
    exit 1
}

# azd 環境にも入れておく（.azure/<env>/.env は gitignore 済み）
azd env set AZURE_OPENAI_IMAGE_API_KEY $key | Out-Null

# --- 音声合成 (Azure AI Speech) ---
$speechAccount = azd env get-value AZURE_SPEECH_ACCOUNT_NAME
$speechRg = azd env get-value AZURE_SPEECH_RESOURCE_GROUP
$speechRegion = azd env get-value AZURE_SPEECH_REGION

$speechKey = az cognitiveservices account keys list `
    --name $speechAccount `
    --resource-group $speechRg `
    --query key1 `
    --output tsv

if (-not $speechKey) {
    Write-Host 'Speech のキー取得に失敗しました。' -ForegroundColor Red
    exit 1
}

azd env set AZURE_SPEECH_API_KEY $speechKey | Out-Null

Write-Host ''
Write-Host '=== 音声合成リソース ===' -ForegroundColor Green
Write-Host ("  リソースグループ : {0}" -f $speechRg)
Write-Host ("  アカウント       : {0}" -f $speechAccount)
Write-Host ("  リージョン       : {0}" -f $speechRegion)
Write-Host ''

# --- 生成物の保存先 (Blob Storage) ---
# キーは取得しない。共有キー認証を無効にしてあり、アプリは Entra ID
# （az login / マネージド ID）で接続する。
$storageAccount = azd env get-value AZURE_STORAGE_ACCOUNT_NAME
$storageUrl = azd env get-value AZURE_STORAGE_ACCOUNT_URL
$storageContainer = azd env get-value AZURE_STORAGE_CONTAINER

Write-Host '=== 生成物の保存先 ===' -ForegroundColor Green
Write-Host ("  アカウント : {0}" -f $storageAccount)
Write-Host ("  コンテナ   : {0}（生成物） / {1}（OAuth トークン）" -f $storageContainer, (azd env get-value AZURE_TOKEN_CONTAINER))
Write-Host '  認証       : Entra ID（キーなし。共有キー認証は無効）'
Write-Host ''

Write-Host '.env に次の行を追加してください:' -ForegroundColor Yellow
Write-Host ''
Write-Host ("AZURE_OPENAI_IMAGE_ENDPOINT={0}" -f $endpoint)
Write-Host ("AZURE_OPENAI_IMAGE_API_KEY={0}" -f $key)
Write-Host ("AZURE_OPENAI_IMAGE_DEPLOYMENT={0}" -f $deployment)
Write-Host ("AZURE_SPEECH_API_KEY={0}" -f $speechKey)
Write-Host ("AZURE_SPEECH_REGION={0}" -f $speechRegion)
Write-Host ("AZURE_STORAGE_ACCOUNT_URL={0}" -f $storageUrl)
Write-Host ("AZURE_STORAGE_CONTAINER={0}" -f $storageContainer)
Write-Host ("AZURE_TOKEN_CONTAINER={0}" -f (azd env get-value AZURE_TOKEN_CONTAINER))
Write-Host '# ARTIFACT_STORE=blob / TOKEN_STORE=blob にすると Blob に保存する（既定は local）'
Write-Host '# トークンは先に送る: uv run python -m scripts.push_tokens'
Write-Host ''
Write-Host '確認:' -ForegroundColor Yellow
Write-Host '  uv run pytest -q'
Write-Host '  uv run python main.py "テストトピック" -l ja -v'
Write-Host ''
