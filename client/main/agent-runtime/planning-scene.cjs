'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { accountScopeKey } = require('../device-auth-request');
const { planningEnabled } = require('../managed-runtime.cjs');

function adapter(scene) {
  if (scene === 'business') return 'ai-planning';
  if (scene === 'coding') return 'pi';
  throw Error('请选择普通业务或 Coding 场景');
}
function forNew(app, options) {
  if (!planningEnabled(app) || options.chatId) return '';
  return options.planningScene || 'business';
}
function file(app, identity, sessionId) {
  const account = accountScopeKey(identity);
  if (!account || !sessionId || !identity.hub || !identity.clientId) throw Error('无法绑定当前身份的本机会话');
  const id = createHash('sha256').update(JSON.stringify([identity.hub, identity.clientId, account, sessionId])).digest('hex');
  return path.join(app.getPath('userData'), 'planning-scenes', id);
}
function restore(app, identity, id) {
  if (!id || !accountScopeKey(identity)) return '';
  try {
    const scene = fs.readFileSync(file(app, identity, id), 'utf8');
    adapter(scene);
    return scene;
  } catch (error) { if (error.code === 'ENOENT') return ''; throw error; }
}
function bind(app, identity, handle, scene) {
  if (!scene) return;
  if (adapter(scene) !== handle.adapterId) throw Error('当前执行底座与已绑定场景不一致');
  const dest = file(app, identity, handle.sessionId);
  fs.mkdirSync(path.dirname(dest), { recursive: true, mode: 0o700 });
  try { fs.writeFileSync(dest, scene, { flag: 'wx', mode: 0o600 }); }
  catch (error) {
    if (error.code !== 'EEXIST' || restore(app, identity, handle.sessionId) !== scene) throw error;
  }
  handle.planningScene = scene;
}
function localOnly(handle) { return Boolean(handle?.planningScene || handle?.adapterId === 'ai-planning'); }
function controlledTurnError(handle, options) {
  return options?.controlled && localOnly(handle) ? { ok: false, reason: '当前场景已绑定本机执行，请使用本机会话执行需求' } : null;
}
module.exports = { adapter, forNew, restore, bind, localOnly, controlledTurnError };
