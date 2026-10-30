// Replays synthetic sessions through dashboard/console.html in headless Chromium and prints the inbox as JSON.
// Usage: node run_inbox_cases.cjs <cases.json> [full|slim|snapshot]
//   full: list_sessions returns full records (classification test).
//   slim: list_sessions returns the slim record (2026-10 format) and get_session returns the full one (enrichment test).
//   snapshot: no connector in this view; the ledger holds a snapshot of the full records (fallback test).
// Needs the `playwright` package (NODE_PATH) and a Chromium build; tests/test_console_inbox.py skips when either is missing.
const fs = require('fs'); const os = require('os'); const path = require('path');
const { chromium } = require('playwright');
const NOW = '2026-10-30T00:00:00Z';
const ANCHOR = 'S.inboxNow = inbox;';
const casesPath = process.argv[2]; const mode = process.argv[3] || 'full';
const cases = JSON.parse(fs.readFileSync(casesPath, 'utf8'));
const at = (h) => new Date(Date.parse(NOW) - h * 3600000).toISOString();
const ids = cases.map((_, i) => 'session_case' + String(i + 1).padStart(3, '0'));
function full(c, i) {
  const pts = { needs_action: c.needs_action || '', status_detail: c.status_detail || '', recent_action: c.recent_action || '', status_category: c.status_category || '' };
  return {
    id: ids[i], title: c.case.slice(0, 40), session_status: 'SESSION_STATUS_' + c.session_status, status_bucket: 'SESSION_STATUS_BUCKET_' + c.bucket,
    created_at: at(c.created_hours_ago), updated_at: at(c.updated_hours_ago), origin: c.child ? 'claude_code_mcp_seed' : 'web_claude_ai',
    ...(c.child ? { parent_session_id: 'session_parent' } : {}), unread: !!c.unread, tags: [], post_turn_summary: pts,
    session_context: { sources: c.repo ? [{ git_repository: { url: 'https://github.com/' + c.repo } }] : [] },
    external_metadata: { context_usage: { used_tokens: c.ctx_used == null ? 0 : c.ctx_used, max_tokens: 1000000 }, post_turn_summary: pts },
  };
}
function slim(rec) {
  const { session_context, external_metadata, ...rest } = rec;
  const pts = {}; if (rec.post_turn_summary.needs_action) pts.needs_action = rec.post_turn_summary.needs_action; if (rec.post_turn_summary.recent_action) pts.recent_action = rec.post_turn_summary.recent_action;
  const out = { ...rest }; delete out.post_turn_summary; if (Object.keys(pts).length) out.post_turn_summary = pts; return out;
}
(async () => {
  const fulls = cases.map(full);
  const list = { ccr: { data: mode === 'slim' ? fulls.map(slim) : fulls, has_more: false } };
  const byId = Object.fromEntries(fulls.map((r) => [r.id, r]));
  const src = fs.readFileSync(path.join(__dirname, '..', '..', 'dashboard', 'console.html'), 'utf8');
  if (!src.includes(ANCHOR)) { console.error('anchor not found in console.html: ' + ANCHOR); process.exit(3); }
  const page = src.replace(ANCHOR, ANCHOR + ' window.__inbox = inbox.map((i) => ({ kind: i.kind, prio: i.prio, sid: i.s ? i.s.id : null, note: i.note || "", superseded: !!i.superseded, n: (i.sessions || []).map((x) => x.id) }));');
  const tmp = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'aok-')), 'page.html'); fs.writeFileSync(tmp, page);
  const browser = await chromium.launch(fs.existsSync('/opt/pw-browsers/chromium') ? { executablePath: '/opt/pw-browsers/chromium' } : {});
  const p = await browser.newPage(); const errors = [];
  await p.clock.setFixedTime(new Date(NOW));
  p.on('pageerror', (e) => errors.push(e.message));
  const snap = mode === 'snapshot' ? { at: NOW, records: fulls, triggers: [] } : null;
  await p.addInitScript(({ list, byId, snap }) => {
    window.__fetched = [];
    const empty = { docs: [], exists: false, data: () => ({}) };
    const coll = () => ({ onSnapshot: (cb) => { setTimeout(() => cb(empty), 5); return () => {}; }, orderBy() { return this; }, limit() { return this; } });
    const db = { collection: coll, doc: (path) => ({ onSnapshot: (cb) => { setTimeout(() => cb(path === 'data/users/u_test/snapshot' && snap ? { exists: true, data: () => snap } : empty), 5); return () => {}; }, set: async () => {} }) };
    const mcp = {
      watchTool: (srv, tool, input, handler) => { setTimeout(() => handler({ type: 'data', result: { payload: tool === 'list_sessions' ? list : { data: [] } } }), 20); return () => {}; },
      callTool: async (srv, tool, input) => {
        if (tool === 'get_session') { window.__fetched.push(input.session_id); return { payload: { ccr: byId[input.session_id] } }; }
        if (tool === 'list_sessions') return { payload: list };
        return { payload: {} };
      },
      invalidate: async () => {},
    };
    const user = { canEdit: async () => true, can: async () => true, id: async () => 'u_test' };
    window.claude = { use: async (n) => (n === 'mcp' ? (snap ? null : mcp) : n === 'user' ? user : n === 'db' ? db : null) };
  }, { list, byId, snap });
  await p.goto('file://' + tmp);
  await p.waitForFunction(() => Array.isArray(window.__inbox) && window.__inbox.length > 0, null, { timeout: 10000 });
  if (mode === 'slim') await p.waitForTimeout(1500);
  const inbox = await p.evaluate(() => window.__inbox); const fetched = await p.evaluate(() => window.__fetched);
  const notices = await p.$$eval('#notices .notice p', (els) => els.map((e) => e.textContent));
  await browser.close();
  console.log(JSON.stringify({ mode, ids, inbox, fetched, notices, errors }));
})().catch((e) => { console.error(String(e && e.stack || e)); process.exit(2); });
