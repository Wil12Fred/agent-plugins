// Claude Terminal Bridge — inject text into the exact VS Code integrated terminal,
// matched by process id, so `claude-sessions --send-live` can reach a specific tab.
//
// It watches ~/.claude/vscode-inbox.jsonl. Each new line is a JSON object:
//   {"claude_pid": 12345, "text": "your message"}   -> finds the terminal whose
//        shell process is an ancestor of claude_pid, and sends the text.
//   {"terminal": "name", "text": "..."}              -> match by terminal name.
// Only NEW lines (appended after activation) are processed.
const vscode = require('vscode');
const fs = require('fs');
const os = require('os');
const path = require('path');

const INBOX = path.join(os.homedir(), '.claude', 'vscode-inbox.jsonl');

function ancestors(pid) {
  const fam = new Set([pid]);
  let cur = pid;
  for (let i = 0; i < 25; i++) {
    try {
      const stat = fs.readFileSync(`/proc/${cur}/stat`, 'utf8');
      const ppid = parseInt(stat.slice(stat.lastIndexOf(')') + 2).trim().split(/\s+/)[1], 10);
      if (!ppid || ppid <= 1) break;
      fam.add(ppid); cur = ppid;
    } catch (e) { break; }
  }
  return fam;
}

async function findByPid(claudePid) {
  const fam = ancestors(claudePid);
  for (const t of vscode.window.terminals) {
    const pid = await t.processId;        // the terminal's shell pid
    if (pid && fam.has(pid)) return t;
  }
  return null;
}

function activate(context) {
  fs.mkdirSync(path.dirname(INBOX), { recursive: true });
  if (!fs.existsSync(INBOX)) fs.writeFileSync(INBOX, '');
  let offset = fs.statSync(INBOX).size;   // start at EOF: ignore old lines

  const process = async () => {
    let size;
    try { size = fs.statSync(INBOX).size; } catch (e) { return; }
    if (size < offset) offset = 0;        // truncated/rotated
    if (size === offset) return;
    const buf = Buffer.alloc(size - offset);
    const fd = fs.openSync(INBOX, 'r');
    fs.readSync(fd, buf, 0, buf.length, offset);
    fs.closeSync(fd);
    offset = size;
    for (const line of buf.toString('utf8').split('\n')) {
      if (!line.trim()) continue;
      let msg; try { msg = JSON.parse(line); } catch (e) { continue; }
      if (msg.new_terminal && msg.text) {           // open a NEW terminal tab and run a command
        const t = vscode.window.createTerminal(msg.name ? { name: msg.name, cwd: msg.cwd } : { cwd: msg.cwd });
        t.show(false);
        t.sendText(msg.text, true);                 // run it (with Enter)
        continue;
      }
      if (!msg.text && !msg.interrupt) continue;
      let term = null;
      if (msg.claude_pid) term = await findByPid(msg.claude_pid);
      else if (msg.terminal) term = vscode.window.terminals.find(t => t.name === msg.terminal);
      if (term) {
        term.show(false);
        if (msg.interrupt) {
          term.sendText('\x1b', false);            // ESC -> stop current generation (no Enter)
        } else {
          term.sendText(msg.text, false);          // text only (no newline)
          setTimeout(() => term.sendText('\r', false), 150);  // real Enter -> submit (TUI needs \r)
        }
      }
      else vscode.window.showWarningMessage('claude-bridge: no terminal for ' + line.slice(0, 80));
    }
  };

  const watcher = fs.watch(INBOX, () => { process().catch(() => {}); });
  context.subscriptions.push({ dispose: () => watcher.close() });
  console.log('Claude Terminal Bridge active, watching', INBOX);
}

function deactivate() {}
module.exports = { activate, deactivate };
