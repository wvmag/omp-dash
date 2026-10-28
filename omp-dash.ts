// omp-dash: pushes session progress to, and accepts commands from, the local dashboard.
// Server: ./server.py (same directory as this file; default http://127.0.0.1:8734)
// Install: ~/.omp/agent/extensions/omp-dash.ts

import { lookup } from "@oh-my-pi/pi-coding-agent/config/registry";
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
interface TodoItem {
	content: string;
	status: string;
	blocker?: string;
}
interface TodoPhase {
	name: string;
	tasks: TodoItem[];
}

export default function ompDash(pi: ExtensionAPI): void {
	const dashUrl =
		(typeof process !== "undefined" && process.env?.OMP_DASH_URL) || "http://127.0.0.1:8734/report";
	const dashBase = dashUrl.replace(/\/report$/, "");
	const z = pi.zod;
	pi.registerTool({
		name: "post_link",
		label: "Post Link",
		description:
			"Pin a link (PR, ticket, doc, design, dashboard) to this session's card on the local omp dashboard. " +
			"Use it when you create or find a URL relevant to the current work, or when the user asks to save/share a link.",
		parameters: z.object({
			url: z.string().describe("Absolute URL to pin"),
			label: z.string().optional().describe("Short label shown on the card (defaults to the URL)"),
		}),
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const sessionId = ctx.sessionManager?.getSessionId?.() ?? "unknown";
			const res = await fetch(`${dashBase}/link`, {
				method: "POST",
				headers: { "content-type": "application/json" },
				body: JSON.stringify({ sessionId, url: params.url, label: params.label }),
				signal: AbortSignal.timeout(2000),
			});
			if (!res.ok) {
				return {
					content: [{ type: "text", text: `dashboard refused the link (HTTP ${res.status})` }],
					details: { posted: false },
				};
			}
			return { content: [{ type: "text", text: "link pinned to the session's dashboard card" }], details: { posted: true } };
		},
	});
	let lastTodos: TodoPhase[] = [];
	let reported = 0;
	let attention: string | null = null;
	let termInfo: { tty?: string; termProgram?: string } = {};
	let summary: string | null = null;
	let planReview: { reviewId: string; choices: string[] } | null = null;
	let yolo = false;
	const approvalHandle = lookup("tools.approvalMode");
	let serverSpawnTried = false;

	/** Start the dashboard server if it is not running. Safe to call repeatedly. */
	async function ensureServer(
		ctx: { setTimeout?: (fn: () => void, ms: number) => unknown } = {},
	): Promise<boolean> {
		if (typeof fetch !== "function") return false;
		const signal = typeof AbortSignal !== "undefined" && "timeout" in AbortSignal ? AbortSignal.timeout(1200) : undefined;
		try {
			const res = await fetch(`${dashBase}/state`, { signal });
			if (res.ok) return true;
		} catch { /* not running */ }
		if (serverSpawnTried) return false;
		serverSpawnTried = true;
		const serverPath =
			(typeof process !== "undefined" && process.env?.OMP_DASH_SERVER) ||
			new URL("./server.py", import.meta.url).pathname;
		const escaped = String(serverPath).replaceAll("'", "'\\''");
		try {
			await pi.exec("/bin/sh", ["-c", `nohup python3 '${escaped}' >> /tmp/omp-dash-server.log 2>&1 & disown`]);
			pi.logger.debug("omp-dash: spawned dashboard server");
			const { promise, resolve } = Promise.withResolvers<void>();
			const t = ctx.setTimeout?.(() => resolve(), 1500);
			if (t) {
				void t;
			} else {
				setTimeout(resolve, 1500);
			}
			await promise;
			const res = await fetch(`${dashBase}/state`, { signal: AbortSignal.timeout(1200) });
			return res.ok;
		} catch (err) {
			pi.logger.warn(`omp-dash: failed to start dashboard server: ${err instanceof Error ? err.message : String(err)}`);
			return false;
		}
	}

	pi.registerCommand("dash", {
		description: "Open the omp dashboard (starts its server if needed)",
		handler: async (_args, ctx) => {
			const up = await ensureServer(ctx);
			if (up) {
				void pi.exec("open", [dashBase]).catch(() => {});
				ctx.ui.notify(`omp dashboard: ${dashBase}`, "info");
			} else {
				ctx.ui.notify("omp dashboard server failed to start (see /tmp/omp-dash-server.log)", "warning");
			}
		},
	});
 
	function detectTerminal(): void {
		const env = typeof process !== "undefined" ? process.env : undefined;
		termInfo.termProgram = env?.TERM_PROGRAM ?? env?.LC_TERMINAL ?? undefined;
		if (env?.TMUX) {
			termInfo.termProgram = "tmux";
			termInfo.tty = env.TMUX_PANE;
			return;
		}
		// pi.exec's shell has no controlling tty; walk up to the omp process and read its tty.
		const script =
			'p=$PPID; while [ "$p" -gt 1 ]; do c=$(ps -o comm= -p $p 2>/dev/null); ' +
			'case "$c" in *omp*) t=$(ps -o tty= -p $p 2>/dev/null | tr -d " "); ' +
			'[ "$t" != "??" ] && [ -n "$t" ] && echo "/dev/$t"; break;; esac; ' +
			'p=$(ps -o ppid= -p $p 2>/dev/null | tr -d " "); done';
		void pi
			.exec("/bin/sh", ["-c", script])
			.then((r: unknown) => {
				const out = typeof r === "string" ? r : ((r as { stdout?: string })?.stdout ?? "");
				const tty = String(out).trim();
				pi.logger.debug(`omp-dash: tty probe -> ${tty}`);
				if (tty.startsWith("/dev/")) termInfo.tty = tty;
			})
			.catch((err: unknown) => {
				pi.logger.warn(`omp-dash: tty probe failed: ${err instanceof Error ? err.message : String(err)}`);
			});
	}
	detectTerminal();

	let gitInfo: { repoUrl?: string; branch?: string; prUrl?: string } = {};

	function detectGit(cwd: string | undefined): void {
		if (!cwd) return;
		const q = cwd.replaceAll("'", "'\\''");
		const script =
			`b=$(git -C '${q}' branch --show-current 2>/dev/null); ` +
			`r=$(git -C '${q}' remote get-url origin 2>/dev/null); ` +
			`p=""; ` +
			`if [ -n "$b" ] && command -v gh >/dev/null 2>&1; then ` +
			`p=$(gh pr list --head "$b" --json url --jq '.[0].url' 2>/dev/null); fi; ` +
			`printf 'BR=%s\\nREPO=%s\\nPR=%s\\n' "$b" "$r" "$p"`;
		void pi
			.exec("/bin/sh", ["-c", script])
			.then((r: unknown) => {
				const out = typeof r === "string" ? r : ((r as { stdout?: string })?.stdout ?? "");
				const val = (k: string) => out.match(new RegExp(`^${k}=(.*)$`, "m"))?.[1]?.trim();
				const branch = val("BR");
				const repoUrl = val("REPO");
				const prUrl = val("PR");
				pi.logger.debug(`omp-dash: git probe -> branch=${branch} repo=${repoUrl} pr=${prUrl}`);
				if (branch) gitInfo.branch = branch;
				if (repoUrl) gitInfo.repoUrl = repoUrl;
				if (prUrl) gitInfo.prUrl = prUrl;
			})
			.catch((err: unknown) => {
				pi.logger.warn(`omp-dash: git probe failed: ${err instanceof Error ? err.message : String(err)}`);
			});
	}

	/** Latest successful `todo` tool result on the branch carries message.details.phases. */
	function todosFromBranch(ctx: { sessionManager?: { getBranch?: () => unknown[] } }): TodoPhase[] {
		try {
			const entries = ctx.sessionManager?.getBranch?.() ?? [];
			for (let i = entries.length - 1; i >= 0; i--) {
				const entry = entries[i];
				if (typeof entry !== "object" || entry === null || !("message" in entry)) continue;
				const message: unknown = entry.message;
				if (typeof message !== "object" || message === null || !("role" in message) || !("toolName" in message))
					continue;
				const msg = message as { role: unknown; toolName: unknown; details?: unknown };
				if (msg.role !== "toolResult" || msg.toolName !== "todo") continue;
				if (typeof msg.details === "object" && msg.details !== null && "phases" in msg.details) {
					const phases: unknown = msg.details.phases;
					if (Array.isArray(phases) && phases.length > 0) return phases as TodoPhase[];
				}
			}
		} catch {
			// branch shape differs across versions; fall back to last seen state
		}
		return lastTodos;
	}

	async function report(
		ctx: {
			agent?: { kind?: string; name?: string; parentId?: string };
			cwd?: string;
			model?: { provider?: string; id?: string } | null;
			getContextUsage?: () => { percent?: number } | undefined;
			isIdle?: () => boolean;
			sessionManager?: { getSessionId?: () => string };
		},
		status: string,
		todos?: TodoPhase[],
	): Promise<void> {
		if (todos) lastTodos = todos;
		if (typeof fetch !== "function") {
			pi.logger.warn("omp-dash: global fetch unavailable, cannot report");
			return;
		}
		try {
			const agent = ctx.agent ?? { kind: "main" as const };
			const usage = ctx.getContextUsage?.();
			const payload = {
				sessionId: ctx.sessionManager?.getSessionId?.() ?? "unknown",
				kind: agent.kind ?? "main",
				parentId: agent.parentId,
				agentName: agent.name,
				name: pi.getSessionName?.(),
				cwd: ctx.cwd,
				model: ctx.model ? { provider: ctx.model.provider, id: ctx.model.id } : null,
				status,
				todos: lastTodos,
				contextPercent: usage?.percent ?? null,
				...termInfo,
				repoUrl: (agent.kind ?? "main") === "main" ? gitInfo.repoUrl : undefined,
				branch: (agent.kind ?? "main") === "main" ? gitInfo.branch : undefined,
				prUrl: (agent.kind ?? "main") === "main" ? gitInfo.prUrl : undefined,
				attention,
				yolo: yolo || approvalHandle?.get(pi.pi.settings) === "yolo" || undefined,
				planReview: (agent.kind ?? "main") === "main" ? planReview : undefined,
				summary,
				ts: Date.now(),
			};
			const signal =
				typeof AbortSignal !== "undefined" && "timeout" in AbortSignal ? AbortSignal.timeout(2000) : undefined;
			return fetch(dashUrl, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload), signal })
				.then(() => {
					reported++;
					pi.logger.debug(`omp-dash: reported #${reported} (${payload.status})`);
				})
				.catch((err: unknown) => {
					pi.logger.warn(`omp-dash: report failed: ${err instanceof Error ? err.message : String(err)}`);
				});
		} catch (err) {
			pi.logger.warn(`omp-dash: report build failed: ${err instanceof Error ? err.message : String(err)}`);
		}
	}

	let summaryBusy = false;
	let lastSummaryTs = 0;

	/** Run the ste-check vale style on a sentence; returns the finding lines (empty = clean). */
	function steFindings(text: string): Promise<string[]> {
		const valeDir = (typeof process !== "undefined" && process.env?.OMP_STE_VALE_DIR) || "";
		if (!valeDir) return Promise.resolve([]);
		if (typeof text !== "string" || !text.trim()) return Promise.resolve([]);
		const escaped = text.replaceAll("'", "'\\''");
		const dirEscaped = valeDir.replaceAll("'", "'\\''");
		const cmd =
			`printf '%s\\n' '${escaped}' > /tmp/omp-dash-ste.md && ` +
			`cd '${dirEscaped}' 2>/dev/null && vale --output=line /tmp/omp-dash-ste.md 2>/dev/null || true`;
		return pi
			.exec("/bin/sh", ["-c", cmd])
			.then((r: unknown) => {
				const out = typeof r === "string" ? r : ((r as { stdout?: string })?.stdout ?? "");
				return out.split("\n").map((l) => l.trim()).filter((l) => l.includes("ste."));
			})
			.catch(() => []);
	}

	async function refreshSummary(ctx: {
		agent?: { kind?: string };
		runEphemeralTurn?: (opts: {
			promptText: string;
			tools: false;
			maxTokens: number;
			maxContextBytes: number;
		}) => Promise<{ replyText?: string }>;
	}): Promise<void> {
		if (summaryBusy || Date.now() - lastSummaryTs < 300_000) return;
		if ((ctx.agent?.kind ?? "main") !== "main" || typeof ctx.runEphemeralTurn !== "function") return;
		summaryBusy = true;
		try {
			const res = await ctx.runEphemeralTurn({
				promptText:
					"In ONE short sentence (max 20 words), state what this coding session is currently working on and its current state. " +
					"Style rules: active voice, simple present, no -ly adverbs, no noun clusters of 4+ words. " +
					"Reply with only that sentence: plain text, no quotes, no preamble.",
				tools: false,
				maxTokens: 400,
				maxContextBytes: 1_000_000,
			});
			let text = (res?.replyText ?? "").trim().replace(/^["'`]+|["'`]+$/g, "").split("\n")[0].slice(0, 200);
			if (text) {
				// STE gate: check the sentence, rewrite once if it fails, keep the cleaner version.
				const findings = await steFindings(text);
				pi.logger.debug(`omp-dash: ste findings (${findings.length}): ${findings.join(" | ") || "clean"}`);
				if (findings.length > 0) {
					const fix = await ctx.runEphemeralTurn({
						promptText:
							"Rewrite the sentence so it passes these Simplified Technical English checks. Keep the meaning. " +
							"Rules: max 20 words, active voice, simple present, no -ly adverbs, no noun clusters of 4+ words. " +
							`Reply with only the rewritten sentence.\n\nSentence: ${text}\n\nChecks that failed:\n${findings.join("\n")}`,
						tools: false,
						maxTokens: 400,
						maxContextBytes: 1_000_000,
					});
					const rewrite = (fix?.replyText ?? "").trim().replace(/^["'`]+|["'`]+$/g, "").split("\n")[0].slice(0, 200);
					let candidate = rewrite;
					if (!candidate) {
						// rewrite came back empty: regenerate with the style rules instead
						pi.logger.debug("omp-dash: ste rewrite empty; regenerating summary with rules");
						const regen = await ctx.runEphemeralTurn({
							promptText:
								"In ONE short sentence (max 20 words), state what this coding session is currently working on and its current state. " +
								"Style rules: active voice, simple present, no -ly adverbs, no noun clusters of 4+ words. " +
								"Reply with only that sentence: plain text, no quotes, no preamble.",
							tools: false,
							maxTokens: 400,
							maxContextBytes: 1_000_000,
						});
						candidate = (regen?.replyText ?? "").trim().replace(/^["'`]+|["'`]+$/g, "").split("\n")[0].slice(0, 200);
					}
					if (candidate) {
						const candidateFindings = await steFindings(candidate);
						pi.logger.debug(`omp-dash: ste rewrite findings (${candidateFindings.length}): ${candidateFindings.join(" | ") || "clean"}`);
						if (candidateFindings.length <= findings.length) {
							text = candidate;
						}
					}
				}
				summary = text;
				lastSummaryTs = Date.now();
				pi.logger.debug(`omp-dash: summary -> ${text}`);
				void report(ctx, "working");
			}
		} catch (err) {
			pi.logger.debug(`omp-dash: summary failed: ${err instanceof Error ? err.message : String(err)}`);
		} finally {
			summaryBusy = false;
		}
	}

	function isCommand(v: unknown): v is { type: string; text?: string; choice?: string; reviewId?: string; value?: string } {
		if (typeof v !== "object" || v === null || !("type" in v)) return false;
		const c = v as { type: unknown; text?: unknown; choice?: unknown; reviewId?: unknown; value?: unknown };
		if (typeof c.type !== "string") return false;
		for (const k of ["text", "choice", "reviewId", "value"] as const) {
			if (k in c && typeof c[k] !== "string") return false;
		}
		return true;
	}

	async function executeCommand(
		command: { type: string; text?: string; choice?: string; reviewId?: string; value?: string },
		ctx: {
			agent?: { kind?: string };
			isIdle?: () => boolean;
			resolvePlanReview?: (reviewId: string, choice: string, input?: { feedback: string }) => Promise<boolean>;
		},
	): Promise<void> {
		if (command.type === "steer" && command.text?.trim()) {
			// idle: starts a turn; streaming: steers the run — same as typing in the TUI
			await pi.sendUserMessage(command.text.trim());
			pi.logger.debug("omp-dash: executed steer command");
		} else if (command.type === "plan_review" && command.reviewId && command.choice) {
			if (typeof ctx.resolvePlanReview !== "function") {
				pi.logger.warn("omp-dash: resolvePlanReview unavailable on this host");
				return;
			}
			const input = command.choice === "refine" && command.text ? { feedback: command.text } : undefined;
			const resolved = await ctx.resolvePlanReview(command.reviewId, command.choice, input);
			pi.logger.debug(`omp-dash: plan review ${command.reviewId} -> ${command.choice}: ${resolved}`);
		} else if (command.type === "approval_mode" && approvalHandle) {
			if (command.value === "yolo") {
				approvalHandle.override(pi.pi.settings, "yolo");
			} else {
				approvalHandle.clearOverride(pi.pi.settings);
			}
			yolo = approvalHandle.get(pi.pi.settings) === "yolo";
			pi.logger.debug(`omp-dash: approval mode override -> ${yolo ? "yolo" : "session default"}`);
			void report(ctx, ctx.isIdle?.() ? "idle" : "working");
		}
	}

	async function pollCommands(ctx: {
		agent?: { kind?: string };
		isIdle?: () => boolean;
		resolvePlanReview?: (reviewId: string, choice: string, input?: { feedback: string }) => Promise<boolean>;
		sessionManager?: { getSessionId?: () => string };
	}): Promise<void> {
		const sid = ctx.sessionManager?.getSessionId?.();
		if (!sid || typeof fetch !== "function") return;
		try {
			const signal =
				typeof AbortSignal !== "undefined" && "timeout" in AbortSignal ? AbortSignal.timeout(1500) : undefined;
			const res = await fetch(`${dashBase}/commands/${encodeURIComponent(sid)}`, { signal });
			if (!res.ok) return;
			const data = (await res.json()) as { commands?: unknown };
			const commands = Array.isArray(data.commands) ? data.commands.filter(isCommand) : [];
			for (const command of commands) {
				await executeCommand(command, ctx);
			}
		} catch (err) {
			pi.logger.debug(`omp-dash: command poll failed: ${err instanceof Error ? err.message : String(err)}`);
		}
	}

	pi.on("session_start", (_event, ctx) => {
		void ensureServer(ctx);
		detectGit(ctx.cwd);
		void report(ctx, ctx.isIdle?.() ? "idle" : "working", todosFromBranch(ctx));
		if (ctx.sessionManager?.getSessionId?.()) {
			ctx.setInterval(() => {
				void pollCommands(ctx);
			}, 2000);
		}
	});
	pi.on("turn_start", (_event, ctx) => {
		void report(ctx, "working");
	});
	pi.on("turn_end", (_event, ctx) => {
		if (askPending) { askPending = false; attention = null; }
		void report(ctx, "idle", todosFromBranch(ctx));
		void refreshSummary(ctx);
	});
	let askPending = false;
	pi.on("tool_call", (event, ctx) => {
		if (!("toolName" in event) || event.toolName !== "ask") return;
		let question: unknown;
		const src = ("input" in event ? event.input : undefined) ?? ("args" in event ? event.args : undefined);
		if (src && typeof src === "object" && "questions" in src && Array.isArray(src.questions)) {
			const first = src.questions[0];
			if (first && typeof first === "object" && "question" in first) question = first.question;
		}
		attention = typeof question === "string" && question.trim()
			? `input: ${question.trim().slice(0, 60)}${question.trim().length > 60 ? "…" : ""}`
			: "needs your input";
		askPending = true;
		pi.logger.debug(`omp-dash: attention set (${attention})`);
		void report(ctx, "working");
	});
	pi.on("tool_result", (event, ctx) => {
		if (event.toolName === "ask") {
			askPending = false;
			attention = null;
			void report(ctx, ctx.isIdle?.() ? "idle" : "working");
			return;
		}
		if (event.toolName !== "todo") return;
		const details: unknown = event.details;
		if (typeof details === "object" && details !== null && "phases" in details) {
			const phases: unknown = details.phases;
			if (Array.isArray(phases)) void report(ctx, "working", phases as TodoPhase[]);
		}
	});
	pi.on("todo_reminder", (event, ctx) => {
		const raw: unknown = (event as { todos?: unknown }).todos;
		if (!Array.isArray(raw)) return;
		const tasks: TodoItem[] = [];
		for (const t of raw) {
			if (typeof t === "object" && t !== null && "content" in t && "status" in t) {
				const item = t as { content: unknown; status: unknown };
				if (typeof item.content === "string" && typeof item.status === "string") {
					tasks.push({ content: item.content, status: item.status });
				}
			}
		}
		if (tasks.length > 0) void report(ctx, "working", [{ name: "Tasks", tasks }]);
	});
	// Observer-only handlers: returning nothing never answers the approval itself.
	pi.on("tool_approval_requested", (event, ctx) => {
		const tool = (event as { toolName?: unknown }).toolName;
		attention = `approval needed: ${typeof tool === "string" ? tool : "tool"}`;
		pi.logger.debug(`omp-dash: attention set (${attention})`);
		void report(ctx, "working");
	});
	pi.on("tool_approval_resolved", (_event, ctx) => {
		attention = null;
		void report(ctx, ctx.isIdle?.() ? "idle" : "working");
	});
	pi.on("plan_review_requested", (event, ctx) => {
		attention = "plan review";
		const raw: unknown = (event as { reviewId?: unknown; choices?: unknown }).choices;
		const reviewId: unknown = (event as { reviewId?: unknown }).reviewId;
		if (typeof reviewId === "string" && Array.isArray(raw)) {
			const ids = raw
				.map((c) => (typeof c === "object" && c !== null && "id" in c ? String((c as { id: unknown }).id) : null))
				.filter((v): v is string => v !== null);
			planReview = { reviewId, choices: ids };
		}
		void report(ctx, "working");
	});
	pi.on("plan_review_resolved", (_event, ctx) => {
		attention = null;
		planReview = null;
		void report(ctx, ctx.isIdle?.() ? "idle" : "working");
	});
	pi.on("session_shutdown", (_event, ctx) => report(ctx, "ended"));
}
