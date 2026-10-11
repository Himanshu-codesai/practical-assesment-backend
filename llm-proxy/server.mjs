// A small LLM gateway: POST /v1/messages in, text reply out.
// Each request runs the `claude` command-line tool once (`claude -p`) and returns its
// answer. The tool signs in with CLAUDE_CODE_OAUTH_TOKEN from the environment.
// The backend only ever talks to this address, so swapping in another model server
// means changing LLM_BASE_URL, not code.
import http from 'node:http';
import { spawn } from 'node:child_process';
import crypto from 'node:crypto';

const PORT = Number(process.env.PORT || 8787);
const HOST = process.env.HOST || '127.0.0.1';
const CLAUDE_BIN = process.env.CLAUDE_BIN || 'claude';
const API_KEY = process.env.PROXY_API_KEY || '';

// Message content is either a string or a list of {type: "text", text} parts.
const text = (c) => typeof c === 'string' ? c : (c || []).filter((b) => b.type === 'text').map((b) => b.text).join('\n');

// The command-line tool takes one prompt, so a multi-turn conversation is written out as a transcript.
function buildPrompt(messages) {
  if (messages.length === 1) return text(messages[0].content);
  return messages.map((m) => `${m.role === 'assistant' ? 'Assistant' : 'Human'}: ${text(m.content)}`).join('\n\n') + '\n\nAssistant:';
}

function runClaude(body) {
  // --tools "" turns every tool off: the model can only write text.
  const args = ['-p', '--output-format', 'json', '--tools', '', '--no-session-persistence', '--disable-slash-commands'];
  if (body.model) args.push('--model', body.model);
  const system = text(body.system);
  if (system) args.push('--system-prompt', system);
  const child = spawn(CLAUDE_BIN, args, { stdio: ['pipe', 'pipe', 'pipe'] });
  child.stdin.end(buildPrompt(body.messages));
  return child;
}

const send = (res, code, obj) => { res.writeHead(code, { 'content-type': 'application/json' }); res.end(JSON.stringify(obj)); };
const fail = (res, code, message) => send(res, code, { type: 'error', error: { type: 'api_error', message } });

async function readJson(req) {
  let s = '';
  for await (const chunk of req) s += chunk;
  return JSON.parse(s || '{}');
}

async function handleMessages(req, res) {
  const body = await readJson(req);
  if (!Array.isArray(body.messages) || !body.messages.length) return fail(res, 400, 'messages is required');
  const child = runClaude(body);
  let out = '', stderr = '';
  child.stdout.on('data', (d) => { out += d; });
  child.stderr.on('data', (d) => { stderr += d; });
  child.on('error', (e) => fail(res, 502, `could not start ${CLAUDE_BIN}: ${e.message}`));
  res.on('close', () => child.kill());   // the caller went away: stop the work
  child.on('close', (code) => {
    if (res.writableEnded) return;
    let r;
    try { r = JSON.parse(out); } catch { return fail(res, 502, stderr.trim() || `claude exited ${code}`); }
    if (r.is_error) return fail(res, 502, r.result || 'claude error');
    send(res, 200, {
      id: 'msg_' + crypto.randomUUID().replace(/-/g, ''),
      type: 'message', role: 'assistant', model: body.model || 'claude',
      content: [{ type: 'text', text: r.result }],
      stop_reason: 'end_turn', stop_sequence: null,
      usage: { input_tokens: r.usage?.input_tokens || 0, output_tokens: r.usage?.output_tokens || 0 },
    });
  });
}

http.createServer(async (req, res) => {
  try {
    if (req.method === 'GET' && req.url === '/health') return send(res, 200, { ok: true });
    if (API_KEY && (req.headers['x-api-key'] || req.headers.authorization?.replace(/^Bearer /, '')) !== API_KEY) return fail(res, 401, 'invalid api key');
    if (req.method === 'POST' && req.url === '/v1/messages') return await handleMessages(req, res);
    fail(res, 404, 'not found');
  } catch (e) {
    if (!res.headersSent) fail(res, 500, String(e.message || e));
  }
}).listen(PORT, HOST, () => console.log(`llm gateway listening on http://${HOST}:${PORT}`));
