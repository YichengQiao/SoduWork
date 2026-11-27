'use strict';

const path = require('node:path');
const { spawnSync } = require('node:child_process');

function release(env = process.env) {
  const wheel = env.SODUWORK_PLANNING_WHEEL || env.SODUWORK_PLANNING_BUNDLE;
  const sha = env.SODUWORK_PLANNING_SHA256;
  if (!wheel && !sha) return null;
  if (env.SODUWORK_PLANNING_WHEEL && env.SODUWORK_PLANNING_BUNDLE && env.SODUWORK_PLANNING_BUNDLE !== wheel) {
    throw Error('Conflicting Planning Harness wheel paths');
  }
  const identity = /^soduwork_planning_harness-(\d+(?:\.\d+)+(?:a\d+|b\d+|rc\d+)?(?:\.post\d+)?(?:\.dev\d+)?)-py3-none-any\.whl$/
    .exec(path.basename(wheel || ''));
  if (!identity || !path.isAbsolute(wheel) || !/^[a-f0-9]{64}$/i.test(sha || '')) {
    throw Error('Planning Harness requires its published wheel and a trusted SHA-256');
  }
  return { wheel, version: identity[1], sha: sha.toLowerCase() };
}
function configuration(base, env = process.env) {
  const bundle = release(env);
  if (!bundle) return base;
  return { ...base, extraMetadata: { ...base.extraMetadata,
    soduworkPlanningHarness: { id: 'ai-planning', distribution: 'soduwork-planning-harness', version: bundle.version } },
    extraResources: [...(base.extraResources || []),
      { from: path.posix.join('.runtime/planning-harness', process.platform + '-' + process.arch, bundle.sha),
        to: 'planning', filter: ['**/*'] }] };
}
function prepare({ root, platform, arch, development = false }, env = process.env) {
  const bundle = release(env);
  if (!bundle) {
    if (development) throw Error('Set SODUWORK_PLANNING_WHEEL and SODUWORK_PLANNING_SHA256');
    return;
  }
  if (platform !== process.platform || arch !== process.arch) {
    throw Error('Build the Planning-enabled installer on its native target');
  }
  const wheelhouse = env.SODUWORK_PLANNING_WHEELHOUSE;
  if (!wheelhouse || !path.isAbsolute(wheelhouse)) throw Error('Set SODUWORK_PLANNING_WHEELHOUSE to the offline dependency directory');
  const python = require('./prepare-python-runtime.cjs').executable(path.join(root, '.runtime/python'), platform);
  const destination = development ? path.join(root, '.runtime/planning')
    : path.join(root, '.runtime/planning-harness', platform + '-' + arch, bundle.sha);
  const result = spawnSync(python, ['-I', '-B', path.join(__dirname, 'unpack-planning.py'), bundle.wheel,
    bundle.sha, destination, platform, arch,
    require('../package.json').version, require('../runtime-manifest.json').schema,
    require('./prepare-python-runtime.cjs').VERSION, wheelhouse], { stdio: 'inherit' });
  if (result.error || result.status !== 0) throw result.error || Error('Planning Harness wheel verification failed');
}
async function prepareDevelopment() {
  const root = path.resolve(__dirname, '..');
  if (!release()) throw Error('Set SODUWORK_PLANNING_WHEEL and SODUWORK_PLANNING_SHA256');
  const target = { root, platform: process.platform, arch: process.arch };
  await require('./prepare-node-runtime.cjs').prepare(target);
  await require('./prepare-python-runtime.cjs').prepare(target);
  prepare({ ...target, development: true });
  const fs = require('node:fs');
  const scripts = path.join(root, '.runtime/skills/office/scripts');
  fs.mkdirSync(scripts, { recursive: true });
  for (const file of ['planning_workbook.py', 'inspect_office.py']) {
    fs.copyFileSync(path.join(root, '../skills/office/scripts', file), path.join(scripts, file));
  }
}
if (require.main === module) {
  if (process.argv.length !== 3 || process.argv[2] !== '--development') {
    console.error('Usage: node scripts/prepare-planning.cjs --development');
    process.exitCode = 2;
  } else {
    prepareDevelopment().catch(error => { console.error(error.message); process.exitCode = 1; });
  }
}
module.exports = { release, configuration, prepare };
