// ジョブ表（jobs）と投稿表（social_posts）を置く共有データベース。
//
// なぜ要るのか（Issue #56 / #3）
// -----------------------------
// この2つのテーブルは**コンテナのローカルディスク上の SQLite**にあった。
// Azure Files のマウントは /app/data で /app/state はマウント外なので、
// リビジョンごとに新しい空のディスクになり、起動時の `alembic upgrade head` が
// 空のテーブルを作り直していた。エラーも警告も出ないまま次が起きていた。
//
//   1. X 投稿キューがデプロイで消え、その日の残りの投稿が出ないまま終わる
//      （下書きを積むのは 06:30 の1回だけ。直近24時間で CD は8回走っていた）
//   2. 実行待ちの動画ジョブと履歴が消える
//   3. 予算ガードが効かない（monthly_post_counts が数える POSTED 行が消える）
//
// **下書きを Azure Files に写して復元する案は却下した。** ACA は
// activeRevisionsMode = Single で、新リビジョンが ready になるまで旧リビジョンを
// 落とさない（.github/scripts/wait_for_revision.sh がそれを待っている）。つまり
// デプロイのたびに2つのレプリカが1〜2分同時に走るので、per-replica のコピーを
// 持たせると両方が同じ行を持ち、各自が別のファイルを見るぶん claim の排他が
// 効かず、同じ投稿が二度出る。共有 DB なら claim が1箇所の条件付き UPDATE に
// なるので、この窓は構造的に消える。
//
// **Azure Files（SMB）の上の SQLite に再挑戦しないこと。** journal_mode を
// DELETE にしても CREATE TABLE で固まり、リビジョンが Activating のまま
// 起動しない（実測済み）。
//
// なぜ Cosmos DB ではないのか
// ---------------------------
// 単価はサーバーレスの方が安い（$0.25/100万RU ≒ 月$0.3 に対し、ここは月約$16）。
// それでも採らないのは3つの理由から。
//
//   - **無料レベルはサブスクリプションに1つで、既に別のアカウントが使っている**
//   - プロビジョニングは最小 400 RU/s = 月$23 で、こちらより高い
//   - 安さの代償が「JobRepository / SocialPostRepository と claim の排他を
//     ETag の楽観的同時実行で作り直すこと」＝**二重投稿を防いでいるまさに
//     そのコード**。同じ書き換え量なら Table Storage の方が安い（月$0.01）ので、
//     Cosmos はこの規模でコスパの頂点にならない
//
// PostgreSQL なら src/storage/jobs.py に既に SKIP LOCKED の分岐があり、
// Alembic もテストもそのまま動く。月$16 はそのコードを触らないための費用。
//
// 費用（westus3 / retail、2026-08-23 に Retail Prices API で実測）
//   Burstable B1MS    $0.017/時   ≒ 月$12.4
//   ストレージ 32GB   $0.115/GB月 ≒ 月$3.7
//   バックアップ      ストレージと同容量までは無料
//   Defender for PostgreSQL は**有効にしない**（$15/ノード月）

@description('リソースを作るリージョン')
param location string

@description('リソースに付けるタグ')
param tags object

@description('リソース名に使う環境名のスラグ')
param envSlug string

@description('リソース名に使う一意なトークン')
param resourceToken string

@description('アプリのユーザー割り当て ID の名前。**そのまま PostgreSQL のロール名になる**')
param identityName string

@description('アプリのユーザー割り当て ID のプリンシパル ID（objectId）')
param identityPrincipalId string

@description('アプリが使うデータベース名')
param databaseName string = 'newsvideo'

// サーバー名はグローバルに一意。小文字・数字・ハイフンだけが使える。
var serverName = 'psql-${take(envSlug, 30)}-${resourceToken}'

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: serverName
  location: location
  tags: tags
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: '17'
    // **パスワードを持たない。** administratorLogin を書かないのは
    // passwordAuth を無効にしているため（書くと ARM が矛盾で落ちる）。
    // アプリはマネージド ID で取った Entra のアクセストークンを
    // パスワード欄に渡して接続する（src/storage/db.py を参照）。
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Disabled'
      tenantId: subscription().tenantId
    }
    storage: {
      // 32GB は最小。行は1日数件しか増えないので自動拡張は要らない
      // （拡張は不可逆で、縮められない＝課金が戻らない）。
      storageSizeGB: 32
      autoGrow: 'Disabled'
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      // Burstable は HA 非対応。単一レプリカのアプリなので不要。
      mode: 'Disabled'
    }
    network: {
      // ACA 環境は VNet 統合していない（app-hosting.bicep の
      // managedEnvironments に vnetConfiguration が無い）ので、送信 IP を
      // 固定できない。**VNet 化は環境の作り直しになるので採らない。**
      // 代わりに公開エンドポイント + 下の firewallRule で Azure からの接続だけを
      // 通し、認証は Entra のみにしてある（パスワード認証は無効なので、
      // 接続には当テナントの有効なトークンと一致する DB ロールが必要）。
      publicNetworkAccess: 'Enabled'
    }
  }
}

// アプリが使うデータベース。
//
// テーブルは起動時の `alembic upgrade head` が作る。Azure の PostgreSQL では
// **public スキーマの所有者が全バージョンで azure_pg_admin** であり
// （PG15 以降の pg_database_owner への変更は適用されない）、新規データベースの
// public には既定で全ロールにオブジェクト作成権限があるので、下の Entra 管理者に
// した ID でそのまま作れる。追加の GRANT は要らない。
resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: server
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// Azure のサービスからの接続を許可する（0.0.0.0-0.0.0.0 がその意味の特別な値）。
resource allowAzureServices 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: server
  name: 'AllowAllAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
  // 子リソースは親サーバーを変更する操作なので**直列化する**。並列に走らせると
  // `Another operation is in progress on the resource` で片方が落ちる
  // （Foundry のプロジェクトとモデルデプロイで一度踏んだのと同じ形）。
  dependsOn: [database]
}

// アプリのマネージド ID をサーバーの Entra 管理者にする。
//
// これで `pgaadauth_create_principal` を手で流す工程が消え、
// `alembic upgrade head` がテーブルの所有者になる。
//
// **人を管理者に入れるのはここではやらない。** administrators は User に対して
// principalName（UPN）を要求するが、main.bicep が持っているのは objectId だけ。
// メールアドレスを UPN として流用すると、テナントの UPN と一致しなければ
// provision が落ちるか無効な管理者ができる。必要になったら後から足す:
//
//   az postgres flexible-server ad-admin create -g <rg> -s <server> \
//     --object-id <objectId> --display-name <UPN> --type User
resource appAdmin 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' = {
  parent: server
  name: identityPrincipalId
  properties: {
    // サービスプリンシパルでは**表示名**を渡す（ユーザー割り当て ID の
    // 表示名はリソース名と同じ）。これがそのまま PostgreSQL のロール名になり、
    // 接続 URL の user と一致していなければ
    // `password authentication failed for user "..."` になる。
    principalName: identityName
    principalType: 'ServicePrincipal'
    tenantId: subscription().tenantId
  }
  dependsOn: [allowAzureServices]
}

@description('接続先のホスト名')
output fqdn string = server.properties.fullyQualifiedDomainName

@description('サーバー名')
output serverName string = server.name

@description('データベース名')
output databaseName string = databaseName

@description('接続に使う PostgreSQL のロール名（= マネージド ID の名前）')
output loginName string = identityName
