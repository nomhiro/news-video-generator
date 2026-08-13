// アプリを Azure 上で動かすためのホスティング（Container Apps）。
//
// **既定では払い出さない**（main.bicep の deployApp = false）。
//
// 理由: このアプリは ffmpeg を subprocess で呼び、テキストオーバーレイに
// 日本語フォントを使う。コンテナイメージにそれらを同梱する Dockerfile が
// まだ実機で検証できていない（Docker デーモンが動かせなかった）。
// 動かないイメージのために Container Apps 環境を払い出すと課金だけが
// 発生するため、検証が済むまで無効にしている。
//
// 有効化する手順:
//   1. Dockerfile を書き、`docker build` と `docker run` で
//      ffmpeg と日本語フォントが効くことを確認する
//   2. azd env set DEPLOY_APP true
//   3. azd up
//
// この定義自体は `az deployment sub what-if` で ARM 側の検証を通してある
// （リソースは作成していない）。

@description('リージョン')
param location string

@description('タグ')
param tags object

@description('リソース名に使える形へ正規化した環境名')
param envSlug string

@description('リソース名の一意サフィックス（uniqueString の13文字）')
param resourceToken string

@description('コンテナレジストリ名。長さと文字種の制約は main.bicep 側で担保している')
param registryName string

@description('Container App に渡す CPU コア数')
param cpu string = '1.0'

@description('''Container App に渡すメモリ。
ffmpeg のエンコードと画像の同時保持があるため 2Gi を下限にしている。''')
param memory string = '2Gi'

// ログ。Container Apps 環境は Log Analytics を要求する。
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${take(envSlug, 40)}-${resourceToken}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    // 既定は30日。動画生成のログは長期保持する価値が薄いので最短にする。
    retentionInDays: 30
  }
}

// イメージの置き場所。
resource registry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: registryName
  location: location
  tags: tags
  sku: {
    // Basic で足りる。geo-replication も大きなストレージも要らない。
    name: 'Basic'
  }
  properties: {
    // 管理者ユーザーは無効にし、マネージドIDの AcrPull で引く。
    // 管理者パスワードは共有シークレットになり、失効管理ができない。
    adminUserEnabled: false
  }
}

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-${take(envSlug, 40)}-${resourceToken}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${take(envSlug, 40)}-${resourceToken}'
  location: location
  tags: tags
}

// AcrPull: イメージを引くための最小ロール。
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, identity.id, acrPullRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ca-${take(envSlug, 20)}-${resourceToken}'
  location: location
  tags: union(tags, {
    // azd がどのサービスに対応するコンテナかを識別するためのタグ
    'azd-service-name': 'web'
  })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironment.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: identity.id
        }
      ]
    }
    template: {
      containers: [
        {
          // 初回は azd が実イメージに差し替える。
          // それまでは起動しないプレースホルダで構わない。
          name: 'web'
          image: 'mcr.microsoft.com/k8se/quickstart:latest'
          resources: {
            cpu: json(cpu)
            memory: memory
          }
        }
      ]
      scale: {
        // 動画生成は進捗をプロセスメモリに持つため、
        // 複数レプリカにすると進捗が共有されない。
        // ジョブテーブルへ移すまでは 1 に固定する。
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
  dependsOn: [
    acrPull
  ]
}

output registryLoginServer string = registry.properties.loginServer
output registryName string = registry.name
output containerAppName string = containerApp.name
output containerAppFqdn string = containerApp.properties.configuration.ingress.fqdn
output identityClientId string = identity.properties.clientId
