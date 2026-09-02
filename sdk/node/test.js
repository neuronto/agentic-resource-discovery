/** Smoke test against the live index. No mocks: the point is that the field
 *  names in this package match the ones the service actually returns, which is
 *  exactly what mocks would hide. */
import { findResource, findTool, registryStats, liveness } from './index.js';

let failed = 0;
const ok = (name, cond, extra = '') => {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${extra ? '  ' + extra : ''}`);
  if (!cond) failed++;
};

const { results, federation } = await findResource('read a PDF', { limit: 3 });
ok('findResource returns results', results.length > 0, `${results.length}`);
ok('results carry displayName/identifier', results.every(r => r.displayName || r.identifier));
ok('results carry a numeric score', results.every(r => typeof r.score === 'number'));
ok('federation reports registries', !federation || Array.isArray(federation.registries));

const local = await findResource('read a PDF', { limit: 3, federate: false });
ok('federate:false still returns', local.results.length > 0);
ok('federate:false does not federate', local.federation === null);

const tools = await findTool('convert currency', { limit: 3 });
ok('findTool returns tools', tools.length > 0, `${tools.length}`);
ok('tools carry `tool` not `name`', tools.every(t => typeof t.tool === 'string'));

const s = await registryStats();
ok('stats carry index + reachability', !!(s.index && s.reachability));
ok('stats state their window', typeof s.window?.days === 'number');
ok('stats publish limitations', Array.isArray(s.limitations) && s.limitations.length > 0);

const dead = await liveness({ dead: true, limit: 5 });
ok('liveness dead list', Array.isArray(dead.items));
ok('dead entries are not answering', dead.items.every(i => i.answering === false));

console.log(failed ? `\n${failed} failed` : '\nall passed');
process.exit(failed ? 1 : 0);
