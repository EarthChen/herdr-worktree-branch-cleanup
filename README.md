# Worktree Branch Cleanup

Herdr 插件。删除 worktree 时，由用户选择是否同时删除该 worktree 检出的同名分支。

Herdr 原生的删除 worktree 菜单（Delete worktree checkout...）不删分支，也无法让插件往菜单里加选项。
插件提供三种方式：

1. **删除后询问（默认，`ask = true`）**：在原生菜单里确认删除后，立刻弹窗问「是否同时删除分支」。
2. **交互 action**：先弹窗选择「仅删 worktree」(`w`) /「同时删分支」(`b`) /「同时强制删分支」(`f`) /「取消」(`q` 或 Esc)，再执行。
3. **自动删除（`auto = true`）**：任何入口删除 worktree 后，不询问，直接删同名分支。

## 安装

```bash
herdr plugin install EarthChen/herdr-worktree-branch-cleanup
herdr plugin update worktree-branch-cleanup   # 更新
```

需要 `uv`。本地开发时用 `herdr plugin link <本地目录>`。

## 交互 action

在 `config.toml` 绑定快捷键，然后 `herdr server reload-config`：

```toml
[[keys.command]]
key = "prefix+x"
type = "plugin_action"
command = "worktree-branch-cleanup.remove-worktree"
description = "Remove worktree (choose branch handling)"
```

也可用 `herdr plugin action invoke remove-worktree --plugin worktree-branch-cleanup`。
action 作用于当前聚焦的工作区，且只对 linked worktree 生效（主 checkout 不可删）。
删除 worktree 会关闭该工作区，因此实际操作由后台进程执行，结果以 Herdr 通知显示。
worktree 删除失败（如有未提交改动）时，不会删除分支。

## 按键

所有弹窗按单键响应，不用回车。弹窗出现前已输入的按键会被丢弃，避免误触。其他按键等同取消。

## 事件钩子行为

- 默认（`ask = true`）：`worktree.removed` 事件后弹出询问框，`y` 用 `-d`，`f` 用 `-D`，`n`、Esc 或其他按键保留分支。
  action 已经问过的删除，不会重复询问。
- `auto = true` 时，不询问，监听 `worktree.removed` 事件，在主仓库执行 `git branch -d <branch>`。
- 分支有未合并提交时，Git 拒绝删除。分支保留，原因写入插件日志：
  `herdr plugin log list --plugin worktree-branch-cleanup`
- 以下情况跳过：非 linked worktree、detached HEAD、bare、受保护分支、主仓库路径未知。

## 配置（可选）

文件：`$(herdr plugin config-dir worktree-branch-cleanup)/config.toml`

```toml
ask = true                                 # true 时，删除 worktree 后弹窗询问是否删分支
auto = false                               # true 时启用事件钩子，所有入口删除 worktree 都自动删分支
force = false                              # true 时改用 -D，丢弃未合并提交
protected = ["main", "master", "develop", "trunk"]   # 这些分支永不删除
```

## 测试

```bash
uv run --with pytest python -m pytest
```

## 许可证

[Apache-2.0](LICENSE)
