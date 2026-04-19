#!/usr/bin/env python3
"""
学术论文 Figure 提取工具 GUI 版

说明：
1. 正常启动时，打开桌面图形界面
2. 当以 --run-extractor 模式启动时，作为后台提取子进程运行

这样设计的目的是为了兼容 PyInstaller 打包后的 exe。
GUI 可以通过“再次启动自己”的方式在后台执行提取逻辑，
从而继续保留日志输出能力，而不依赖外部 .py 文件。
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from pathlib import Path


def _prepare_tk_environment() -> None:
    """在 Windows/Conda 环境下补齐 Tcl/Tk 路径，避免 Tkinter 启动失败。"""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        runtime_base = Path(sys._MEIPASS)
        bundled_tcl = runtime_base / "tcl8.6"
        bundled_tk = runtime_base / "tk8.6"
        if bundled_tcl.exists() and bundled_tk.exists():
            os.environ.setdefault("TCL_LIBRARY", str(bundled_tcl))
            os.environ.setdefault("TK_LIBRARY", str(bundled_tk))
            return

    base_prefix = Path(sys.base_prefix)
    candidates = [
        base_prefix / "Library" / "lib",
        Path(sys.executable).resolve().parent / "Library" / "lib",
    ]

    for lib_dir in candidates:
        tcl_dir = lib_dir / "tcl8.6"
        tk_dir = lib_dir / "tk8.6"
        if tcl_dir.exists() and tk_dir.exists():
            os.environ.setdefault("TCL_LIBRARY", str(tcl_dir))
            os.environ.setdefault("TK_LIBRARY", str(tk_dir))
            break


_prepare_tk_environment()

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


def run_embedded_extractor(argv: list[str]) -> int:
    """以命令行模式调用提取器主逻辑。"""
    import extract_figures

    try:
        extract_figures.main(argv)
        return 0
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 1


class FigureExtractorGUI:
    """桌面界面主程序。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("PDF Figure 提取工具")
        self.root.geometry("920x680")
        self.root.minsize(820, 620)

        self.base_dir = Path(__file__).resolve().parent
        self.process: subprocess.Popen[str] | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.worker_thread: threading.Thread | None = None

        self.input_path_var = tk.StringVar()
        self.output_path_var = tk.StringVar(value=str(self.base_dir / "figure_output_gui"))
        self.dpi_var = tk.StringVar(value="300")
        self.padding_var = tk.StringVar(value="8")
        self.keep_chart_like_var = tk.BooleanVar(value=False)

        self._build_ui()
        self.root.after(120, self._drain_log_queue)

    def _build_ui(self) -> None:
        """搭建界面布局。"""
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        title = ttk.Label(
            outer,
            text="PDF Figure 提取工具",
            font=("Microsoft YaHei UI", 16, "bold"),
        )
        title.pack(anchor="w")

        subtitle = ttk.Label(
            outer,
            text="从 PDF 中提取 figure 图片，并把 caption 一起导出到同一张图片中",
        )
        subtitle.pack(anchor="w", pady=(4, 12))

        options = ttk.LabelFrame(outer, text="参数设置", padding=12)
        options.pack(fill="x")
        options.columnconfigure(1, weight=1)

        ttk.Label(options, text="输入路径").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=6)
        ttk.Entry(options, textvariable=self.input_path_var).grid(row=0, column=1, sticky="ew", pady=6)

        input_buttons = ttk.Frame(options)
        input_buttons.grid(row=0, column=2, sticky="e", padx=(8, 0), pady=6)
        ttk.Button(input_buttons, text="选择 PDF", command=self._choose_pdf).pack(side="left", padx=(0, 6))
        ttk.Button(input_buttons, text="选择目录", command=self._choose_input_dir).pack(side="left")

        ttk.Label(options, text="输出目录").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=6)
        ttk.Entry(options, textvariable=self.output_path_var).grid(row=1, column=1, sticky="ew", pady=6)
        ttk.Button(options, text="选择输出", command=self._choose_output_dir).grid(
            row=1, column=2, sticky="e", padx=(8, 0), pady=6
        )

        ttk.Label(options, text="DPI").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=6)
        ttk.Entry(options, textvariable=self.dpi_var, width=12).grid(row=2, column=1, sticky="w", pady=6)

        ttk.Label(options, text="留白 padding").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=6)
        ttk.Entry(options, textvariable=self.padding_var, width=12).grid(row=3, column=1, sticky="w", pady=6)

        ttk.Checkbutton(
            options,
            text="保留图表类 figure（默认不勾选时会过滤柱状图、折线图等）",
            variable=self.keep_chart_like_var,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 2))

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=12)

        self.run_button = ttk.Button(actions, text="开始提取", command=self._start_run)
        self.run_button.pack(side="left")

        self.stop_button = ttk.Button(actions, text="停止运行", command=self._stop_run, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))

        ttk.Button(actions, text="打开输出目录", command=self._open_output_dir).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="清空日志", command=self._clear_log).pack(side="left", padx=(8, 0))

        self.status_var = tk.StringVar(value="状态：等待开始")
        ttk.Label(actions, textvariable=self.status_var).pack(side="right")

        log_frame = ttk.LabelFrame(outer, text="运行日志", padding=8)
        log_frame.pack(fill="both", expand=True)

        self.log_text = tk.Text(
            log_frame,
            wrap="word",
            font=("Consolas", 10),
            state="disabled",
        )
        self.log_text.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scrollbar.set)

        usage = ttk.Label(
            outer,
            text="说明：GUI 版可直接打包为 exe，底层会在后台调用内置提取逻辑。",
        )
        usage.pack(anchor="w", pady=(8, 0))

    def _choose_pdf(self) -> None:
        """选择单个 PDF 文件。"""
        path = filedialog.askopenfilename(
            title="选择 PDF 文件",
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")],
            initialdir=str(self.base_dir),
        )
        if path:
            self.input_path_var.set(path)

    def _choose_input_dir(self) -> None:
        """选择包含 PDF 的目录。"""
        path = filedialog.askdirectory(title="选择输入目录", initialdir=str(self.base_dir))
        if path:
            self.input_path_var.set(path)

    def _choose_output_dir(self) -> None:
        """选择输出目录。"""
        path = filedialog.askdirectory(title="选择输出目录", initialdir=str(self.base_dir))
        if path:
            self.output_path_var.set(path)

    def _append_log(self, message: str) -> None:
        """向日志框追加文本。"""
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        """清空日志显示。"""
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _validate_inputs(self) -> list[str] | None:
        """校验输入参数，并组装后台命令参数。"""
        input_path = self.input_path_var.get().strip()
        output_path = self.output_path_var.get().strip()

        if not input_path:
            messagebox.showwarning("缺少输入", "请先选择 PDF 文件或输入目录。")
            return None

        if not Path(input_path).exists():
            messagebox.showwarning("输入不存在", f"输入路径不存在：\n{input_path}")
            return None

        if not output_path:
            messagebox.showwarning("缺少输出目录", "请先填写输出目录。")
            return None

        try:
            dpi = int(self.dpi_var.get().strip())
            padding = float(self.padding_var.get().strip())
        except ValueError:
            messagebox.showwarning("参数错误", "DPI 必须是整数，padding 必须是数字。")
            return None

        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--run-extractor",
            input_path,
            "-o",
            output_path,
            "--dpi",
            str(dpi),
            "--padding",
            str(padding),
        ]

        if self.keep_chart_like_var.get():
            command.append("--keep-chart-like")

        return command

    def _start_run(self) -> None:
        """启动后台提取任务。"""
        if self.process is not None:
            messagebox.showinfo("任务进行中", "当前已经有一个任务在运行。")
            return

        command = self._validate_inputs()
        if not command:
            return

        self._append_log("=" * 72 + "\n")
        self._append_log("开始运行命令：\n")
        self._append_log(" ".join(f'"{part}"' if " " in part else part for part in command) + "\n\n")

        self.status_var.set("状态：正在运行")
        self.run_button.configure(state="disabled")
        self.stop_button.configure(state="normal")

        self.worker_thread = threading.Thread(target=self._run_process, args=(command,), daemon=True)
        self.worker_thread.start()

    def _run_process(self, command: list[str]) -> None:
        """在后台线程中运行提取子进程，并把输出推送到日志队列。"""
        try:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"

            self.process = subprocess.Popen(
                command,
                cwd=str(self.base_dir),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.log_queue.put(line)

            return_code = self.process.wait()
            if return_code == 0:
                self.log_queue.put("\n[GUI] 任务执行完成。\n")
            else:
                self.log_queue.put(f"\n[GUI] 任务执行失败，退出码：{return_code}\n")
        except Exception as exc:  # pragma: no cover
            self.log_queue.put(f"\n[GUI] 启动失败：{exc}\n")
        finally:
            self.process = None
            self.log_queue.put("__PROCESS_FINISHED__")

    def _stop_run(self) -> None:
        """停止当前运行中的任务。"""
        if self.process is None:
            return

        try:
            self.process.terminate()
            self.log_queue.put("\n[GUI] 已请求停止当前任务。\n")
        except Exception as exc:  # pragma: no cover
            self.log_queue.put(f"\n[GUI] 停止任务失败：{exc}\n")

    def _drain_log_queue(self) -> None:
        """定时刷新日志显示。"""
        while True:
            try:
                message = self.log_queue.get_nowait()
            except queue.Empty:
                break

            if message == "__PROCESS_FINISHED__":
                self.status_var.set("状态：已结束")
                self.run_button.configure(state="normal")
                self.stop_button.configure(state="disabled")
            else:
                self._append_log(message)

        self.root.after(120, self._drain_log_queue)

    def _open_output_dir(self) -> None:
        """打开输出目录。"""
        output_path = self.output_path_var.get().strip()
        if not output_path:
            messagebox.showinfo("没有输出目录", "请先填写输出目录。")
            return

        path = Path(output_path)
        if not path.exists():
            messagebox.showinfo("目录不存在", f"输出目录还不存在：\n{path}")
            return

        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", str(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:  # pragma: no cover
            messagebox.showerror("打开失败", f"无法打开目录：\n{exc}")


def launch_gui() -> None:
    """启动图形界面。"""
    root = tk.Tk()
    try:
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except tk.TclError:
        pass

    FigureExtractorGUI(root)
    root.mainloop()


def main() -> int:
    """程序入口。"""
    if len(sys.argv) >= 2 and sys.argv[1] == "--run-extractor":
        return run_embedded_extractor(sys.argv[2:])

    launch_gui()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
