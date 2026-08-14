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

@description('''音声合成（Azure AI Speech）のリージョン。
日本語ナレーションが主なので japaneast が既定。
画像生成と違いクォータの制約が無いため、レイテンシで選べる。''')
param speechLocation string = 'japaneast'

@description('''音声合成リソースを置くリソースグループ名。
画像生成とはリージョンも用途も違うため分けている。
画像側の RG 名（rg-<環境名>）は環境名が "img" 由来で、
音声を同居させると名前が実態と合わなくなる。''')
param speechResourceGroupName string = 'rg-newsvideo-speech'

@description('生成物（台本・画像・音声・動画）を入れる Blob コンテナ名')
param artifactContainerName string = 'artifacts'

@description('OAuth トークンを入れる Blob コンテナ名。生成物とは分ける')
param tokenContainerName string = 'tokens'

@description('''アプリのホスティング（Container Apps）も払い出すか。
既定は false。イメージ自体は検証済みだが、進捗状態と OAuth トークンが
まだプロセス／ローカルファイル前提
（infra/core/app-hosting.bicep の冒頭を参照）。''')
param deployApp bool = false

@description('リソースへのデータ面アクセスを与える principal（自分の objectId）。省略時は RBAC を割り当てない')
param principalId string = ''

// --- アプリを動かすための設定（deployApp のときだけ使う） ---
//
// 台本生成の Azure OpenAI は azd の管理外（別プロジェクトの既存リソース）
// なので、値を渡してもらう必要がある。画像生成と音声合成のキーは
// postprovision フックが azd env に入れたものを受け取る。
//
// キーは @secure()。ARM のデプロイ履歴に平文で残らない。

@description('台本生成の Azure OpenAI エンドポイント（deployApp のとき必須）')
param scriptEndpoint string = ''

@description('台本生成モデルのデプロイ名')
param scriptDeployment string = 'gpt-5.1'

@secure()
@description('台本生成の API キー（deployApp のとき必須）')
param scriptApiKey string = ''

@secure()
@description('画像生成の API キー（deployApp のとき必須）')
param imageApiKey string = ''

@secure()
@description('音声合成の API キー（deployApp のとき必須）')
param speechApiKey string = ''

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

// ストレージアカウント名は 3〜24文字の英小文字と数字のみ（ハイフン不可）。
// 'st' (2) + ハイフンを除いた envSlug (最大9) + resourceToken (13) = 最大24。
var storageAccountName = 'st${take(replace(envSlug, '-', ''), 9)}${resourceToken}'

// アカウント URL をここで組み立てる理由:
//   storage モジュールの output を app-hosting に渡すと、
//   storage が app-hosting の principalId を必要とするため循環参照になる。
//   名前は決定的なので、URL は参照せずに組み立てられる。
var storageAccountUrl = 'https://${storageAccountName}.blob.${environment().suffixes.storage}'

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

// 音声合成は用途もリージョンも画像生成と別なので、リソースグループを分ける。
resource speechRg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: speechResourceGroupName
  location: speechLocation
  tags: union(tags, { purpose: 'speech-synthesis' })
}

module speech 'core/speech.bicep' = {
  name: 'speech'
  scope: speechRg
  params: {
    location: speechLocation
    tags: union(tags, { purpose: 'speech-synthesis' })
    // Speech アカウント名は 2〜64文字。
    // 'spch-' (5) + envSlug (最大40) + '-' (1) + resourceToken (13) = 最大59。
    accountName: 'spch-${take(envSlug, 40)}-${resourceToken}'
    principalId: principalId
  }
}

// 生成物の保存先。画像生成と同じリソースグループに置く
// （生成物は動画1本ごとに数十MB。用途が近く、分ける理由がない）。
module storage 'core/storage.bicep' = {
  name: 'storage'
  scope: rg
  params: {
    location: location
    tags: tags
    accountName: storageAccountName
    containerNames: [artifactContainerName, tokenContainerName]
    principalId: principalId
    // Container App のマネージド ID にもアクセスを与える。
    // deployApp が false のときは空文字なので割り当てない。
    appPrincipalId: deployApp ? appHosting!.outputs.identityPrincipalId : ''
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
    artifactAccountUrl: storageAccountUrl
    artifactContainerName: artifactContainerName
    tokenContainerName: tokenContainerName
    scriptEndpoint: scriptEndpoint
    scriptDeployment: scriptDeployment
    scriptApiKey: scriptApiKey
    imageEndpoint: aiFoundry.outputs.endpoint
    imageDeployment: aiFoundry.outputs.imageDeploymentName
    imageApiKey: imageApiKey
    speechRegion: speech.outputs.region
    speechApiKey: speechApiKey
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

output AZURE_STORAGE_ACCOUNT_NAME string = storage.outputs.accountName
output AZURE_STORAGE_ACCOUNT_URL string = storage.outputs.accountUrl
output AZURE_STORAGE_CONTAINER string = artifactContainerName
output AZURE_TOKEN_CONTAINER string = tokenContainerName

output AZURE_SPEECH_RESOURCE_GROUP string = speechRg.name
output AZURE_SPEECH_ACCOUNT_NAME string = speech.outputs.accountName
output AZURE_SPEECH_REGION string = speech.outputs.region

output APP_HOSTING_DEPLOYED bool = deployApp

// azd deploy がイメージの push 先を知るために読む値。
// 出力していないと "could not determine container registry endpoint" で失敗する。
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = deployApp
  ? appHosting!.outputs.registryLoginServer
  : ''
output AZURE_CONTAINER_APP_NAME string = deployApp ? appHosting!.outputs.containerAppName : ''
output SERVICE_WEB_ENDPOINT_URL string = deployApp
  ? 'https://${appHosting!.outputs.containerAppFqdn}'
  : ''
