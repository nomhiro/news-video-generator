#!/bin/sh
# 払い出し後に、アプリが必要とする値を表示する。
# 意図は postprovision.ps1 と同じ。詳細はそちらのコメントを参照。
set -eu

account_name=$(azd env get-value AZURE_OPENAI_IMAGE_ACCOUNT_NAME)
resource_group=$(azd env get-value AZURE_RESOURCE_GROUP)
endpoint=$(azd env get-value AZURE_OPENAI_IMAGE_ENDPOINT)
deployment=$(azd env get-value AZURE_OPENAI_IMAGE_DEPLOYMENT)
capacity=$(azd env get-value AZURE_OPENAI_IMAGE_CAPACITY)

echo ""
echo "=== 画像生成リソースの払い出しが完了しました ==="
echo "  リソースグループ : ${resource_group}"
echo "  アカウント       : ${account_name}"
echo "  エンドポイント   : ${endpoint}"
echo "  デプロイ名       : ${deployment}"
echo "  capacity         : ${capacity} (≒ ${capacity} images/min)"
echo ""

key=$(az cognitiveservices account keys list \
    --name "${account_name}" \
    --resource-group "${resource_group}" \
    --query key1 \
    --output tsv)

if [ -z "${key}" ]; then
    echo "キーの取得に失敗しました。az login の状態を確認してください。" >&2
    exit 1
fi

azd env set AZURE_OPENAI_IMAGE_API_KEY "${key}" >/dev/null

echo ".env に次の3行を追加してください:"
echo ""
echo "AZURE_OPENAI_IMAGE_ENDPOINT=${endpoint}"
echo "AZURE_OPENAI_IMAGE_API_KEY=${key}"
echo "AZURE_OPENAI_IMAGE_DEPLOYMENT=${deployment}"
echo ""
echo "確認:"
echo "  uv run python -m scripts.verify_image_generation"
echo ""
