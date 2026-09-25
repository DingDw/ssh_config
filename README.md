# xxx SSH 连接管理

在 PowerShell 7、Alacritty 或 Windows Terminal 中运行 `xxx`。

工具由 Python 实现，只读取 `C:\Users\ghoul\ssh_config\config.json`。不支持其他路径、OpenSSH 文本配置或独立密码文件。现有 JSON 和密码无需修改。

## 操作

- 界面使用深色配色，选中行及搜索匹配字符高亮，显示筛选数量、滚动条和快捷键提示。中文连接名称按终端显示宽度对齐，过长内容自动省略。
- 宽窗口右侧显示当前连接的主机、用户、端口及是否保存密码；窄窗格自动收起详情，底部仍显示当前连接地址。退出旧的选择器并重新运行 `xxx` 即可使用新版界面。
- 输入文字实时模糊筛选，支持连接名称、地址及用户名，绝不搜索或显示密码。
- 上下键选择，Enter 在 Zellij 中新建并切换到以连接 `name` 命名的标签页；新标签页将已保存的密码复制到系统剪贴板，然后执行 OpenSSH。在密码提示处用终端的粘贴快捷键粘贴。
- 没有保存密码的连接使用默认密钥、ssh-agent 或手动输入；剪贴板保持原样。
- 运行交互式 `xxx` 后，将所在窗格和标签页重命名为 `xxx`。原标签页始终保留连接列表及搜索条件，可切回后继续选择其他连接。
- SSH 标签页由 Zellij 独立管理，关闭它不会退出原来的 `xxx`。SSH 正常退出后自动关闭该标签页；异常退出时显示退出码，按 Enter 再关闭。
- SSH 新标签页的窗格标题也显示连接 `name`，在该窗格中继续支持 WinSCP 快捷键。每次连接都重新读取配置和密码。
- 退出 `xxx` 时恢复原窗格和标签页标题。在 Zellij 外运行时保持原有的当前终端连接、退出 SSH 后回到列表的行为。
- Backspace 删除字符，Ctrl+U 清空搜索，PageUp/PageDown 翻页，Esc/Ctrl+C 在选择器中退出。
- 交互式 `xxx` 每次启动时自动清理一次 `runtime/connections` 中的无效关联 JSON，并在页面底部显示清理、有效和跳过数量，无需按 F8。进程已结束、进程身份不符、配置目标被删除或改变、内容损坏等无效记录会被清理；有效记录会保留，权限不足、版本未知或已被新记录替换的文件会跳过。清理失败会提示但仍可选择连接，不会关闭 Zellij 会话或 SSH。返回列表、启动 SSH 子进程以及 `--list`、`--check`、`--name` 模式不会触发自动清理。

```powershell
xxx                       # 交互模式
xxx --list                # 列出连接，不显示密码
xxx --check               # 校验固定 JSON 文件，不建立连接
xxx --name '连接完整名称'   # Zellij 内打开独立 SSH 标签页；其他终端内单次连接
```

如果当前终端还没有加载 `.bin` PATH，可重新打开终端或运行完整路径：

```powershell
& 'C:\Users\ghoul\ssh_config\xxx.cmd'
```

## 文件

- `ssh_manager.py`：Python 实现。
- `ssh_integration.py`：Zellij 窗格关联和 WinSCP 快捷键。
- `split_pane.py`：拆分窗格时继承当前 xxx SSH 连接目标。
- `runtime/connections/*.json`：按会话和窗格分别保存正在运行的 SSH 连接记录，不包含密码；正常退出时自动删除，异常退出后的旧记录会被拒绝使用。
- `xxx.cmd`：调用本机 `C:\Python312\python.exe` 的启动命令。
- `C:\Users\ghoul\.bin\xxx.cmd`：启动命令的硬链接，与原文件共享同一份内容。当前 Windows 进程缺少创建符号链接的权限，因此按用户选择使用硬链接。
- `config.json`：唯一配置，保留 `version: 1` 和 `connections` 数组。

本地窗格默认标题由 `C:\Users\ghoul\Documents\PowerShell\Microsoft.PowerShell_profile.ps1` 中的 Zellij 启动片段设置为 `local`，对新启动的 PowerShell 窗格生效，Zellij 外的 PowerShell 不受影响。

## 在 SSH 窗格中拆分

Zellij 原有拆分快捷键已接入连接识别：

- `Ctrl+P` 后 `r`：向右拆分；`d`：向下拆分；`n`：自动拆分；`s`：堆叠。
- `Alt+N`：自动拆分。
- tmux 模式 `Ctrl+B` 后 `"` / `%`：向下 / 向右拆分。

来源窗格有有效的 xxx SSH 记录时，新窗格读取同一个 `config.json` 条目，在当前标签页连接相同主机、端口和用户名，标题显示连接名称，WinSCP 快捷键继续可用。来源是本地窗格时，仍打开默认 PowerShell，标题为 `local`。原 SSH 连接和 xxx 列表不会被关闭。

Windows OpenSSH 不支持 ControlMaster，因此这里继承的是连接配置，新窗格建立独立 SSH 连接；保存的密码会重新复制到剪贴板，需要在密码提示处粘贴。不会继承远端 shell 的当前目录或环境变量。直接手工运行的 SSH 没有 xxx 记录，无法自动识别。

拆分实现会短暂打开一个临时窗格捕获来源，然后恢复原窗格，由后台辅助进程定向创建新窗格。不会向远端 shell 输入拆分命令。发现失效连接记录、目标改变或无法唯一识别来源时取消拆分。自定义插件及直接执行 `zellij action new-pane` 不经过这些快捷键，保持 Zellij 默认行为。

JSON 每条连接包含 `name`、`host`、`username`、整数 `port`（默认 22）及可选 `password`。密码保存在 JSON 中，复制后留在系统剪贴板；密码不会传入 SSH 命令行。连接使用 `ssh.exe -F none -p <端口> -l <用户名> -- <地址>`，不合并其他 SSH 配置。

交互界面使用已安装的 `prompt_toolkit`，窗格与进程识别使用 `psutil`，剪贴板使用 Python 调用 Windows Unicode 剪贴板接口，连接使用本机 OpenSSH；不调用 PowerShell 脚本。Python 环境重装后可运行 `C:\Python312\python.exe -m pip install -r C:\Users\ghoul\ssh_config\requirements.txt`。

## Alacritty 中打开 WinSCP

1. 更新后退出旧版 `xxx`，重新运行并连接。只有新版发起的 SSH 进程有窗格记录。
2. 在当前 SSH 窗格中复制远程目录的完整绝对路径，例如 `/home/ddw/my project`。Alacritty 可按住 Shift 拖动选择，再按 Ctrl+Shift+C 复制。
3. 按 **Ctrl+Shift+Alt+E**。WinSCP 使用当前窗格对应连接的用户名、主机、端口和最新配置密码，通过 SFTP 进入复制的目录。

快捷键已写入 `%APPDATA%\alacritty\alacritty.toml`，使用 Windows E 键的物理扫描码 18，规避 Ctrl+Alt 下的字母键识别问题；通过 `pythonw.exe` 在后台启动 `winscp_hotkey.py`，WinSCP 正常显示窗口。启动诊断记录于 `runtime/winscp-hotkey.log`，不记录密码或剪贴板内容。剪贴板原内容保持不变。第一次连接到尚未被 WinSCP 信任的服务器时，按 WinSCP 提示核对主机密钥。

只接受单行、以 `/` 开头的远程目录绝对路径；不展开 `~`、环境变量或 shell 命令。前后空白及一对包裹路径的引号会被去掉。不会自动读取远端当前目录，也不将路径当作文件下载或编辑。

多个窗口、标签和窗格分别定位；焦点位于非 SSH 窗格时弹出提示。若同一个 Zellij 会话同时被多个客户端连接且焦点不同，脚本拒绝猜测目标。手动执行 SSH、远端二次跳转 SSH、重命名会话后留下的旧关联不在本工具的识别范围内。

连接记录在 SSH 进程启动时建立，表示该进程的目标，不代表已经完成密码认证。密码不写入连接记录或 WinSCP 命令行；通过当前用户临时目录中的密码文件传给 WinSCP，独立 WinSCP 进程退出后删除。辅助进程被强制终止时可能留下 `xxx-winscp-*.tmp` 临时文件。WinSCP 未配置密码时使用自身的密钥/交互认证。

诊断命令（不启动 WinSCP、不输出密码）：

```powershell
C:\Python312\python.exe C:\Users\ghoul\ssh_config\ssh_integration.py --inspect
```

## 验证

在本目录运行 `C:\Python312\python.exe -m unittest -v test_ssh_manager.py test_ssh_integration.py test_split_pane.py`，使用模拟数据验证，不连接真实服务器。
