/**
 * Web search for trip facts (season, routes, opening periods).
 *
 * Usage:
 *   node scripts/search.js "<query>"
 *
 * Why a script instead of the gateway's web_search tool: web_search returns long,
 * wrapped JSON that stays in the agent's context for the rest of the task, and the
 * local model's context is only 32K (one web_search result cost ~5K tokens and was
 * part of an AMC-257 overflow). This script keeps the full results in a file under
 * /tmp/acer-travel-plan/ and prints only a short, URL-free preview:
 *   resultsFile: <path>
 *   source: duckduckgo | wiki
 *   count: <n>
 *   [0] <title> — <snippet>
 *
 * Sources: DuckDuckGo's HTML endpoint (the same request the gateway's web_search
 * makes) for current pages; when it rate-limits (HTTP 202) or returns nothing,
 * the Wikivoyage and Wikipedia search APIs, which do not block scripted access.
 * Bing is not used: from the device network it returned unrelated pages to
 * automated requests.
 *
 * At most MAX_SEARCHES calls are served per WINDOW_MS; after that it prints "limit
 * reached" and exits 0 without searching, so the cap holds even when the model ignores
 * the instruction. The count is shared by every session on this host, so prepare.py
 * (run first for every new trip) deletes it: each trip gets its own searches.
 */

const fs = require('fs');
const path = require('path');

const RESULTS_DIR = '/tmp/acer-travel-plan';
const COUNTER_FILE = path.join(RESULTS_DIR, 'search-count.json');
const MAX_SEARCHES = 2;
const WINDOW_MS = 10 * 60 * 1000;
const MAX_RESULTS = 5;
const TITLE_CHARS = 80;
const SNIPPET_CHARS = 160;
const TIMEOUT_MS = 15000;
const DDG_USER_AGENT =
  'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36';
const WIKI_USER_AGENT = 'acer-travel-plan/1.3 (OpenClaw skill)';
const WIKI_HOSTS = ['en.wikivoyage.org', 'en.wikipedia.org'];

// Tells the tray's Agent browser panel about this search (scripts/live.js protocol, the
// socket the other skills share). The panel shows the results and their links in its
// Sources tab; the model never sees them. Best effort: no relay means no event.
const LIVE_SOCKET = '/tmp/acer-search-product-price/live.sock';

function liveEmit(event) {
  return new Promise((resolve) => {
    try {
      const net = require('net');
      const socket = net.createConnection(LIVE_SOCKET);
      const done = () => {
        socket.destroy();
        resolve();
      };
      socket.setTimeout(1000, done);
      socket.on('error', done);
      const line = JSON.stringify({ ...event, v: 1, script: 'web_search', pid: process.pid, ts: Date.now() });
      socket.end(`${line}\n`, done);
    } catch {
      resolve();
    }
  });
}

const hostOf = (url) => {
  try {
    return new URL(url).host;
  } catch {
    return '';
  }
};

// Returns false when the cap is used up; otherwise records this call.
function takeSearchSlot() {
  fs.mkdirSync(RESULTS_DIR, { recursive: true });
  const now = Date.now();
  let calls = [];
  try {
    calls = JSON.parse(fs.readFileSync(COUNTER_FILE, 'utf8')).filter((t) => now - t < WINDOW_MS);
  } catch {
    // missing or unreadable counter: start fresh
  }
  if (calls.length >= MAX_SEARCHES) return false;
  calls.push(now);
  fs.writeFileSync(COUNTER_FILE, JSON.stringify(calls), 'utf8');
  return true;
}

function writeResultsFile(query, results) {
  const slug =
    query
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 40) || 'query';
  const filePath = path.join(RESULTS_DIR, `${slug}-${Math.random().toString(36).slice(2, 8)}.json`);
  fs.writeFileSync(filePath, JSON.stringify(results, null, 2), 'utf8');
  return filePath;
}

const ENTITIES = { '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#x27;': "'", '&#39;': "'", '&nbsp;': ' ' };
const stripHtml = (html) =>
  String(html || '')
    .replace(/<[^>]+>/g, '')
    .replace(/&(?:amp|lt|gt|quot|nbsp|#x27|#39);/g, (e) => ENTITIES[e])
    .replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(Number(n)));
const clean = (text, max) => stripHtml(text).replace(/\s+/g, ' ').trim().slice(0, max);

// DuckDuckGo wraps result links as //duckduckgo.com/l/?uddg=<target>.
function ddgTarget(href) {
  try {
    const url = new URL(href, 'https://duckduckgo.com');
    return url.searchParams.get('uddg') || url.href;
  } catch {
    return href;
  }
}

async function searchDuckDuckGo(query) {
  const url = new URL('https://html.duckduckgo.com/html');
  url.searchParams.set('q', query);
  url.searchParams.set('kp', '-1');
  const res = await fetch(url, { headers: { 'User-Agent': DDG_USER_AGENT }, signal: AbortSignal.timeout(TIMEOUT_MS) });
  if (res.status !== 200) throw new Error(`HTTP ${res.status}`);
  const html = await res.text();
  // Each result is <div class="result results_links ...">; sponsored ones also carry result--ad.
  return html
    .split('<div class="result results_links')
    .slice(1)
    .filter((block) => !block.slice(0, 200).includes('result--ad'))
    .map((block) => ({
      title: clean(block.match(/class="result__a"[^>]*>([\s\S]*?)<\/a>/)?.[1], 300),
      snippet: clean(block.match(/class="result__snippet"[^>]*>([\s\S]*?)<\/a>/)?.[1], 600),
      url: ddgTarget(block.match(/class="result__a"[^>]*href="([^"]+)"/)?.[1] || ''),
    }))
    .filter((r) => r.title && r.snippet);
}

async function searchWiki(query) {
  const results = [];
  for (const host of WIKI_HOSTS) {
    const url = new URL(`https://${host}/w/api.php`);
    const params = { action: 'query', list: 'search', srsearch: query, srlimit: '3', format: 'json', utf8: '1' };
    Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
    try {
      const res = await fetch(url, { headers: { 'User-Agent': WIKI_USER_AGENT }, signal: AbortSignal.timeout(TIMEOUT_MS) });
      const hits = (await res.json()).query?.search || [];
      for (const hit of hits) {
        results.push({
          title: clean(hit.title, 300),
          snippet: clean(hit.snippet, 600),
          url: `https://${host}/wiki/${encodeURIComponent(hit.title.replace(/ /g, '_'))}`,
        });
      }
    } catch (error) {
      console.error(`search.js: ${host} failed (${error.message || error})`);
    }
  }
  return results;
}

async function main() {
  const query = process.argv.slice(2).join(' ').trim();
  if (!query) {
    console.error('Usage: node scripts/search.js "<query>"');
    process.exit(2);
  }
  if (!takeSearchSlot()) {
    console.log(`limit reached: ${MAX_SEARCHES} searches already used. Do not search again; write the JSON with what you have.`);
    return;
  }

  const callId = `search-${process.pid}-${Date.now()}`;
  await liveEmit({ type: 'tool-start', callId, query });

  let source = 'duckduckgo';
  let results = [];
  try {
    results = await searchDuckDuckGo(query);
  } catch (error) {
    console.error(`search.js: duckduckgo failed (${error.message || error})`);
  }
  if (!results.length) {
    source = 'wiki';
    results = await searchWiki(query);
  }
  results = results.slice(0, MAX_RESULTS);
  await liveEmit({
    type: 'tool-result',
    callId,
    query,
    provider: source,
    ok: true,
    results: results.map((r) => ({
      title: clean(r.title, 300),
      url: r.url,
      snippet: clean(r.snippet, 300),
      site: hostOf(r.url),
    })),
  });

  const lines = [`resultsFile: ${writeResultsFile(query, results)}`, `source: ${source}`, `count: ${results.length}`];
  results.forEach((r, i) => lines.push(`[${i}] ${clean(r.title, TITLE_CHARS)} — ${clean(r.snippet, SNIPPET_CHARS)}`));
  if (!results.length) lines.push('No results. Continue without searching again.');
  console.log(lines.join('\n'));
}

main().catch((error) => {
  console.error('search.js failed:', error.message || error);
  process.exit(1);
});
