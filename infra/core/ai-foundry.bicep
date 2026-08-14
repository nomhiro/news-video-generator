// AI Foundry（AIServices）リソースと、画像生成専用のプロジェクト・デプロイ。

@description('リージョン')
param location string

@description('タグ')
param tags object

@minLength(2)
@maxLength(64)
@description('AIServices アカウント名。カスタムサブドメインにも使われるためグローバルに一意である必要がある')
param accountName string

@description('Foundry プロジェクト名')
param projectName string

@description('gpt-image-2 のデプロイ名')
param imageDeploymentName string

@description('gpt-image-2 の capacity')
param imageDeploymentCapacity int

@description('データ面アクセスを与える principal の objectId。空なら割り当てない')
param principalId string = ''

// gpt-image-2 のモデル定義。
// バージョンを固定する理由: 自動更新に任せると、挙動が変わったときに
// 「いつ何が変わったか」を追えない。使用中のモデルとバージョンは
// src/model_registry.py にも記録し、廃止日をテストで見張っている。
var imageModelName = 'gpt-image-2'
var imageModelVersion = '2026-04-21'

resource account 'Microsoft.CognitiveServices/accounts@2026-07-01' = {
  name: accountName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    // /openai/v1 形式のエンドポイントを使うために必須。
    // 設定しないとリージョン共通のエンドポイントになり、
    // openai SDK の base_url 組み立てが成立しない。
    customSubDomainName: accountName

    // Foundry プロジェクトを作るために必要。
    allowProjectManagement: true

    // API キー認証を残す。アプリは現状キーで接続している。
    // Entra ID 認証（DefaultAzureCredential）へ移すなら true にできるが、
    // 台本生成側（別リソース）がキー認証のままなので混在を避けている。
    disableLocalAuth: false

    publicNetworkAccess: 'Enabled'
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2026-07-01' = {
  parent: account
  name: projectName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: '画像生成 (gpt-image-2)'
    description: 'news-video-generator の動画用画像を生成する'
  }
  // プロジェクトとモデルデプロイは、どちらも親アカウントを変更する操作。
  // 依存を書かないと Bicep が並列に実行し、
  // 「Another operation is in progress on the resource」で片方が失敗する
  // （実際に一度踏んだ）。順序はどちらでもよいが直列化は必要。
  dependsOn: [
    imageDeployment
  ]
}

resource imageDeployment 'Microsoft.CognitiveServices/accounts/deployments@2026-07-01' = {
  parent: account
  name: imageDeploymentName
  sku: {
    // GlobalStandard は gpt-image-2 が対応する唯一の SKU
    // （2026-08 時点で確認済み）。
    name: 'GlobalStandard'
    capacity: imageDeploymentCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: imageModelName
      version: imageModelVersion
    }
    // バージョンを自動で上げない。上げると挙動が変わった原因を追えない。
    versionUpgradeOption: 'NoAutoUpgrade'
    raiPolicyName: 'Microsoft.DefaultV2'
  }
}

// Cognitive Services User: 推論 API を呼べる最小のロール。
// キー認証を使っている今は必須ではないが、割り当てておくと
// Entra ID 認証へ移すときにインフラ側の変更が不要になる。
var cognitiveServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'

resource inferenceAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  scope: account
  name: guid(account.id, principalId, cognitiveServicesUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      cognitiveServicesUserRoleId
    )
    principalId: principalId
  }
}

output accountName string = account.name
output accountId string = account.id

// エンドポイントは決定的に組み立てる。
//
// properties.endpoints は 'Azure OpenAI Legacy API - Latest moniker' のような
// スペース入りのキーを持つマップで、キー名の安定性に依存したくない。
// customSubDomainName を accountName にしているので、
// openai.azure.com のホスト名は確定する。
// アプリは このURLに /openai/v1 を足して使う（既存の台本生成と同じ形）。
output endpoint string = 'https://${account.name}.openai.azure.com'
output projectName string = project.name
output imageDeploymentName string = imageDeployment.name
output imageModelName string = imageModelName
output imageModelVersion string = imageModelVersion
