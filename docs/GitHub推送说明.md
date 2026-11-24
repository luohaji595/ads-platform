# 推到 GitHub 的说明（2026-11-24）

本仓库的远端是私有仓库 **https://github.com/luohaji595/ads-platform**（`luohaji595/ads-platform`，`main` 分支）。

## 先说结论：这台机器上 `git push` 走不通

不是配置问题，是网络层。实测（2026-11-24）：

| 目标 | TCP 443 | TLS 握手 | 结果 |
|---|---|---|---|
| `github.com` → `20.247.166.64` | 通 | **必超时** | `git push` 报 `Recv failure: Operation timed out` |
| `github.com` → `140.82.112.4` / `20.205.243.166` | 通 | 部分通 | 多数 IP 直接 `Network is unreachable` |
| `api.github.com` → `20.205.243.168` | 通 | **正常（TLSv1.3）** | `gh api` 一直可用 |
| `codeload.github.com` → `57.155.223.241` | 通 | 正常 | 可下载 |
| `127.0.0.1:7897`（本机代理） | 端口开 | **对外全超时** | 走代理反而更糟 |

所以凡是依赖 `github.com:443` 上 git 智能 HTTP 协议的操作（`push` / `fetch` / `ls-remote`）都会失败；
凡是走 `api.github.com` 的操作（`gh api`）都正常。

SSH 也不行：`git@github.com:22` 和 `ssh.github.com:443` 都能握手，但本机没有私钥，
而当前 token 的 scope 是 `gist, read:org, repo, workflow`，**不含 `admin:public_key`**，传不了公钥。

## 那怎么推：用 Git Data API 自己拼提交

Git 的对象是内容寻址的 —— blob 按字节哈希、tree 按条目哈希、commit 按 tree+父+作者+信息哈希。
所以只要把同样字节按同样路径同样权限写进 GitHub，重建出的 **tree SHA 和 commit SHA 与本地完全一致**，
等于把本地那个提交原样搬上去，不产生分叉。

脚本：`tools/push_via_api.py`

```bash
# 只看会传什么，不写远端
python3 tools/push_via_api.py --dry-run

# 真推（默认推到 luohaji595/ads-platform）
python3 tools/push_via_api.py
```

它做四件事：

1. `POST /git/blobs` —— 每个文件一个 blob（base64），并**校验返回的 blob SHA 与 `git ls-files -s` 里的 SHA 一致**，不一致立刻停（防止传了半截）。
2. `POST /git/trees` —— 一棵树装全部路径（GitHub 允许同一棵树里写带 `/` 的嵌套路径）。
3. `POST /git/commits` —— 沿用本地的 author / committer / 时间戳 / 提交信息 / 父提交。
4. `POST /git/refs` 建 `refs/heads/main`；已存在就 `PATCH` 强制更新。

两个前置坑（脚本已处理）：

- **完全空的仓库不能建 blob**，`GET /git/ref` 和 `POST /git/blobs` 都返回 `409 Git Repository is empty`。
  脚本会先用 Contents API 塞一个 `.gitkeep` 占位提交把 `main` 开出来，之后 force 更新 ref，
  那个占位提交变成悬挂对象，不进 `main` 的历史。
- **代理环境变量**会让 urllib 走 7897 端口然后全超时。脚本内部用空 `ProxyHandler` 直连，
  保险起见命令行也 `env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy` 包一层。

## 已经推上去的那一次（可复核）

```bash
gh api repos/luohaji595/ads-platform --jq '{full_name,private,default_branch}'
# {"full_name":"luohaji595/ads-platform","private":true,"default_branch":"main"}

gh api repos/luohaji595/ads-platform/git/ref/heads/main --jq .object.sha
# 832d3f96582a3799f1244a50587128489939fc04

gh api repos/luohaji595/ads-platform/git/commits/832d3f96582a3799f1244a50587128489939fc04 \
  --jq '{tree:.tree.sha, parents:[.parents[].sha], message:(.message|split("\n")[0])}'
# {"tree":"10362a629445ec31926c0df5c251619377b10369","parents":[],"message":"初始提交：广告自动化平台（工作台 v1.0 落地）"}

gh api "repos/luohaji595/ads-platform/git/trees/main?recursive=1" \
  --jq '[.tree[]|select(.type=="blob")]|length'
# 132
```

本地 `git log` 的 tree 是 `10362a629445…`、commit 是 `832d3f96…`，与远端逐位相同。
文件字节另有抽样校验：`run_agents.py` / `engine/rules.py` / `.gitignore` 三个文件
远端 `contents` 解 base64 后的 SHA-256 与本地一致。

## 以后要再推

改完代码本地正常 `git commit`，然后跑一次 `python3 tools/push_via_api.py` 就行 ——
它会读当前 `HEAD` 的元数据重建，父提交也照抄，所以增量提交一样能对上 SHA。

`git fetch` / `git pull` 目前用不了（同样卡在 443）。本地已经手工建了
`refs/remotes/origin/main`，所以 `git status` 能正常显示「与 origin/main 一致」，
但这个引用是手工 `git update-ref` 写进去的，**不会自动更新** —— 换机器或在网络恢复后，
用 `git fetch origin` 重新同步一次即可。

## 网络恢复后再考虑

- `git config --global --unset url.https://ghfast.top/https://github.com/.insteadOf`
  已于 2026-11-24 清掉（`ghfast.top` 已 DNS 失败，留着会让所有 https 访问绕道死镜像）。已清。
- 若要恢复标准 `git push`：给 gh 补 `admin:public_key` scope 上传 SSH key 走 `ssh.github.com:443`，
  或有 sudo 时把 `github.com` 指到可用边缘 IP 写进 `/etc/hosts`。
