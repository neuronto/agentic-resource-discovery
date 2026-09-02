#!/usr/bin/env node
/**
 * neuronto: find the MCP servers, skills, agents and APIs that can do a task.
 *
 * Usable with no install (`npx neuronto "read a PDF"`), because the gap between
 * hearing about a tool and trying it is where most adoption is lost.
 */
import { findResource, findTool, registryStats, liveness, NeurontoError } from './index.js';

const HELP = `
neuronto  find the agentic resources that can do a task

  npx neuronto "read a PDF and extract tables"     search (default)
  npx neuronto find "post to slack" [--kind mcp]   search explicitly
  npx neuronto tools "convert currency"            search individual tools
  npx neuronto stats                               what the index holds, measured
  npx neuronto dead [--limit 20]                   endpoints that stopped answering

Options
  --limit N        how many results (default 10)
  --kind mcp|api|skill|agent
  --local          this index only, do not federate to other ARD registries
  --json           raw JSON, for piping
  --key KEY        a verified-domain key. Keyed searches are not logged.

The score is semantic relevance only. It is never a trust or safety rating.
Docs: https://neuronto.com/connect
`;

const KINDS = {
  mcp: 'application/mcp-server+json',
  api: 'application/vnd.oai.openapi+json',
  skill: 'application/ai-skill+json',
  agent: 'application/ai-agent+json',
};

function parse(argv) {
  const out = { _: [], limit: 10, federate: true, json: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--json') out.json = true;
    else if (a === '--local') out.federate = false;
    else if (a === '--limit') out.limit = parseInt(argv[++i], 10) || 10;
    else if (a === '--kind') out.kind = KINDS[argv[++i]] || argv[i];
    else if (a === '--key') out.apiKey = argv[++i];
    else if (a === '-h' || a === '--help') out.help = true;
    else out._.push(a);
  }
  return out;
}

function line(i, r) {
  const bits = [];
  if (r.type) bits.push(String(r.type).replace('application/', ''));
  if (typeof r.score === 'number') bits.push(`relevance ${r.score}`);
  if (r.source) { try { bits.push('via ' + new URL(r.source).host); } catch {} }
  const head = `${String(i).padStart(2)}. ${r.displayName || r.identifier || 'unnamed'}`;
  const meta = bits.length ? `\n    ${bits.join('  ·  ')}` : '';
  const url = r.url ? `\n    ${r.url}` : '';
  const desc = r.description ? `\n    ${String(r.description).slice(0, 150)}` : '';
  return head + meta + url + desc;
}

async function main() {
  const argv = process.argv.slice(2);
  const o = parse(argv);
  if (o.help || !o._.length) { console.log(HELP.trim()); return 0; }

  let cmd = o._[0];
  let query = o._.slice(1).join(' ');
  if (!['find', 'tools', 'stats', 'dead'].includes(cmd)) { query = o._.join(' '); cmd = 'find'; }

  if (cmd === 'stats') {
    const s = await registryStats(o);
    if (o.json) { console.log(JSON.stringify(s, null, 2)); return 0; }
    console.log(`${s.index.entries.toLocaleString()} resources from ` +
                `${s.index.publishers.toLocaleString()} publishers`);
    console.log(`${s.reachability.share_answering_pct}% of probed endpoints answer ` +
                `(${s.reachability.not_answering.toLocaleString()} do not)`);
    console.log(`${s.tools.verified_tools_total.toLocaleString()} verified tools, ` +
                `median ${s.tools.median_tools_per_server} per server`);
    console.log(`\nmeasured over ${s.window.days} days, ` +
                `${s.window.observations.toLocaleString()} recorded changes`);
    return 0;
  }

  if (cmd === 'dead') {
    const d = await liveness({ dead: true, limit: o.limit, ...o });
    if (o.json) { console.log(JSON.stringify(d, null, 2)); return 0; }
    console.log(`${d.count} endpoint(s) that stopped answering:\n`);
    for (const i of d.items) console.log(`  ${i.http_status || '---'}  ${i.url}`);
    console.log(`\nfree to reuse, no attribution required`);
    return 0;
  }

  if (!query) { console.log(HELP.trim()); return 2; }

  if (cmd === 'tools') {
    const tools = await findTool(query, o);
    if (o.json) { console.log(JSON.stringify(tools, null, 2)); return 0; }
    if (!tools.length) { console.log(`No verified tool matches "${query}".`); return 1; }
    console.log(`${tools.length} tool(s) for "${query}":\n`);
    // The tool search returns `tool` (the name the server itself gave it),
    // `server`, `endpoint` and `verified`. Checked against a live response:
    // reading `name` here printed "undefined" three times.
    tools.forEach((t, i) => {
      const head = `${String(i + 1).padStart(2)}. ${t.tool || t.name || 'unnamed'}`;
      const on = t.server ? `\n    on ${t.server}${t.endpoint ? '  ' + t.endpoint : ''}` : '';
      const desc = t.description ? `\n    ${String(t.description).slice(0, 150)}` : '';
      console.log(head + on + desc + '\n');
    });
    console.log('every tool above was read from that server\'s own tools/list');
    return 0;
  }

  const { results, federation } = await findResource(query, o);
  if (o.json) { console.log(JSON.stringify({ results, federation }, null, 2)); return 0; }
  if (!results.length) {
    console.log(`Nothing matched "${query}" in this index or the registries it federates.`);
    return 1;
  }
  const answered = federation && Array.isArray(federation.registries)
    ? federation.registries.filter((r) => r.ok).length : 0;
  console.log(`${results.length} match(es) for "${query}"` +
              (answered ? `, ${answered} registries answered` : '') + ':\n');
  results.forEach((r, i) => console.log(line(i + 1, r) + '\n'));
  console.log('score is relevance only, never a trust or safety rating');
  return 0;
}

main().then((c) => process.exit(c || 0)).catch((e) => {
  if (e instanceof NeurontoError) console.error(`neuronto: ${e.message}`);
  else console.error(`neuronto: ${e && e.message ? e.message : e}`);
  process.exit(1);
});
