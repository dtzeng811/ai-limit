# 踩坑全录

## 001 · LaunchAgent 开机自启与 macOS 系统设置不同步

**现象**：用 `~/Library/LaunchAgents/` plist 实现开机自启，App 菜单里显示"已启用"（plist 存在），但 macOS 系统设置→登录项里显示为禁用/灰色，用户从系统设置无法开启。

**原因**：macOS 系统设置的"登录项"管理的是 Apple SMLoginItemSetEnabled API 注册的项目，与 LaunchAgent plist 是两套独立机制。LaunchAgent 会被 `launchd` 加载，但未公证的 App 会被 macOS 标记为不可信，系统设置里开关失效。

**解法**：
- 短期：移除该功能，避免用户困惑
- 长期：对 App 做公证（需 Developer ID Application 证书），或改用 SMLoginItemSetEnabled（需 Swift）

**相关**：Developer ID Application 证书已在 Apple Developer Portal（到期 2027/02/02），未下载安装。

---

## 002 · py2app bundle 里 AttributeError：菜单项 add 顺序

**现象**：`_about_menu.add(self._star_item)` 写在 `_star_item` 创建之前，App 启动时 crash，弹出 "Launch error" 对话框。

**原因**：`_build_menu` 里 `_about_menu` 的 `add` 调用发生在 `_star_item = rumps.MenuItem(...)` 之前，属性未赋值。

**解法**：把 `self._about_menu.add(self._star_item)` 移到 `self._star_item = ...` 赋值语句之后。

---

## 003 · Gitee Release 需要 token，无浏览器 OAuth

**现象**：GitHub 有 `gh` CLI 可以浏览器登录创建 Release，Gitee 没有等效官方 CLI。

**解法**：用 Gitee REST API + 个人令牌（只需 `projects` 权限）：
1. `POST /api/v5/repos/{owner}/{repo}/releases` 创建 Release，需带 `target_commitish`
2. `POST /api/v5/repos/{owner}/{repo}/releases/{id}/attach_files` 上传附件
用完后在 Gitee 令牌页撤销 token。
