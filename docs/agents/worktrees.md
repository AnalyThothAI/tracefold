# 任务分支与 worktree

[手册](../README.md) · [开发](../DEVELOPMENT.md) · [Issue / PR](issue-tracker.md)

目标是隔离改动和保护现有工作，不强制一种本地目录形状。本约定同样适用于通过 GitHub connector 编辑。

## 使用已有任务环境或创建隔离工作树

先检查分支、工作区状态与现有 worktree。合适的已分配 checkout 可以直接复用；遇到其他任务的修改、并行开发或部署目录时，创建独立 worktree。只读调查无需分支或 worktree。

下面是本地工作树示例，`task-name` 替换为描述当前结果的名称：

```bash
git status --short --branch
git worktree list
git fetch origin main
git worktree add -b docs/task-name ../tracefold-task-name origin/main
```

本地 `.git` 可能是工作树指针文件，不是每份 checkout 都拥有独立 Git 目录。让 `git worktree` 管理它，不手工删除、改成目录或写入另一个操作系统无法解析的绝对路径。WSL / Windows 混用时尤其要核对命令实际运行的文件系统。

## 开发与验证

PR 改动放在任务分支，不直接修改 main 部署 checkout。分支名描述结果即可，不强制工具前缀或 Issue 编号。connector-only 编辑使用远程隔离分支和已知 base SHA，不声称创建过本地 checkout 或执行过本地测试。

保留其他任务的修改与 worktree，不通过 reset、clean、覆盖或删除它们取得“干净状态”。生产数据库、broker、账户和凭据不能作为测试 fixture。

同一分支可以包含多个实施步骤和本地检查点。按[风险选择验证](../DEVELOPMENT.md#risk-tiered-local-verification)，只安装需要的依赖，不把创建 PR 绑定为先跑一遍全部真实资源测试。

## 交付与清理

记录实际调查的 base SHA，交付前尽可能比较目标分支；无法确认新鲜度时明确说明。工作树内容、目标分支和正在部署的镜像是不同身份。

在请求范围内推送并创建 PR，允许 Draft 或后续修正；如实说明 pending、失败和未运行检查。合并、部署与删除任务资源需要各自授权，按照[完成边界](../DEVELOPMENT.md#completion)与运维指南执行。

缺少依赖或权限只阻止需要它的动作。可修复自己隔离的资源并继续其他已授权工作，不能擅用旁边的生产服务通过测试。部署目录有未提交修改不妨碍另建独立分支，也不构成丢弃这些修改的理由。
