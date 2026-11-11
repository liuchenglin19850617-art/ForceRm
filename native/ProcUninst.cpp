// SPDX-License-Identifier: AGPL-3.0-only
// ProcUninst.cpp - ForceRm 进程疏通辅助程序（隐藏参数 --procuninst 的 C++ 实现）
//
// 功能：采样 CPU 与内存，结束高负荷且非必要的进程，随后重启 Windows 资源管理器。
// 安全边界：必要程序白名单、系统目录进程、自身与父进程一律跳过。
// 输出：以 UTF-8 写入管道分隔的事件文件，交由 ProcUninstLog.java 生成日志。
//
// 编译（MSVC）：cl /nologo /O2 /EHsc /DUNICODE /utf-8 ProcUninst.cpp /Fe:ProcUninst.exe /link psapi.lib
// 编译（MinGW）：g++ -O2 -municode -static ProcUninst.cpp -o ProcUninst.exe -lpsapi
//
// 参数：--events <文件> [--cpu <单核占用阈值>] [--mem <工作集阈值 MB>]

#include <windows.h>
#include <tlhelp32.h>
#include <psapi.h>

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <set>
#include <string>
#include <thread>
#include <vector>

namespace {

const double kCpuThreshold = 0.50;                       // 占满单个核心的比例
const unsigned long long kMemThreshold = 768ULL * 1024 * 1024;

struct ProcInfo {
    DWORD pid = 0;
    DWORD ppid = 0;
    std::wstring name;
    std::wstring path;
    unsigned long long cpuTicks = 0;
    unsigned long long mem = 0;
};

std::wstring LowerAscii(std::wstring s) {
    for (auto& c : s) {
        if (c >= L'A' && c <= L'Z') c = static_cast<wchar_t>(c - L'A' + L'a');
    }
    return s;
}

std::string ToUtf8(const std::wstring& s) {
    int n = WideCharToMultiByte(CP_UTF8, 0, s.c_str(),
                                static_cast<int>(s.size()), nullptr, 0, nullptr, nullptr);
    std::string out(n > 0 ? n : 0, '\0');
    if (n > 0) {
        WideCharToMultiByte(CP_UTF8, 0, s.c_str(), static_cast<int>(s.size()),
                            &out[0], n, nullptr, nullptr);
    }
    return out;
}

const std::set<std::wstring>& Whitelist() {
    static const std::set<std::wstring> kNames = {
        L"system", L"system idle process", L"registry", L"memory compression",
        L"smss.exe", L"csrss.exe", L"wininit.exe", L"winlogon.exe",
        L"services.exe", L"lsass.exe", L"svchost.exe", L"fontdrvhost.exe",
        L"dwm.exe", L"audiodg.exe", L"conhost.exe", L"wudfhost.exe",
        L"spoolsv.exe", L"searchindexer.exe", L"searchhost.exe",
        L"securityhealthservice.exe", L"securityhealthsystray.exe",
        L"msmpeng.exe", L"nissrv.exe", L"mpdefendercoreservice.exe",
        L"ctfmon.exe", L"explorer.exe", L"shellexperiencehost.exe",
        L"startmenuexperiencehost.exe", L"runtimebroker.exe",
        L"applicationframehost.exe", L"taskhostw.exe", L"sihost.exe",
        L"textinputhost.exe", L"lockapp.exe", L"widgets.exe",
        L"phoneexperiencehost.exe", L"taskmgr.exe", L"trustedinstaller.exe",
        L"tiworker.exe", L"dism.exe", L"cleanmgr.exe", L"wuauclt.exe",
        L"forcerm.exe", L"forcerm-high.exe", L"python.exe", L"pythonw.exe",
        L"procuninst.exe", L"java.exe", L"javaw.exe",
    };
    return kNames;
}

std::vector<std::wstring> SystemDirs() {
    wchar_t win[MAX_PATH] = {0};
    GetWindowsDirectoryW(win, MAX_PATH);
    const std::wstring base = std::wstring(win) + L"\\";
    const wchar_t* subs[] = {L"system32\\", L"syswow64\\", L"winsxs\\"};
    std::vector<std::wstring> out;
    for (const wchar_t* sub : subs) out.push_back(LowerAscii(base + sub));
    return out;
}

std::vector<ProcInfo> Snapshot() {
    std::vector<ProcInfo> out;
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return out;
    PROCESSENTRY32W pe;
    ZeroMemory(&pe, sizeof(pe));
    pe.dwSize = sizeof(pe);
    if (Process32FirstW(snap, &pe)) {
        do {
            ProcInfo p;
            p.pid = pe.th32ProcessID;
            p.ppid = pe.th32ParentProcessID;
            p.name = pe.szExeFile;
            out.push_back(p);
        } while (Process32NextW(snap, &pe));
    }
    CloseHandle(snap);
    return out;
}

unsigned long long CpuTicks(HANDLE h) {
    FILETIME create, exitTime, kernel, user;
    if (!GetProcessTimes(h, &create, &exitTime, &kernel, &user)) return 0;
    const unsigned long long k =
        (static_cast<unsigned long long>(kernel.dwHighDateTime) << 32) | kernel.dwLowDateTime;
    const unsigned long long u =
        (static_cast<unsigned long long>(user.dwHighDateTime) << 32) | user.dwLowDateTime;
    return k + u;
}

unsigned long long CpuTicksByPid(DWORD pid) {
    HANDLE h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!h) return 0;
    const unsigned long long v = CpuTicks(h);
    CloseHandle(h);
    return v;
}

void FillDetails(ProcInfo& p) {
    HANDLE h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, p.pid);
    if (!h) return;
    wchar_t buf[1024] = {0};
    DWORD size = 1024;
    if (QueryFullProcessImageNameW(h, 0, buf, &size)) p.path = buf;
    PROCESS_MEMORY_COUNTERS pmc;
    ZeroMemory(&pmc, sizeof(pmc));
    pmc.cb = sizeof(pmc);
    if (GetProcessMemoryInfo(h, &pmc, sizeof(pmc))) {
        p.mem = static_cast<unsigned long long>(pmc.WorkingSetSize);
    }
    CloseHandle(h);
}

bool IsProtected(const ProcInfo& p, DWORD selfPid, DWORD selfPpid,
                 const std::vector<std::wstring>& sysDirs) {
    if (p.pid == 0 || p.pid == 4 || p.pid == selfPid || p.pid == selfPpid) return true;
    if (Whitelist().count(LowerAscii(p.name))) return true;
    if (!p.path.empty()) {
        const std::wstring path = LowerAscii(p.path);
        for (const auto& d : sysDirs) {
            if (path.compare(0, d.size(), d) == 0) return true;
        }
    }
    return false;
}

bool TerminatePid(DWORD pid) {
    HANDLE h = OpenProcess(PROCESS_TERMINATE, FALSE, pid);
    if (!h) return false;
    const BOOL ok = TerminateProcess(h, 1);
    CloseHandle(h);
    return ok != FALSE;
}

int RestartExplorer(std::vector<std::wstring>& events) {
    int stopped = 0;
    for (const auto& p : Snapshot()) {
        if (LowerAscii(p.name) != L"explorer.exe") continue;
        if (TerminatePid(p.pid)) {
            ++stopped;
            events.push_back(L"RESTART|" + std::to_wstring(p.pid) +
                             L"|explorer.exe|0|0|已结束资源管理器");
        }
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(700));
    wchar_t win[MAX_PATH] = {0};
    GetWindowsDirectoryW(win, MAX_PATH);
    const std::wstring exe = std::wstring(win) + L"\\explorer.exe";
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    ZeroMemory(&pi, sizeof(pi));
    si.cb = sizeof(si);
    std::wstring cmd = L"\"" + exe + L"\"";
    const BOOL ok = CreateProcessW(
        nullptr, &cmd[0], nullptr, nullptr, FALSE, 0, nullptr, nullptr, &si, &pi);
    if (ok) {
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
    }
    events.push_back(std::wstring(L"RESTART|0|explorer.exe|0|0|") +
                     (ok ? L"已重启资源管理器" : L"重启资源管理器失败"));
    return stopped;
}

}  // namespace

int wmain(int argc, wchar_t** argv) {
    std::wstring eventsPath = L"procuninst_events.txt";
    double cpuLimit = kCpuThreshold;
    unsigned long long memLimit = kMemThreshold;
    for (int i = 1; i + 1 < argc; i += 2) {
        const std::wstring key = argv[i];
        if (key == L"--events") eventsPath = argv[i + 1];
        else if (key == L"--cpu") cpuLimit = _wtof(argv[i + 1]);
        else if (key == L"--mem") {
            memLimit = static_cast<unsigned long long>(_wtoi64(argv[i + 1])) * 1024 * 1024;
        }
    }
    if (cpuLimit <= 0) cpuLimit = kCpuThreshold;
    if (memLimit == 0) memLimit = kMemThreshold;

    const DWORD selfPid = GetCurrentProcessId();
    const std::vector<std::wstring> sysDirs = SystemDirs();
    std::vector<ProcInfo> procs = Snapshot();
    DWORD selfPpid = 0;
    for (const auto& p : procs) {
        if (p.pid == selfPid) selfPpid = p.ppid;
    }

    std::vector<unsigned long long> first;
    first.reserve(procs.size());
    for (auto& p : procs) {
        FillDetails(p);
        first.push_back(CpuTicksByPid(p.pid));
    }
    const auto t0 = std::chrono::steady_clock::now();
    std::this_thread::sleep_for(std::chrono::milliseconds(800));
    const auto t1 = std::chrono::steady_clock::now();
    const double interval =
        static_cast<double>(std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count())
        / 1e7;  // 100ns 单位
    if (interval <= 0) return 2;

    std::vector<std::wstring> events;
    int terminated = 0, failed = 0, skipped = 0;
    for (size_t i = 0; i < procs.size(); ++i) {
        ProcInfo& p = procs[i];
        if (IsProtected(p, selfPid, selfPpid, sysDirs)) {
            ++skipped;
            events.push_back(L"SKIP|" + std::to_wstring(p.pid) + L"|" + p.name +
                             L"|0|0|必要程序或系统目录");
            continue;
        }
        const unsigned long long nowTicks = CpuTicksByPid(p.pid);
        const double cpu = nowTicks >= first[i]
            ? static_cast<double>(nowTicks - first[i]) / interval : 0.0;
        const unsigned long long memMb = p.mem / (1024 * 1024);
        if (cpu < cpuLimit && p.mem < memLimit) continue;

        wchar_t detail[64];
        swprintf(detail, 64, L"%.1f|%llu", cpu * 100.0, memMb);
        if (TerminatePid(p.pid)) {
            ++terminated;
            events.push_back(L"TERMINATE|" + std::to_wstring(p.pid) + L"|" + p.name +
                             L"|" + detail + L"|高负荷进程已结束");
        } else {
            ++failed;
            events.push_back(L"FAIL|" + std::to_wstring(p.pid) + L"|" + p.name +
                             L"|" + detail + L"|无权限或进程已退出");
        }
    }

    RestartExplorer(events);
    wchar_t summary[160];
    swprintf(summary, 160, L"SUMMARY|0|ProcUninst|0|0|结束 %d 个，失败 %d 个，跳过 %d 个",
             terminated, failed, skipped);
    events.push_back(summary);

    std::ofstream out(ToUtf8(eventsPath), std::ios::binary | std::ios::app);
    if (!out) return 3;
    for (const auto& line : events) out << ToUtf8(line) << "\n";
    out.close();
    return 0;
}
