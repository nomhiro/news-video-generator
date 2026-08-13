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

Write-Host '.env に次の3行を追加してください:' -ForegroundColor Yellow
Write-Host ''
Write-Host ("AZURE_OPENAI_IMAGE_ENDPOINT={0}" -f $endpoint)
Write-Host ("AZURE_OPENAI_IMAGE_API_KEY={0}" -f $key)
Write-Host ("AZURE_OPENAI_IMAGE_DEPLOYMENT={0}" -f $deployment)
Write-Host ''
Write-Host '確認:' -ForegroundColor Yellow
Write-Host '  uv run python -m scripts.verify_image_generation'
Write-Host ''
