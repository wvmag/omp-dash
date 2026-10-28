#!/usr/bin/env python3
"""omp-dash: local dashboard for omp session progress.

Run:  python3 server.py
Env:  OMP_DASH_PORT (default 8734), OMP_DASH_HOST (default 127.0.0.1)

Endpoints:
  POST /report  {sessionId, name?, cwd?, model?, status, todos?, ...}
  GET  /state   JSON snapshot of all sessions
  GET  /        dashboard HTML
"""

import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("OMP_DASH_PORT", "8734"))
MAX_AGE_MS = 24 * 60 * 60 * 1000  # forget sessions unseen for a day


def _load_link_patterns() -> list:
    path = os.environ.get("OMP_DASH_LINKS",
                          os.path.join(os.path.dirname(os.path.abspath(__file__)), "links.json"))
    try:
        with open(path) as f:
            return json.load(f).get("patterns", [])
    except (OSError, ValueError):
        return []


LINK_PATTERNS = _load_link_patterns()
STATE_FILE = os.environ.get("OMP_DASH_STATE",
                            os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"))
STATE_LOCK = threading.Lock()
PENDING_COMMANDS: dict = {}  # sessionId -> [command, ...] (in-memory, not persisted)


def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            raw = json.load(f)
        now_ms = time.time() * 1000
        return {k: v for k, v in raw.items() if now_ms - (v.get("lastSeen") or 0) < MAX_AGE_MS}
    except (OSError, ValueError):
        return {}


def _save_state() -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(STATE, f)
    except OSError as e:
        print(f"omp-dash: failed to save state: {e}")


STATE = _load_state()  # sessionId -> last report (dict)
STATUS_ORDER = {"working": 0, "idle": 1, "ended": 2}

ITERM_SCRIPT = """
tell application "iTerm2"
	repeat with w in windows
		repeat with t in tabs of w
			repeat with s in sessions of t
				if (tty of s) is equal to "__TTY__" then
					select w
					select t
					select s
					activate
					return "matched"
				end if
			end repeat
		end repeat
	end repeat
end tell
return "nomatch"
"""

TERMINAL_SCRIPT = """
tell application "Terminal"
	repeat with w in windows
		repeat with t in tabs of w
			if (tty of t) is equal to "__TTY__" then
				set frontmost to true
				set index of w to 1
				set selected of t to true
				return "matched"
			end if
		end repeat
	end repeat
end tell
return "nomatch"
"""
TTY_RE = re.compile(r"^/dev/tty[sS]?[0-9]+$")



def _focus_session(sid: str) -> tuple[bool, str]:
    session = STATE.get(sid)
    if not session:
        return False, "unknown session"
    tty = session.get("tty") or ""
    app = session.get("termProgram") or ""
    if app == "tmux" and tty.startswith("%"):
        try:
            panes = subprocess.run(
                ["tmux", "list-panes", "-a", "-F", "#{pane_id} #{session_name}:#{window_index}"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            for line in panes.splitlines():
                pane_id, _, target = line.partition(" ")
                if pane_id == tty:
                    subprocess.run(["tmux", "switch-client", "-t", target],
                                   capture_output=True, timeout=5)
                    return True, target
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, str(e)
        return False, "tmux pane not found"
    if not TTY_RE.match(tty):
        return False, "session has no scriptable terminal tty"
    if "iTerm" in app:
        script = ITERM_SCRIPT.replace("__TTY__", tty)
    elif app == "Apple_Terminal":
        script = TERMINAL_SCRIPT.replace("__TTY__", tty)
    else:
        return False, f"unsupported terminal: {app}"
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    if result.returncode != 0:
        return False, result.stderr.strip() or "osascript failed"
    if "matched" not in result.stdout:
        return False, f"no {app} tab with tty {tty} (stale session? restart omp to re-report)"
    return True, "ok"

PAGE = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>omp dashboard</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2064%2064'%3E%3Crect%20width='64'%20height='64'%20rx='12'%20fill='%2316110c'/%3E%3Crect%20x='4'%20y='4'%20width='56'%20height='56'%20rx='9'%20fill='none'%20stroke='%23c9a227'%20stroke-width='2.5'/%3E%3Ctext%20x='32'%20y='45'%20font-size='34'%20text-anchor='middle'%20fill='%23e8c95a'%3E%E2%9A%9C%3C/text%3E%3C/svg%3E">
<style>
@font-face { font-family: "Cinzel"; src: url("/fonts/cinzel.woff2") format("woff2");
             font-weight: 400 900; font-display: swap; }
@font-face { font-family: "Alegreya"; src: url("/fonts/alegreya.woff2") format("woff2");
             font-weight: 400 900; font-style: normal; font-display: swap; }
@font-face { font-family: "Alegreya"; src: url("/fonts/alegreya-italic.woff") format("woff");
             font-weight: 400 900; font-style: italic; font-display: swap; }
 :root {
  --ink: #33291a; --ink-soft: #6b5c40; --parchment: #f2e8d0; --parchment2: #e4d4ae;
  --bronze: #8a6d3b; --gold: #c9a227; --gold-bright: #e8c95a;
  --night: #16110c; --panel: #241b10; --panel-fg: #e8d9a8;
  --ember: #c25e1e; --moss: #7a7a52; --blood: #8b1a1a; --faded: #a89a7d;
  --mana: #5b7fa6; --ok: #4a6b2a;
}
* { box-sizing: border-box; }
body {
  background:
    radial-gradient(ellipse at 50% -10%, #2e2416 0%, rgba(22, 17, 12, 0) 55%),
    radial-gradient(ellipse at 85% 110%, rgba(194, 94, 30, .07) 0%, rgba(22, 17, 12, 0) 50%),
    repeating-linear-gradient(45deg, rgba(255, 255, 255, .012) 0 2px, rgba(0, 0, 0, 0) 2px 5px),
    var(--night);
  background-attachment: fixed;
  color: var(--ink); margin: 0; padding: 24px;
  font: 14px/1.5 "Alegreya", Georgia, "Times New Roman", serif;
}
h1 {
  font-family: "Cinzel", Georgia, serif;
  font-size: 26px; margin: 0; text-align: center; letter-spacing: 7px;
  text-transform: uppercase;
  background:
    linear-gradient(115deg, rgba(255,250,225,0) 42%, rgba(255,250,225,.75) 50%, rgba(255,250,225,0) 58%) no-repeat,
    linear-gradient(180deg, #f6e396 0%, var(--gold-bright) 40%, #9a7a28 100%);
  background-size: 250% 100%, 100% 100%;
  background-position: 180% 0, 0 0;
  -webkit-background-clip: text; background-clip: text; color: transparent;
  filter: drop-shadow(0 2px 10px rgba(201, 162, 39, .3));
  animation: sheen 5.5s ease-in-out 2.5s infinite;
}
.rule { display: flex; align-items: center; gap: 14px; margin: 6px auto 18px; max-width: 560px; }
.rule::before, .rule::after { content: ""; flex: 1; height: 1px;
  background: linear-gradient(90deg, rgba(201, 162, 39, 0), rgba(201, 162, 39, .7)); }
.rule::after { background: linear-gradient(90deg, rgba(201, 162, 39, .7), rgba(201, 162, 39, 0)); }
.rule span { color: var(--gold); font-size: 13px; }
.chips { display: flex; flex-wrap: wrap; justify-content: center; gap: 8px; margin-bottom: 18px; }
.chip { display: inline-block; padding: 3px 14px; border-radius: 3px;
        background: linear-gradient(180deg, #2c2114 0%, #1f180e 100%);
        border: 1px solid var(--bronze); color: var(--panel-fg); font-size: 12px;
        box-shadow: inset 0 1px 0 rgba(232, 201, 90, .14), 0 2px 6px rgba(0, 0, 0, .4); }
.chip b { color: var(--gold-bright); }
.chip.attention { background: var(--blood); border-color: #c23b3b; color: #ffe9e9;
                  font-weight: bold; animation: blinkA 1s infinite; }
.paneltitle { font-family: "Cinzel", Georgia, serif; color: var(--gold); font-size: 12px; letter-spacing: 4px; text-transform: uppercase;
              margin: 0 0 6px; text-align: center; }
.nowpanel { background: var(--panel); border: 1px solid var(--bronze); border-radius: 4px;
            padding: 12px 18px; margin: 0 auto 22px; max-width: 900px;
            box-shadow: inset 0 0 30px rgba(0,0,0,.5); }
.nowrow { display: flex; gap: 10px; align-items: baseline; padding: 4px 0; font-size: 13px;
          color: var(--panel-fg); border-bottom: 1px solid rgba(138, 109, 59, .25); }
.nowrow:last-child { border-bottom: none; }
.nowrow .dot { align-self: center; }
.nowname { font-weight: bold; flex: none; max-width: 34%; overflow: hidden;
           text-overflow: ellipsis; white-space: nowrap; color: var(--gold-bright); }
.nowname, .cardname { cursor: pointer; }
.nowname:hover, .cardname:hover { text-decoration: underline; text-decoration-color: var(--gold); }
.nowtask { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.nowtask .mut { color: #a8977a; font-style: italic; }
.nowprog { flex: none; color: #a8977a; font-size: 11px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(370px, 1fr));
        gap: 16px; }
.card {
  background:
    radial-gradient(140% 90% at 15% 0%, rgba(255, 255, 255, .5) 0%, rgba(255, 255, 255, 0) 55%),
    radial-gradient(100% 130% at 100% 100%, rgba(138, 109, 59, .18) 0%, rgba(138, 109, 59, 0) 60%),
    linear-gradient(160deg, var(--parchment) 0%, var(--parchment2) 100%);
  border: 2px solid var(--bronze); border-radius: 4px; padding: 14px 16px;
  box-shadow: 0 0 0 1px var(--gold) inset, 0 3px 12px rgba(0, 0, 0, .55), inset 0 0 40px rgba(138, 109, 59, .12);
  position: relative;
  transition: transform .15s ease, box-shadow .15s ease;
}
.card:hover { transform: translateY(-2px);
  box-shadow: 0 10px 24px rgba(0, 0, 0, .65), inset 0 0 40px rgba(138, 109, 59, .12); }
.card::before {
  content: ""; position: absolute; inset: 3px; border: 1px solid rgba(201, 162, 39, .55);
  border-radius: 2px; pointer-events: none;
}
.card::after {
  content: "\2767"; position: absolute; right: 10px; bottom: 6px; color: var(--bronze);
  opacity: .4; font-size: 15px; pointer-events: none;
}
.card.needs { border-color: var(--blood); animation: needGlow 1.6s ease-in-out infinite; }
@keyframes needGlow {
  0%, 100% { box-shadow: 0 0 10px rgba(139, 26, 26, .55), inset 0 0 40px rgba(138, 109, 59, .12); }
  50% { box-shadow: 0 0 26px rgba(139, 26, 26, .95), inset 0 0 40px rgba(138, 109, 59, .12); }
}
.card h2 { font-family: "Cinzel", Georgia, serif; font-size: 14.5px; margin: 0 0 4px; display: flex; align-items: center; gap: 8px;
           letter-spacing: .5px; color: #7a1f1f; text-shadow: 0 1px 0 rgba(255,255,255,.5); }
.dot { width: 9px; height: 9px; border-radius: 50%; flex: none; box-shadow: 0 0 0 1px rgba(0,0,0,.25); }
.dot.working { background: var(--ember); animation: pulse 1.4s infinite; }
.dot.idle { background: var(--moss); }
.dot.ended { background: var(--faded); }
.dot.stale { background: #b08d3f; }
.dot.needs { background: var(--blood); animation: blinkA .9s infinite; }
.nowpanel .dot.working { background: #ff9d4d; }
.nowpanel .dot.idle { background: #9aa06a; }
.nowpanel .dot.needs { background: #ff3b30; }
@keyframes pulse { 50% { opacity: .35; } }
.seal { background: var(--blood); border: 1px solid #5e0f0f; color: #ffe9e9 !important;
        font-weight: bold; animation: blinkA 1s infinite; text-transform: none; letter-spacing: 0;
        white-space: nowrap; font-size: 10px; padding: 1px 8px; flex: none; }
.stamp { font-family: "Cinzel", Georgia, serif; font-size: 9px; text-transform: uppercase; letter-spacing: 1px;
         padding: 1px 9px; border-radius: 2px; border: 1.5px solid var(--ink-soft);
         color: var(--ink-soft); background: rgba(255,255,255,.25);
         transform: rotate(-1.5deg); text-shadow: 0 1px 0 rgba(255,255,255,.4); }
.stamp.stale { border-style: dashed; transform: none; }
.summary { font-style: italic; color: #5e4a26; font-size: 12.5px; margin: 2px 0 8px;
           border-left: 2px solid var(--gold); padding-left: 9px; }
.meta { color: var(--ink-soft); font-size: 11px; margin-bottom: 8px; font-style: italic; }
.links { margin: 4px 0 6px; display: flex; flex-wrap: wrap; gap: 6px; }
a.link { color: var(--ink); text-decoration: none; font-size: 11px; padding: 1px 9px;
         border-radius: 2px; border: 1px solid var(--bronze);
         background: rgba(255,255,255,.35); }
a.link:hover { border-color: var(--gold); color: #6b4e0e; }
.bar { height: 8px; background: rgba(0,0,0,.2); border-radius: 2px; overflow: hidden;
       margin: 6px 0 3px; border: 1px solid rgba(107, 92, 64, .45);
       background-image: repeating-linear-gradient(90deg, rgba(0,0,0,0) 0 9px, rgba(0,0,0,.14) 9px 10px); }
.bar > div { height: 100%; background: linear-gradient(90deg, var(--gold-bright) 0%, #8a6d3b 100%);
             box-shadow: inset 0 1px 0 rgba(255,255,255,.55); }
.progress-label { font-size: 11px; color: var(--ink-soft); letter-spacing: .5px; }
.current { font-size: 13px; margin: 8px 0 4px; color: #5e3a12; }
.current b { color: var(--ember); font-weight: bold; }
.phase { margin-top: 10px; font-size: 12.5px; }
.phase .name { color: var(--ink-soft); text-transform: uppercase; font-size: 10px;
               letter-spacing: 2px; border-bottom: 1px solid rgba(138, 109, 59, .35);
               padding-bottom: 2px; margin-bottom: 3px; }
.task { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; padding: 1px 0; }
.task.pending::before { content: "☐  "; color: var(--ink-soft); }
.task.in_progress::before { content: "➤  "; color: var(--ember); }
.task.completed::before { content: "✔  "; color: var(--ok); }
.task.abandoned::before { content: "✘  "; color: var(--faded); }
.task.blocked::before { content: "⚑  "; color: var(--blood); }
.task.completed, .task.abandoned { color: var(--faded); font-style: italic; }
.ctx { margin-top: 10px; }
.ctx .bar > div { background: linear-gradient(90deg, #5b7fa6 0%, #39546e 100%); }
.subs { margin-top: 12px; border-top: 1px dashed var(--bronze); padding-top: 8px; }
.subs .subrow { font-size: 12px; display: flex; gap: 8px; align-items: center; padding: 2px 0; }
.subs .name { flex: 1; overflow: hidden; text-overflow: ellipsis; font-style: italic; }
.msgrow { display: flex; gap: 6px; margin-top: 10px; }
.msgrow input { flex: 1; min-width: 0; background: rgba(255,255,255,.5);
                border: 1px solid var(--bronze); border-radius: 2px; color: var(--ink);
                font: 12.5px "Alegreya", Georgia, serif; padding: 5px 9px; }
.msgrow input:focus { outline: none; border-color: var(--gold); }
.msgrow .btn { flex: none; padding: 4px 12px; }
.subs .what { color: var(--ink-soft); flex: 2; overflow: hidden;
              text-overflow: ellipsis; white-space: nowrap; }
.empty { color: var(--gold); padding: 60px 20px; text-align: center; font-size: 16px;
         font-style: italic; letter-spacing: 1px; }
#toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
         background: linear-gradient(160deg, var(--parchment), var(--parchment2));
         border: 1px solid var(--bronze); border-top: 3px solid var(--gold); color: var(--ink);
         padding: 10px 20px; border-radius: 2px; font-size: 13px; opacity: 0;
         transition: opacity .3s; pointer-events: none; z-index: 10;
         box-shadow: 0 6px 20px rgba(0,0,0,.65); }
.btn {
  font-family: "Alegreya", Georgia, serif; font-weight: bold; font-size: 12px;
  letter-spacing: 1.5px; text-transform: uppercase;
  color: #f0d9a8; background: linear-gradient(180deg, #8a2a1e 0%, #5e150d 55%, #430d07 100%);
  border: 1px solid #2a0805; border-radius: 3px; padding: 5px 16px; cursor: pointer;
  box-shadow: inset 0 1px 0 rgba(255, 200, 150, .28), inset 0 -2px 5px rgba(0, 0, 0, .45), 0 2px 6px rgba(0, 0, 0, .5);
  text-shadow: 0 1px 2px rgba(0, 0, 0, .85);
}
.btn:hover { background: linear-gradient(180deg, #a13a2a 0%, #6e1c11 55%, #4d1008 100%); color: #ffe9b8; }
.btn:active { transform: translateY(1px);
  box-shadow: inset 0 2px 8px rgba(0, 0, 0, .6), 0 1px 3px rgba(0, 0, 0, .5); }
.btn.ghost { background: none; border: 1.5px solid currentColor; color: inherit;
  box-shadow: none; text-shadow: none; padding: 4px 14px; }
.btn.ghost:hover { background: rgba(138, 109, 59, .12); }
.btn.ghost:active { transform: translateY(1px); box-shadow: none; }
.toggle .btn.ghost { color: var(--gold-bright); border-color: var(--bronze); letter-spacing: 2px; }
.toggle .btn.ghost:hover { background: rgba(201, 162, 39, .08); border-color: var(--gold); }
.actions { display: flex; gap: 8px; margin-top: 10px; }

::selection { background: rgba(201, 162, 39, .4); }
::-webkit-scrollbar { width: 10px; }
::-webkit-scrollbar-track { background: var(--night); }
::-webkit-scrollbar-thumb { background: #3a2d1a; border-radius: 5px; border: 1px solid var(--bronze); }
.toggle { text-align: center; margin-bottom: 14px; }
.toggle label { color: #a8977a; font-size: 12px; cursor: pointer; }
.toggle input { accent-color: var(--gold); vertical-align: -2px; }
canvas#embers { position: fixed; inset: 0; z-index: -1; pointer-events: none; }
@keyframes sheen { 0%, 55% { background-position: 180% 0, 0 0; } 80%, 100% { background-position: -80% 0, 0 0; } }
@keyframes cardIn { from { opacity: 0; transform: translateY(14px) scale(.98); } }
.card.enter { animation: cardIn .55s cubic-bezier(.2,.8,.3,1) both; }
@keyframes stampIn { 0% { opacity: 0; transform: scale(2.2) rotate(-9deg); } 60% { opacity: 1; transform: scale(.92) rotate(2deg); } 100% { opacity: 1; transform: scale(1) rotate(0); } }
.seal.stampIn { animation: stampIn .5s cubic-bezier(.2,1.8,.4,1) 1, blinkA 1s .55s infinite; }
.card.tilt { will-change: transform; }
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation: none !important; transition: none !important; } }
</style>
</head>
<body>
<h1>omp dashboard</h1>
<div class="rule"><span>&#9880;</span></div>
<div class="chips" id="summary"></div>
<div class="toggle"><button class="btn ghost" id="showEnded">show ended sessions</button></div>
<div id="nowpanel"></div>
<div class="grid" id="grid"></div>
<div class="empty" id="empty" style="display:none">
  No sessions have reported yet.<br>
  <span style="font-size:12px; color:#a8977a">start omp to begin reporting (extension: ~/.omp/agent/extensions/omp-dash.ts)</span>
</div>
<script>
const STATUS_ORDER = { working: 0, idle: 1, ended: 2 };
let linkPatterns = [];
function sendCommand(sid, type, choice, reviewId, text) {
  fetch("/command", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ sessionId: sid, command: { type, choice, reviewId, value: type === "approval_mode" ? choice : undefined, text } }),
  }).then(r => toast(r.ok ? "sent to session" : "send failed")).catch(() => toast("send failed: server unreachable"));
}
function sendMsg(sid, text) {
  if (text.trim()) sendCommand(sid, "steer", undefined, undefined, text.trim());
}
function sendFromInput(btn) {
  const input = btn.previousElementSibling;
  if (!input.value.trim()) return;
  sendMsg(input.dataset.sid, input.value);
  input.value = "";
}
function esc(s) { const d = document.createElement("span"); d.textContent = s ?? "";
  return d.innerHTML.replaceAll('"', "&quot;").replaceAll("'", "&#39;"); }
function focusSession(id) {
  fetch("/focus/" + encodeURIComponent(id))
    .then(r => r.json().then(j => ({ ok: r.ok, j })))
    .then(({ ok, j }) => toast(ok ? "brought terminal to front" : "focus failed: " + (j.error || "?")))
    .catch(() => toast("focus failed: dashboard server unreachable"));
}
function toast(msg) {
  let t = document.getElementById("toast");
  if (!t) {
    t = document.createElement("div");
    t.id = "toast";
    t.innerHTML = msg;
    document.body.appendChild(t);
  } else { t.innerHTML = msg; }
  t.style.opacity = "1";
  clearTimeout(t._h);
  t._h = setTimeout(() => { t.style.opacity = "0"; }, 3500);
}
function age(ts, now) { const s = Math.max(0, Math.round(now / 1000 - ts / 1000));
  return s < 60 ? s + "s" : s < 3600 ? Math.round(s / 60) + "m" : Math.round(s / 3600) + "h"; }
function tasksOf(todos) { return (todos ?? []).flatMap(p => p.tasks ?? []); }
function activeTask(todos) { return tasksOf(todos).find(t => t.status === "in_progress"); }
function progress(todos) {
  const all = tasksOf(todos);
  return [all.filter(t => t.status === "completed" || t.status === "abandoned").length, all.length];
}
function taskRow(t) {
  return `<div class="task ${esc(t.status)}" title="${esc(t.content)}${t.blocker ? " — " + esc(t.blocker) : ""}">${esc(t.content)}</div>`;
}
function phaseHtml(p) {
  const [d, n] = progress([p]);
  return `<div class="phase"><div class="name">${esc(p.name)} &mdash; ${d}/${n}</div>${(p.tasks ?? []).map(taskRow).join("")}</div>`;
}
function statusDot(s, lastSeen, now) {
  if (s.attention) return "needs";
  return (s.status !== "ended" && now - s.lastSeen > 120000) ? "stale" : s.status;
}
function stampLabel(s, dotCls) {
  if (dotCls === "stale") return "stale";
  if (s.status === "working") return "working";
  if (s.status === "idle") return "idle";
  return "ended";
}
function subRow(s, now) {
  const what = activeTask(s.todos);
  return `<div class="subrow"><span class="dot ${statusDot(s, s.lastSeen, now)}"></span>
    <span class="name">${esc(s.agentName || "subagent")}</span>
    <span class="what">${esc(what ? what.content : "")}</span>
    <span class="progress-label">${age(s.lastSeen, now)}</span></div>`;
}
function ghRepoUrl(remote) {
  if (!remote) return "";
  let m = remote.match(/git@([^:]+):(.+?)(?:\.git)?$/);
  if (m) return "https://" + m[1] + "/" + m[2];
  m = remote.match(/https?:\/\/([^/]+)\/(.+?)(?:\.git)?$/);
  return m ? "https://" + m[1] + "/" + m[2] : "";
}
function linksHtml(s) {
  const gh = ghRepoUrl(s.repoUrl);
  const seen = new Set();
  const chips = [];
  const add = (url, label) => {
    if (!url || seen.has(url)) return;
    seen.add(url);
    chips.push(`<a class="link" target="_blank" href="${esc(url)}">${label}</a>`);
  };
  if (gh && s.branch) add(`${gh}/tree/${encodeURIComponent(s.branch)}`, `&#9099; ${esc(s.branch)}`);
  if (s.prUrl) add(s.prUrl, `PR &#8599;`);
  for (const l of s.links ?? []) add(l.url, esc(l.label || l.url));
  const text = [s.name, ...tasksOf(s.todos).map(t => t.content)].join(" ");
  for (const p of linkPatterns) {
    try {
      const re = new RegExp(p.pattern, "g");
      let m, guard = 0;
      while ((m = re.exec(text)) !== null && guard++ < 10) {
        const url = (p.url || "").replace(/\{(\d+)\}/g, (_, i) => m[+i] ?? "");
        add(url, esc(m[0]));
      }
    } catch { /* bad user regex: skip */ }
  }
  return chips.length ? `<div class="links">${chips.join("")}</div>` : "";
}
function cardHtml(s, subs, now) {
  const [d, n] = progress(s.todos);
  const pct = n ? Math.round(100 * d / n) : 0;
  const cur = activeTask(s.todos);
  const phases = (s.todos ?? []).filter(p => (p.tasks ?? []).length).map(phaseHtml).join("");
  const ctx = s.contextPercent != null
    ? `<div class="ctx"><div class="progress-label">context ${Math.round(s.contextPercent)}%</div>
       <div class="bar"><div style="width:${Math.min(100, s.contextPercent)}%"></div></div></div>` : "";
  return `<div class="card ${s.attention ? "needs" : ""}">
    <h2><span class="dot ${statusDot(s, s.lastSeen, now)}"></span><span class="cardname" title="click to bring terminal to front" onclick="focusSession('${esc(s.sessionId)}')">${esc(s.name || s.sessionId.slice(0, 8))}</span>
      <span class="stamp ${statusDot(s, s.lastSeen, now) === "stale" ? "stale" : ""}">${stampLabel(s, statusDot(s, s.lastSeen, now))}</span>
      ${s.attention ? `<span class="chip seal">&#10071; ${esc(s.attention)}</span>` : ""}</h2>
    ${s.summary ? `<div class="summary">&#8220;${esc(s.summary)}&#8221;</div>` : ""}
    <div class="meta">${esc(s.cwd ?? "")} &middot; ${esc(s.model ? s.model.provider + "/" + s.model.id : "no model")}
      &middot; last seen ${age(s.lastSeen, now)} ago</div>
    ${linksHtml(s)}
    ${n ? `<div class="bar"><div style="width:${pct}%"></div></div>
      <div class="progress-label">${d}/${n} tasks done</div>` : ""}
    ${cur ? `<div class="current"><b>&rsaquo;</b> ${esc(cur.content)}</div>` : ""}
    ${phases}
    ${ctx}
    ${subs.length ? `<div class="subs"><div class="progress-label" style="margin-bottom:2px">subagents</div>${subs.map(x => subRow(x, now)).join("")}</div>` : ""}
    ${s.planReview ? `<div class="actions"><span class="progress-label" style="align-self:center">plan:</span>` +
      s.planReview.choices.map(c => `<button class="btn" onclick="sendCommand('${esc(s.sessionId)}','plan_review','${esc(c)}','${esc(s.planReview.reviewId)}')">${esc(c)}</button>`).join("") + `</div>` : ""}
    ${s.attention && s.attention.startsWith("approval") ? `<div class="actions">` +
      (s.yolo ? `<span class="chip seal">auto-approve is ON</span>` +
        `<button class="btn" onclick="sendCommand('${esc(s.sessionId)}','approval_mode','','off')">restore asking</button>`
      : `<button class="btn" onclick="sendCommand('${esc(s.sessionId)}','approval_mode','','yolo')">auto-approve from here on</button>`) + `</div>
      <div class="progress-label">the already-open approval prompt must still be answered in the terminal</div>` : ""}
    ${s.status !== "ended" ? `<div class="msgrow">
      <input data-sid="${esc(s.sessionId)}" placeholder="send a message to this session&hellip;" maxlength="4000">
      <button class="btn" onclick="sendFromInput(this)">send</button></div>` : ""}
    <div class="actions"><button class="btn ghost" onclick="focusSession('${esc(s.sessionId)}')">&#8982; focus terminal</button></div>
  </div>`;
}
const seen = new Set(), attentionSig = {};
let firstGrid = true;
let showEnded = localStorage.getItem("showEnded") === "1";
const showEndedBtn = document.getElementById("showEnded");
function syncToggle() { showEndedBtn.textContent = showEnded ? "hide ended sessions" : "show ended sessions"; }
syncToggle();
showEndedBtn.addEventListener("click", () => {
  showEnded = !showEnded;
  localStorage.setItem("showEnded", showEnded ? "1" : "0");
  syncToggle();
  refresh();
});
async function refresh() {
  let data;
  try { data = await (await fetch("/state")).json(); } catch { return; }
  const now = Math.floor((data.now * 1000) / 10000) * 10000; // 10s buckets: stable DOM between changes
  linkPatterns = data.linkPatterns ?? [];
  const sessions = data.sessions ?? [];
  const mains = sessions.filter(s => (s.kind ?? "main") === "main" && s.status !== "ended");
  const ended = showEnded ? sessions.filter(s => s.status === "ended") : [];
  const gridHtml = mains.map(s => cardHtml(s, sessions.filter(x => x.parentId === s.sessionId), now)).join("")
    + ended.map(s => cardHtml(s, [], now)).join("");
  const grid = document.getElementById("grid");
  if (grid.dataset.sig !== gridHtml) {
    const draft = {};
    grid.querySelectorAll("input[data-sid]").forEach(i => { if (i.value) draft[i.dataset.sid] = i.value; });
    const activeSid = document.activeElement?.getAttribute?.("data-sid");
    grid.innerHTML = gridHtml;
    grid.dataset.sig = gridHtml;
    for (const [sid, val] of Object.entries(draft)) {
      const input = grid.querySelector(`input[data-sid="${sid}"]`);
      if (input) input.value = val;
    }
    if (activeSid) grid.querySelector(`input[data-sid="${activeSid}"]`)?.focus();
    const ordered = mains.concat(ended);
    grid.querySelectorAll(":scope > .card").forEach((el, i) => {
      const s = ordered[i];
      if (!s) return;
      const sig = s.attention || "";
      const seal = el.querySelector(".seal");
      if (seal && (firstGrid || attentionSig[s.sessionId] !== sig)) seal.classList.add("stampIn");
      attentionSig[s.sessionId] = sig;
      if (!seen.has(s.sessionId)) {
        seen.add(s.sessionId);
        el.classList.add("enter");
        if (firstGrid) el.style.animationDelay = Math.min(i * 60, 720) + "ms";
      }
    });
    firstGrid = false;
  }
  const working = mains.filter(s => s.status === "working").length;
  const idle = mains.length - working;
  const active = mains.reduce((a, s) => a + tasksOf(s.todos).filter(t => t.status === "in_progress").length, 0);
  const all = mains.flatMap(s => tasksOf(s.todos));
  const done = all.filter(t => t.status === "completed" || t.status === "abandoned").length;
  const needYou = mains.filter(s => s.attention).length;
  const agents = sessions.filter(s => s.kind === "sub" && s.status === "working").length;
  document.getElementById("summary").innerHTML =
    `<span class="chip">${mains.length} sessions</span><span class="chip">${working} working</span>` +
    `<span class="chip">${idle} idle</span>` +
    `<span class="chip">${all.length ? done + "/" + all.length + " tasks done" : "no tasks"}</span>` +
    `<span class="chip">${active} in progress</span>` +
    (agents ? `<span class="chip">${agents} agent${agents === 1 ? "" : "s"} running</span>` : "") +
    (needYou ? `<span class="chip attention">${needYou} need attention</span>` : "");
  const rows = mains.map(s => {
    const cur = activeTask(s.todos);
    const [d, n] = progress(s.todos);
    const what = cur ? `&rsaquo; ${esc(cur.content)}`
      : s.summary ? `&rsaquo; ${esc(s.summary)}`
      : `<span class="mut">idle${s.status === "idle" ? " · " + age(s.lastSeen, now) + " ago" : ""}</span>`;
    return `<div class="nowrow"><span class="dot ${statusDot(s, s.lastSeen, now)}"></span>` +
      `<span class="nowname" title="click to bring terminal to front" onclick="focusSession('${esc(s.sessionId)}')">${esc(s.name || s.sessionId.slice(0, 8))}</span>` +
      `<span class="nowtask">${what}</span>` +
      (n ? `<span class="nowprog">${d}/${n}</span>` : "") +
      `<span class="nowprog">${age(s.lastSeen, now)}</span></div>`;
  }).join("");
  document.getElementById("nowpanel").innerHTML =
    rows ? `<div class="paneltitle">active sessions</div><div class="nowpanel">${rows}</div>` : "";
  document.getElementById("empty").style.display = sessions.length ? "none" : "block";
}
refresh();
setInterval(refresh, 2000);
// ambient effects: drifting embers + card tilt (off when the OS prefers reduced motion)
if (!matchMedia("(prefers-reduced-motion: reduce)").matches) {
  const cv = document.createElement("canvas");
  cv.id = "embers";
  document.body.appendChild(cv);
  const ex = cv.getContext("2d");
  let W, H;
  const sparks = [];
  const sizeEmbers = () => { W = cv.width = innerWidth; H = cv.height = innerHeight; };
  addEventListener("resize", sizeEmbers);
  sizeEmbers();
  const mkSpark = fromBottom => ({ x: Math.random() * W, y: fromBottom ? H + 8 : Math.random() * H,
    r: .5 + Math.random() * 2.4, vy: .14 + Math.random() * .55,
    ph: Math.random() * 6.28, vph: .005 + Math.random() * .014, a: .25 + Math.random() * .5 });
  for (let i = 0; i < 70; i++) sparks.push(mkSpark(false));
  (function tick() {
    requestAnimationFrame(tick);
    if (document.hidden) return;
    ex.clearRect(0, 0, W, H);
    for (const p of sparks) {
      p.y -= p.vy; p.ph += p.vph; p.x += Math.sin(p.ph) * .35;
      if (p.y < -10) Object.assign(p, mkSpark(true));
      ex.beginPath();
      ex.arc(p.x, p.y, p.r, 0, 6.29);
      ex.fillStyle = `hsla(${38 + 10 * Math.sin(p.ph)},85%,62%,${p.a * (.55 + .45 * Math.sin(p.ph * 4))})`;
      ex.fill();
    }
  })();
  let tiltCard = null;
  const untilt = () => { if (tiltCard) { tiltCard.classList.remove("tilt"); tiltCard.style.transform = ""; tiltCard = null; } };
  document.addEventListener("pointermove", e => {
    const c = e.target.closest?.(".card");
    if (c !== tiltCard) { untilt(); tiltCard = c; if (c) c.classList.add("tilt"); }
    if (!c) return;
    const r = c.getBoundingClientRect();
    const dx = (e.clientX - r.left) / r.width - .5, dy = (e.clientY - r.top) / r.height - .5;
    c.style.transform = `perspective(900px) rotateX(${(-dy * 6).toFixed(2)}deg) rotateY(${(dx * 6).toFixed(2)}deg) translateY(-2px)`;
  });
  document.documentElement.addEventListener("pointerleave", untilt);
}
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: str, ctype: str) -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        if self.path == "/command":
            try:
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                sid, command = data.get("sessionId"), data.get("command")
                if not sid or not isinstance(command, dict) or not command.get("type"):
                    self._send(400, "missing sessionId or command", "text/plain")
                    return
                with STATE_LOCK:
                    PENDING_COMMANDS.setdefault(sid, []).append(command)
                self._send(204, "", "text/plain")
            except Exception as e:
                self._send(400, str(e), "text/plain")
            return
        if self.path == "/link":
            try:
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                sid, url = data.get("sessionId"), data.get("url")
                if not sid or not url:
                    self._send(400, "missing sessionId or url", "text/plain")
                    return
                with STATE_LOCK:
                    entry = STATE.setdefault(sid, {"sessionId": sid, "status": "idle",
                                                   "lastSeen": time.time() * 1000})
                    links = [l for l in entry.get("links", []) if l.get("url") != url]
                    links.append({"url": url, "label": data.get("label") or url})
                    entry["links"] = links
                    _save_state()
                self._send(204, "", "text/plain")
            except Exception as e:
                self._send(400, str(e), "text/plain")
            return
        if self.path != "/report":
            self._send(404, "not found", "text/plain")
            return
        try:
            report = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            sid = report.get("sessionId")
            if not sid:
                self._send(400, "missing sessionId", "text/plain")
                return
            now_ms = time.time() * 1000
            report["lastSeen"] = now_ms
            with STATE_LOCK:
                report["firstSeen"] = STATE.get(sid, {}).get("firstSeen", now_ms)
                report["links"] = STATE.get(sid, {}).get("links", [])  # keep pinned links
                STATE[sid] = report
                _save_state()
            self._send(204, "", "text/plain")
        except Exception as e:
            self._send(400, str(e), "text/plain")

    def do_GET(self) -> None:
        if self.path.startswith("/commands/"):
            sid = self.path[len("/commands/"):]
            with STATE_LOCK:
                commands = PENDING_COMMANDS.pop(sid, [])
            self._send(200, json.dumps({"commands": commands}), "application/json")
        elif self.path.startswith("/fonts/"):
            name = os.path.basename(self.path[len("/fonts/"):])
            fpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", name)
            if not os.path.isfile(fpath):
                self._send(404, "not found", "text/plain")
                return
            ctype = "font/woff2" if fpath.endswith(".woff2") else "font/woff"
            with open(fpath, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/state":
            sessions = sorted(
                STATE.values(),
                key=lambda s: (
                    0 if s.get("attention") else 1,
                    STATUS_ORDER.get(s.get("status"), 3),
                    -(s.get("lastSeen") or 0),
                ),
            )
            self._send(200, json.dumps({"sessions": sessions, "now": time.time(), "linkPatterns": LINK_PATTERNS}), "application/json")
        elif self.path.startswith("/focus/"):
            ok, message = _focus_session(self.path[len("/focus/"):])
            body = json.dumps({"ok": ok, **({"error": message} if not ok else {})})
            self._send(200 if ok else 409, body, "application/json")
        elif self.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")

    def log_message(self, *args: object) -> None:  # keep the terminal quiet
        pass


if __name__ == "__main__":
    host = os.environ.get("OMP_DASH_HOST", "127.0.0.1")
    print(f"omp-dash on http://{host}:{PORT}")
    ThreadingHTTPServer((host, PORT), Handler).serve_forever()
