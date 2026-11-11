// SPDX-License-Identifier: AGPL-3.0-only
// ProcUninstLog.java - ForceRm 进程疏通日志辅助程序
//
// 读取 C++ / Python 生成的管道分隔事件文件，追加写入 UTF-8 日志。
// 编译：javac -encoding UTF-8 ProcUninstLog.java
// 调用：java -cp <目录> ProcUninstLog <事件文件> <日志文件>
//
// 事件格式：ACTION|PID|NAME|CPU|MEM|DETAIL

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardOpenOption;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.List;

public final class ProcUninstLog {

    private static final DateTimeFormatter FMT =
            DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss");

    private ProcUninstLog() {
    }

    public static void main(String[] args) {
        if (args.length < 2) {
            System.err.println("用法: ProcUninstLog <事件文件> <日志文件>");
            System.exit(2);
            return;
        }
        try {
            write(Paths.get(args[0]), Paths.get(args[1]));
        } catch (IOException e) {
            System.err.println("写入日志失败: " + e.getMessage());
            System.exit(3);
        }
    }

    private static void write(Path events, Path log) throws IOException {
        List<String> lines = Files.readAllLines(events, StandardCharsets.UTF_8);
        String ts = LocalDateTime.now().format(FMT);
        StringBuilder sb = new StringBuilder();
        sb.append("[").append(ts).append("] 进程疏通开始，事件 ")
                .append(lines.size()).append(" 条\n");
        for (String raw : lines) {
            if (raw.trim().isEmpty()) {
                continue;
            }
            String[] f = raw.split("\\|", -1);
            String action = field(f, 0);
            String pid = field(f, 1);
            String name = field(f, 2);
            String cpu = field(f, 3);
            String mem = field(f, 4);
            String detail = field(f, 5);
            switch (action) {
                case "TERMINATE":
                    sb.append("[").append(ts).append("] 结束进程 PID=").append(pid)
                            .append(" 名称=").append(name)
                            .append(" CPU=").append(cpu).append("%")
                            .append(" 内存=").append(mem).append("MB")
                            .append(" 说明=").append(detail).append("\n");
                    break;
                case "FAIL":
                    sb.append("[").append(ts).append("] 结束失败 PID=").append(pid)
                            .append(" 名称=").append(name)
                            .append(" 说明=").append(detail).append("\n");
                    break;
                case "SKIP":
                    sb.append("[").append(ts).append("] 跳过 PID=").append(pid)
                            .append(" 名称=").append(name)
                            .append(" 说明=").append(detail).append("\n");
                    break;
                case "RESTART":
                    sb.append("[").append(ts).append("] 资源管理器 PID=").append(pid)
                            .append(" 说明=").append(detail).append("\n");
                    break;
                case "DRYRUN":
                    sb.append("[").append(ts).append("] 干跑 PID=").append(pid)
                            .append(" 名称=").append(name)
                            .append(" CPU=").append(cpu).append("%")
                            .append(" 内存=").append(mem).append("MB\n");
                    break;
                case "SUMMARY":
                    sb.append("[").append(ts).append("] 疏通结束 ").append(detail).append("\n");
                    break;
                default:
                    sb.append("[").append(ts).append("] ").append(raw).append("\n");
                    break;
            }
        }
        Files.writeString(log, sb.toString(), StandardCharsets.UTF_8,
                StandardOpenOption.CREATE, StandardOpenOption.APPEND);
    }

    private static String field(String[] parts, int index) {
        return index < parts.length ? parts[index] : "";
    }
}
