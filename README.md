# Switch-agent

一个基于 `LangChain + Ollama + joycontrol-kb` 的 Nintendo Switch 远程控制项目。

本项目把运行在远端 Linux / 树莓派上的 `joycontrol-kb` 封装成 HTTP API，再在本地提供一个自然语言 Agent，用中文命令控制 Switch，包括按键、双摇杆移动、NFC 加载和自动重连。

## 项目结构

```text
switch-remote/
├─ api.py            # 远端 Flask API，直接驱动 joycontrol-kb
├─ main.py           # 本地 LangChain Agent / 命令入口
├─ bin/              # 本地 NFC .bin 文件目录
├─ joycontrol/       # vendored joycontrol-kb Python 包
├─ scripts/          # joycontrol-kb 自带脚本
├─ run_controller_cli.py
├─ setup.py
├─ README.joycontrol-kb.md
└─ README.md
```

## 当前架构

1. 远端设备运行 `api.py`
   - 使用 `joycontrol-kb` 模拟 `PRO_CONTROLLER`
   - 提供 `/start`、`/wait`、`/press`、`/hold`、`/release`、`/stick/*`、`/nfc` 等 HTTP 接口
2. 本地电脑运行 `main.py`
   - 使用 `LangChain create_agent`
   - 默认模型为 `Ollama qwen3.5:9b`
   - 将自然语言请求转换为远端控制动作
3. NFC 文件保存在本地 `bin/`
   - `main.py` 会直接读取本地 `.bin`
   - 通过 base64 上传到远端 API
   - 不再依赖树莓派本地也存在同名文件

## 已实现功能

### 1. 自然语言控制

支持中文自然语言指令，例如：

- `按下A键`
- `长按home键`
- `松开ZR`
- `前进`
- `推摇杆前进`
- `松开摇杆`
- `镜头向右`
- `推右摇杆向上`
- `松开右摇杆`
- `列出前5个NFC文件`
- `加载黄昏相关的NFC`

### 2. 常用命令硬路由

为了避免模型误判，`main.py` 对一部分高频命令做了本地直通解析，命中后不经过 LLM 推理，直接调用 API：

- 短按：`按下 / 按一下 / 点一下 / 按一次`
- 长按：`长按 / 按住`
- 松开：`松开 / 释放`
- 全部释放：`释放全部`
- 左摇杆移动：`前进 / 后退 / 向左 / 向右 / 推摇杆前进`
- 右摇杆移动：`镜头向右 / 视角向上 / 推右摇杆向前 / 松开右摇杆`

这部分是为了修正早期“普通按下被误执行成长按”“前进被误映射为方向键上”的问题。

### 3. 双摇杆控制

远端 API 目前已经支持：

- `POST /stick/push`
- `POST /stick/hold`
- `POST /stick/release`

本地 Agent 里的语义约定：

- `前进`：左摇杆短暂前推
- `推摇杆前进`：左摇杆持续前推
- `松开摇杆`：左摇杆回中
- `镜头向右`：右摇杆短暂右推
- `推右摇杆向上`：右摇杆持续上推
- `松开右摇杆`：右摇杆回中

### 4. NFC 上传与切换

支持：

- 列出本地 `bin/` 下全部 `.bin`
- 按关键词搜索 NFC
- 读取本地 NFC 文件内容并上传到远端
- 清空当前 NFC 内容

远端相关接口：

- `POST /nfc`
- `POST /nfc/remove`

### 5. 自动重连

为了解决 Switch 偶发断连，`api.py` 增加了自动重连逻辑：

- 检查底层 transport 是否仍然有效
- 动作执行时如果检测到典型断连异常，自动：
  1. 停止旧连接
  2. 使用缓存的 `reconnect_bt_addr` 重新启动
  3. 等待连上
  4. 重试当前动作一次

同时新增：

- `POST /reconnect`

相关环境变量：

- `SWITCH_RECONNECT_BT_ADDR`
- `SWITCH_AUTO_RECONNECT`
- `SWITCH_RECONNECT_TIMEOUT`

### 6. 默认重连地址

当前默认重连地址是：

```text
78:81:8C:16:7B:A9
```

如果不传 `reconnect_bt_addr`，远端 API 会自动使用这个地址。

## 运行方式

### 远端

在树莓派 / Linux 端运行：

```bash
sudo -E /usr/bin/python3 api.py
```

要求：

- 远端已安装 `joycontrol-kb` 依赖
- 需要 root 权限
- 蓝牙环境可正常工作

### 本地

本地运行 Agent：

```bash
python main.py
```

单次执行：

```bash
python main.py --prompt "按下A键"
python main.py --prompt "前进"
python main.py --prompt "加载黄昏相关NFC"
```

## 主要接口

### 状态与连接

- `GET /health`
- `POST /start`
- `POST /wait`
- `POST /reconnect`
- `POST /stop`

### 按键

- `POST /press`
- `POST /hold`
- `POST /release`
- `POST /release_all`
- `POST /sequence`

### 摇杆

- `POST /stick/push`
- `POST /stick/hold`
- `POST /stick/release`

说明：

- `stick=left` 控制左摇杆
- `stick=right` 控制右摇杆

### NFC

- `POST /nfc`
- `POST /nfc/remove`

## 这次项目里做过的关键改造

相对于最初状态，当前版本已经完成了这些改造：

1. 用 `LangChain create_agent` + `ChatOllama(qwen3.5:9b)` 搭好了自然语言控制入口。
2. 把 `joycontrol-kb` 包装成 Flask API，方便本地通过 HTTP 控制远端 Switch controller。
3. 把本地 `bin/` NFC 文件改成上传模式，解决远端找不到本地路径的问题。
4. 修正了按键语义，区分短按和长按。
5. 为明确指令加了硬路由，减少模型误操作。
6. 增加了双摇杆控制，不再把“前进”错误映射为方向键上，并支持镜头/视角类命令。
7. 增加了自动重连和手动 `/reconnect` 能力。
8. 加入默认 Switch MAC 自动重连逻辑。
9. 清理了 `joycontrol-kb` 的嵌套 `.git`，并将整个项目整理为独立仓库。
10. 将 vendored `joycontrol-kb` 代码平铺到项目根目录，避免子目录导入和维护麻烦。

## 当前限制

### 1. Amiibo / NFC 一天一次限制

`/nfc/remove` 只能清空当前挂载的 NFC 内容，不能绕过游戏侧对“同一个 amiibo 一天一次”的限制。

也就是说：

- remove 再 reload 同一个 `.bin`
- 断开重连后再扫同一个 `.bin`

通常仍然会被识别为同一个 tag。

### 2. sequence 中途断连

如果 `sequence` 执行到一半断开，当前恢复策略会从整段序列头部重试一次，而不是从中断点继续。

### 3. 首次蓝牙配对仍依赖远端环境

如果远端蓝牙栈、BlueZ 插件或 joycontrol-kb 本身状态异常，仍然需要在远端排查。

## 后续可继续做的事情

- 增加更细粒度的摇杆力度和持续时间控制
- 给 `/nfc` 增加“reset -> reconnect -> reload”一键流程
- 改进多步动作脚本
- 增加 Web UI 或桌面 GUI
