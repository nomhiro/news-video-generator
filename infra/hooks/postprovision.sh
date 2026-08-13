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

# --- 音声合成 (Azure AI Speech) ---
speech_account=$(azd env get-value AZURE_SPEECH_ACCOUNT_NAME)
speech_rg=$(azd env get-value AZURE_SPEECH_RESOURCE_GROUP)
speech_region=$(azd env get-value AZURE_SPEECH_REGION)

speech_key=$(az cognitiveservices account keys list \
    --name "${speech_account}" \
    --resource-group "${speech_rg}" \
    --query key1 \
    --output tsv)

if [ -z "${speech_key}" ]; then
    echo "Speech のキー取得に失敗しました。" >&2
    exit 1
fi

azd env set AZURE_SPEECH_API_KEY "${speech_key}" >/dev/null

echo ""
echo "=== 音声合成リソース ==="
echo "  リソースグループ : ${speech_rg}"
echo "  アカウント       : ${speech_account}"
echo "  リージョン       : ${speech_region}"
echo ""

# --- 生成物の保存先 (Blob Storage) ---
# キーは取得しない。共有キー認証を無効にしてあり、アプリは Entra ID
# （az login / マネージド ID）で接続する。
storage_account=$(azd env get-value AZURE_STORAGE_ACCOUNT_NAME)
storage_url=$(azd env get-value AZURE_STORAGE_ACCOUNT_URL)
storage_container=$(azd env get-value AZURE_STORAGE_CONTAINER)

echo "=== 生成物の保存先 ==="
echo "  アカウント : ${storage_account}"
echo "  コンテナ   : ${storage_container}"
echo "  認証       : Entra ID（キーなし。共有キー認証は無効）"
echo ""

echo ".env に次の行を追加してください:"
echo ""
echo "AZURE_OPENAI_IMAGE_ENDPOINT=${endpoint}"
echo "AZURE_OPENAI_IMAGE_API_KEY=${key}"
echo "AZURE_OPENAI_IMAGE_DEPLOYMENT=${deployment}"
echo "AZURE_SPEECH_API_KEY=${speech_key}"
echo "AZURE_SPEECH_REGION=${speech_region}"
echo "AZURE_STORAGE_ACCOUNT_URL=${storage_url}"
echo "AZURE_STORAGE_CONTAINER=${storage_container}"
echo "# ARTIFACT_STORE=blob にすると生成物を Blob に保存する（既定は local）"
echo ""
echo "確認:"
echo "  uv run pytest -q"
echo "  uv run python main.py \"テストトピック\" -l ja -v"
echo ""
