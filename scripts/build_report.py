"""Generate the HoursX engineering report as a PDF.

Every figure in the report comes from this script's constants, which are
transcribed from an actual test and coverage run — the document should never
contain a number nobody measured.

    python scripts/build_report.py [output.pdf]
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

# ----------------------------------------------------------------- brand

INK = colors.HexColor("#0b1220")
SLATE = colors.HexColor("#334155")
MUTED = colors.HexColor("#64748b")
PULSE = colors.HexColor("#0d9488")
PULSE_SOFT = colors.HexColor("#ccfbf1")
RULE = colors.HexColor("#cbd5e1")
ZEBRA = colors.HexColor("#f8fafc")
GOOD = colors.HexColor("#15803d")
WARN = colors.HexColor("#b45309")

PAGE_W, PAGE_H = A4
MARGIN = 20 * mm

# ------------------------------------------------------- measured figures

TOTAL_TESTS = 467
TOTAL_COVERAGE = "67%"
SRC_LOC = 11_271
TEST_LOC = 5_058

SUBSYSTEMS = [
    ("api/", 1853, "REST, SSE, WebSocket, RBAC-gated routers"),
    ("tools/", 1501, "Registry, policy executor, 31 built-in tools"),
    ("cli/", 1411, "In-process engine, commands, terminal rendering"),
    ("system/", 930, "Kernel introspection, host ops, privilege envelope"),
    ("remediation/", 830, "Post-conditions, change ledger, auto-revert"),
    ("providers/", 814, "Model adapters, alias router, resilience"),
    ("gui/", 662, "Tkinter desktop application and asyncio bridge"),
    ("db/", 359, "SQLAlchemy models and async engine"),
    ("sdk/", 198, "Plugin manifest, discovery, marketplace"),
    ("auth/", 156, "Credentials, JWT, role-based access control"),
    ("core modules", 2557, "Runtime, orchestration, memory, RAG, quotas"),
]

TEST_SUITES = [
    ("test_system_privileges.py", 61, "Refusals hold under full privilege"),
    ("test_cli.py", 44, "Parsing, rendering, engine, GUI bridge"),
    ("test_conditions.py", 35, "Post-condition language and safe failure"),
    ("test_guarded_change.py", 33, "Apply, verify, revert, dead-man switch"),
    ("test_system_probe_tools.py", 30, "Kernel introspection and tool gating"),
    ("test_resilience.py", 28, "Retry classification, circuit breaker"),
    ("test_api_admin.py", 28, "Keys, members, audit, quota, cancellation"),
    ("test_run_control.py", 24, "Atomic claiming, cancellation, idempotency"),
    ("test_quotas_pagination.py", 19, "Workspace limits, keyset cursors"),
    ("test_context_events_scheduler.py", 19, "Budgets, event bus, cron, plugins"),
    ("test_vectorstore_audit.py", 17, "Store protocol, audit trail"),
    ("test_tools.py", 15, "Registry grants, executor envelope, sandbox"),
    ("test_router_resilience.py", 15, "Fallback chains, breaker integration"),
    ("test_auth.py", 15, "Hashing, JWT, RBAC matrix"),
    ("test_api_changes.py", 14, "Change ledger over HTTP"),
    ("test_errors.py", 13, "Domain error taxonomy"),
    ("test_providers.py", 11, "Streaming, routing, embeddings"),
    ("test_api.py", 11, "Auth flow, chat flow, workspace isolation"),
    ("test_builtin_tools.py", 10, "Filesystem, shell, git, code, delegation"),
    ("test_run_loop.py", 9, "Tool dispatch, approvals, delegation"),
    ("test_recovery.py", 9, "Orphaned-run requeue"),
    ("test_memory_knowledge.py", 7, "Recall ranking, chunking, retrieval"),
]

COVERAGE_HIGHLIGHTS = [
    ("vectorstore.py", "100%", "Storage protocol and hybrid scoring"),
    ("errors.py", "100%", "Error taxonomy and HTTP mapping"),
    ("quotas.py", "100%", "Concurrency and rate limits"),
    ("audit.py", "100%", "Append-only trail with redaction"),
    ("tools/executor.py", "100%", "The tool policy envelope"),
    ("api/routers/changes.py", "100%", "Operator revert path"),
    ("resilience.py", "99%", "Retry and circuit breaker"),
    ("system/privileges.py", "98%", "The refusal boundary"),
    ("prompts.py", "98%", "Context budgeting"),
    ("recovery.py", "93%", "Crashed-worker recovery"),
    ("knowledge.py", "91%", "Chunking and retrieval"),
    ("agents.py", "89%", "The run loop"),
    ("ledger.py", "85%", "Change ledger and revert"),
    ("guard.py", "83%", "Apply-verify-revert orchestration"),
]

DEFECTS = [
    (
        "Run claiming was a read-then-write race",
        "Two queue workers could claim the same run and duplicate every side "
        "effect it performed: files written twice, shell commands run twice, "
        "model spend doubled.",
        "Replaced with a compare-and-swap that only proceeds when it changed "
        "exactly one row. Found by a test firing concurrent claims.",
    ),
    (
        "The atomic claim was defeated by its own allowed-state set",
        "The CAS permitted claiming from 'running', so a second worker could "
        "still take a run already in flight — the fix was inert.",
        "Restricted to 'queued' only, and added heartbeat-based orphan "
        "recovery so crashed workers still release their work.",
    ),
    (
        "The run loop held a transaction across model and tool calls",
        "Any tool that touched the database deadlocked the parent run. "
        "Delegated child runs failed immediately.",
        "The loop now works from an immutable snapshot and opens short write "
        "transactions per step.",
    ),
    (
        "GUI teardown raced the database driver",
        "Closing the window stopped the event loop while SQLAlchemy was still "
        "disposing, printing tracebacks on every exit.",
        "Teardown now cancels the subscription, disposes the database, then "
        "stops the loop — and waits for bootstrap before starting.",
    ),
    (
        "Documented pgvector swap had no seam",
        "The architecture doc claimed retrieval storage was swappable, but "
        "scoring was inlined in the knowledge engine.",
        "Extracted a VectorStore protocol with two implementations, both "
        "asserted to satisfy it by test.",
    ),
]

ROADMAP = [
    (
        "Fleet operations",
        "Open",
        "One host per process today. Needs a host registry and remote "
        "execution seam; the workspace, RBAC, and audit models already "
        "generalise to many hosts.",
    ),
    (
        "Learning from outcomes",
        "Unblocked",
        "The change ledger now records whether each change actually held. "
        "That verified-versus-reverted signal is exactly what a feedback "
        "loop needs, and it did not exist before the guard shipped.",
    ),
    (
        "Cost governance",
        "Partial",
        "Token and duration accounting is recorded per run and per step. "
        "Budgets, per-workspace attribution, and alerting are not built.",
    ),
    (
        "pgvector implementation",
        "Partial",
        "The protocol and a disabled subclass exist; the backing migration "
        "and index do not. Correct at curated scale, ceiling at corpus scale.",
    ),
]

VERIFIED = [
    ("Test suite", f"{TOTAL_TESTS} tests pass; no network or external services"),
    ("Static analysis", "ruff lint and format clean across 109 files"),
    ("CLI", "Exercised live against real kernel data on this host"),
    ("Desktop GUI", "Constructed under a virtual display; five behavioural assertions"),
    ("Guarded change", "Proven end to end against a live vm.swappiness parameter"),
    ("Console build", "TypeScript compiles; Next.js production build succeeds"),
]

UNVERIFIED = [
    ("Container images", "Never built — no Docker daemon in the build environment"),
    ("Kubernetes manifests", "Parse correctly; never applied to a cluster"),
    ("Managed PostgreSQL", "Tests run on SQLite; Postgres path is untested here"),
    ("Redis queue backend", "arq job bodies are covered; the broker path is not"),
]


# ------------------------------------------------------------------ styles


def build_styles() -> dict:
    base = getSampleStyleSheet()
    return {
        "cover_title": ParagraphStyle(
            "cover_title",
            parent=base["Title"],
            fontSize=44,
            leading=48,
            textColor=colors.white,
            alignment=TA_CENTER,
            spaceAfter=8,
        ),
        "cover_sub": ParagraphStyle(
            "cover_sub",
            parent=base["Normal"],
            fontSize=12.5,
            leading=18,
            textColor=colors.HexColor("#94a3b8"),
            alignment=TA_CENTER,
        ),
        "cover_meta": ParagraphStyle(
            "cover_meta",
            parent=base["Normal"],
            fontSize=9.5,
            leading=15,
            textColor=MUTED,
            alignment=TA_CENTER,
        ),
        "h1": ParagraphStyle(
            "h1",
            parent=base["Heading1"],
            fontSize=19,
            leading=23,
            textColor=INK,
            spaceBefore=4,
            spaceAfter=3,
        ),
        "h2": ParagraphStyle(
            "h2",
            parent=base["Heading2"],
            fontSize=12.5,
            leading=16,
            textColor=PULSE,
            spaceBefore=14,
            spaceAfter=5,
        ),
        "body": ParagraphStyle(
            "body",
            parent=base["Normal"],
            fontSize=9.8,
            leading=15,
            textColor=SLATE,
            alignment=TA_JUSTIFY,
            spaceAfter=8,
        ),
        "lead": ParagraphStyle(
            "lead",
            parent=base["Normal"],
            fontSize=11,
            leading=17,
            textColor=INK,
            alignment=TA_JUSTIFY,
            spaceAfter=10,
        ),
        "cell": ParagraphStyle(
            "cell",
            parent=base["Normal"],
            fontSize=8.4,
            leading=11.5,
            textColor=SLATE,
        ),
        "cell_b": ParagraphStyle(
            "cell_b",
            parent=base["Normal"],
            fontSize=8.4,
            leading=11.5,
            textColor=INK,
            fontName="Helvetica-Bold",
        ),
        "cell_h": ParagraphStyle(
            "cell_h",
            parent=base["Normal"],
            fontSize=8.2,
            leading=11,
            textColor=colors.white,
            fontName="Helvetica-Bold",
        ),
        "kicker": ParagraphStyle(
            "kicker",
            parent=base["Normal"],
            fontSize=8,
            leading=11,
            textColor=MUTED,
            fontName="Helvetica-Bold",
            alignment=TA_CENTER,
        ),
        "stat_n": ParagraphStyle(
            "stat_n",
            parent=base["Normal"],
            fontSize=21,
            leading=24,
            textColor=PULSE,
            alignment=TA_CENTER,
            fontName="Helvetica-Bold",
        ),
        "stat_l": ParagraphStyle(
            "stat_l",
            parent=base["Normal"],
            fontSize=7.6,
            leading=10,
            textColor=MUTED,
            alignment=TA_CENTER,
        ),
        "quote": ParagraphStyle(
            "quote",
            parent=base["Normal"],
            fontSize=9.5,
            leading=15,
            textColor=INK,
            leftIndent=10,
            spaceAfter=8,
        ),
    }


# ------------------------------------------------------------- page frames


def cover_page(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(INK)
    canvas.rect(0, PAGE_H - 74 * mm, PAGE_W, 74 * mm, stroke=0, fill=1)
    canvas.setFillColor(PULSE)
    canvas.rect(0, PAGE_H - 76.5 * mm, PAGE_W, 2.5 * mm, stroke=0, fill=1)
    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", 8)
    canvas.drawCentredString(PAGE_W / 2, 15 * mm, "HoursX Engineering Report")
    canvas.restoreState()


def content_page(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN, PAGE_H - 14 * mm, PAGE_W - MARGIN, PAGE_H - 14 * mm)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(MARGIN, PAGE_H - 12 * mm, "HoursX  |  Engineering Report")
    canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - 12 * mm, date.today().isoformat())
    canvas.line(MARGIN, 14 * mm, PAGE_W - MARGIN, 14 * mm)
    canvas.setFont("Helvetica", 7.5)
    canvas.drawRightString(PAGE_W - MARGIN, 10 * mm, f"{doc.page}")
    canvas.drawString(MARGIN, 10 * mm, "Autonomous AI agent platform")
    canvas.restoreState()


# -------------------------------------------------------------- components


def data_table(rows, widths, styles, *, header=True, aligns=None):
    body_style = TableStyle(
        [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
        ]
    )
    table = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    table.setStyle(body_style)
    if header:
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), INK),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, 0), 8),
                    ("TOPPADDING", (0, 0), (-1, 0), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                ]
            )
        )
        for index in range(2, len(rows), 2):
            table.setStyle(TableStyle([("BACKGROUND", (0, index), (-1, index), ZEBRA)]))
    for column, align in (aligns or {}).items():
        table.setStyle(TableStyle([("ALIGN", (column, 0), (column, -1), align)]))
    return table


def stat_band(items, styles):
    cells = [
        [Paragraph(str(value), styles["stat_n"]) for value, _ in items],
        [Paragraph(label, styles["stat_l"]) for _, label in items],
    ]
    width = (PAGE_W - 2 * MARGIN) / len(items)
    table = Table(cells, colWidths=[width] * len(items))
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, 0), 10),
                ("BOTTOMPADDING", (0, 1), (-1, 1), 10),
                ("BACKGROUND", (0, 0), (-1, -1), PULSE_SOFT),
                ("LINEBEFORE", (1, 0), (-1, -1), 0.5, colors.white),
            ]
        )
    )
    return table


def callout(text, styles, *, tone=PULSE):
    table = Table([[Paragraph(text, styles["quote"])]], colWidths=[PAGE_W - 2 * MARGIN])
    table.setStyle(
        TableStyle(
            [
                ("LINEBEFORE", (0, 0), (0, -1), 2.5, tone),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ("BACKGROUND", (0, 0), (-1, -1), ZEBRA),
            ]
        )
    )
    return table


def section(title, styles):
    line = Table([[""]], colWidths=[PAGE_W - 2 * MARGIN], rowHeights=[2])
    line.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), PULSE)]))
    return [Paragraph(title, styles["h1"]), line, Spacer(1, 9)]


# ------------------------------------------------------------------ story


def build_story(styles) -> list:
    s = styles
    p = lambda text, style="body": Paragraph(text, s[style])  # noqa: E731
    cell = lambda text: Paragraph(str(text), s["cell"])  # noqa: E731
    cellb = lambda text: Paragraph(str(text), s["cell_b"])  # noqa: E731
    head = lambda text: Paragraph(str(text), s["cell_h"])  # noqa: E731
    full = PAGE_W - 2 * MARGIN
    story = []

    # ------------------------------------------------------------- cover
    story += [
        Spacer(1, 20 * mm),
        Paragraph("HoursX", s["cover_title"]),
        Paragraph(
            "An autonomous AI agent platform with governed host access",
            s["cover_sub"],
        ),
        Spacer(1, 46 * mm),
        stat_band(
            [
                (f"{SRC_LOC:,}", "LINES OF SOURCE"),
                (TOTAL_TESTS, "TESTS PASSING"),
                (TOTAL_COVERAGE, "BRANCH COVERAGE"),
                ("31", "AGENT TOOLS"),
            ],
            s,
        ),
        Spacer(1, 26 * mm),
        Paragraph("ENGINEERING REPORT", s["kicker"]),
        Spacer(1, 5),
        Paragraph(
            f"Repository <b>tedo001/hoursX-</b> &nbsp;|&nbsp; branch <b>tedo</b><br/>"
            f"{date.today().strftime('%d %B %Y')}",
            s["cover_meta"],
        ),
        NextPageTemplate("content"),
        PageBreak(),
    ]

    # -------------------------------------------------- executive summary
    story += section("Executive summary", s)
    story += [
        p(
            "HoursX is an agent platform built around a capability most agent "
            "frameworks avoid: letting an agent read and change the machine it "
            "runs on. The engineering effort has gone into making that "
            "survivable rather than merely possible.",
            "lead",
        ),
        p(
            "Three properties distinguish the system. Host operations are "
            "classified before they run, into observation, approved mutation, "
            "or outright refusal. Mutations pass through a per-call human "
            "approval gate that checkpoints the run and waits. And changes are "
            "provisional by default: the agent declares what a change should "
            "achieve, and any change that fails to achieve it is reverted "
            "automatically using the value captured beforehand."
        ),
        p(
            "The result is an agent whose host access produces an audit trail "
            "an operator can review and reverse, rather than an opaque set of "
            "actions taken at machine speed. That combination — real "
            "capability, governed and reversible — is the system's actual "
            "differentiator."
        ),
        callout(
            "<b>The central design commitment:</b> a boundary that is only "
            "tested on the happy path is not a boundary. Refusals are asserted "
            "to hold even when every permission flag is enabled and a human "
            "has already approved the call.",
            s,
        ),
        Spacer(1, 12),
    ]

    story += [Paragraph("Delivered scope", s["h2"])]
    scope_rows = [
        [head("Layer"), head("Delivered")],
        [
            cell("Runtime"),
            cell(
                "Goal-directed run loop with streaming, step budgets, approval "
                "checkpoints, multi-agent delegation, and exactly-once claiming"
            ),
        ],
        [
            cell("Knowledge"),
            cell(
                "Three-tier memory, document ingestion, hybrid vector and keyword "
                "retrieval behind a storage protocol"
            ),
        ],
        [
            cell("Host access"),
            cell(
                "Kernel introspection over /proc and /sys, process and service "
                "control, sysctl tuning — all inside a classified privilege envelope"
            ),
        ],
        [
            cell("Assurance"),
            cell(
                "Post-condition verification, automatic revert, dead-man expiry, "
                "and an operator-facing change ledger"
            ),
        ],
        [
            cell("Platform"),
            cell(
                "Multi-tenant workspaces, four-role RBAC, JWT and API-key auth, "
                "audit trail, quotas, keyset pagination"
            ),
        ],
        [
            cell("Interfaces"),
            cell(
                "REST and WebSocket API, Next.js web console, full CLI, and a "
                "stdlib desktop application"
            ),
        ],
        [
            cell("Operations"),
            cell(
                "Container image, Compose stack, Kubernetes manifests, CI "
                "pipeline, and five documentation guides"
            ),
        ],
    ]
    story += [data_table(scope_rows, [26 * mm, full - 26 * mm], s), Spacer(1, 6)]

    story += [PageBreak()]

    # ------------------------------------------------------- architecture
    story += section("Architecture", s)
    story += [
        p(
            "The backend is asynchronous throughout: FastAPI for transport, "
            "SQLAlchemy 2.0 for persistence, and arq for background work. arq "
            "was chosen over Celery deliberately — job bodies are ordinary "
            "coroutines calling the same runtime code the inline path uses, so "
            "no logic exists in only one execution backend."
        ),
        p(
            "Every subsystem sits behind a small protocol selected by "
            "configuration: model providers, vector stores, task backends, and "
            "tools. Agent profiles reference model <i>intent</i> such as "
            "'fast' or 'deep' rather than vendor names, so changing providers "
            "is a configuration change rather than a code change."
        ),
        Paragraph("Subsystem sizes", s["h2"]),
    ]
    sub_rows = [[head("Module"), head("Lines"), head("Responsibility")]]
    sub_rows += [[cell(name), cell(f"{loc:,}"), cell(desc)] for name, loc, desc in SUBSYSTEMS]
    sub_rows += [[cellb("Total"), cellb(f"{SRC_LOC:,}"), cell("")]]
    story += [
        data_table(sub_rows, [30 * mm, 18 * mm, full - 48 * mm], s, aligns={1: "RIGHT"}),
        Spacer(1, 12),
    ]

    story += [
        Paragraph("Reliability guarantees", s["h2"]),
        p(
            "<b>Exactly-once execution.</b> A run transitions to running "
            "through a compare-and-swap that must change exactly one row. Two "
            "workers pulling the same job cannot both execute it, which "
            "matters because agent side effects are not idempotent."
        ),
        p(
            "<b>Orphan recovery.</b> Single-winner claiming needs a matching "
            "liveness rule. Claimed runs heartbeat, and a sweep re-queues runs "
            "whose heartbeat goes stale — so a crashed worker loses no work. "
            "Runs parked on human approval are excluded, since they wait on a "
            "person rather than a process."
        ),
        p(
            "<b>Cooperative cancellation.</b> Cancellation sets a flag the "
            "loop observes at step boundaries rather than killing the task, so "
            "a tool never dies half-applied."
        ),
        p(
            "<b>Provider resilience.</b> Transient failures retry with "
            "jittered exponential backoff; permanent ones fail immediately. "
            "Per-provider circuit breakers mean a dead vendor is skipped "
            "instantly while a healthy fallback exists."
        ),
    ]

    story += [PageBreak()]

    # ----------------------------------------------------------- security
    story += section("Security model", s)
    story += [
        p(
            "The primary threat is not a malicious operator but a manipulated "
            "model: prompt injection through retrieved documents, fetched "
            "pages, or file contents can make an agent attempt actions nobody "
            "intended. The controls are arranged around that assumption.",
            "lead",
        ),
        Paragraph("Operation classification", s["h2"]),
    ]
    class_rows = [
        [head("Class"), head("Gate"), head("Rationale")],
        [
            cell("READ"),
            cell("None"),
            cell(
                "An agent diagnosing an incident should not need a human for every "
                "/proc read; observation cannot damage the host"
            ),
        ],
        [
            cell("MUTATE"),
            cell("Human approval, per call"),
            cell(
                "Reversible and scoped. The operator sees the tool, its exact "
                "arguments, and any declared post-conditions before deciding"
            ),
        ],
        [
            cell("REFUSED"),
            cell("Always denied"),
            cell(
                "No approval makes the operation recoverable, so no approval is "
                "offered. The refusal states its reason and the alternative"
            ),
        ],
    ]
    story += [
        data_table(class_rows, [20 * mm, 38 * mm, full - 58 * mm], s),
        Spacer(1, 12),
        Paragraph("The refusal set", s["h2"]),
        p(
            "Refusals are deliberately few and specific. A blanket ban would "
            "push operators toward handing the agent a root shell, which is "
            "strictly worse because it discards classification, approval, and "
            "audit in a single step. Naming the genuinely unrecoverable "
            "operations and permitting the rest under approval keeps the safe "
            "path also the convenient one."
        ),
        callout(
            "<b>Kernel module loading is the one capability refused outright "
            "rather than gated.</b> Kernel code has no sandbox, no rollback, "
            "and no error boundary; a bad argument panics the host rather than "
            "raising an exception. Approval does not help, because an operator "
            "who approves a module load cannot un-panic a kernel. Module "
            "inventory remains readable.",
            s,
            tone=WARN,
        ),
        Spacer(1, 8),
        p(
            "Also refused: eight security-critical kernel parameters whose "
            "modification disables the protections that make everything else "
            "survivable; signals to the init process or to the agent's own "
            "process; and stopping the services that carry the operator's own "
            "route back into the machine. All remain fully inspectable."
        ),
        p(
            "The whole host capability is disabled by default and enabled in "
            "two deliberate steps — one for reading, a second for writing. "
            "Enabling inspection never implicitly enables modification."
        ),
    ]

    story += [PageBreak()]

    # -------------------------------------------------- guarded change
    story += section("Guarded change", s)
    story += [
        p(
            "An agent that can change a host but cannot tell whether the "
            "change worked is only half a tool. Guarded change closes that "
            "loop: the agent declares what should become true, and the "
            "platform holds it to that claim.",
            "lead",
        ),
    ]
    seq_rows = [
        [head("Step"), head("Action"), head("Why in this order")],
        [
            cell("1"),
            cell("Read prior state"),
            cell("The revert path must exist before anything is touched"),
        ],
        [
            cell("2"),
            cell("Write ledger row"),
            cell("A crash between apply and record would otherwise strand an unrecorded mutation"),
        ],
        [cell("3"), cell("Apply"), cell("The change reaches the host")],
        [
            cell("4"),
            cell("Settle"),
            cell(
                "Tunables and services do not take effect instantly; verifying "
                "immediately would measure the old state and revert a good change"
            ),
        ],
        [
            cell("5"),
            cell("Verify"),
            cell("Evaluate the declared post-conditions against live host state"),
        ],
        [
            cell("6"),
            cell("Revert on failure"),
            cell("Restore the recorded prior value; unverifiable counts as failed"),
        ],
    ]
    story += [
        data_table(seq_rows, [12 * mm, 34 * mm, full - 46 * mm], s),
        Spacer(1, 12),
        p(
            "<b>Conditions are data, never code.</b> The agent selects a probe "
            "from a closed set of eleven, names a target, and states a "
            "comparison. Nothing model-authored is ever evaluated as an "
            "expression — an evaluator inside a component that also holds host "
            "privileges is precisely the defect this subsystem exists to "
            "prevent."
        ),
        p(
            "<b>Unverifiable counts as failed.</b> If a probe cannot read what "
            "it needs, the change reverts rather than being assumed good. "
            "Treating a broken check as a successful change is how automated "
            "remediation quietly makes incidents worse."
        ),
        callout(
            "<b>The dead-man switch.</b> A change that severs the operator's "
            "access also prevents them from reverting it. So the revert is "
            "armed before the change and fires on silence: the change is "
            "undone unless a human confirms within a set window. This is the "
            "software equivalent of a timed configuration rollback.",
            s,
        ),
        Spacer(1, 8),
        p(
            "The operator path deliberately does not route through the agent. "
            "The moment you most need to undo an agent's change is the moment "
            "you least want to ask the agent to do it, so revert and confirm "
            "are available from the CLI and the API independently."
        ),
    ]

    story += [PageBreak()]

    # ----------------------------------------------------------- testing
    story += section("Testing and verification", s)
    story += [
        p(
            f"The suite is {TOTAL_TESTS} tests across 22 files, {TEST_LOC:,} "
            "lines of test code against "
            f"{SRC_LOC:,} lines of source. It runs with no network access and "
            "no external services: storage is SQLite and the model is a "
            "scriptable deterministic provider, so the full run loop — "
            "including tool dispatch, approval pause and resume, and "
            "multi-agent delegation — is exercised hermetically.",
            "lead",
        ),
        Paragraph("Suite composition", s["h2"]),
    ]
    suite_rows = [[head("Suite"), head("Tests"), head("Focus")]]
    suite_rows += [[cell(n), cell(c), cell(d)] for n, c, d in TEST_SUITES]
    suite_rows += [[cellb("Total"), cellb(TOTAL_TESTS), cell("")]]
    story += [
        data_table(suite_rows, [48 * mm, 14 * mm, full - 62 * mm], s, aligns={1: "RIGHT"}),
        Spacer(1, 6),
        p(
            "The largest suite covers the privilege boundary, and does so by "
            "asserting refusals hold under maximally permissive conditions "
            "rather than by confirming the happy path works."
        ),
    ]

    story += [PageBreak()]
    story += [Paragraph("Coverage of critical modules", s["h2"])]
    cov_rows = [[head("Module"), head("Coverage"), head("What it governs")]]
    cov_rows += [[cell(n), cell(c), cell(d)] for n, c, d in COVERAGE_HIGHLIGHTS]
    story += [
        data_table(cov_rows, [40 * mm, 20 * mm, full - 60 * mm], s, aligns={1: "CENTER"}),
        Spacer(1, 8),
        p(
            f"Overall branch coverage is {TOTAL_COVERAGE}. The distribution is "
            "intentional rather than uniform: the modules that enforce "
            "boundaries, hold privileges, or decide whether a change survives "
            "are covered at 83 to 100 percent, while presentation code such as "
            "CLI command bodies and vendor HTTP adapters is lower. Coverage "
            "was spent where a defect would be dangerous rather than merely "
            "visible."
        ),
        PageBreak(),
    ]

    # Heading, intro, and table are bound together: a table that orphans onto
    # the next page without its heading reads as an unlabelled data dump.
    ver_rows = [[head("Area"), head("Evidence")]]
    ver_rows += [[cell(a), cell(e)] for a, e in VERIFIED]
    story.append(
        KeepTogether(
            [
                Paragraph("Verification status", s["h2"]),
                data_table(ver_rows, [38 * mm, full - 38 * mm], s),
            ]
        )
    )
    story.append(Spacer(1, 12))

    unver_rows = [[head("Area"), head("Status")]]
    unver_rows += [[cell(a), cell(e)] for a, e in UNVERIFIED]
    story.append(
        KeepTogether(
            [
                Paragraph("Explicitly not verified", s["h2"]),
                p(
                    "The following were built and are structurally sound, but "
                    "have not been executed in a real environment. They are "
                    "listed so the gap is visible rather than assumed away."
                ),
                data_table(unver_rows, [38 * mm, full - 38 * mm], s),
            ]
        )
    )

    story += [PageBreak()]

    # ----------------------------------------------------------- defects
    story += section("Defects found and corrected", s)
    story += [
        p(
            "The following were genuine bugs discovered during development, "
            "several of them in code written earlier in the same effort. They "
            "are recorded because the process that surfaced them is part of "
            "the report: in two cases a newly written test invalidated a fix "
            "that had appeared correct.",
            "lead",
        ),
    ]
    for title, impact, fix in DEFECTS:
        block = [
            Paragraph(title, s["h2"]),
            Paragraph(f"<b>Impact.</b> {impact}", s["body"]),
            Paragraph(f"<b>Resolution.</b> {fix}", s["body"]),
        ]
        story.append(KeepTogether(block))

    story += [
        Spacer(1, 4),
        callout(
            "The atomic-claim defect is the clearest example. A "
            "compare-and-swap was written to stop two workers claiming the "
            "same run, but its allowed-state set still included 'running', "
            "making the fix inert. A test firing three concurrent claims "
            "caught it; without that test the system would have shipped "
            "looking correct.",
            s,
        ),
    ]

    story += [PageBreak()]

    # ----------------------------------------------------------- roadmap
    story += section("Assessment and roadmap", s)
    story += [
        p(
            "The system's defensible position is narrow and worth protecting: "
            "it makes agent host access auditable instead of all-or-nothing. "
            "The prevailing alternatives are an agent that cannot touch "
            "anything useful, or one that can touch everything and therefore "
            "cannot pass a security review. The classification, approval, "
            "audit, and revert machinery is what closes that gap.",
            "lead",
        ),
        p(
            "The strongest near-term application is incident response. An "
            "agent that reads the kernel ring buffer, correlates process and "
            "filesystem state, proposes a tunable change with its prior value "
            "recorded for rollback, and waits for a human is a useful on-call "
            "colleague — and the approval gate is what makes a team willing to "
            "run it unattended."
        ),
        Paragraph("Remaining work", s["h2"]),
    ]
    road_rows = [[head("Item"), head("Status"), head("Detail")]]
    road_rows += [[cell(n), cell(st), cell(d)] for n, st, d in ROADMAP]
    story += [
        data_table(road_rows, [34 * mm, 20 * mm, full - 54 * mm], s),
        Spacer(1, 12),
        Paragraph("Recommended constraints", s["h2"]),
        p(
            "<b>Resist becoming a general-purpose agent framework.</b> That "
            "market is crowded and commoditising. Competing on breadth would "
            "mean abandoning the one property that is actually differentiated."
        ),
        p(
            "<b>Do not make the refusal set configurable.</b> Requests to "
            "relax it will arrive. The value of the boundary is precisely that "
            "it is not negotiable; the moment refusals become flags, the "
            "compliance argument collapses. The allowlist can be extended "
            "deliberately — the refusals should not become options."
        ),
        Spacer(1, 10),
        callout(
            "<b>Realistic ceiling.</b> Not autonomous infrastructure "
            "management, but something more useful and more attainable: the "
            "agent layer that operations teams are actually permitted to run "
            "in production. A tool that converts a class of incidents from a "
            "long human investigation into a short reviewed proposal, with a "
            "record of what changed and who approved it.",
            s,
        ),
    ]

    return story


def main() -> int:
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "HoursX-Engineering-Report.pdf")
    styles = build_styles()

    doc = BaseDocTemplate(
        str(output),
        pagesize=A4,
        title="HoursX Engineering Report",
        author="tedo001",
        subject="Autonomous AI agent platform with governed host access",
        creator="HoursX",
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN,
    )
    frame_cover = Frame(MARGIN, MARGIN, PAGE_W - 2 * MARGIN, PAGE_H - 2 * MARGIN, id="cover")
    frame_content = Frame(
        MARGIN, 18 * mm, PAGE_W - 2 * MARGIN, PAGE_H - 18 * mm - 20 * mm, id="content"
    )
    doc.addPageTemplates(
        [
            PageTemplate(id="cover", frames=[frame_cover], onPage=cover_page),
            PageTemplate(id="content", frames=[frame_content], onPage=content_page),
        ]
    )
    doc.build(build_story(styles))

    size_kb = output.stat().st_size / 1024
    print(f"wrote {output} ({size_kb:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
