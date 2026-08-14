// 実行時の状態（記事の選択状態・ジョブ表）を置くファイル共有。
//
// なぜ生成物の Blob と**別のストレージアカウント**なのか:
//   Container Apps の Azure Files マウントは SMB で、アカウントキーを
//   要求する。生成物とトークンを置いているアカウントは
//   allowSharedKeyAccess: false にしてあり、そこを緩めると
//   「キーを知っていれば RBAC を迂回できる」状態に戻ってしまう。
//   キーが必要な用途だけを別アカウントに隔離する。
//
//   結果として、キーが漏れても失うのは「記事の選択状態とジョブの履歴」
//   だけになり、生成物と OAuth トークンには届かない。
//
// なぜ永続化するのか:
//   コンテナのファイルシステムはリビジョン更新や再起動で消える。
//   生成物とトークンは Blob にあるので残るが、記事の選択状態と
//   生成済みフラグ、実行中のジョブは消える。運用していると
//   リビジョン更新は普通に起きるので、都度選び直すことになる。

@description('リージョン')
param location string

@description('タグ')
param tags object

@minLength(3)
@maxLength(24)
@description('ストレージアカウント名。3〜24文字の英小文字と数字のみ')
param accountName string

@description('ファイル共有名')
param shareName string = 'data'

@description('''共有のクォータ（GiB）。
入るのは記事の JSON（数百KB）と SQLite（数MB）だけなので最小でよい。
Standard の従量課金では使った分だけの課金で、クォータは上限の指定。''')
param shareQuotaGb int = 100

resource storage 'Microsoft.Storage/storageAccounts@2025-01-01' = {
  name: accountName
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    // ここはキー認証が必要（Container Apps の SMB マウントが使う）。
    // だからこそ生成物・トークンとは別アカウントにしている。
    allowSharedKeyAccess: true
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled'
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2025-01-01' = {
  parent: storage
  name: 'default'
  properties: {
    shareDeleteRetentionPolicy: {
      enabled: true
      days: 7
    }
  }
}

resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2025-01-01' = {
  parent: fileService
  name: shareName
  properties: {
    shareQuota: shareQuotaGb
    // TransactionOptimized が Standard 共有の既定。
    // アクセスは小さな読み書きが中心なので、これで足りる。
    accessTier: 'TransactionOptimized'
  }
}

output accountName string = storage.name
output shareName string = share.name
