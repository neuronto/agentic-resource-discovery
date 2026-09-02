/**
 * Neuronto: find the agentic resources that can do a task.
 *
 * A thin, dependency-free client for a public API. It is thin on purpose: the
 * ranking, the federation across every other public ARD registry and the
 * verification all happen server-side, so a fatter client would only add ways
 * for this library and the service to disagree about what the index contains.
 *
 * Node 18+ for global fetch. No dependencies, deliberately: a package whose job
 * is helping you find tools should not drag a tree of them in behind it.
 */

const BASE = process.env.NEURONTO_BASE || 'https://neuronto.com';
const UA = 'neuronto-node/1.0.0 (+https://neuronto.com/connect)';

class NeurontoError extends Error {
  constructor(message, status, body) {
    super(message);
    this.name = 'NeurontoError';
    this.status = status;
    this.body = body;
  }
}

async function post(path, body, { base = BASE, signal, apiKey } = {}) {
  const res = await fetch(base + path, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'user-agent': UA,
      ...(apiKey ? { authorization: `Bearer ${apiKey}` } : {}),
    },
    body: JSON.stringify(body),
    signal,
  });
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
  if (!res.ok) {
    throw new NeurontoError(
      data.detail || data.error || `the index answered ${res.status}`, res.status, data);
  }
  return data;
}

async function get(path, { base = BASE, signal, apiKey } = {}) {
  const res = await fetch(base + path, {
    headers: { 'user-agent': UA, ...(apiKey ? { authorization: `Bearer ${apiKey}` } : {}) },
    signal,
  });
  if (!res.ok) throw new NeurontoError(`the index answered ${res.status}`, res.status, null);
  return res.json();
}

/**
 * Search for a resource that can do something.
 *
 * @param {string} query        plain language: what you need it to do
 * @param {object} [opts]
 * @param {number} [opts.limit=10]
 * @param {string} [opts.kind]      restrict by media type, e.g. an MCP server
 * @param {boolean} [opts.federate=true]  also ask every other public ARD registry
 * @param {string} [opts.apiKey]    a verified-domain key. Keyed searches are not logged.
 * @returns {Promise<{results: Array, federation: object|null}>}
 */
export async function findResource(query, opts = {}) {
  const { limit = 10, kind, federate = true } = opts;
  const body = {
    query: { text: query, ...(kind ? { filter: { type: [kind] } } : {}) },
    pageSize: limit,
    ...(federate ? {} : { federation: 'none' }),
  };
  const d = await post('/search', body, opts);
  return { results: d.results || [], federation: d.federation || null };
}

/** Search individual tools rather than whole servers. Names come from each server's own tools/list. */
export async function findTool(query, opts = {}) {
  const { limit = 10 } = opts;
  const d = await post('/tools', { query: { text: query }, pageSize: limit }, opts);
  // Results carry `tool` (the server's own name for it), `server`, `endpoint`,
  // `score` and `verified`. Not `name`: that was assumed once and printed
  // "undefined" until it was checked against a real response.
  return d.results || d.tools || [];
}

/** How big the index is, what answers, and which registries it federates. */
export async function registryStats(opts = {}) {
  return get('/state-of-mcp', opts);
}

/**
 * Liveness observations, including the endpoints that stopped answering.
 * Free to use and redistribute; no key and no attribution required.
 */
export async function liveness({ dead = false, since = 0, limit = 500, cursor = 0, ...opts } = {}) {
  const qs = new URLSearchParams();
  if (dead) qs.set('dead', '1');
  if (since) qs.set('since', String(since));
  if (limit) qs.set('limit', String(limit));
  if (cursor) qs.set('cursor', String(cursor));
  return get('/liveness?' + qs.toString(), opts);
}

/** Get a domain indexed. Verified rather than trusted: the endpoint has to answer. */
export async function publish({ endpoint, domain, ...opts } = {}) {
  return post('/submit', { endpoint, domain }, opts);
}

export { NeurontoError, BASE };
export default { findResource, findTool, registryStats, liveness, publish, NeurontoError };
