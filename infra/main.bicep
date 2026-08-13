// news-video-generator のインフラ定義（サブスクリプションスコープ）。
//
// azd は環境ごとに1つのリソースグループを作る前提で書いている。
// 画像生成を専用のリソースグループ / Foundry プロジェクトで管理するのが目的。
//
// なぜ画像生成が既存の Azure OpenAI と別リソースなのか:
//   gpt-image-2 のクォータはサブスクリプション単位・リージョン単位で
//   上限 4 であり、eastus2 は既存デプロイ（ai-poc-eastus2-resource の
//   gpt-image-2-1, capacity 4）で使い切っている。新規に払い出せる
//   リージョンが westus3 / swedencentral しかないため、リージョンを分けた。
//   結果として台本生成（eastus2）と画像生成（westus3）でエンドポイントが
//   2つになる。アプリ側は AZURE_OPENAI_IMAGE_ENDPOINT で受ける。

targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('azd の環境名。リソース名とタグに使う')
param environmentName string

@minLength(1)
@description('''AI リソースのリージョン。
gpt-image-2 が使えるのは eastus2 / westus3 / swedencentral のみ
（2026-08 時点で確認済み）。eastus2 はクォータを使い切っているため
既定は westus3。''')
@allowed([
  'westus3'
  'swedencentral'
  'eastus2'
])
param location string = 'westus3'

@description('gpt-image-2 のデプロイ名。モデル名と一致させなくてよいが、一致させた方が混乱が少ない')
param imageDeploymentName string = 'gpt-image-2'

@description('''gpt-image-2 の capacity（≒ images/min）。
サブスクリプションのクォータ上限は各リージョン 4。
それ以上にするには Azure ポータルからの引き上げ申請が必要。''')
@minValue(1)
@maxValue(50)
param imageDeploymentCapacity int = 4

@description('''アプリのホスティング（Container Apps）も払い出すか。
既定は false。Dockerfile を実機で検証できていない状態で払い出すと、
動かないリソースに課金が発生するため。''')
param deployApp bool = false

@description('リソースへのデータ面アクセスを与える principal（自分の objectId）。省略時は RBAC を割り当てない')
param principalId string = ''

// azd の慣習に合わせたタグ。azd がリソースを環境に紐付けるのに使う。
var tags = {
  'azd-env-name': environmentName
  application: 'news-video-generator'
  purpose: 'image-generation'
}

// 一意なサフィックス（uniqueString は常に13文字）。
// リソース名がグローバルに一意である必要のあるもの（AIServices の
// カスタムサブドメイン、ACR）に使う。
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))

// リソース名に使える形へ正規化する。
// AIServices のアカウント名はカスタムサブドメインになるため小文字のみ。
// ACR は英数字のみでハイフンを許さない。
var envSlug = toLower(replace(replace(environmentName, '_', '-'), ' ', '-'))

// AIServices アカウント名は 2〜64文字。
// 'aif-' (4) + envSlug (最大40) + '-' (1) + resourceToken (13) = 最大58。
var aiAccountName = 'aif-${take(envSlug, 40)}-${resourceToken}'

// ACR 名は 5〜50文字の英数字のみ。
// 'cr' (2) + ハイフンを除いた envSlug (最大20) + resourceToken (13) = 15〜35。
var registryName = 'cr${take(replace(envSlug, '-', ''), 20)}${resourceToken}'

resource rg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: 'rg-${envSlug}'
  location: location
  tags: tags
}

module aiFoundry 'core/ai-foundry.bicep' = {
  name: 'ai-foundry'
  scope: rg
  params: {
    location: location
    tags: tags
    accountName: aiAccountName
    projectName: 'proj-image-generation'
    imageDeploymentName: imageDeploymentName
    imageDeploymentCapacity: imageDeploymentCapacity
    principalId: principalId
  }
}

module appHosting 'core/app-hosting.bicep' = if (deployApp) {
  name: 'app-hosting'
  scope: rg
  params: {
    location: location
    tags: tags
    envSlug: envSlug
    resourceToken: resourceToken
    registryName: registryName
  }
}

// azd が .azure/<env>/.env に書き出す値。
// キーは出力しない。Bicep の output はデプロイ履歴に平文で残るため。
// キーは postprovision フックが az CLI で取得する。
output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_OPENAI_IMAGE_ACCOUNT_NAME string = aiFoundry.outputs.accountName
output AZURE_OPENAI_IMAGE_ENDPOINT string = aiFoundry.outputs.endpoint
output AZURE_OPENAI_IMAGE_DEPLOYMENT string = aiFoundry.outputs.imageDeploymentName
output AZURE_OPENAI_IMAGE_PROJECT_NAME string = aiFoundry.outputs.projectName
output AZURE_OPENAI_IMAGE_MODEL string = aiFoundry.outputs.imageModelName
output AZURE_OPENAI_IMAGE_MODEL_VERSION string = aiFoundry.outputs.imageModelVersion
output AZURE_OPENAI_IMAGE_CAPACITY int = imageDeploymentCapacity
output APP_HOSTING_DEPLOYED bool = deployApp
