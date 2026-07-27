# IP 安全度检测 — 设计文档

**状态**：已确认，待实现
**日期**：2026-07-26
**目标**：在 ai-limit 双端新增 IP 安全度指示（盾牌图标 + 面板卡片），点击跳转 `https://ip.net.coffee/claude/`

---

## 1. 背景与定位

用户希望一眼看出当前网络环境对 Claude 是否安全——尤其关心 **Claude 可用性**、**DNS 泄露**、**WebRTC 泄露**。

参考站点 `ip.net.coffee/claude/` 提供这些检测，但**没有公开 API 文档**。本设计的做法是：**复刻该站点自己调用的接口**，拿到与网页显示同源的数据，而不是另找第三方服务自行判定。

### 已验证的事实（2026-07-26 实测）

扒取页面源码后确认其数据链路，并逐个实测：

| 接口 | 用途 | 实测结果 |
|---|---|---|
| `https://claude.ai/cdn-cgi/trace` | Claude 视角的出口 IP + 可用性 | ✅ `ip=203.0.113.42 loc=US colo=SJC warp=off` |
| `https://ip.net.coffee/api/iprisk/<ip>` | IP 风险判定（信任分原始数据） | ✅ `is_datacenter=true, is_vpn/proxy/tor/abuser=false, abuser_score="0.0039 (Low)", company_type="hosting"` |
| `https://ip.net.coffee/api/geoip/<ip>` | 地理与 ISP | ✅ `United States / California / San Jose / Example Networks Inc` |
| `https://ip.net.coffee/api/dns/result/<token>` | DNS 泄露检测结果 | ✅ 返回 `dns_servers: []`（即"未暴露出口"）|

**DNS 检测机制**（从页面源码逆向）：客户端生成随机 token → 向 `<token>-1.d.ip.net.coffee` 和 `<token>-2.d.ip.net.coffee` 发起解析（页面用 `<img>` 触发，我们用 `socket.getaddrinfo`）→ 该站自建权威 DNS 记录下"是谁来解析的" → 轮询 `/api/dns/result/<token>` 取回 DNS 出口 IP 列表。

**判定逻辑**（与页面一致）：
- `dns_servers` 为空 → **DNS 加密或未暴露出口**（安全）
- `dns_servers` 非空且其国家 ≠ 出口 IP 国家 → **泄露**

> 注意：本机开启 Clash fake-IP 模式时，DNS 查询不出公网，`dns_servers` 恒为空——这在该站判定里正是"安全"，不是检测失败。早期用 `bash.ws` 得出的"不可用"结论是错的，已废弃该方案。

### WebRTC：明确不做后台检测

WebRTC 泄露必须由浏览器建立 `RTCPeerConnection` 并收集 ICE candidate 才能测出，纯 HTTP 客户端**无法模拟**。

**决定**：卡片上如实显示「WebRTC — 需浏览器检测 →」，作为引导点击跳转的钩子，不伪装成已检测。

---

## 2. 探针设计

三个探针，**10 分钟一次**（IP/DNS 状态不会分钟级变化），复用现有后台线程与抖动/退避机制。

| 探针 | 实现 | 输出字段 |
|---|---|---|
| **可用性 + 出口 IP** | GET `claude.ai/cdn-cgi/trace`，解析 `k=v` 文本 | `reachable`, `ip`, `loc`, `colo` |
| **IP 风险** | GET `ip.net.coffee/api/iprisk/<ip>` + `/api/geoip/<ip>` | `is_datacenter`, `is_vpn`, `is_proxy`, `is_tor`, `is_abuser`, `abuser_score`, `country`, `city`, `isp` |
| **DNS 泄露** | token 触发解析 + 轮询 `/api/dns/result/<token>` | `dns_servers[]`（含 ip / country）|

### 判定表（合并取最差）

| 盾牌 | 符号 | 条件 |
|---|---|---|
| 🔴 红 | 感叹号 | `claude.ai` 不可达 **或** DNS 出口国家 ≠ 出口 IP 国家（真泄露）**或** `is_abuser=true` |
| 🟡 黄 | 横线 | 出口 IP 相比上次**发生变化** |
| 🟢 绿 | 勾 | 以上都不满足 |
| ⚪ 灰 | 圆点 | 检测中，或连续 3 次检测失败 |

**机房 IP 不点亮盾牌**——用户长期走机房出口（`is_datacenter=true`），若据此常亮黄灯则告警失去意义。`is_datacenter` 仅在卡片里以标签注明。

---

## 3. UI 设计

### 3.1 盾牌图标（常驻）

- **macOS**：菜单栏在两个环之后追加盾牌，总宽 99px → 约 119px
- **Windows**：第三个独立托盘图标（每应用一图标位，无法与环合并）
- 形状按语义区分，**不依赖颜色**（小尺寸/色觉障碍下可读）：绿=勾、黄=横线、红=感叹号、灰=圆点
- **点击**：macOS 打开面板（卡片内跳转）；Windows 左键直接跳转 `ip.net.coffee/claude/`

### 3.2 面板卡片

放在 Claude / CodeX 卡片下方，同构样式（圆角卡片 + 右上状态点）：

```
🛡 IP 安全                    ●     [悬停显示：在网页中检测 ↗]
出口 IP   203.0.113.42  US · San Jose
风险      [机房 IP]  滥用分 Low
DNS       未暴露出口（加密）  [安全]
WebRTC    需浏览器检测  →
```

- **整张卡片是点击热区**，跳转 `https://ip.net.coffee/claude/`
- 悬停时右上角浮出「在网页中检测 ↗」提示可点击
- 可通过现有「详情面板显示哪些」子菜单增加的「IP 安全」项隐藏；隐藏后盾牌图标一并消失

---

## 4. 错误处理

沿用现有抖动抑制哲学（见 `_absorb_fetch` / `ServiceState.absorb`）：

- **单次失败**：不改变盾牌，沿用上次结果（网络抖动不报警）
- **连续 3 次失败**：盾牌变**灰** + 卡片显示「检测不可用」——**不是红**，明确区分"没测到"与"测到问题"
- **ip.net.coffee 不可用时降级**：仍用 `claude.ai/cdn-cgi/trace` 维持可用性与出口 IP 判断，风险分与 DNS 行显示「不可用」
- **DNS 探针超时**：视作本轮无结果，不影响其他两个探针的判定

---

## 5. 隐私与请求量

- **不调用 `/api/session`**：该接口是页面用于"共享分析"的 IP 上报，与检测无关。只调用纯查询接口 `/api/iprisk/`、`/api/geoip/`、`/api/dns/result/`
- **检测结果只存内存**，不落盘、不写入现有 `~/.ai-limit-menubar-history.jsonl`
- **请求量**：10 分钟一轮，每轮约 2 个 DNS 查询 + 4~5 个 HTTP 请求，合计约 30 次/小时，与现有额度轮询同量级
- 复用现有 jitter（0~20s 随机延迟）与连败指数退避

---

## 6. 代码结构

遵循现有分层：**数据层跨平台共享，UI 层各端自绘**。

| 文件 | 职责 | 变更 |
|---|---|---|
| `ipsec.py`（新增，仓库根） | 三个探针 + 判定逻辑，纯数据、无 UI 依赖、可离线单测 | 新建 |
| `menubar/panelui.py` | 新增 IP 卡片绘制函数 | 扩展 |
| `menubar/ai-limit-app.py` | 盾牌 NSImage 绘制、菜单栏拼接、卡片 payload、点击跳转 | 扩展 |
| `winbar/ai-limit-tray.py` | 第三个托盘图标、flyout 卡片区、左键跳转 | 扩展 |

`ipsec.py` 对外接口：

```python
SHIELD_OK, SHIELD_WARN, SHIELD_CRIT, SHIELD_IDLE = "ok", "warn", "crit", "idle"

def probe() -> dict:
    """跑一轮完整检测。返回：
    {
      "level": "ok|warn|crit|idle",
      "reachable": bool,
      "ip": str|None, "country": str|None, "city": str|None, "isp": str|None,
      "is_datacenter": bool, "is_vpn": bool, "is_proxy": bool,
      "is_tor": bool, "is_abuser": bool, "abuser_score": str|None,
      "dns_servers": [{"ip": str, "country": str}],
      "dns_leaked": bool,
      "ip_changed": bool,
      "error": str|None,       # 本轮整体失败时的原因
      "degraded": bool,        # ip.net.coffee 不可用但 trace 成功
    }
    """
```

判定与格式化留在各端 UI 层（i18n 在各端做，与现有 `panelui` 只管画的原则一致）。

---

## 7. 验收标准

- [ ] `ipsec.probe()` 在本机返回 `level="ok"`（或 IP 刚变时 `warn`）、`dns_leaked=False`、`is_datacenter=True`
- [ ] 三个探针各自失败时，`probe()` 不抛异常，降级字段正确
- [ ] macOS 菜单栏出现盾牌，四种状态渲染正确，总宽 ≈119px
- [ ] macOS 面板出现 IP 卡片，点击跳转浏览器
- [ ] Windows 出现第三个托盘图标，左键跳转，flyout 出现 IP 卡片
- [ ] 连续 3 次失败盾牌变灰而非变红
- [ ] 不产生对 `/api/session` 的请求
- [ ] 双端离线单测覆盖判定表全部分支
