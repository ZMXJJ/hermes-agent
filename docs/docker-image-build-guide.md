# Hermes Agent Docker 镜像构建与发布指南

本文档说明如何在本地构建 Hermes Agent Docker 镜像，并推送到阿里云 ACR（Alibaba Container Registry）。

适用场景：代码变更后需要发布新版镜像供部署环境拉取。

---

## 1. 前置条件

### 1.1 本地环境

- macOS Apple Silicon（arm64）或 Linux arm64 主机
- Docker Desktop、OrbStack 或其他 Docker 运行时
- Git（用于克隆仓库和获取版本信息）
- 能访问阿里云 ACR 的网络环境（如有代理需求见第 4 节）

验证 Docker 可用：

```bash
docker version
docker buildx version
```

### 1.2 ACR 仓库信息

| 项目 | 值 |
|------|---|
| Registry | `modelbest-registry.cn-beijing.cr.aliyuncs.com` |
| 命名空间 | `openbmb` |
| 仓库名 | `hermes-agent` |
| 完整前缀 | `modelbest-registry.cn-beijing.cr.aliyuncs.com/openbmb/hermes-agent` |

### 1.3 登录 ACR

```bash
docker login modelbest-registry.cn-beijing.cr.aliyuncs.com
```

输入阿里云 ACR 的用户名和密码。登录凭据会缓存在 `~/.docker/config.json`，无需每次登录。

---

## 2. 镜像版本命名规则

根据已有镜像的命名惯例：

```
<项目版本>-supercpm-<日期YYYYMMDD>-build<序号>
```

示例：

| 标签 | 说明 |
|------|------|
| `0.12.0-supercpm-20260620-build01` | 0.12.0 版本，2026-06-20 第 1 次构建 |
| `0.12.0-supercpm-20260620-build02` | 同一天的第 2 次构建 |
| `latest` | 始终指向最新的稳定构建 |

确认当前项目版本号：

```bash
grep '^version' pyproject.toml
# 输出示例：version = "0.12.0"
```

---

## 3. 构建镜像

### 3.1 进入项目根目录

```bash
cd /path/to/hermes-agent
```

### 3.2 确定标签

```bash
# 根据当前日期和版本号生成标签
VERSION=$(grep '^version' pyproject.toml | sed 's/.*"\(.*\)"/\1/')
DATE=$(date +%Y%m%d)
TAG="${VERSION}-supercpm-${DATE}-build01"
REGISTRY="modelbest-registry.cn-beijing.cr.aliyuncs.com/openbmb/hermes-agent"

echo "将构建: ${REGISTRY}:${TAG}"
```

> 如果同一天多次构建，手动递增 `build01` → `build02`。

### 3.3 执行构建

```bash
docker buildx build \
  --platform linux/arm64 \
  -t "${REGISTRY}:${TAG}" \
  -t "${REGISTRY}:latest" \
  --load \
  .
```

参数说明：

| 参数 | 作用 |
|------|------|
| `--platform linux/arm64` | 目标平台为 arm64（匹配 Mac mini 部署环境） |
| `-t ...:<TAG>` | 打版本标签 |
| `-t ...:latest` | 同时更新 latest 标签 |
| `--load` | 构建完成后加载到本地 Docker 中 |

构建过程耗时约 2-5 分钟（取决于网络和缓存），主要步骤：

1. 安装系统依赖（apt-get）
2. npm install + Playwright chromium
3. 前端构建（web dashboard + TUI）
4. Python 虚拟环境 + `uv pip install -e ".[all]"`

### 3.4 验证构建结果

```bash
docker images | grep hermes-agent
```

预期输出（本地未压缩约 6.3-6.4 GB，推送后压缩至约 2.75 GB）：

```
REPOSITORY                                                          TAG                                IMAGE ID       SIZE
modelbest-registry...hermes-agent   0.12.0-supercpm-20260620-build01   e088f1e699f0   6.39GB
modelbest-registry...hermes-agent   latest                             e088f1e699f0   6.39GB
```

---

## 4. 网络代理配置

构建过程中需要从外网下载依赖（npm、pip、apt、Playwright），如果本地有代理（如 Clash Verge），需要在构建命令中传递代理参数。

### 4.1 检查本地代理

```bash
# 查看系统代理设置（macOS）
networksetup -getwebproxy Wi-Fi

# 常见代理地址
# Clash Verge / ClashX: 127.0.0.1:7890 或 127.0.0.1:10808
# V2Ray:               127.0.0.1:10808
```

### 4.2 构建时传递代理

Docker 构建在 VM 中运行，不能直接访问宿主机的 `127.0.0.1`。需使用 `host.docker.internal`（Docker Desktop / OrbStack 均支持）：

```bash
docker buildx build \
  --platform linux/arm64 \
  --build-arg HTTP_PROXY=http://host.docker.internal:10808 \
  --build-arg HTTPS_PROXY=http://host.docker.internal:10808 \
  --build-arg NO_PROXY=localhost,127.0.0.1 \
  -t "${REGISTRY}:${TAG}" \
  -t "${REGISTRY}:latest" \
  --load \
  .
```

### 4.3 推送时使用代理

推送到 ACR 走的是 Docker daemon 的网络。如果 daemon 已配置代理则无需额外设置，否则可通过环境变量指定：

```bash
HTTPS_PROXY=http://127.0.0.1:10808 docker push "${REGISTRY}:${TAG}"
```

> 注意：推送命令使用宿主机的 `127.0.0.1`（不是 `host.docker.internal`），因为 `docker push` 在宿主机进程中执行。

---

## 5. 推送到 ACR

### 5.1 推送版本标签

```bash
docker push "${REGISTRY}:${TAG}"
```

大部分层已存在于远端（`Layer already exists`），只有变更的层需要上传，通常 1-5 分钟完成。

### 5.2 推送 latest 标签

```bash
docker push "${REGISTRY}:latest"
```

由于 `latest` 和版本标签指向同一镜像，所有层都会显示 `Layer already exists`，推送很快。

### 5.3 验证推送结果

```bash
# 查看远端镜像的 digest
docker inspect --format='{{index .RepoDigests 0}}' "${REGISTRY}:${TAG}"
```

也可以在阿里云容器镜像服务控制台中确认：
`容器镜像服务 → 实例列表 → 镜像仓库 → hermes-agent → 镜像版本`

---

## 6. 完整一键脚本

将以下内容保存为脚本，一键完成构建和推送：

```bash
#!/usr/bin/env bash
set -euo pipefail

REGISTRY="modelbest-registry.cn-beijing.cr.aliyuncs.com/openbmb/hermes-agent"
VERSION=$(grep '^version' pyproject.toml | sed 's/.*"\(.*\)"/\1/')
DATE=$(date +%Y%m%d)
BUILD_NUM="${1:-build01}"
TAG="${VERSION}-supercpm-${DATE}-${BUILD_NUM}"

# 代理地址（根据实际情况修改，留空则不使用代理）
PROXY="${DOCKER_BUILD_PROXY:-http://host.docker.internal:10808}"
PUSH_PROXY="${DOCKER_PUSH_PROXY:-http://127.0.0.1:10808}"

echo "=== 构建镜像: ${REGISTRY}:${TAG} ==="

PROXY_ARGS=""
if [ -n "$PROXY" ]; then
    PROXY_ARGS="--build-arg HTTP_PROXY=${PROXY} --build-arg HTTPS_PROXY=${PROXY} --build-arg NO_PROXY=localhost,127.0.0.1"
fi

docker buildx build \
    --platform linux/arm64 \
    ${PROXY_ARGS} \
    -t "${REGISTRY}:${TAG}" \
    -t "${REGISTRY}:latest" \
    --load \
    .

echo "=== 推送版本标签 ==="
HTTPS_PROXY="${PUSH_PROXY}" docker push "${REGISTRY}:${TAG}"

echo "=== 推送 latest 标签 ==="
HTTPS_PROXY="${PUSH_PROXY}" docker push "${REGISTRY}:latest"

echo "=== 完成 ==="
echo "镜像: ${REGISTRY}:${TAG}"
docker inspect --format='Digest: {{index .RepoDigests 0}}' "${REGISTRY}:${TAG}" 2>/dev/null || true
```

使用方式：

```bash
# 默认 build01
./scripts/build-and-push-image.sh

# 指定构建序号
./scripts/build-and-push-image.sh build02

# 不使用代理
DOCKER_BUILD_PROXY="" DOCKER_PUSH_PROXY="" ./scripts/build-and-push-image.sh
```

---

## 7. 通知部署人员

镜像推送成功后，通知负责部署的人员：

1. **新镜像标签**：如 `0.12.0-supercpm-20260620-build01`
2. **拉取命令**：

   ```bash
   docker pull modelbest-registry.cn-beijing.cr.aliyuncs.com/openbmb/hermes-agent:0.12.0-supercpm-20260620-build01
   ```

3. **变更内容摘要**：本次构建包含了哪些改动（对应 git log）
4. **配置变更**：是否需要修改 `config.yaml`（如有新增配置项需说明）

---

## 8. 常见问题

### Q: 构建失败提示网络超时

检查代理配置是否正确。构建容器内需使用 `host.docker.internal` 访问宿主机代理：

```bash
# 验证构建容器能否访问代理
docker run --rm alpine sh -c "wget -qO- http://host.docker.internal:10808 || echo 'proxy unreachable'"
```

### Q: 推送提示 `denied` 或 `unauthorized`

重新登录 ACR：

```bash
docker login modelbest-registry.cn-beijing.cr.aliyuncs.com
```

### Q: 本地磁盘空间不足

清理旧镜像和构建缓存：

```bash
# 删除悬挂镜像
docker image prune -f

# 清理 buildx 缓存
docker buildx prune -f

# 查看磁盘占用
docker system df
```

### Q: 需要构建 amd64 镜像

将 `--platform` 改为 `linux/amd64`。在 Apple Silicon 上会使用 QEMU 模拟，构建速度会显著变慢（约 3-5 倍）：

```bash
docker buildx build --platform linux/amd64 ...
```

### Q: 如何查看已推送的镜像列表

ACR 不支持 `docker search`，需通过控制台或 API 查看：

```bash
# 通过阿里云 CLI（需安装 aliyun-cli）
aliyun cr GetRepoTags --RepoNamespace openbmb --RepoName hermes-agent
```
