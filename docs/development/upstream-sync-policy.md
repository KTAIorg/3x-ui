# 上游同步与 Fork 开发策略（3x-ui）

日期：2026-09-29
状态：生效
规范来源：kt-agent-skills/oss-fork-maintenance
需求来源：KTAIorg/kt-vpn-control-plane#23（自有 VPN 节点，数据平面）

## 仓库角色

- `upstream` = https://github.com/MHSanaei/3x-ui（只读，push 已禁用）
- `origin` = KTAIorg/3x-ui（KT fork，可写）

采纳基线：upstream `main@8c023d13`（= v3.8.5，2026-09-16 发布；与生产节点 bwg-us-la-kt-node1 所装版本一致）

## 分支模型

- `main`：KT 生产线（上游基线 + 白名单定制）
- `upstream-sync`：上游跟随缓冲线（merge 上游 tag，在此解冲突）
- `gitops-prod`：预留。当前数据平面交付走 VPS Ansible 流水线消费 release tag，不经 Argo CD；若未来节点面板进 ACK 再启用

## KT 定制边界（白名单，超出需 PR 论证）

1. **认证 / kt-identity**：面板登录对接 kt-identity OIDC。上游无 OIDC，需最小定制（参考 new-api custom_oauth 收敛教训：优先泛化实现，不新造登录体系；登录成功 ≠ 有权限，业务授权在 kt-identity 侧）
2. **品牌 / branding**：面板标题、Logo、文案（面向 KT 内部运维的皮卡化外观）
3. **CI 与部署**：`.github/workflows/`、构建/发布脚本、部署物（VPS Ansible 流水线消费）
4. **文档与治理**：`docs/development/`（本策略及后续治理文档）

红线：不修改 xray 核心行为、不动入站协议实现，协议层一律跟上游。

## 同步频率

- 安全修复：即时（xray 漏洞跟进优先级最高）
- 常规：上游 release 约月更，每 release 评估；季度至少同步一次
- 同步目标**优先 tag（如 v3.8.x），不追 upstream/main HEAD**

## 冲突优先级

安全修复以上游为准 > 上游已内建则收敛 KT 定制 > 白名单定制保留重施 > 纯风格以上游为准

## KT 发布与版本标记

- KT 发布 tag：`vX.Y.Z-kt.N`（如 `v3.8.5-kt.1`），其中 `vX.Y.Z` **必须是真实上游 release tag**
- 发布流水线：`.github/workflows/release-vps.yml`（Release · VPS），内建血统断言不可绕过：
  1. tag 格式匹配 `^v\d+\.\d+\.\d+-kt\.\d+$`
  2. 对应上游 tag 在本 tag 祖先链中（从 MHSanaei/3x-ui 直拉的权威 tag 校验）
  3. tag 打在 main 上（发布必经 PR，不允许旁路分支出包）
- 产物：linux/amd64 静态完整包（x-ui 面板 + xray-core + geodata + tuic + mtg-multi，构建路径镜像上游 release.yml 的 amd64 分支）+ sha256
- 消费：VPS Ansible 流水线按 tag 锁定拉取，**禁止浮动 latest**
- 手动触发（workflow_dispatch）：跳过断言与发布，仅产出 workflow artifact 供试跑

### 上游 workflow 在 fork 上的处置（触发器中和为 workflow_dispatch，保同步最小 diff）

- 中和：`release.yml`（七平台+Windows 构建）、`docker.yml`（推上游 Docker Hub）、`docs-deploy.yml`（上游 Pages）、`mutation.yml`（每晚定时跑量）、`claude-issue-analyst.yml` / `claude-pr-review.yml`（上游 Claude 凭据 + pull_request_target 安全敏感）
- 保留：`ci.yml`（PR 门禁）、`codeql.yml`（安全扫描）、`docs-ci.yml`（文档检查）、`cleanup_caches.yml`（缓存清理）
- 同步冲突处置：上游改动上述 workflow 时，以"中和意图"优先，重新套用触发器中和再合入

## 验证清单（每次同步后）

- [ ] build + test（Go 后端 + web 前端）
- [ ] 面板登录 E2E（本地账号 + kt-identity 定制域若已启用）
- [ ] 测试节点实装验证：入站创建 / 客户端订阅 / Reality 握手
- [ ] 生产节点升级演练：先快照 → 升级 → 回归 → 保留快照一个观察期
- [ ] 上游安全 fix 确认已包含

## 同步记录

| 日期       | 上游 from→to                         | 冲突摘要         | 验证            | 操作者         |
| ---------- | ------------------------------------ | ---------------- | --------------- | -------------- |
| 2026-09-29 | Day 0 采纳 @ main 8c023d13（v3.8.5） | 无（零定制起点） | clone/fork 校验 | Arise0852 (AI) |
