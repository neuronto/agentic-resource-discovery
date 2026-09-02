/**
 * Neuronto Agent Finder for VS Code.
 *
 * Two jobs, and the first is the reason the extension exists at all: registering
 * the MCP server so nobody has to hand-edit `.vscode/mcp.json` and get the shape
 * wrong. VS Code nests servers under `servers` rather than `mcpServers` and wants
 * the transport stated explicitly, so a config copied from Cursor or Claude
 * silently does nothing. An extension removes that failure mode entirely.
 *
 * The second job is a command, because discovery is useful outside a chat turn:
 * ask for a capability, see what exists, open the one you want. It calls the same
 * REST interface the MCP tools call, so the two cannot disagree.
 *
 * No dependencies. VS Code ships a Node with global fetch, and a discovery
 * extension that pulls a tree of packages would be its own argument against itself.
 */
const vscode = require('vscode');

const UA = 'neuronto-vscode/0.1.0 (+https://neuronto.com/connect/vscode)';

function cfg() {
  const c = vscode.workspace.getConfiguration('neuronto');
  return {
    endpoint: c.get('endpoint', 'https://neuronto.com/mcp'),
    search: c.get('searchEndpoint', 'https://neuronto.com/search'),
    federate: c.get('federate', true),
  };
}

/** Ask the index what can do a task. Returns [] rather than throwing on a miss. */
async function findResource(query, token) {
  const { search, federate } = cfg();
  const controller = new AbortController();
  const sub = token && token.onCancellationRequested(() => controller.abort());
  try {
    const res = await fetch(search, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'user-agent': UA },
      body: JSON.stringify({
        query: { text: query },
        pageSize: 15,
        ...(federate ? {} : { federation: 'none' }),
      }),
      signal: controller.signal,
    });
    if (!res.ok) throw new Error(`the index answered ${res.status}`);
    const data = await res.json();
    const results = Array.isArray(data.results) ? data.results : [];
    results.searched = summariseFederation(data.federation);
    return results;
  } finally {
    if (sub) sub.dispose();
  }
}

/**
 * Build the picker rows from what the REST interface actually returns.
 *
 * Deliberately does not reach for a `verification` field: that is on the MCP
 * tool's output, not on a REST result, and reading it here would have rendered
 * an always-empty badge implying we had checked something we had not.
 */
function quickPickItems(results) {
  return results.map((r) => {
    const bits = [];
    if (r.type) bits.push(String(r.type).replace('application/', ''));
    if (typeof r.score === 'number') bits.push(`relevance ${r.score}`);
    if (r.source) bits.push(`via ${safeHost(r.source) || r.source}`);
    return {
      label: r.displayName || r.identifier || 'unnamed resource',
      description: bits.join('  ·  '),
      detail: r.description ? String(r.description).slice(0, 220) : undefined,
      url: r.url,
      identifier: r.identifier,
    };
  });
}

async function runFind() {
  const query = await vscode.window.showInputBox({
    title: 'Neuronto: find an agentic resource',
    prompt: 'What do you need it to do?',
    placeHolder: 'read a PDF and extract tables',
    ignoreFocusOut: true,
  });
  if (!query) return;

  let results;
  try {
    results = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification,
        title: `Searching ARD registries for ${query}`,
        cancellable: true },
      (_p, token) => findResource(query, token)
    );
  } catch (err) {
    if (err && err.name === 'AbortError') return;
    vscode.window.showErrorMessage(`Neuronto: ${err && err.message ? err.message : err}`);
    return;
  }

  if (!results.length) {
    vscode.window.showInformationMessage(
      `Nothing matched "${query}" in this index or the registries it federates.`);
    return;
  }

  const searched = results.searched ? `, ${results.searched}` : '';
  const pick = await vscode.window.showQuickPick(quickPickItems(results), {
    title: `${results.length} match${results.length === 1 ? '' : 'es'} for "${query}"${searched}`,
    placeHolder: 'Relevance only. This is not a trust, safety or quality rating.',
    matchOnDescription: true,
    matchOnDetail: true,
  });
  if (!pick) return;

  // Never connect anything on the user's behalf: show them what it is and let
  // them decide. Installing something a search returned is their call, always.
  const open = 'Open resource';
  const details = 'What was verified';
  const choice = await vscode.window.showInformationMessage(
    pick.label, { modal: false }, open, details);
  if (choice === open && pick.url) {
    vscode.env.openExternal(vscode.Uri.parse(pick.url));
  } else if (choice === details) {
    const host = pick.url ? safeHost(pick.url) : null;
    vscode.env.openExternal(vscode.Uri.parse(
      host ? `https://neuronto.com/ard-publishers/${host}` : 'https://neuronto.com/connect/vscode'));
  }
}

/** "Neuronto + 3 registries", from whatever shape the federation block takes. */
function summariseFederation(fed) {
  if (!fed) return null;
  // The block is `{mode, registries: [{name, source, ok, ms, results}]}`. Checked
  // against a live response rather than assumed: an earlier guess of `sources`
  // would have silently produced no summary at all.
  const list = Array.isArray(fed) ? fed
    : (Array.isArray(fed.registries) ? fed.registries : null);
  if (!list) return null;
  const ok = list.filter((f) => f && (f.ok === undefined || f.ok)).length;
  return ok ? `${ok} registr${ok === 1 ? 'y' : 'ies'} answered` : null;
}

function safeHost(u) {
  try { return new URL(u).host; } catch { return null; }
}

function activate(context) {
  const didChange = new vscode.EventEmitter();

  context.subscriptions.push(
    vscode.lm.registerMcpServerDefinitionProvider('neuronto.agentFinder', {
      onDidChangeMcpServerDefinitions: didChange.event,
      provideMcpServerDefinitions: async () => [
        new vscode.McpHttpServerDefinition({
          label: 'Neuronto ARD Registry',
          uri: vscode.Uri.parse(cfg().endpoint),
          version: '0.1.0',
        }),
      ],
      // Nothing to resolve: the endpoint is public and unauthenticated, so there
      // is no key to prompt for and no reason to make the user answer a question.
      resolveMcpServerDefinition: async (server) => server,
    })
  );

  // Re-register when the endpoint is pointed somewhere else.
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration('neuronto.endpoint')) didChange.fire();
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('neuronto.findResource', runFind)
  );
}

function deactivate() {}

module.exports = { activate, deactivate };
