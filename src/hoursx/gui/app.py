"""The HoursX desktop window.

Four tabs, matching what an operator actually does: talk to an agent, watch
what it did, decide on what it wants to do, and look at the machine.
"""

from __future__ import annotations

import sys
from typing import Any

from hoursx.events import Event, EventType
from hoursx.gui.bridge import EngineBridge, UiMessage, describe_event

# Dark palette shared with the web console so the two feel like one product.
INK = "#0b1220"
INK_PANEL = "#111a2e"
INK_EDGE = "#243252"
PULSE = "#38b2ac"
TEXT = "#e2e8f0"
MUTED = "#94a3b8"
WARN = "#f59e0b"
BAD = "#ef4444"

POLL_INTERVAL_MS = 60


def _tk():
    """Import Tkinter lazily so the package imports on headless installs.

    Tkinter is stdlib but optional at the OS level (``python3-tk`` on Debian),
    and importing it at module scope would break ``import hoursx.gui`` — and
    therefore the whole CLI — on a server that will never draw a window.
    """
    import tkinter as tk
    from tkinter import scrolledtext, ttk

    return tk, ttk, scrolledtext


class HoursXWindow:
    def __init__(self) -> None:
        tk, ttk, scrolledtext = _tk()
        self._tk, self._ttk, self._scrolled = tk, ttk, scrolledtext

        self.bridge = EngineBridge()
        self.session_id: str | None = None
        self.pending_approval: dict[str, Any] | None = None
        self._streaming = False

        self.root = tk.Tk()
        self.root.title("HoursX")
        self.root.geometry("1080x720")
        self.root.configure(bg=INK)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._style()
        self._build_header()
        self._build_tabs()
        self._set_status("starting engine…", MUTED)

        self.bridge.start()
        self.root.after(POLL_INTERVAL_MS, self._poll)

    # ------------------------------------------------------------- chrome

    def _style(self) -> None:
        style = self._ttk.Style()
        # 'clam' is the one built-in theme that honours custom colours across
        # Linux, macOS, and Windows; the native themes ignore most of them.
        with_clam = "clam" in style.theme_names()
        style.theme_use("clam" if with_clam else style.theme_use())
        style.configure("TNotebook", background=INK, borderwidth=0)
        style.configure(
            "TNotebook.Tab", background=INK_PANEL, foreground=MUTED, padding=(16, 8), borderwidth=0
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", INK_EDGE)],
            foreground=[("selected", PULSE)],
        )
        style.configure("TFrame", background=INK)
        style.configure("TLabel", background=INK, foreground=TEXT)
        style.configure(
            "Treeview",
            background=INK_PANEL,
            fieldbackground=INK_PANEL,
            foreground=TEXT,
            borderwidth=0,
            rowheight=24,
        )
        style.configure("Treeview.Heading", background=INK_EDGE, foreground=TEXT, borderwidth=0)
        style.map("Treeview", background=[("selected", INK_EDGE)])

    def _build_header(self) -> None:
        tk = self._tk
        header = tk.Frame(self.root, bg=INK, padx=16, pady=10)
        header.pack(fill="x")

        badge = tk.Label(header, text=" hX ", bg=PULSE, fg=INK, font=("TkDefaultFont", 13, "bold"))
        badge.pack(side="left")
        tk.Label(header, text="  HoursX", bg=INK, fg=TEXT, font=("TkDefaultFont", 14, "bold")).pack(
            side="left"
        )

        self.status_label = tk.Label(header, text="", bg=INK, fg=MUTED)
        self.status_label.pack(side="right")

    def _build_tabs(self) -> None:
        notebook = self._ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._build_chat_tab(notebook)
        self._build_activity_tab(notebook)
        self._build_approvals_tab(notebook)
        self._build_system_tab(notebook)

    def _text_area(self, parent: Any, *, height: int = 10, editable: bool = False):
        widget = self._scrolled.ScrolledText(
            parent,
            bg=INK_PANEL,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            borderwidth=0,
            height=height,
            wrap="word",
            font=("TkFixedFont", 10),
            padx=10,
            pady=8,
        )
        if not editable:
            widget.configure(state="disabled")
        return widget

    # --------------------------------------------------------------- chat

    def _build_chat_tab(self, notebook: Any) -> None:
        tk = self._tk
        frame = self._ttk.Frame(notebook)
        notebook.add(frame, text="Chat")

        self.transcript = self._text_area(frame, height=24)
        self.transcript.pack(fill="both", expand=True, pady=(8, 6))
        self.transcript.tag_configure("you", foreground=PULSE, font=("TkFixedFont", 10, "bold"))
        self.transcript.tag_configure("agent", foreground=TEXT)
        self.transcript.tag_configure("tool", foreground=MUTED)
        self.transcript.tag_configure("warn", foreground=WARN)
        self.transcript.tag_configure("bad", foreground=BAD)

        entry_row = tk.Frame(frame, bg=INK)
        entry_row.pack(fill="x", pady=(0, 8))

        self.entry = tk.Text(
            entry_row,
            height=3,
            bg=INK_PANEL,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            wrap="word",
            padx=10,
            pady=8,
        )
        self.entry.pack(side="left", fill="x", expand=True)
        # Enter sends; Shift-Enter inserts a newline, matching chat convention.
        self.entry.bind("<Return>", self._on_enter)
        self.entry.bind("<Shift-Return>", lambda _event: None)

        self.send_button = tk.Button(
            entry_row,
            text="Send",
            command=self._send,
            bg=PULSE,
            fg=INK,
            relief="flat",
            padx=18,
            font=("TkDefaultFont", 10, "bold"),
            activebackground=PULSE,
        )
        self.send_button.pack(side="right", padx=(8, 0), fill="y")

    def _on_enter(self, event: Any) -> str:
        self._send()
        return "break"  # suppress the newline Tk would otherwise insert

    def _send(self) -> None:
        goal = self.entry.get("1.0", "end").strip()
        if not goal:
            return
        if self.bridge.context is None:
            self._append("engine is still starting…\n", "warn")
            return
        self.entry.delete("1.0", "end")
        self._append(f"\nyou › {goal}\n", "you")
        self._append("agent › ", "agent")
        self._streaming = True
        self.send_button.configure(state="disabled")
        self._set_status("running…", PULSE)

        session_id = self.session_id
        from hoursx.cli.engine import ensure_agent, ensure_session

        async def work(context: Any) -> str:
            nonlocal session_id
            if session_id is None:
                agent_id = await ensure_agent(context, "operator")
                session_id = await ensure_session(context, agent_id, "Desktop session")
                self.session_id = session_id
            return await context.conductor.submit_message(
                session_id=session_id, user_id=context.user_id, text=goal
            )

        self.bridge.submit(work, token="submit")

    def _append(self, text: str, tag: str = "agent") -> None:
        self.transcript.configure(state="normal")
        self.transcript.insert("end", text, tag)
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    # ----------------------------------------------------------- activity

    def _build_activity_tab(self, notebook: Any) -> None:
        frame = self._ttk.Frame(notebook)
        notebook.add(frame, text="Activity")
        self.activity = self._text_area(frame, height=26)
        self.activity.pack(fill="both", expand=True, pady=8)

    def _log_activity(self, line: str) -> None:
        self.activity.configure(state="normal")
        self.activity.insert("end", line + "\n")
        self.activity.see("end")
        self.activity.configure(state="disabled")

    # ---------------------------------------------------------- approvals

    def _build_approvals_tab(self, notebook: Any) -> None:
        tk = self._tk
        frame = self._ttk.Frame(notebook)
        notebook.add(frame, text="Approvals")

        self.approval_text = self._text_area(frame, height=16)
        self.approval_text.pack(fill="both", expand=True, pady=8)
        self._render_approval(None)

        buttons = tk.Frame(frame, bg=INK)
        buttons.pack(fill="x", pady=(0, 8))
        self.approve_button = tk.Button(
            buttons,
            text="Approve",
            command=lambda: self._decide(True),
            bg=PULSE,
            fg=INK,
            relief="flat",
            padx=20,
            state="disabled",
            font=("TkDefaultFont", 10, "bold"),
        )
        self.approve_button.pack(side="left")
        self.deny_button = tk.Button(
            buttons,
            text="Deny",
            command=lambda: self._decide(False),
            bg=INK_EDGE,
            fg=TEXT,
            relief="flat",
            padx=20,
            state="disabled",
        )
        self.deny_button.pack(side="left", padx=8)

    def _render_approval(self, payload: dict[str, Any] | None) -> None:
        self.approval_text.configure(state="normal")
        self.approval_text.delete("1.0", "end")
        if payload is None:
            self.approval_text.insert(
                "end",
                "Nothing is waiting on you.\n\n"
                "When an agent reaches a tool call that changes the host, the run "
                "pauses here with the exact arguments it intends to use.",
            )
        else:
            import json

            self.approval_text.insert("end", "The agent wants to run:\n\n")
            self.approval_text.insert("end", f"  {payload.get('tool')}\n\n")
            self.approval_text.insert(
                "end", json.dumps(payload.get("arguments", {}), indent=2) + "\n\n"
            )
            self.approval_text.insert("end", f"{payload.get('reason', '')}\n")
        self.approval_text.configure(state="disabled")

    def _decide(self, approved: bool) -> None:
        if self.pending_approval is None:
            return
        approval_id = str(self.pending_approval.get("approval_id"))
        self.pending_approval = None
        self._render_approval(None)
        self.approve_button.configure(state="disabled")
        self.deny_button.configure(state="disabled")
        self._append(
            f"\n  [{'approved' if approved else 'denied'}]\n", "tool" if approved else "warn"
        )

        async def work(context: Any) -> None:
            await context.conductor.decide_approval(
                approval_id=approval_id, decided_by=context.user_id, approved=approved
            )

        self.bridge.submit(work, token="decide")

    # ------------------------------------------------------------- system

    def _build_system_tab(self, notebook: Any) -> None:
        tk = self._tk
        frame = self._ttk.Frame(notebook)
        notebook.add(frame, text="System")

        controls = tk.Frame(frame, bg=INK)
        controls.pack(fill="x", pady=(8, 0))
        tk.Button(
            controls,
            text="Refresh",
            command=self._refresh_system,
            bg=INK_EDGE,
            fg=TEXT,
            relief="flat",
            padx=16,
        ).pack(side="left")

        self.system_text = self._text_area(frame, height=26)
        self.system_text.pack(fill="both", expand=True, pady=8)
        self._refresh_system()

    def _refresh_system(self) -> None:
        """Read host facts directly — introspection is synchronous and fast, so
        routing it through the engine thread would add latency and no safety."""
        from hoursx.system.probe import kernel_facts, resource_snapshot

        facts = kernel_facts()
        resources = resource_snapshot()

        lines = ["KERNEL", ""]
        for key, value in facts.as_dict().items():
            lines.append(f"  {key:<20} {value}")
        lines += ["", "RESOURCES", ""]
        if resources.memory_total_kb:
            lines.append(
                f"  {'memory used':<20} {resources.memory_used_percent}% "
                f"of {resources.memory_total_kb // 1024} MiB"
            )
        if resources.load_average:
            lines.append(
                f"  {'load average':<20} "
                + ", ".join(f"{value:.2f}" for value in resources.load_average)
            )
        lines.append(f"  {'processes':<20} {resources.process_count}")
        if resources.open_file_descriptors is not None:
            lines.append(
                f"  {'file descriptors':<20} "
                f"{resources.open_file_descriptors} / {resources.file_descriptor_limit}"
            )
        if resources.disks:
            lines += ["", "FILESYSTEMS", ""]
            for disk in resources.disks[:12]:
                lines.append(
                    f"  {disk['mountpoint']:<28} {disk['used_percent']:>5}% used, "
                    f"{disk['free_bytes'] // (1024**3)} GiB free"
                )

        self.system_text.configure(state="normal")
        self.system_text.delete("1.0", "end")
        self.system_text.insert("end", "\n".join(lines))
        self.system_text.configure(state="disabled")

    # --------------------------------------------------------------- loop

    def _set_status(self, text: str, colour: str = MUTED) -> None:
        self.status_label.configure(text=text, fg=colour)

    def _poll(self) -> None:
        """Drain engine messages on the UI thread and apply them to widgets."""
        for message in self.bridge.drain():
            self._handle(message)
        self.root.after(POLL_INTERVAL_MS, self._poll)

    def _handle(self, message: UiMessage) -> None:
        if message.kind == "ready":
            self._set_status("ready", PULSE)
            return
        if message.kind == "error":
            self._set_status("error", BAD)
            self._append(f"\n[error] {message.payload}\n", "bad")
            self._log_activity(f"[error] {message.payload}")
            self.send_button.configure(state="normal")
            return
        if message.kind == "result":
            return
        if message.kind != "event" or not isinstance(message.payload, Event):
            return

        event: Event = message.payload
        self._log_activity(describe_event(event))

        if event.type is EventType.RUN_DELTA:
            self._append(str(event.payload.get("text", "")))
        elif event.type is EventType.RUN_STEP:
            mark = "✓" if event.payload.get("ok") else "✗"
            self._append(f"\n  {mark} {event.payload.get('tool')}\n", "tool")
        elif event.type is EventType.RUN_AWAITING_APPROVAL:
            self.pending_approval = dict(event.payload)
            self._render_approval(self.pending_approval)
            self.approve_button.configure(state="normal")
            self.deny_button.configure(state="normal")
            self._append(
                f"\n  ⏸ waiting for approval: {event.payload.get('tool')} "
                f"(see the Approvals tab)\n",
                "warn",
            )
            self._set_status("waiting for approval", WARN)
        elif event.type is EventType.RUN_FINISHED:
            self._streaming = False
            self.send_button.configure(state="normal")
            status = event.payload.get("status")
            if status == "succeeded":
                self._set_status("ready", PULSE)
                self._append("\n")
            else:
                self._set_status(str(status), BAD if status == "failed" else WARN)
                self._append(f"\n  [{status}] {event.payload.get('error') or ''}\n", "bad")

    def _on_close(self) -> None:
        self._set_status("shutting down…", MUTED)
        self.bridge.stop()
        self.root.after(200, self.root.destroy)

    def run(self) -> None:
        self.root.mainloop()


def launch() -> int:
    """Entry point for ``hoursx gui``."""
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print(
            "The desktop GUI needs Tkinter, which is not installed.\n"
            "  Debian/Ubuntu:  sudo apt install python3-tk\n"
            "  Fedora:         sudo dnf install python3-tkinter\n"
            "  macOS/Windows:  included with python.org builds\n\n"
            "Everything the GUI does is also available from the terminal — "
            "try 'hoursx chat'.",
            file=sys.stderr,
        )
        return 1
    try:
        HoursXWindow().run()
    except Exception as exc:  # noqa: BLE001 — a GUI crash should explain itself
        print(f"GUI failed to start: {exc}", file=sys.stderr)
        return 1
    return 0
