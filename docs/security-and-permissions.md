# 权限、安全与执行边界

## 1. 身份与请求保护

- 服务端 Session Cookie；
- 修改请求使用 CSRF Token；
- 登录与助手入口有独立速率限制；
- CORS 和 Trusted Host 显式配置；
- 密码使用 Argon2；
- API 只返回稳定错误码，不向前端暴露堆栈或内部 Schema。

## 2. 授权模型

系统同时使用三类约束：

1. 角色：employee / reviewer / admin 等粗粒度入口；
2. Capability：是否具备采购复核、组织管理等能力；
3. Organization Scope：Capability 可以作用于哪些组织节点及其子树。

owner-scoped API 始终从 Session actor 推导当前用户，不能由模型或前端提交 `employee_id` 替换。前端隐藏菜单只改善体验，后端仍是唯一权威边界。

## 3. 事务内重新鉴权

审批列表可以使用单请求、不可跨 actor 复用的只读授权上下文减少 N+1 查询。写操作不复用该缓存：服务在事务中锁定任务和实例后，重新加载用户、任职关系、Capability Grant 和组织范围。

这可以处理以下竞争：

- 页面打开后权限被管理员撤销；
- 两个审批人同时操作同一任务；
- 申请人撤回与审批人批准同时发生；
- 客户端因超时重复提交同一命令。

## 4. LLM 安全边界

- Tool Schema 只由 Registry 生成；
- Flow Policy 只能缩小角色可见工具，不能扩大权限；
- Tool 输出以不可信数据返回模型，不能升级为系统指令；
- Candidate 必须有当前用户原文中的 `source_quote`；
- 模型不能从助手回复、Tool 输出或其他会话补造写参数；
- 写工具只生成 proposal；
- Confirmation 之后仍执行权限、幂等和领域校验。

## 5. 数据与审计

Tool Invocation、Confirmation、审批决策和 Product Event 分层记录。日志只记录低敏阶段、工具名、风险等级和稳定错误码，不保存 Provider 密钥、Session、完整 Tool 结果或用户业务正文。

## 6. 发布仓库安全

公开仓库不包含：

- `.env`、API Key、数据库密码、Cookie、Token 或 SSH 私钥；
- 数据库 dump、Docker volume、上传文件或浏览器 profile；
- 本地模型权重；
- 原始私有 Git 历史、内部 planning 和本机备份；
- 真实企业制度、人员或业务记录。

发布候选使用 gitleaks 默认规则扫描，并对模型下载脚本中的三个固定 SHA-256 校验值设置窄范围 allowlist。该 allowlist 不豁免整个文件，也不会忽略其他 API Key 规则。

## 7. 威胁边界与非目标

本项目证明的是应用层安全设计与自动化验证，不宣称完成独立第三方渗透测试。在线 Demo 的 TLS、云主机、网络 ACL、密钥托管和系统补丁仍属于部署运维责任。
