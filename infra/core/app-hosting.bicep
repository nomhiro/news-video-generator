// アプリを Azure 上で動かすためのホスティング（Container Apps）。
//
// **既定では払い出さない**（main.bicep の deployApp = false）。
//
// Dockerfile は実機で検証済み（ffmpeg 7.1.5、日本語フォントの描画、
// 全ルートの応答、healthcheck が healthy になること）。
// それでも既定を false にしているのは、払い出すと課金が始まるうえ、
// クラウドで動かすには次の未解決事項が残っているため。
//
//   - DB が SQLite で、ファイルはコンテナのファイルシステム上にある。
//     進捗はジョブ表（行）になったが、ファイルが共有されないので
//     minReplicas/maxReplicas は 1 固定のまま。DATABASE_URL を
//     PostgreSQL に向ければ外せる
//   （OAuth トークンは Blob に置けるようになったが、初回の認証は
//     ローカルで行い scripts/push_tokens.py で送る運用が必要）
//
// 解決済み:
//   - 音声合成の資格情報。Azure AI Speech へ移したのでキーだけで足り、
//     マウントするシークレットファイルが無くなった
//   - 生成物の保存先。Blob Storage（core/storage.bicep）に publish する。
//     このマネージド ID に Storage Blob Data Contributor を与えている
//   - 進捗の永続化。ジョブ表に持つので、再起動しても消えない
//
// 有効化する手順:
//   azd env set DEPLOY_APP true
//   azd up
//
// この定義は `az deployment sub what-if` で ARM 側の検証を通してある
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

@description('生成物を置く Blob のアカウント URL。アプリの ARTIFACT_STORE=blob で使う')
param artifactAccountUrl string = ''

@description('生成物を置く Blob コンテナ名')
param artifactContainerName string = 'artifacts'

@description('OAuth トークンを置く Blob コンテナ名')
param tokenContainerName string = 'tokens'

// --- アプリが必要とする設定 ---
//
// キーは @secure() で受け、Container App の secrets に入れて
// secretRef で参照する。@secure() のパラメータは ARM のデプロイ履歴に
// 記録されないので、平文が残る経路が無い。
// （出力に混ぜると履歴に残る。だから output にはしない。）

@description('台本生成の Azure OpenAI エンドポイント')
param scriptEndpoint string

@description('台本生成モデルのデプロイ名')
param scriptDeployment string

@secure()
@description('台本生成の API キー')
param scriptApiKey string

@description('画像生成のエンドポイント（台本生成と別リソース）')
param imageEndpoint string

@description('画像生成モデルのデプロイ名')
param imageDeployment string

@secure()
@description('画像生成の API キー')
param imageApiKey string

@description('音声合成のリージョン')
param speechRegion string

@secure()
@description('音声合成の API キー')
param speechApiKey string

@description('''Container App に渡す CPU コア数。
1.0 では ffmpeg のエンコードが 0.4x speed しか出ず、実測で ffmpeg が
異常終了した（1080x1920 / preset=medium）。
consumption プロファイルは CPU:メモリ = 1:2 の組み合わせしか受け付けない
（0.25/0.5Gi, 0.5/1Gi, 1/2Gi, 2/4Gi, 4/8Gi）。''')
param cpu string = '2.0'

@description('''Container App に渡すメモリ。
ffmpeg のエンコードと画像の同時保持があるため、CPU の2倍を確保する。''')
param memory string = '4Gi'

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
      // キーはここに入れ、env からは secretRef で参照する。
      // env に直接書くと `az containerapp show` の出力に平文で出る。
      secrets: [
        {
          name: 'azure-openai-api-key'
          value: scriptApiKey
        }
        {
          name: 'azure-openai-image-api-key'
          value: imageApiKey
        }
        {
          name: 'azure-speech-api-key'
          value: speechApiKey
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
          env: [
            // --- AI サービス ---
            {
              name: 'AZURE_OPENAI_ENDPOINT'
              value: scriptEndpoint
            }
            {
              name: 'AZURE_OPENAI_API_KEY'
              secretRef: 'azure-openai-api-key'
            }
            {
              name: 'AZURE_OPENAI_DEPLOYMENT'
              value: scriptDeployment
            }
            {
              name: 'AZURE_OPENAI_IMAGE_ENDPOINT'
              value: imageEndpoint
            }
            {
              name: 'AZURE_OPENAI_IMAGE_API_KEY'
              secretRef: 'azure-openai-image-api-key'
            }
            {
              name: 'AZURE_OPENAI_IMAGE_DEPLOYMENT'
              value: imageDeployment
            }
            {
              name: 'AZURE_SPEECH_API_KEY'
              secretRef: 'azure-speech-api-key'
            }
            {
              name: 'AZURE_SPEECH_REGION'
              value: speechRegion
            }
            // --- 生成物とトークンの保存先 ---
            // こちらはマネージド ID で認証するのでシークレットが無い
            // （AZURE_CLIENT_ID は公開情報）。
            {
              name: 'ARTIFACT_STORE'
              value: empty(artifactAccountUrl) ? 'local' : 'blob'
            }
            {
              name: 'AZURE_STORAGE_ACCOUNT_URL'
              value: artifactAccountUrl
            }
            {
              name: 'AZURE_STORAGE_CONTAINER'
              value: artifactContainerName
            }
            {
              // トークンもコンテナのファイルシステムには置けない
              // （再起動で消え、YouTube の OAuth はコンテナ内で完了できない）。
              name: 'TOKEN_STORE'
              value: empty(artifactAccountUrl) ? 'local' : 'blob'
            }
            {
              name: 'AZURE_TOKEN_CONTAINER'
              value: tokenContainerName
            }
            {
              // DefaultAzureCredential にどのマネージド ID を使うかを教える。
              // ユーザー割り当て ID では省略できず、省略するとシステム割り当てを
              // 探して認証に失敗する。
              name: 'AZURE_CLIENT_ID'
              value: identity.properties.clientId
            }
            {
              name: 'WEB_HOST'
              value: '0.0.0.0'
            }
          ]
        }
      ]
      scale: {
        // 進捗はジョブ表に持つようになったが、その DB が
        // コンテナ内の SQLite ファイルなのでレプリカ間で共有されない。
        // DATABASE_URL を共有 DB（PostgreSQL）に向けるまでは 1 に固定する。
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

// 生成物の Blob コンテナに RBAC を割り当てるために main.bicep が使う。
output identityPrincipalId string = identity.properties.principalId
