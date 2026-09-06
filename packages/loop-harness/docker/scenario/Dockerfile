# Python 3.12 を取り込むための donor ステージ。apt の python3 は bookworm 標準で 3.11 系
# しか提供せず、リポジトリの `requires-python = ">=3.12"`（3.12 構文のテストを含む）を
# 満たせないため、公式 python イメージから /usr/local 配下のみをサブパス単位でコピーする。
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS python

FROM node:22.17.0-bookworm-slim@sha256:b04ce4ae4e95b522112c2e5c52f781471a5cbc3b594527bcddedee9bc48c03a0

ARG CLAUDE_CODE_VERSION=2.1.207

# node イメージの /usr/local/bin/node・npm 等を壊さないよう、python 関連のサブパスのみを
# コピーする（/usr/local を丸ごと COPY すると node の実行ファイルが失われる恐れがある）。
COPY --from=python /usr/local/bin/python3* /usr/local/bin/
COPY --from=python /usr/local/bin/pip3* /usr/local/bin/
COPY --from=python /usr/local/lib/python3.12 /usr/local/lib/python3.12
COPY --from=python /usr/local/include/python3.12 /usr/local/include/python3.12
COPY --from=python /usr/local/lib/libpython3.12* /usr/local/lib/

RUN ldconfig \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        procps \
    && npm install --global "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && python3 -m pip install --no-cache-dir \
        jsonschema \
        pytest \
        pyyaml \
        ruff==0.15.1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /root/.cache

# /usr/local/bin が /usr/bin より PATH 優先されるため、apt で python3 を追加インストールしない
# 限り `python3` は上記でコピーした 3.12 系に解決される（bash -lc 'command -v python3' で確認可能）。

WORKDIR /workspace

USER 65532:65532

ENTRYPOINT []
