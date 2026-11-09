const {test} = require('node:test');
const assert = require('node:assert/strict');
const {derive} = require('../app/static/progress.js');
const event = (stage, kind = 'stage_start') => ({kind, detail: {stage}});
const run = (status, search = true, result = null) => ({status, payload: {search_enabled: search}, result});

test('queue does not pretend that an agent has already started', () => {
  const view = derive(run('queued'));
  assert.equal(view.active, null);
  assert.equal(view.done, 0);
  assert.ok(Object.values(view.states).every(s => s === 'waiting'));
});
test('actual events select the current agent and completed steps', () => {
  const view = derive(run('running'), [event('intake', 'stage_done'), event('search')]);
  assert.equal(view.active, 'search');
  assert.equal(view.states.intake, 'done');
  assert.equal(view.states.search, 'running');
  assert.equal(view.states.report, 'waiting');
  assert.equal(view.done, 1);
});
test('intake-only completion does not claim that all six agents ran', () => {
  const view = derive(run('completed', false, {}), [event('intake', 'stage_done')]);
  assert.equal(view.total, 1);
  assert.equal(view.done, 1);
  assert.equal(view.states.search, 'skipped');
  assert.equal(view.states.report, 'skipped');
});
test('clarification, cancellation and failure stay visible', () => {
  assert.equal(derive(run('needs_clarification')).states.intake, 'attention');
  const paused = derive(run('needs_clarification'), [event('research')]);
  assert.ok(Object.values(paused.states).every(state => state !== 'running'));
  const stopped = derive(run('cancelled'), [event('search')]);
  assert.equal(stopped.states.search, 'stopped');
  assert.equal(stopped.states.report, 'waiting');
  const failed = derive(run('failed'), [event('research')]);
  assert.equal(failed.states.research, 'error');
  assert.equal(failed.states.report, 'waiting');
});
test('a critic can return to comparison without fictitious report completion', () => {
  const view = derive(run('running'), [event('intake', 'stage_done'), event('search', 'stage_done'),
    event('research', 'stage_done'), event('comparison', 'stage_done'), event('critique', 'stage_done'),
    event('comparison', 'revision_requested')]);
  assert.equal(view.active, 'comparison');
  assert.equal(view.states.comparison, 'running');
  assert.equal(view.states.report, 'waiting');
});
test('empty source result leaves unexecuted stages skipped', () => {
  const view = derive(run('completed', true, {search: {warnings: ['Источник недоступен'],
    coverage: {remote_failed: true}}}), [event('intake', 'stage_done'), event('search')]);
  assert.equal(view.states.search, 'attention');
  assert.equal(view.states.research, 'skipped');
  assert.equal(view.states.report, 'skipped');
  assert.equal(view.done, 2);
});
