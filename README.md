# ForceRm

Windows 垃圾清理工具，基于 Python 与 CustomTkinter 构建图形界面。仅清理白名单内的缓存与临时文件，不涉及注册表与使用痕迹。

## 功能

- 系统、浏览器及常用软件缓存清理，规则集中于 `LibRm/LibRm.json`（238 条，单文件加载）。
- 智能软件缓存：一次遍历 AppData，按 `cache` / `crashpad` / `temp` / `logs` 等目录关键字识别；新增软件无需添加规则，该项默认不勾选。
- 深度清理：清空回收站、DISM 组件清理、刷新 DNS。
- 扫描与清理分离，清理前显示确认对话框；删除失败时尝试提权强制删除，仍失败则跳过并写入 `WinERR.log`。
- 多线程扫描与删除，规则库与智能扫描结果不重复统计。

## 清理方式

直接删除，不可恢复，程序不提供回收站模式。使用「全选」将同时启用 `C:\Windows.old` 整目录、事件日志、清空回收站、DISM 组件清理等不可逆项目。

## 文件

- `ForceRm.pyw` / `ForceRm-high.pyw`：主程序；high 版另含被占用文件的解锁重试逻辑。
- `LibRm/LibRm.json`：合并后的规则库，结构为 `{"version":1,"count":238,"targets":[...]}`。该文件缺失或损坏时，加载器回退兼容旧版单软件散文件。
- `logo.png` / `config.json`：图标与配置，`auto_elevate` 控制是否自动申请管理员权限。
- `LICENSE`：GNU AGPL v3.0 全文。

## 运行

源码运行需要 Python 3.10+ 与 `customtkinter`（`pip install customtkinter`），运行 `.pyw` 即可。发行包解压后运行 `ForceRm.exe` 或 `ForceRm-high.exe`，程序将自动申请管理员权限。

隐藏参数：

- `--selftest`：在程序同目录写入 `_selftest.json` 自检报告后退出，不启动界面。
- `--elevated`：提权实例标记，用于跳过 UAC 重启，仅供内部使用。

## 许可证

本项目采用 **GNU Affero General Public License v3.0**（AGPL-3.0），全文见 `LICENSE`。

```
SPDX-License-Identifier: AGPL-3.0-only
Copyright (C) 2026 ForceRm contributors
```

AGPL 为强 copyleft 许可：分发本程序或其修改版的二进制时，必须以同一许可提供对应源码；第 13 条另规定，若将修改后的程序通过网络提供给使用者，须向其提供源码获取途径。本项目源码随发行内容一并提供。

第三方组件各自保持原有许可，不受本项目许可变更影响：Python 运行时（PSF License）、customtkinter（MIT）、Pillow（HPND）、PyInstaller（GPLv2 附打包例外，允许打包非 GPL 程序）。上述组件被打包至 `_internal/`，其许可文本由各自项目提供。
