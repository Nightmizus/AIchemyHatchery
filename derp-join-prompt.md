# 任务：把这台机器接入 rex 的 Tailnet，用北京家宽 DERP 中继

你是一个在**朋友机器上**运行的编码 agent。本文件是你的完整任务说明，包含所有已核实的事实。
按"执行步骤"做完，按"验收标准"自证，最后按"交付报告"格式输出结论。

---

## 0. 一句话目标

让本机加入 rex 的 tailnet `tail7559a9.ts.net`，从而自动使用架在**北京家宽**上的自建 DERP 中继
（region `901 / bjhome`），把 Tailscale 的中继延迟从官方境外节点的 **160–400ms** 降到 **~15ms**。

---

## 1. 为什么是"加入 tailnet"，不是"自己配 derpMap"

**这是硬约束，不要试图绕过。**

服务端 derper 的启动参数里带 `-verify-clients`（已于 2026-09-23 登录服务端核实，见附录 A）。
该选项让 derper 在握手时通过**本机 tailscaled 的 WhoIs** 校验客户端公钥是否属于**同一个 tailnet**。

后果：

- ✅ 加入 `tail7559a9.ts.net` 的节点 → 校验通过，能用这个 DERP。
- ❌ 在**另一个 tailnet** 的 ACL 里手写 derpMap 指向它 → TCP/TLS 能连上，但 DERP 握手后会被服务端关闭，
  表现为"连接反复建立又断开"，且**客户端日志不会明确说是被拒**。这是最容易浪费几小时的坑。
- ❌ Tailscale 的 **node sharing（共享节点）也不行**：被共享进来的节点仍运行在它原本的 tailnet 里，
  用的是**原 tailnet 的 derpMap**，拿不到 901。（rex 的 tailnet 里现有 4 个 `tail9f3ef1.ts.net` 的
  共享节点，它们就用不上这个 DERP——不要把它们当成成功案例。）

所以唯一路径：**本机用 rex 发的邀请链接登录他的 tailnet。**

derpMap 是 tailnet 级的配置，写在 rex 的 ACL 策略文件里。你一旦加入，**零配置自动继承**，
不需要、也**不允许**你在本机写任何 derpMap / 改任何 Tailscale 服务端设置。

---

## 2. 开工前必须先问人类的两件事

**不要跳过。第 1 条如果踩了，会直接掐断这台机器现有的 Tailscale 连接。**

1. **本机当前是否已登录另一个 tailnet？**
   一台机器的一个 tailscaled 实例**只能同时登录一个 tailnet**。
   执行 `tailscale status` 查看。如果已登录别的 tailnet：
   - 加入 rex 的 tailnet 会让本机**从原 tailnet 掉线**，原来靠 Tailscale 的 SSH / 远程桌面 / 内网访问**全部中断**。
   - ⚠️ 如果你**正通过原 tailnet 的 IP SSH 在这台机器上**，执行 `tailscale up` 的瞬间你自己会被踢掉。
     必须先换成局域网 IP 或物理终端，或者用 `nohup` / `screen` 脱离会话。
   - 确认人类接受这个代价后再继续。若不接受，停下来报告——保留双 tailnet 需要跑第二个
     tailscaled 实例（不同 `--socket` / `--statedir` / `--port`），属于本任务范围外。

2. **拿到邀请链接了吗？**
   需要 rex 在 `login.tailscale.com` → Users → **Invite external users** 生成的一次性邀请链接，
   形如 `https://login.tailscale.com/admin/invite/xxxxxxxx`。
   这个链接**只有 rex 能生成**，你和人类都造不出来。没有就停下来管人类要。

---

## 3. 执行步骤

### 3.1 安装 Tailscale（已装则跳过）

- **macOS**：优先 App Store 版或 `brew install --cask tailscale`。
  ⚠️ 如果装的是 `brew install tailscale`（开源 CLI 版），它和 GUI 版**会抢 TUN/SystemExtension**，只能留一个。
- **Linux**：`curl -fsSL https://tailscale.com/install.sh | sh`
- **Windows**：官网 msi，或 `winget install tailscale.tailscale`

版本要求：**≥ 1.60**。低版本对 derpMap 里 `sha256-raw:` 形式的证书指纹支持不完整。
用 `tailscale version` 确认，低了先升级。

### 3.2 加入 tailnet

1. 人类在**本机的浏览器**里打开邀请链接，用他自己的账号（Google/GitHub/Microsoft 均可）接受邀请。
   接受后他成为 `tail7559a9.ts.net` 的一个 external user。
2. 本机执行：

```bash
# 若已登录别的 tailnet，先退出（会掉线，确保你不是在用它的 IP 连着）
tailscale logout

# 登录，会打印一个 auth URL
tailscale up --accept-routes=false
```

3. 把打印出的 auth URL 交给人类，**用他刚接受邀请的那个账号**在浏览器里完成授权。
   ⚠️ 用错账号就会建出一个新的空 tailnet，然后你会发现一切都"看起来正常"但 DERP 还是境外的——
   这是第二个高频坑。授权后回到第 4 节验收，**用 MagicDNS 后缀来确认账号没选错**。
4. rex 可能需要在 admin console 里点一下 **Approve**（若 tailnet 开了 device approval）。
   `tailscale status` 停在 `Logged out` 或节点不出现，就是在等他批准。

### 3.3 不要做的事

- ❌ 不要在本机写 `--login-server`、不要碰 `/etc/default/tailscaled`、不要手写 derpMap。
- ❌ 不要去改 rex 的 ACL 策略文件——你没权限，也不需要。
- ❌ 不要在本机另装一个 derper。
- ❌ 不要试图给自签证书装 CA 信任链（见附录 B，靠的是指纹不是信任链）。
- ❌ 不要用 `--exit-node` 或 `--advertise-exit-node`，除非人类明确要求。

---

## 4. 验收标准（三条全过才算成功）

**警告：`tailscale ping` 能通、能 SSH 通，都 _不_ 说明走上了 901。**
必须逐条验证下面三项，并把原始输出贴进交付报告。

### ✅ 判据 1：确实进了正确的 tailnet

```bash
tailscale status --json | python3 -c "import sys,json;d=json.load(sys.stdin);print('suffix:',d.get('MagicDNSSuffix'));print('self:',d['Self'].get('DNSName'))"
```

**必须输出 `suffix: tail7559a9.ts.net`。**
如果是别的后缀（例如 `tailXXXXXX.ts.net`），说明 3.2 步骤 3 用错账号了，
`tailscale logout` 重来，并让人类确认他用的是接受邀请的那个账号。

### ✅ 判据 2：derpMap 里拿到了 901

```bash
tailscale debug derp-map | python3 -c "import sys,json;r=json.load(sys.stdin)['Regions'].get('901');print(json.dumps(r,indent=2,ensure_ascii=False) if r else 'MISSING 901')"
```

**必须输出一个含 `derp-home.insightmap.top` 的 region**，且带：

```json
{
  "RegionID": 901,
  "RegionCode": "bjhome",
  "RegionName": "Beijing Home (mini)",
  "Nodes": [
    {
      "Name": "bjhome1",
      "RegionID": 901,
      "HostName": "derp-home.insightmap.top",
      "CertName": "sha256-raw:8c77a759673b513881453787e192c4faad4f02c896147fbb87b1ff0192448236",
      "STUNPort": 3478,
      "DERPPort": 8443
    }
  ]
}
```

输出 `MISSING 901` → 你进的 tailnet 不对（回判据 1），或 rex 改过 ACL，向他确认。

### ✅ 判据 3：本机真的选中了它

```bash
tailscale netcheck
```

**必须满足 `Nearest DERP: Beijing Home (mini)`，且 `bjhome` 那行延迟为个位数到 ~30ms。**

参考值（rex 的 MacBook，北京，2026-09-23 实测）：

```
* Nearest DERP: Beijing Home (mini)
* DERP latency:
    - bjhome: 13.9ms  (Beijing Home (mini))
    - hkg:    161ms   (Hong Kong)
    - nue:    187.3ms (Nuremberg)
```

再看一条实际连接走的中继：

```bash
tailscale status    # 看 peer 行尾的 "relay bjhome" 或 "direct x.x.x.x:port"
tailscale ping <某个peer名>
```

`via DERP(bjhome)` 或 `direct` 都算通过；出现 `via DERP(nue)` / `DERP(hkg)` / `DERP(lax)` 则**未通过**，去第 5 节。

> ⚠️ **如果本机不在中国大陆**：DERP 选择是**按实测延迟自动**的，不是强制的。
> 人在境外时官方节点可能比北京家宽更近，`Nearest DERP` 合理地不是 901——这**不是故障**。
> 此时判据 3 改为：901 出现在 netcheck 列表里且延迟可测即可。据实写进报告，不要为了"过验收"去强行锁定。

---

## 5. 排障表

| 现象 | 大概率原因 | 处理 |
|---|---|---|
| `netcheck` 里 `bjhome` 显示为超时/不出现 | 本机出网被墙/防火墙挡了 TCP 8443 | `curl --noproxy '*' -sk -o /dev/null -w '%{http_code}\n' https://derp-home.insightmap.top:8443/derp/probe`，应返回 `200` |
| 上面这条 curl 超时，但浏览器能开网页 | **本地代理软件（Clash/V2Ray 等）劫持** | 见下方「代理陷阱」 |
| TCP 连得上、DERP 却反复断 | 你还在别的 tailnet 里，被 `-verify-clients` 拒了 | 回第 1 节，必须**加入** tail7559a9，不能手写 derpMap |
| `MISSING 901` | 进错 tailnet | 回判据 1 |
| 节点不出现在 status 里 | tailnet 开了 device approval | 让 rex 在 admin console 点 Approve |
| DNS 解析不出 `derp-home.insightmap.top` | 本机 DNS 被污染或用了不干净的上游 | `dig +short derp-home.insightmap.top @223.5.5.5`，应为 `222.131.205.240` |
| 延迟正常但选了境外 DERP | 本机确实离境外更近，或 netcheck 采样抖动 | 连跑 3 次 netcheck；若稳定选境外且人在国内，报告给 rex |

### 代理陷阱（**最高频的误判来源**）

本地代理会让你把"服务端故障"和"本机测试环境脏"搞混，rex 已经在这上面栽过两次。验证时：

- `curl` **必须**带 `--noproxy '*'`，或先 `export https_proxy= http_proxy= all_proxy=`（大小写两套都要清）。
- 代理开 **TUN 模式**会劫持 UDP，让 STUN/netcheck 结果完全失真。测之前先关 TUN。
- 判断是否被劫持：`netstat -rn | grep -E '^0/1|^128.0/1|198.18'`，有命中就是 TUN 开着。

### 一条命令快速自检

```bash
export https_proxy= http_proxy= all_proxy= HTTPS_PROXY= HTTP_PROXY= ALL_PROXY=
echo "--- DNS ---";        dig +short derp-home.insightmap.top @223.5.5.5
echo "--- TCP 8443 ---";   curl --noproxy '*' -sk --max-time 8 -o /dev/null -w "HTTP %{http_code}\n" https://derp-home.insightmap.top:8443/derp/probe
echo "--- cert fp ---";    echo | openssl s_client -connect derp-home.insightmap.top:8443 2>/dev/null | openssl x509 -noout -fingerprint -sha256
echo "--- tailnet ---";    tailscale status --json | python3 -c "import sys,json;print(json.load(sys.stdin).get('MagicDNSSuffix'))"
echo "--- region 901 ---"; tailscale debug derp-map | python3 -c "import sys,json;print('OK' if '901' in json.load(sys.stdin)['Regions'] else 'MISSING')"
echo "--- netcheck ---";   tailscale netcheck 2>&1 | grep -E 'Nearest|bjhome'
```

证书指纹那行**必须**是：
`8C:77:A7:59:67:3B:51:38:81:45:37:87:E1:92:C4:FA:AD:4F:02:C8:96:14:7F:BB:87:B1:FF:01:92:44:82:36`
对不上 → **停止，立即报告人类**。这意味着要么服务端换了证书，要么你连到了冒充的主机。

---

## 6. 交付报告格式

做完后按此输出，**每条都要贴原始命令输出，不要只写"已完成"**：

```
## 接入结果

- 平台 / Tailscale 版本：
- 判据 1 MagicDNSSuffix：        （贴输出）
- 判据 2 region 901：            （贴输出）
- 判据 3 netcheck Nearest DERP： （贴输出）
- tailscale ping <peer> 结果：   （贴输出）
- 本机地理位置（国内/境外）：

## 与加入前的对比
- 加入前：本机原属 tailnet = ___ / 无
- 加入后是否失去了原 tailnet 的访问：是 / 否 / 不适用

## 未解决项
（如实列出；没有就写"无"）
```

**纪律：不要把失败写成正常值。** 任一判据没过就如实写"未通过"并贴输出，
不要用"基本可用""应该没问题"之类的措辞盖过去。没验证的东西标"未验证"，不要推测。

---

# 附录 A：服务端现状（2026-09-23 登录核实，仅供你判断，**不要去改**）

| 项 | 值 |
|---|---|
| 主机 | Mac mini（tailnet 名 `rexs-mac-mini`，100.108.201.65），macOS 26.4.1 / arm64 |
| 内网 IP | 192.168.1.131 |
| 公网 IPv4 | 222.131.205.240（家宽，会变，靠 DDNS 跟） |
| 公网 IPv6 | 2408:8207:c96:c8d0:… （有 AAAA 记录，部分客户端走 v6 连入） |
| derper 版本 | 1.102.4（`~/go/bin/derper`，go1.27.1 darwin/arm64 本机编译） |
| 常驻方式 | LaunchAgent `com.rex.derper`，`RunAtLoad` + `KeepAlive` |
| DDNS | LaunchAgent `com.rex.derper-ddns`，每 **300 秒**跑 `~/.derper/ddns.py`（阿里云 DNS API） |
| DNS TTL | A 记录 225s |

启动参数（原样）：

```
derper -c ~/.derper/derper.key \
  -hostname=derp-home.insightmap.top \
  -certmode=manual -certdir=~/derper-certs \
  -a :8443 -http-port=-1 \
  -stun -stun-port=3478 \
  -verify-clients
```

# 附录 B：为什么是自签证书，客户端却不用装 CA

证书是**自签**的（subject == issuer == `CN=derp-home.insightmap.top`，2026-09-21 签发，
有效期 10 年到 **2036-09-18**）。

Tailscale 客户端**不走 PKI 信任链**校验它，而是比对 derpMap 里 `CertName` 字段钉死的
`sha256-raw:<指纹>`。所以：

- ✅ 不需要 Let's Encrypt，不需要 acme.sh，**不需要 ICP 备案**，不需要开 80 端口。
- ✅ 客户端不需要安装任何根证书。
- ⚠️ 代价：**服务端一旦重签证书，指纹就变，ACL 里的 `CertName` 必须同步更新**，
  否则全体客户端同时连不上。这是 rex 需要知道的运维约束，不是你的活。

# 附录 C：已知缺陷 —— STUN UDP 3478 对外不通

**2026-09-23 实测确认，且已做对照组排除探测脚本问题：**

- 服务端 derper 确实带 `-stun` 启动，且本机确实在监听 `udp46 *.3478`。
- 但从公网两个不同位置（北京某家宽、阿里云 ECS 182.92.245.176）对
  `222.131.205.240:3478` 发标准 STUN binding request，**均超时**。
- 对照组：同一台机器同一脚本打 `stun.miwifi.com:3478` 和 `stun.chat.bilibili.com:3478`
  **都正常回包**（type=0x0101）⇒ 不是脚本问题，是**家宽路由器没给 UDP 3478 做端口转发**
  （TCP 8443 有转发，UDP 3478 漏了）。
- IPv6 侧是否通**未验证**。

**对你的影响**：derpMap 里那个 `STUNPort: 3478` 目前是死配置。你的节点做 NAT 探测时会
回退到官方 DERP 的 STUN（境外，160ms+），可能让 netcheck 偏慢、NAT 类型判定偏保守，
**但不影响 901 的中继功能本身**（中继走 TCP 8443，是通的）。

所以：**看到 netcheck 里 `PortMapping` 为空、或 `MappingVariesByDestIP: true`，不要以为是你配错了。**
这是已知的服务端侧缺口，修法是 rex 去路由器上补一条 UDP 3478 转发——**不在你的任务范围内**。

# 附录 D：术语澄清（避免走错路）

- **加入 tailnet（Invite external user）** ← 本任务要做的。本机成为 `tail7559a9.ts.net` 的成员，
  继承其 ACL 与 derpMap。
- **共享节点（Share node）** ← **不是**本任务。它只是把单个节点暴露给对方，
  节点仍在原 tailnet 运行、用原 derpMap，**拿不到 901**。
- **自建 derpMap** ← **不是**本任务，且会被 `-verify-clients` 拒。

三者经常在搜索结果里被混为一谈。如果你在网上读到"在 ACL 里加 derpMap 就能用别人的 DERP"，
那是**没开 verify-clients** 的场景，不适用于这里。
