// 音声合成用の Azure AI Speech リソース。
//
// なぜ AIServices（Foundry）ではなく専用の SpeechServices か:
//   TTS だけを使うので、多機能リソースにまとめる利点がない。
//   専用にすると、キーの流出時の影響範囲が音声合成だけに閉じる。
//
// なぜ Google Cloud TTS から移行したか:
//   Chirp 3 HD は SSML の <mark> をサポートせず、セグメント境界の
//   タイミングを取得できなかった。そのため文字数比で按分する推定に
//   頼っており、ナレーションと画像の切り替えがずれていた。
//   Azure AI Speech は SSML <bookmark> と BookmarkReached イベントで
//   正確なオフセットを1回の合成で返す。

@description('リージョン。日本語ナレーションが主なので既定は japaneast')
param location string

@description('タグ')
param tags object

@minLength(2)
@maxLength(64)
@description('Speech アカウント名。カスタムサブドメインにも使うためグローバルに一意である必要がある')
param accountName string

@description('データ面アクセスを与える principal の objectId。空なら割り当てない')
param principalId string = ''

resource speech 'Microsoft.CognitiveServices/accounts@2026-07-01' = {
  name: accountName
  location: location
  tags: tags
  kind: 'SpeechServices'
  sku: {
    // F0（無料）はサブスクリプションに1つまでで、同時リクエスト数も
    // 厳しい。動画1本で6〜16セグメントを合成するので S0 にする。
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    // Speech SDK は <name>.cognitiveservices.azure.com のホストを使う。
    // Entra ID 認証に切り替える場合も必須。
    customSubDomainName: accountName

    // キー認証を残す。Speech SDK は Entra ID にも対応するが、
    // 現状のアプリはキーで接続している。
    disableLocalAuth: false

    publicNetworkAccess: 'Enabled'
  }
}

// Cognitive Services Speech User: 音声合成を呼べる最小のロール。
// キー認証を使っている今は必須ではないが、割り当てておくと
// Entra ID 認証へ移すときにインフラ側の変更が不要になる。
//
// GUID は記憶で書かず、必ず次で確認する。
//   az role definition list --name "Cognitive Services Speech User" --query "[0].name" -o tsv
// 誤った ID を書くと RoleDefinitionDoesNotExist でデプロイが失敗する（一度踏んだ）。
var speechUserRoleId = 'f2dc8367-1007-4938-bd23-fe263f013447'

resource speechAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  scope: speech
  name: guid(speech.id, principalId, speechUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      speechUserRoleId
    )
    principalId: principalId
  }
}

output accountName string = speech.name
output accountId string = speech.id
output location string = location

// Speech SDK は「リージョン」または「エンドポイント」で初期化する。
// リージョン指定の方が一般的なので、そちらを主に出力する。
output region string = location
output endpoint string = 'https://${location}.api.cognitive.microsoft.com'
