#!/usr/bin/env bash
# 新しいリビジョンが実際にトラフィックを受けるまで待つ。
#
# なぜ要るか
# ----------
# 「打ったつもりで Activating のまま残る」のが一番怖い失敗で、
# `az containerapp update` はリビジョンの作成を要求した時点で返る。
# 待たずに終わると、起動していないのにジョブが緑になる。
#
# 何を見ているか
# --------------
# activeRevisionsMode=Single では、新しいリビジョンが ready になるまで
# 旧リビジョンは非活性化されない。移行中は新旧どちらも active なので、
# `active == true` は準備完了の証拠にならない。
# 「ready（プロビジョニング成功 + スケールアップ + 全レプリカがプローブ通過）」を
# 表すのは `latestReadyRevisionName` と、リビジョン側の `trafficWeight == 100`。
#
# 判定キーはイメージではなく**リビジョン名**にする。タグは commit 由来なので、
# 同じ commit を再デプロイすると旧リビジョンのイメージも一致してしまい、
# 古いリビジョンを見て緑になる。
#
# HTTP で叩く検証は使えない。EasyAuth が unauthenticatedClientAction:
# RedirectToLoginPage なので、無認証のランナーからは 302 しか返らない。
# アプリが死んでいてもミドルウェアが 302 を返すため、何も証明しない。
#
# 使い方:
#     APP=... RG=... wait_for_revision.sh <期待するリビジョン名> <期待するイメージ>
set -euo pipefail

EXPECTED_REVISION="${1:?期待するリビジョン名を引数で渡してください}"
EXPECTED_IMAGE="${2:?期待するイメージを引数で渡してください}"
: "${APP:?APP（Container App 名）が未設定です}"
: "${RG:?RG（リソースグループ名）が未設定です}"

TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-900}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-10}"

# フィールドは1つずつ引く。`--query "[a,b,c]" -o tsv` はフラットな配列を
# 1行1値で返すため位置合わせが必要になり、null が混じると壊れやすい。
# jq には頼らない（ランナーには入っているが、手元で試せない依存を増やしたくない）。
# CR を落としているのは、Windows の az が tsv を CRLF で書くため
# （ランナーは Linux なので不要だが、手元で同じスクリプトを試せるようにしておく）。
app_field() {
	az containerapp show --name "$APP" --resource-group "$RG" --query "$1" -o tsv | tr -d '\r'
}

revision_field() {
	az containerapp revision show \
		--name "$APP" --resource-group "$RG" --revision "$EXPECTED_REVISION" \
		--query "$1" -o tsv | tr -d '\r'
}

revision_exists() {
	az containerapp revision show \
		--name "$APP" --resource-group "$RG" --revision "$EXPECTED_REVISION" \
		--output none 2>/dev/null
}

dump_diagnostics() {
	# revision の JSON をまるごと出す。失敗しているときだけ現れるフィールド
	# （runningStateDetails など）があるため、決め打ちの --query では取り落とす。
	echo "::group::revision の状態"
	az containerapp revision show \
		--name "$APP" --resource-group "$RG" --revision "$EXPECTED_REVISION" || true
	echo "::endgroup::"

	echo "::group::replica の一覧"
	az containerapp replica list \
		--name "$APP" --resource-group "$RG" --revision "$EXPECTED_REVISION" || true
	echo "::endgroup::"

	# アプリ側の例外（実際に踏んだ alembic の
	# `CommandError: Path doesn't exist: /app/migrations` など）は console にしか出ない。
	# ただしクラッシュループで replica が消えると空になるので、system も併せて見る。
	echo "::group::console ログ"
	az containerapp logs show \
		--name "$APP" --resource-group "$RG" --revision "$EXPECTED_REVISION" \
		--type console --tail 200 --follow false || true
	echo "::endgroup::"

	# プローブ失敗・イメージの pull 失敗・シークレットの同期といった
	# プラットフォーム側のイベントはこちら。
	# system ログは --revision / --replica / --container を受け付けない
	# （`--type: --container, --replica, and --revision not supported for system logs`）。
	# アプリ全体のイベントとして出る。
	echo "::group::system ログ"
	az containerapp logs show \
		--name "$APP" --resource-group "$RG" \
		--type system --tail 300 --follow false || true
	echo "::endgroup::"
}

echo "期待するリビジョン: $EXPECTED_REVISION"
echo "期待するイメージ:   $EXPECTED_IMAGE"

deadline=$((SECONDS + TIMEOUT_SECONDS))

while ((SECONDS < deadline)); do
	ready=$(app_field "properties.latestReadyRevisionName")

	# 作成要求の直後はまだリビジョンが見えない。失敗を待機扱いにする。
	if ! revision_exists; then
		echo "リビジョンがまだ見えない: $EXPECTED_REVISION"
		sleep "$INTERVAL_SECONDS"
		continue
	fi

	image=$(revision_field "properties.template.containers[0].image")
	provisioning=$(revision_field "properties.provisioningState")
	health=$(revision_field "properties.healthState")
	running=$(revision_field "properties.runningState")
	traffic=$(revision_field "properties.trafficWeight")

	echo "ready=$ready provisioning=$provisioning health=$health running=$running traffic=$traffic"

	# 起動に失敗したことが確定した状態。待ち続けても変わらないので即座に落とす。
	if [[ "$provisioning" == "Failed" ]]; then
		echo "リビジョンのプロビジョニングが失敗しました" >&2
		dump_diagnostics
		exit 1
	fi

	# 押し込んだイメージと違うものが動いていたら、そもそも別のデプロイを見ている。
	if [[ "$image" != "$EXPECTED_IMAGE" ]]; then
		echo "リビジョンのイメージが一致しません: $image" >&2
		dump_diagnostics
		exit 1
	fi

	if [[ "$ready" == "$EXPECTED_REVISION" &&
		"$provisioning" == "Provisioned" &&
		"$health" == "Healthy" &&
		"$traffic" == "100" ]]; then
		# 正常時の runningState は RunningAtMaxScale（minReplicas=maxReplicas=1）。
		# Running とは限らないので、ここでは判定に使わず記録だけする。
		echo "OK: $EXPECTED_REVISION が ready になり、トラフィックを100%受けています"
		exit 0
	fi

	sleep "$INTERVAL_SECONDS"
done

echo "${TIMEOUT_SECONDS}秒待っても ready になりませんでした" >&2
dump_diagnostics
exit 1
