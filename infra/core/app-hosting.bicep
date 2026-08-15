// アプリを Azure 上で動かすためのホスティング（Container Apps）。
//
// **既定では払い出さない**（main.bicep の deployApp = false）。
//
// Dockerfile は実機で検証済み（ffmpeg 7.1.5、日本語フォントの描画、
// 全ルートの応答、healthcheck が healthy になること）。
// それでも既定を false にしているのは、払い出すと課金が始まるうえ、
// クラウドで動かすには次の未解決事項が残っているため。
//
//   - ジョブ表は SQLite で、コンテナのローカルディスクにある。
//     Azure Files には置けなかった（SMB 上の SQLite は CREATE TABLE で
//     固まる。実測済み）。そのためリビジョン更新でジョブの履歴と
//     実行待ちは消え、minReplicas/maxReplicas も 1 固定のまま。
//     DATABASE_URL を PostgreSQL に向ければ両方とも解決する
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

// --- 公開エンドポイントの保護 ---
//
// ingress は external: true（インターネットに出る）。無認証で放置すると、
// URL を知っている者が /generate で課金を発生させ、/youtube/upload で
// チャンネルに動画を公開できてしまう（トークンは Blob にあり、
// アプリはそれを使える）。Container Apps 組み込みの認証で塞ぐ。

@description('Entra ID アプリ登録のクライアントID。空なら認証を設定しない')
param authClientId string = ''

@secure()
@description('Entra ID アプリ登録のクライアントシークレット')
param authClientSecret string = ''

@description('テナントID。認証の発行者URLに使う')
param authTenantId string = ''

@description('''アクセスを許可する principal の objectId。
アプリ登録は単一テナント（AzureADMyOrg）なので、指定しないと
**テナント内の全員**がサインインできてしまう。個人用のツールなので
自分だけに絞る。''')
param authAllowedPrincipalId string = ''

// --- 状態の永続化（Azure Files） ---

@description('状態を置くファイル共有のストレージアカウント名。空ならマウントしない')
param stateAccountName string = ''

@description('ファイル共有名')
param stateShareName string = 'data'

@description('''コンテナ内のマウント先。
ここに置くのは記事の JSON（NEWS_DATA_DIR）だけ。
ジョブ表（SQLite）は SMB 上で動かないためローカルディスクに置く。''')
param stateMountPath string = '/app/data'

// --- 定期実行 ---

@description('''毎日の自動生成を有効にするか。
クラウドでは既定で有効。アプリ側の既定は無効
（ローカル開発中に勝手に生成が走って課金するのを防ぐため）。''')
param scheduleEnabled bool = true

@description('実行時刻（scheduleTimezone のローカル時刻、HH:MM）')
param scheduleTime string = '06:30'

@description('実行時刻の基準タイムゾーン')
param scheduleTimezone string = 'Asia/Tokyo'

@description('''毎日作る形式（カンマ区切り）。
既定はショートのみ。長尺は当面作らない（CLAUDE.md「長尺は当面作らない」を参照）。''')
param scheduleFormats string = 'short'

@description('''形式ごとに何件の記事を対象にするか。
画像生成のクォータ（capacity 4 ≒ 4 images/min）が律速なので、
増やす前にクォータを上げる。''')
param scheduleArticlesPerFormat int = 1

@description('''動かすコンテナイメージ。既定はプレースホルダ。
実イメージは CD（.github/workflows/deploy.yml）が
ghcr.io/nomhiro/news-video-generator/web:gh-<短縮sha> に差し替える。

**CD は azd env を更新しない**ので、provision の前に現行イメージを
SERVICE_WEB_IMAGE_NAME に入れておく必要がある。入れ忘れると
quickstart イメージ（8080 待ち受け）のリビジョンが作られ、プローブが
通らず Activating のまま残る（一度踏んだ）。手順は CLAUDE.md にある。''')
param containerImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

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

// イメージの置き場所は GHCR（ghcr.io/nomhiro/news-video-generator/web）。
// ここにレジストリは作らない。GitHub の外にもう1つレジストリを持つ理由が無く、
// Basic ACR は使わなくても課金が続く（Issue #15）。
//
// GHCR のパッケージは public にしてあるので、Container App の configuration に
// registries を書く必要も、pull 用の資格情報も要らない。private にすると
// リビジョン作成時とレプリカ再起動時の pull に長期の PAT が必要になる。

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

// ファイル共有のアカウント。キーはここで取る。
//
// キーを main.bicep から渡さない理由: `deployApp ? listKeys(...) : ''` の
// 形にすると ARM が両方の分岐を評価し、deployApp が false のときに
// 存在しないリソースへの listKeys で "A referenced resource was not found"
// になる（一度踏んだ）。条件付きモジュールの内側で取れば、その分岐が
// 選ばれたときにしか評価されない。
//
// output にもしない。ARM の output はデプロイ履歴に平文で残る。
resource stateStorage 'Microsoft.Storage/storageAccounts@2025-01-01' existing =
  if (!empty(stateAccountName)) {
    name: stateAccountName
  }

// Container Apps 環境に共有を登録する。Container App 側は
// この名前（volumes[].storageName）で参照する。
resource envStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' =
  if (!empty(stateAccountName)) {
    parent: containerAppsEnvironment
    name: 'state'
    properties: {
      azureFile: {
        accountName: stateAccountName
        accountKey: stateStorage!.listKeys().keys[0].value
        shareName: stateShareName
        // 書き込む（記事の選択状態とジョブ表）
        accessMode: 'ReadWrite'
      }
    }
  }

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${take(envSlug, 40)}-${resourceToken}'
  location: location
  tags: tags
}

// レジストリが GHCR（public）になったため、AcrPull のロール割当は要らない。
// マネージド ID は Blob（生成物とトークン）へのアクセスにだけ使う。

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
      // registries は書かない。GHCR の public パッケージは匿名で pull できる。
      // キーはここに入れ、env からは secretRef で参照する。
      // env に直接書くと `az containerapp show` の出力に平文で出る。
      secrets: concat(
        [
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
        ],
        empty(authClientId)
          ? []
          : [
              {
                name: 'auth-client-secret'
                value: authClientSecret
              }
            ]
      )
    }
    template: {
      containers: [
        {
          name: 'web'
          image: containerImage
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          volumeMounts: empty(stateAccountName)
            ? []
            : [
                {
                  volumeName: 'state'
                  mountPath: stateMountPath
                }
              ]
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
              name: 'NEWS_DATA_DIR'
              value: '${stateMountPath}/news'
            }
            {
              // ジョブ表は**マウントの外**（コンテナのローカルディスク）に置く。
              //
              // Azure Files（SMB）の上の SQLite は使えなかった。journal_mode を
              // DELETE にしても CREATE TABLE で固まり、リビジョンが
              // Activating のまま起動しない（同じイメージでローカルディスクに
              // 向けると25秒で起動する、という切り分けまで実測した）。
              //
              // 引き換えに、リビジョン更新や再起動でジョブの履歴と実行待ちは
              // 消える。記事の選択状態（共有に置いた JSON）は残るので、
              // 選び直しは要らない。ジョブまで永続化したいなら
              // DATABASE_URL を PostgreSQL に向ける。
              name: 'DATABASE_URL'
              value: 'sqlite:////app/state/newsvideo.db'
            }
            // --- 定期実行 ---
            // 生成までを自動で行い、YouTube への公開はしない
            // （人が確認して押す。収益化では「再利用されたコンテンツ」
            //  ポリシーがリスクなので、目視の価値が大きい）。
            {
              name: 'SCHEDULE_ENABLED'
              value: string(scheduleEnabled)
            }
            {
              name: 'SCHEDULE_TIME'
              value: scheduleTime
            }
            {
              name: 'SCHEDULE_TIMEZONE'
              value: scheduleTimezone
            }
            {
              name: 'SCHEDULE_FORMATS'
              value: scheduleFormats
            }
            {
              name: 'SCHEDULE_ARTICLES_PER_FORMAT'
              value: string(scheduleArticlesPerFormat)
            }
            {
              name: 'WEB_HOST'
              value: '0.0.0.0'
            }
          ]
        }
      ]
      // 状態の置き場所。マウントしないと、リビジョン更新のたびに
      // 記事の選択状態と実行中のジョブが消える。
      volumes: empty(stateAccountName)
        ? []
        : [
            {
              name: 'state'
              storageType: 'AzureFile'
              storageName: envStorage!.name
            }
          ]
      scale: {
        // 進捗はジョブ表に持つようになったが、その DB は
        // コンテナのローカルディスク上の SQLite なので共有されない。
        // DATABASE_URL を共有 DB（PostgreSQL）に向けるまでは 1 に固定する。
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

// Container Apps 組み込みの認証（EasyAuth）。
// アプリのコードには一切手を入れず、ingress の手前で止める。
resource auth 'Microsoft.App/containerApps/authConfigs@2024-03-01' =
  if (!empty(authClientId)) {
    parent: containerApp
    name: 'current'
    properties: {
      platform: {
        enabled: true
      }
      globalValidation: {
        // 未認証のリクエストはログインへ飛ばす。
        // 「認証は任意」にすると無認証で全ルートが叩けるままになる。
        unauthenticatedClientAction: 'RedirectToLoginPage'
        redirectToProvider: 'azureactivedirectory'
      }
      identityProviders: {
        azureActiveDirectory: {
          enabled: true
          registration: {
            clientId: authClientId
            // 値そのものではなく、Container App の secrets の名前を指す
            clientSecretSettingName: 'auth-client-secret'
            openIdIssuer: 'https://login.microsoftonline.com/${authTenantId}/v2.0'
          }
          validation: {
            allowedAudiences: [
              'api://${authClientId}'
            ]
            defaultAuthorizationPolicy: empty(authAllowedPrincipalId)
              ? null
              : {
                  // テナント内の全員ではなく、この objectId だけに許可する
                  allowedPrincipals: {
                    identities: [
                      authAllowedPrincipalId
                    ]
                  }
                }
          }
        }
      }
      login: {
        // EasyAuth のトークンストアは無効にする。
        //
        // 有効にすると `SasUrlSettingName for BlobStorage must be set` で
        // デプロイが失敗する（保存先の Blob に SAS URL を要求される）。
        // 生成物用のストレージアカウントは共有キー認証を無効にしてあり
        // SAS を作れないので、有効にするには別の置き場所が必要になる。
        //
        // そこまでする必要がない。ここでの認証は**入口を閉じる**ためだけで、
        // 利用者のトークンでダウンストリームのAPIを呼ぶわけではない
        // （YouTube のトークンはアプリ自身のもので、Blob の tokens
        // コンテナに別途置いている）。セッションは認証クッキーで足りる。
        tokenStore: {
          enabled: false
        }
      }
    }
  }

output containerAppName string = containerApp.name
output containerAppFqdn string = containerApp.properties.configuration.ingress.fqdn
output identityClientId string = identity.properties.clientId

// 生成物の Blob コンテナに RBAC を割り当てるために main.bicep が使う。
output identityPrincipalId string = identity.properties.principalId
