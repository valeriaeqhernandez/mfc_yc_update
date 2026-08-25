"""
control_panel.py

A GUI control panel for both pipelines, wrapping the exact same logic
sr_configure.py / yc_configure.py already have: same launchd plist
handling, same config files, same preflight checks; just a black/white
window instead of a terminal menu. Meant to be launched by double-clicking
"MFC Pipeline Control.app" rather than typing a command.

Because a double-clicked .app does NOT inherit your shell's environment
the way opening Terminal does (same root cause launchd's automatic mode
runs already had to work around), this loads ANTHROPIC_API_KEY /
SMTP_USERNAME / SMTP_PASSWORD out of ~/.zshenv itself at startup if
they're not already set.

Usage:
    python control_panel.py
    (or just double-click "MFC Pipeline Control.app")
"""

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path


def load_env_from_dotfiles():
    """
    Sources ~/.zshenv and ~/.zshrc through a real zsh process and reads
    back the resulting environment, rather than hand-parsing the files
    with a regex. Confirmed live that regex parsing breaks on perfectly
    valid, real-world shell syntax a hand-rolled pattern won't anticipate
    (a real .zshrc here had "export export ANTHROPIC_API_KEY=...", a
    harmless typo that a real shell exports fine but a regex expecting
    exactly one "export" silently skipped). Actually sourcing the files
    handles that and any other valid syntax (quoting, command
    substitution, conditionals) for free, since it's the same mechanism a
    real terminal session uses.

    Only needed for the GUI: a double-clicked .app doesn't inherit your
    shell's environment the way opening Terminal does, or the way
    launchd's automatic-mode jobs get credentials baked into their plist
    at the moment you turn automatic mode on from a real terminal.
    """
    needed = {"ANTHROPIC_API_KEY", "SMTP_USERNAME", "SMTP_PASSWORD"}
    missing = needed - set(os.environ)
    if not missing:
        return
    try:
        result = subprocess.run(
            ["/bin/zsh", "-c", "source ~/.zshenv 2>/dev/null; source ~/.zshrc 2>/dev/null; env"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return
    for line in result.stdout.splitlines():
        name, sep, value = line.partition("=")
        if sep and name in missing:
            os.environ[name] = value


load_env_from_dotfiles()

BLACK = "#000000"
WHITE = "#FFFFFF"
DIM = "#888888"
FONT = ("Menlo", 12)
FONT_BOLD = ("Menlo", 12, "bold")
FONT_HEADER = ("Menlo", 17, "bold")
FONT_CONSOLE = ("Menlo", 11)


class ConsoleRedirect:
    """A file-like object that pushes writes onto a thread-safe queue
    instead of a real stream, so background-thread print() output can be
    drained into the Text widget on the main thread."""

    def __init__(self, q: queue.Queue):
        self.q = q

    def write(self, msg):
        if msg:
            self.q.put(msg)

    def flush(self):
        pass


class App:
    def __init__(self, root):
        self.root = root
        self.task_running = False
        self.log_queue: queue.Queue = queue.Queue()
        self._build_ui()
        self.root.after(80, self._drain_log_queue)
        self._refresh_status()
        self.log(
            "=" * 60
            + "\n  MFC SOURCING CONTROL PANEL\n  YC pre-MFN + a16z Speedrun pipelines\n"
            + "=" * 60
        )

    # ---------- UI construction ----------

    def _build_ui(self):
        self.root.title("MFC Sourcing Control")
        self.root.configure(bg=BLACK)
        self.root.geometry("1000x660")
        self.root.minsize(880, 560)

        tk.Label(self.root, text="MFC SOURCING CONTROL", bg=BLACK, fg=WHITE, font=FONT_HEADER).pack(
            pady=(14, 4)
        )

        columns = tk.Frame(self.root, bg=BLACK)
        columns.pack(fill="x", padx=16, pady=8)

        self.yc_status_var = tk.StringVar()
        self.sr_status_var = tk.StringVar()
        self.yc_buttons = []
        self.sr_buttons = []

        self._build_pipeline_panel(columns, "YC PRE-MFN", "yc").pack(
            side="left", fill="both", expand=True, padx=(0, 8)
        )
        self._build_pipeline_panel(columns, "A16Z SPEEDRUN", "sr").pack(
            side="left", fill="both", expand=True, padx=(8, 0)
        )

        tk.Label(self.root, text="OUTPUT", bg=BLACK, fg=DIM, font=FONT, anchor="w").pack(
            fill="x", padx=16, pady=(12, 0)
        )

        console_frame = tk.Frame(self.root, bg=BLACK)
        console_frame.pack(fill="both", expand=True, padx=16, pady=(2, 16))

        self.console = tk.Text(
            console_frame,
            bg=BLACK,
            fg=WHITE,
            insertbackground=WHITE,
            font=FONT_CONSOLE,
            wrap="word",
            state="disabled",
            highlightthickness=1,
            highlightbackground=DIM,
            bd=0,
        )
        scrollbar = tk.Scrollbar(console_frame, command=self.console.yview)
        self.console.configure(yscrollcommand=scrollbar.set)
        self.console.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _build_pipeline_panel(self, parent, title, kind):
        frame = tk.Frame(parent, bg=BLACK, highlightthickness=1, highlightbackground=DIM)
        tk.Label(frame, text=title, bg=BLACK, fg=WHITE, font=FONT_BOLD).pack(
            anchor="w", padx=10, pady=(10, 4)
        )

        status_var = self.yc_status_var if kind == "yc" else self.sr_status_var
        tk.Label(
            frame, textvariable=status_var, bg=BLACK, fg=WHITE, font=FONT, justify="left", anchor="w"
        ).pack(anchor="w", padx=10, pady=(0, 10), fill="x")

        buttons = [
            ("AUTO ON", lambda: self.auto_on(kind)),
            ("AUTO OFF", lambda: self.auto_off(kind)),
            ("CHANGE INTERVAL", lambda: self.change_interval(kind)),
            ("CHANGE RECIPIENTS", lambda: self.change_recipients(kind)),
            ("CHANGE BATCH", lambda: self.change_batch(kind)),
            ("RUN NOW", lambda: self.run_now(kind)),
            ("PREFLIGHT CHECK", lambda: self.preflight(kind)),
        ]
        btn_frame = tk.Frame(frame, bg=BLACK)
        btn_frame.pack(fill="x", padx=10, pady=(0, 10))
        target_list = self.yc_buttons if kind == "yc" else self.sr_buttons
        for label, cmd in buttons:
            b = self._make_button(btn_frame, label, cmd)
            b.pack(fill="x", pady=2)
            target_list.append(b)

        return frame

    def _make_button(self, parent, text, command):
        btn = tk.Label(
            parent,
            text=text,
            bg=BLACK,
            fg=WHITE,
            font=FONT,
            relief="solid",
            bd=1,
            padx=8,
            pady=4,
            cursor="hand2",
        )
        btn.bind("<Button-1>", lambda e: command())
        btn.bind("<Enter>", lambda e: btn.configure(bg=WHITE, fg=BLACK) if self._btn_enabled(btn) else None)
        btn.bind("<Leave>", lambda e: btn.configure(bg=BLACK, fg=WHITE) if self._btn_enabled(btn) else None)
        return btn

    def _btn_enabled(self, btn):
        return str(btn.cget("fg")) != DIM

    # ---------- status + console ----------

    def _refresh_status(self):
        import sr_config
        import yc_config

        yc = yc_config.load_config()
        sr = sr_config.load_config()
        self.yc_status_var.set(
            f"Batch:      {yc['batch_tag']} / {yc['yc_batch_slug']}\n"
            f"Automatic:  {'ON' if yc['automatic_mode'] else 'OFF'}\n"
            f"Interval:   every {yc['run_interval_hours']:g}h\n"
            f"Recipients: {', '.join(yc['recipients'])}"
        )
        self.sr_status_var.set(
            f"Batch:      {sr['batch']}\n"
            f"Automatic:  {'ON' if sr['automatic_mode'] else 'OFF'}\n"
            f"Interval:   every {sr['run_interval_hours']:g}h\n"
            f"Recipients: {', '.join(sr['recipients'])}"
        )

    def _drain_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.console.configure(state="normal")
                self.console.insert("end", msg)
                self.console.see("end")
                self.console.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(80, self._drain_log_queue)

    def log(self, msg):
        self.log_queue.put(msg + "\n")

    # ---------- threaded task runner ----------

    def _run_task(self, label, fn):
        if self.task_running:
            self.log("[busy: a task is already running, wait for it to finish]")
            return
        self.task_running = True
        self._set_buttons_enabled(False)
        self.log(f"\n=== {label} ===")

        def worker():
            old_out, old_err = sys.stdout, sys.stderr
            sys.stdout = ConsoleRedirect(self.log_queue)
            sys.stderr = ConsoleRedirect(self.log_queue)
            try:
                fn()
            except SystemExit:
                pass
            except Exception as e:
                self.log_queue.put(f"\nERROR: {e}\n")
            finally:
                sys.stdout, sys.stderr = old_out, old_err
                self.root.after(0, self._task_done)

        threading.Thread(target=worker, daemon=True).start()

    def _task_done(self):
        self.task_running = False
        self._set_buttons_enabled(True)
        self._refresh_status()
        self.log("=== done ===")

    def _set_buttons_enabled(self, enabled):
        for b in self.yc_buttons + self.sr_buttons:
            b.configure(fg=(WHITE if enabled else DIM))

    def _mods(self, kind):
        if kind == "yc":
            import yc_config
            import yc_configure

            return yc_config, yc_configure
        import sr_config
        import sr_configure

        return sr_config, sr_configure

    # ---------- actions ----------

    def auto_on(self, kind):
        def task():
            cfg_mod, ctrl_mod = self._mods(kind)
            config = cfg_mod.load_config()
            if ctrl_mod.enable_automatic(config["run_interval_hours"]):
                config["automatic_mode"] = True
                cfg_mod.save_config(config)

        self._run_task(f"{kind.upper()}: turning automatic mode ON", task)

    def auto_off(self, kind):
        def task():
            cfg_mod, ctrl_mod = self._mods(kind)
            ctrl_mod.disable_automatic()
            config = cfg_mod.load_config()
            config["automatic_mode"] = False
            cfg_mod.save_config(config)

        self._run_task(f"{kind.upper()}: turning automatic mode OFF", task)

    def change_interval(self, kind):
        cfg_mod, _ = self._mods(kind)
        config = cfg_mod.load_config()
        value = self._ask_text(
            f"New interval in hours (current {config['run_interval_hours']:g}):",
            str(config["run_interval_hours"]),
        )
        if value is None:
            return
        try:
            hours = float(value)
            if hours <= 0:
                raise ValueError
        except ValueError:
            self.log("Invalid interval: enter a positive number of hours.")
            return

        def task():
            cfg_mod2, ctrl_mod2 = self._mods(kind)
            config2 = cfg_mod2.load_config()
            config2["run_interval_hours"] = hours
            cfg_mod2.save_config(config2)
            if config2["automatic_mode"]:
                ctrl_mod2.enable_automatic(hours)
            else:
                print(f"Interval set to {hours:g}h (automatic mode is off, so nothing reloaded yet).")

        self._run_task(f"{kind.upper()}: interval -> {hours:g}h", task)

    def change_recipients(self, kind):
        cfg_mod, _ = self._mods(kind)
        config = cfg_mod.load_config()
        value = self._ask_text("New recipients, comma-separated:", ", ".join(config["recipients"]))
        if value is None:
            return
        recipients = [r.strip() for r in value.split(",") if r.strip()]
        if not recipients:
            self.log("Need at least one recipient.")
            return

        def task():
            cfg_mod2, _ = self._mods(kind)
            config2 = cfg_mod2.load_config()
            config2["recipients"] = recipients
            cfg_mod2.save_config(config2)
            print(f"Recipients set to: {', '.join(recipients)}")

        self._run_task(f"{kind.upper()}: recipients -> {', '.join(recipients)}", task)

    def change_batch(self, kind):
        cfg_mod, _ = self._mods(kind)
        config = cfg_mod.load_config()

        if kind == "yc":
            tag = self._ask_text('New batch tag, e.g. "YC W27":', config["batch_tag"])
            if tag is None:
                return
            slug = self._ask_text("New YC batch slug, e.g. winter-2027:", config["yc_batch_slug"])
            if slug is None:
                return

            def task():
                cfg_mod2, _ = self._mods(kind)
                config2 = cfg_mod2.load_config()
                config2["batch_tag"] = tag
                config2["yc_batch_slug"] = slug
                cfg_mod2.save_config(config2)
                print(f"Batch set to {tag} / {slug}")

            self._run_task(f"YC: batch -> {tag} / {slug}", task)
        else:
            code = self._ask_text("New batch code, e.g. SR008:", config["batch"])
            if code is None:
                return
            code = code.strip().upper()

            def task():
                cfg_mod2, _ = self._mods(kind)
                config2 = cfg_mod2.load_config()
                config2["batch"] = code
                cfg_mod2.save_config(config2)
                print(f"Batch set to {code}")

            self._run_task(f"SR: batch -> {code}", task)

    def run_now(self, kind):
        def task():
            print(
                "Running non-interactively: any CAPTCHA/checkpoint page will be "
                "skipped rather than paused for (no terminal attached here to "
                "solve it in). Use `python sr_run_pipeline.py` / "
                "`python yc_run_pipeline.py` from a terminal instead if you need "
                "to solve one by hand.\n"
            )
            if kind == "yc":
                import yc_run_pipeline

                yc_run_pipeline.run(interactive=False)
            else:
                import sr_run_pipeline

                sr_run_pipeline.run(interactive=False)

        self._run_task(f"{kind.upper()}: manual run", task)

    def preflight(self, kind):
        def task():
            if kind == "yc":
                import yc_preflight_check as m
            else:
                import sr_preflight_check as m
            m.RESULTS.clear()
            m.check_env_vars()
            m.check_smtp_login()
            m.check_chrome_profile()
            m.check_chrome_detectable()
            if hasattr(m, "check_yc_batch_slug"):
                m.check_yc_batch_slug()
            failed = [label for label, ok, _ in m.RESULTS if not ok]
            if failed:
                print(f"\n{len(failed)} check(s) failed: {', '.join(failed)}")
            else:
                print("\nAll checks passed.")

        self._run_task(f"{kind.upper()}: preflight check", task)

    # ---------- styled input dialog ----------

    def _ask_text(self, prompt, initial=""):
        result = {"value": None}
        dialog = tk.Toplevel(self.root, bg=BLACK)
        dialog.title("")
        dialog.configure(bg=BLACK)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.geometry("+%d+%d" % (self.root.winfo_rootx() + 80, self.root.winfo_rooty() + 80))

        tk.Label(
            dialog, text=prompt, bg=BLACK, fg=WHITE, font=FONT, wraplength=380, justify="left"
        ).pack(padx=16, pady=(16, 8))
        entry = tk.Entry(
            dialog, bg=BLACK, fg=WHITE, insertbackground=WHITE, font=FONT, width=42, relief="solid", bd=1
        )
        entry.insert(0, initial)
        entry.select_range(0, "end")
        entry.pack(padx=16, pady=(0, 12))
        entry.focus_set()

        btn_row = tk.Frame(dialog, bg=BLACK)
        btn_row.pack(pady=(0, 16))

        def submit(event=None):
            result["value"] = entry.get()
            dialog.destroy()

        def cancel(event=None):
            dialog.destroy()

        ok_btn = self._make_button(btn_row, "OK", submit)
        ok_btn.pack(side="left", padx=6)
        cancel_btn = self._make_button(btn_row, "CANCEL", cancel)
        cancel_btn.pack(side="left", padx=6)

        dialog.bind("<Return>", submit)
        dialog.bind("<Escape>", cancel)
        dialog.wait_window()
        return result["value"]


def main():
    root = tk.Tk()
    root.configure(bg=BLACK)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
