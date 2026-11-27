'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { environment, initialize } = require('../main/managed-runtime.cjs');

test('packaged children use private tools without changing host environment', () => {
  const base = { Path: 'C:\\host', PYTHONPATH: 'C:\\unsafe', PYTHONHOME: 'C:\\old', NODE_OPTIONS: '--inspect', NODE_PATH: 'C:\\modules' };
  const env = environment(base, { platform: 'win32', resourcesPath: 'C:\\SoduWork\\resources', exists: () => true });
  assert.ok(env.Path.startsWith('C:\\SoduWork\\resources\\node;C:\\SoduWork\\resources\\python;'));
  assert.equal(env.PYTHONNOUSERSITE, '1');
  assert.equal(env.SODUWORK_OFFICE_PYTHON, 'C:\\SoduWork\\resources\\python\\python.exe');
  for (const name of ['PYTHONPATH', 'PYTHONHOME', 'NODE_OPTIONS', 'NODE_PATH']) assert.equal(env[name], undefined);
  assert.equal(base.PYTHONPATH, 'C:\\unsafe');
});
test('POSIX runtimes use bin and missing components do not add phantom paths', () => {
  const env = environment({ PATH: '' }, { platform: 'linux', resourcesPath: '/product', exists: file => file === '/product/node/bin/node' });
  assert.equal(env.PATH, '/product/node/bin');
  assert.equal(env.SODUWORK_MANAGED_PYTHON, undefined);
});
test('source development retains its explicit environment', () => {
  const env = { PATH: '/dev', PYTHONPATH: '/dev/libs' };
  initialize({ isPackaged: false }, env, '/product');
  assert.deepEqual(env, { PATH: '/dev', PYTHONPATH: '/dev/libs' });
});
test('prepared source launches prefer the same private native Node as packaged clients', t => {
  const fs = require('node:fs'), path = require('node:path'), os = require('node:os');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'source-runtime-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const binary = path.join(root, '.runtime', 'node', process.platform === 'win32' ? 'node.exe' : 'bin/node');
  fs.mkdirSync(path.dirname(binary), { recursive: true }); fs.writeFileSync(binary, 'fixture');
  const env = { PATH: '/host', NODE_OPTIONS: '--require unsafe' };
  initialize({ isPackaged: false, getAppPath: () => root }, env, '/electron/resources');
  assert.equal(env.PATH.split(path.delimiter)[0], path.dirname(binary));
  assert.equal(env.NODE_OPTIONS, undefined);
  assert.equal(env.SODUWORK_RUNTIME_MANAGED, '1');
});
test('Windows PATH aliases and stale Python overrides cannot bypass a missing product runtime', () => {
  const env = environment({ PATH: 'C:\\one', Path: 'C:\\two', SODUWORK_OFFICE_PYTHON: 'evil', SODUWORK_MANAGED_PYTHON: 'evil' },
    { platform: 'win32', resourcesPath: 'C:\\product', exists: () => false });
  assert.equal(Object.keys(env).filter(key => key.toLowerCase() === 'path').length, 1);
  assert.equal(env.SODUWORK_OFFICE_PYTHON, undefined);
  assert.equal(env.SODUWORK_MANAGED_PYTHON, undefined);
  assert.deepEqual(require('../main/conversation/office-engine').pythonCandidates(env, 'win32'), []);
});
test('only the managed interpreter and adjacent Planning extension supply the launcher', () => {
  const base = { soduwork_planning_launcher: 'C:\\host\\foreign.py', soduwork_managed_python: 'foreign' };
  assert.deepEqual(environment(base), { soduwork_managed_python: 'foreign' });
  const options = { platform: 'win32', resourcesPath: 'C:\\app', exists: () => true };
  const env = environment(base, options);
  assert.equal(env.SODUWORK_PLANNING_LAUNCHER, 'C:\\app\\planning\\launch.py');
  assert.equal(env.soduwork_planning_launcher, undefined);
  assert.equal(env.SODUWORK_PLANNING_WORKBOOK, require('node:path').win32.join('C:/app', 'skills', 'office', 'scripts', 'planning_workbook.py'));
  assert.equal(env.soduwork_managed_python, undefined);
  assert.equal(environment(base, { ...options, exists: () => false }).SODUWORK_PLANNING_LAUNCHER, undefined);
});
test('Planning distribution requires both a release artifact and its expected digest', () => {
  const { configuration } = require('../scripts/prepare-planning.cjs');
  const base = { extraResources: [], extraMetadata: { example: 1 } };
  assert.equal(configuration(base, {}), base);
  const bundle = { SODUWORK_PLANNING_WHEEL: require('node:path').resolve('soduwork_planning_harness-0.2.0-py3-none-any.whl') };
  assert.throws(() => configuration(base, bundle));
  const result = configuration(base, { ...bundle, SODUWORK_PLANNING_SHA256: 'ab'.repeat(32) });
  assert.deepEqual(result.extraMetadata, { example: 1, soduworkPlanningHarness: {
    id: 'ai-planning', distribution: 'soduwork-planning-harness', version: '0.2.0',
  } });
  assert.throws(() => configuration(base, { SODUWORK_PLANNING_BUNDLE: require('node:path').resolve('old.zip'),
    SODUWORK_PLANNING_SHA256: 'ab'.repeat(32) }));
  assert.equal(result.extraResources[0].to, 'planning');
});
