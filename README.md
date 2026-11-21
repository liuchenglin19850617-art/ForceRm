# ForceRm - v2.0.6

ForceRm 是面向 Windows 的垃圾清理工具，基于 Python 与 CustomTkinter 构建图形界面。程序采用白名单模型，只清理系统、浏览器与常用软件的缓存及临时文件，不涉及注册表与使用痕迹。清理方式为直接删除，不可恢复；扫描与清理分离，清理前显示确认对话框。

规则集中于单个 `LibRm/LibRm.json`（238 条），软件缓存另由一次遍历 AppData 的智能扫描按目录关键字识别，新增软件无需维护规则。程序覆盖 Windows XP 至 Windows 11 及同内核 Server 版本，启动时依据注册表与系统版本信息判定内核和 build 号，界面只显示当前版本适用的清理项。

发行版采用 PyInstaller `--onefile`，运行时、规则库、品牌图标与 Java 日志组件均内嵌于可执行文件，发行包只含两个 exe、`README.md` 与 `LICENSE`。隐藏参数 `--procuninst` 用于系统响应迟缓时结束高负荷非必要进程并重启 Windows 资源管理器；C++ 辅助引擎为可选组件，未编译时由内置 `ctypes` 实现承接，Java 组件负责格式化写入疏通日志。

## 功能

- 系统、浏览器及常用软件缓存清理，规则集中于 `LibRm/LibRm.json`（238 条，单文件加载）。
- 智能软件缓存：一次遍历 AppData，按 `cache` / `crashpad` / `temp` / `logs` 等目录关键字识别；新增软件无需添加规则，该项默认不勾选。
- 深度清理：清空回收站、DISM 组件清理、刷新 DNS。
- Windows 版本识别：覆盖 XP 至 Win11 及同内核 Server，按 build 号判定，顶栏显示识别结果，界面只显示当前版本适用项。
- 进程疏通：隐藏参数 `--procuninst` 采样 CPU 与工作集，结束高负荷非必要进程并强制重启资源管理器；必要程序、系统目录进程与程序自身不参与。
- 扫描与清理分离，清理前显示确认对话框；删除失败时尝试提权强制删除，仍失败则跳过并写入 `WinERR.log`。
- 多线程扫描与删除，规则库与智能扫描结果不重复统计。

## Windows 版本识别

程序启动时读取 `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion` 与 `sys.getwindowsversion()`，按内核版本与 build 号判定系统，覆盖 Windows XP、Server 2003、Vista、7、8、8.1、10、11 及同内核的 Server 版本。顶栏显示识别结果，格式为「版本名 (build.ubr)」。

Windows 11 的注册表 `ProductName` 仍可能写作 Windows 10，因此 10 与 11 以 build 号 22000 为界，不采用产品名字符串判定。Server 版本依据 `InstallationType` 与 `EditionID` 区分，清理规则按同内核客户端版本选用。

界面列表只显示当前版本适用的清理项，不适用于当前版本的规则不进入列表，也不参与扫描、统计与清理。各版本分支如下：

- XP / Server 2003：使用 `Documents and Settings` 结构，启用 IE 脱机缓存、下载的程序文件与错误报告转储；事件日志按 `system32\config` 下的 `*.evt` 处理。
- Vista / 7：使用 `AppData` 与 `ProgramData` 结构，启用缩略图缓存、INetCache、Windows.old 与升级残留，安装日志位于 `Windows\Panther`。
- 8 / 8.1：启用 DISM 组件存储清理、睡眠研究报告与实时内核报告。
- 10：在 8 的基础上启用传递优化缓存与升级助手残留。
- 11：继承 10 的规则组，并为系统日志追加度量启动日志目录。

DISM 组件存储清理仅在 build 9200（Windows 8 / Server 2012）及以上提供，XP 至 Windows 7 不显示该动作，运行时另有二次校验。刷新 DNS 与清空回收站在所有版本可用。

规则可通过 `min_build`、`max_build`、`families`、`server_only`、`client_only` 字段声明适用版本；规则库 `LibRm/LibRm.json` 中的系统项由代码内的版本映射统一注入约束，规则库文件本身保持不变。`--selftest` 报告包含 `win_version_full`、`win_family`、`win_build` 与 `version_rules` 字段。

## 清理方式

直接删除，不可恢复，程序不提供回收站模式。使用「全选」将同时启用 `C:\Windows.old` 整目录、事件日志、清空回收站、DISM 组件清理等不可逆项目。

## 进程疏通

`--procuninst` 为隐藏参数，用于系统响应迟缓时疏通进程，不启动图形界面。程序枚举当前进程，采样 0.8 秒的 CPU 时间与工作集，结束 CPU 占用达到单个核心 50% 或工作集达到 768 MB 的非必要进程，随后强制重启 Windows 资源管理器。两个阈值可由 `config.json` 中的 `proc_cpu` 与 `proc_mem_mb` 调整。

必要程序不参与疏通，包括系统关键进程、Windows 系统目录下的进程、Defender 与系统维护相关进程、ForceRm 自身及其父进程、Python 与 Java 运行时以及辅助程序本身。白名单由 `PROC_WHITELIST` 定义，系统目录依据 `System32`、`SysWOW64`、`WinSxS` 排除。资源管理器不在批量结束范围内，由独立流程结束并重新启动。

处理引擎优先调用可执行文件同目录的 `ProcUninst.exe`（C++ 实现）；该文件不存在时使用内置的 `ctypes` 实现，两者判定规则一致。事件以 `ACTION|PID|NAME|CPU|MEM|DETAIL` 格式生成，由 `ProcUninstLog.class`（Java）格式化并追加写入 `procuninst.log`；Java 运行时或该组件缺失时由 Python 直接写入。执行摘要另写入 `_procuninst_result.json`。上述文件均生成于可执行文件所在目录。

`native/` 目录包含 C++ 源码 `ProcUninst.cpp`、Java 日志组件的源码与编译产物 `ProcUninstLog.java` / `ProcUninstLog.class`，以及编译脚本 `build_native.cmd`。Java 组件已编译并内嵌于单文件可执行程序；C++ 组件为可选辅助，未编译时程序自动采用内置 `ctypes` 实现。该参数按 `auto_elevate` 设置申请管理员权限。

## 单文件发行

发行包采用 PyInstaller `--onefile` 构建。每个可执行文件自包含 Python 运行时、customtkinter、Pillow、`LibRm/LibRm.json`、`logo.png` 与 `native/ProcUninstLog.class`，不再附带 `_internal` 目录。与旧版 onedir 相比，发行包由 1112 个文件缩减为 4 个文件（两个可执行文件、`README.md`、`LICENSE`），解压后不依赖同目录下的运行时文件。两个可执行文件各含一份完整的运行时，发行包体积高于旧版共用一份运行时的结构。

只读资源在运行时解压至临时目录，由 `sys._MEIPASS` 定位；`config.json`、`WinERR.log`、`procuninst.log` 与自检报告仍写入可执行文件所在目录。程序启动时需要先完成一次运行时解压，因此冷启动时间长于 onedir 版本。

程序优先读取可执行文件同目录下的 `LibRm/LibRm.json` 与 `logo.png`；未找到时再读取内嵌副本。规则库与图标可在不重新构建的情况下替换。

## 文件

- `ForceRm.pyw` / `ForceRm-high.pyw`：主程序；high 版另含被占用文件的解锁重试逻辑。
- `ForceRm.exe` / `ForceRm-high.exe`：单文件可执行程序，运行时与只读资源已内嵌。
- `LibRm/LibRm.json`：源码仓库中的合并规则库，结构为 `{"version":1,"count":238,"targets":[...]}`。发行版将其内嵌于 exe；该文件缺失或损坏时，加载器回退兼容旧版单软件散文件。
- `logo.png`：品牌图标，构建时生成 `ForceRm.ico` 并内嵌于 exe。
- `native/`：进程疏通辅助组件。`ProcUninst.cpp` 为 C++ 源码，`ProcUninstLog.java` 与 `ProcUninstLog.class` 为 Java 日志组件的源码与编译产物，`build_native.cmd` 为编译脚本。主目录与 `GITHUB/native/` 各一份，不随发行包附带，其中 class 已内嵌于 exe。
- `config.json`：运行时在可执行文件同目录生成，`auto_elevate` 控制是否自动申请管理员权限，`threads` 控制并发线程数，`proc_cpu` 与 `proc_mem_mb` 控制进程疏通阈值。
- `dist-full-v1.0.2.zip`：免安装发行包，含两个单文件 exe、`README.md` 与 `LICENSE`。
- `GITHUB/`：发布用源码副本，含与主目录一致的源码、文档、`native/` 组件及发行包副本。
- `LICENSE`：GNU AGPL v3.0 全文。

## 运行

源码运行需要 Python 3.10+ 与 `customtkinter`（`pip install customtkinter`），运行 `.pyw` 即可。发行包解压后运行 `ForceRm.exe` 或 `ForceRm-high.exe`，程序将自动申请管理员权限。

隐藏参数：

- `--selftest`：在程序同目录写入 `_selftest.json` 自检报告后退出，不启动界面；报告包含版本识别、规则数量与辅助组件状态。
- `--procuninst`：执行进程疏通，写入 `procuninst.log` 与 `_procuninst_result.json`，不启动界面。
- `--elevated`：提权实例标记，用于跳过 UAC 重启，仅供内部使用。

## 许可证

本项目采用 **GNU Affero General Public License v3.0**（AGPL-3.0），全文见 `LICENSE`。

```
SPDX-License-Identifier: AGPL-3.0-only
Copyright (C) 2026 ForceRm contributors
```

AGPL 为强 copyleft 许可：分发本程序或其修改版的二进制时，必须以同一许可提供对应源码；第 13 条另规定，若将修改后的程序通过网络提供给使用者，须向其提供源码获取途径。本项目源码随发行内容一并提供。

第三方组件各自保持原有许可，不受本项目许可变更影响：Python 运行时（PSF License）、customtkinter（MIT）、Pillow（HPND）、PyInstaller（GPLv2 附打包例外，允许打包非 GPL 程序）。上述组件随可执行文件一同分发，其许可文本由各自项目提供。
