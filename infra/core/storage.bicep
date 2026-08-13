// 生成物（台本・画像・音声・動画）の保存先。
//
// なぜ必要か:
//   コンテナのファイルシステムは再起動で消え、レプリカ間で共有されない。
//   生成した動画が消えるのは単なる不便ではなく、YouTube にアップロード
//   する前に成果物を失うということ。
//
// なぜアカウントキーを使わないか:
//   共有キーはアカウント全体への全権で、失効させる手段がローテーションしか
//   ない。アプリは Entra ID（マネージド ID / az login）で接続するので、
//   共有キー認証そのものを無効にしてある。キーが漏れる経路が存在しなくなる。

@description('リージョン')
param location string

@description('タグ')
param tags object

@minLength(3)
@maxLength(24)
@description('ストレージアカウント名。3〜24文字の英小文字と数字のみ、グローバルに一意')
param accountName string

@description('''作成する Blob コンテナ名。
生成物（artifacts）と OAuth トークン（tokens）を分けている。
トークンは長期の資格情報で、生成物より扱いが重いため、
将来アクセス制御を分けられるようにコンテナを分離しておく。''')
param containerNames array = ['artifacts', 'tokens']

@description('データ面アクセスを与える principal の objectId。空なら割り当てない')
param principalId string = ''

@description('追加でアクセスを与える principal（Container App のマネージド ID など）。空なら割り当てない')
param appPrincipalId string = ''

resource storage 'Microsoft.Storage/storageAccounts@2025-01-01' = {
  name: accountName
  location: location
  tags: tags
  // 生成物は再生成できるので、地理冗長にコストを払う理由がない。
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    // 共有キー認証を止める。これを true のままにすると、キーを知っている者が
    // Entra ID の RBAC を迂回できる。
    allowSharedKeyAccess: false

    // 匿名読み取りを禁止する。動画は公開前の成果物で、URL を知られただけで
    // 読めてよいものではない。
    allowBlobPublicAccess: false

    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled'
    accessTier: 'Hot'
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2025-01-01' = {
  parent: storage
  name: 'default'
  properties: {
    // 誤って消した生成物とトークンを7日間は戻せるようにする。
    // 動画1本の生成には画像6枚ぶんのクォータと数分かかり、
    // トークンを失うとブラウザでの再認証が必要になる。
    deleteRetentionPolicy: {
      enabled: true
      days: 7
    }
  }
}

resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-01-01' = [
  for name in containerNames: {
    parent: blobService
    name: name
    properties: {
      publicAccess: 'None'
    }
  }
]

// Storage Blob Data Contributor: Blob の読み書きと削除ができる。
// アプリは publish（書き）/ list / fetch（読み）を行うため、
// Reader では足りない。
//
// GUID は記憶で書かず、必ず次で確認する。
//   az role definition list --name "Storage Blob Data Contributor" --query "[0].name" -o tsv
// 誤った ID を書くと RoleDefinitionDoesNotExist でデプロイが失敗する（一度踏んだ）。
var blobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

// 開発者本人（az login の principal）。ローカルから blob 保存を試すのに必要。
// アカウント単位ではなくコンテナ単位で与える。将来トークンだけ
// アクセスを絞りたくなったときに、ここを分けるだけで済む。
resource developerAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for (name, i) in containerNames: if (!empty(principalId)) {
    scope: containers[i]
    name: guid(containers[i].id, principalId, blobDataContributorRoleId)
    properties: {
      roleDefinitionId: subscriptionResourceId(
        'Microsoft.Authorization/roleDefinitions',
        blobDataContributorRoleId
      )
      principalId: principalId
    }
  }
]

// Container App のマネージド ID。deployApp のときだけ渡ってくる。
resource appAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for (name, i) in containerNames: if (!empty(appPrincipalId)) {
    scope: containers[i]
    name: guid(containers[i].id, appPrincipalId, blobDataContributorRoleId)
    properties: {
      roleDefinitionId: subscriptionResourceId(
        'Microsoft.Authorization/roleDefinitions',
        blobDataContributorRoleId
      )
      principalId: appPrincipalId
      // マネージド ID は作成直後だと Entra ID に伝播していないことがある。
      // 種別を明示しないと "principal does not exist" で失敗する。
      principalType: 'ServicePrincipal'
    }
  }
]

output accountName string = storage.name
output containerNames array = containerNames

// アプリが AZURE_STORAGE_ACCOUNT_URL に入れる値。
// primaryEndpoints.blob は末尾に `/` が付き、SDK に渡すと URL が
// 二重スラッシュになるため、自分で組み立てる。
output accountUrl string = 'https://${storage.name}.blob.${environment().suffixes.storage}'
