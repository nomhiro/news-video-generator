# news-video-generator のコンテナイメージ。
#
# 同梱が必須なもの:
#   - ffmpeg / ffprobe : video_composer が subprocess で直接呼ぶ
#   - 日本語フォント    : drawtext のテキストオーバーレイに使う。
#                        入れないと動画合成が「フォントが見つかりません」で失敗する
#   - libssl / libasound2 : Azure Speech SDK のネイティブ依存。
#                        wheel の中身は C++ ライブラリのラッパで、これが無いと
#                        import 時点で OSError になる（Windows の wheel は自己完結）

# ---- ビルドステージ: 依存を解決して仮想環境を作る ----
FROM python:3.13-slim AS builder

# uv は公式イメージからコピーする。pip で入れるより速く、バージョンも固定できる。
COPY --from=ghcr.io/astral-sh/uv:0.9.6 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# 依存だけを先に入れる。ソースの変更でこのレイヤーが無効化されないようにする。
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# ---- 実行ステージ ----
FROM python:3.13-slim AS runtime

# ffmpeg と日本語フォント。
#
# fonts-noto-cjk は約60MB あるが、これが無いと
# テキストオーバーレイが描画できず動画が作れない。
# 実際に入るパスは /usr/share/fonts/opentype/noto/NotoSansCJK-*.ttc で、
# video_composer.JAPANESE_FONT_CANDIDATES がそれを探す。
#
# libssl3 / libasound2 は Azure Speech SDK（azure-cognitiveservices-speech）が
# 動的リンクするもの。python:3.13-slim には入っていない。
# ca-certificates が無いと TLS の検証に失敗する。
RUN apt-get update && apt-get install --no-install-recommends -y \
        ffmpeg \
        fonts-noto-cjk \
        libssl3 \
        libasound2 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# root で動かさない。
RUN useradd --create-home --uid 10001 app

WORKDIR /app

# ビルドステージで作った仮想環境を持ってくる
COPY --from=builder --chown=app:app /app/.venv /app/.venv

# アプリ本体。
# .dockerignore が output/ や .venv/ を除いている。
COPY --chown=app:app config.py main.py web_app.py ./
COPY --chown=app:app src/ ./src/
COPY --chown=app:app templates/ ./templates/
COPY --chown=app:app static/ ./static/

# DB マイグレーション。
# 起動時に `alembic upgrade head` を走らせる（src/storage/schema.py）ので、
# **これが無いと起動に失敗する**。実際に踏んだ:
#   alembic.util.exc.CommandError: Path doesn't exist: /app/migrations
# ローカルでは常に存在するため、コンテナに載せたときだけ露見する。
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app migrations/ ./migrations/

# 運用スクリプト（トークンの移送など）。
COPY --chown=app:app scripts/ ./scripts/

# 生成物とニュースデータの置き場所。
# クラウドでは Blob Storage 等に移す前提だが、それまではコンテナ内に持つ。
RUN mkdir -p /app/output/audio /app/output/images /app/output/videos /app/output/scripts \
             /app/data/news \
    && chown -R app:app /app/output /app/data

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Windows のパスしか無い環境でも確実に日本語フォントを引けるよう明示する
    VIDEO_FONT_PATH=/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8000

USER app

EXPOSE 8000

# ヘルスチェックは /status を使う。
# 動画生成は threadpool で走るので、生成中もこのエンドポイントは応答する
# （応答しなくなる欠陥は修正済み。tests/test_web_background.py が見張っている）。
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/status', timeout=5)"

CMD ["python", "web_app.py", "--host", "0.0.0.0", "--port", "8000"]
