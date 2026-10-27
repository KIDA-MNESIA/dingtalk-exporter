const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

function client(responses) {
    const calls = [];
    const context = vm.createContext({
        window: {},
        document: {addEventListener() {}},
        setTimeout: resolve => resolve(),
        fetch: async url => {
            calls.push(url);
            assert.ok(responses.length, 'Unexpected request');
            const result = responses.shift();
            return {ok: true, json: async () => result};
        },
    });
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/static/app.js'), 'utf8'), context);
    return {wait: context.waitForJob, calls};
}

test('polls only the accepted job and returns its export', async () => {
    const instance = client([
        {status: 'running'},
        {status: 'succeeded', result: {export_path: 'new-export'}},
    ]);
    const result = await instance.wait({status: 'started', job_id: 'my-job'});
    assert.equal(result.export_path, 'new-export');
    assert.deepEqual(instance.calls, ['/api/jobs/my-job', '/api/jobs/my-job']);
});

test('a conflicting job is never mistaken for an accepted export', async () => {
    const instance = client([]);
    await assert.rejects(instance.wait({status: 'already_running', job_id: 'other-job'}), /任务未启动/);
    assert.deepEqual(instance.calls, []);
});

test('a failed job does not reuse an older successful export', async () => {
    const instance = client([{status: 'failed', error: 'test failure'}]);
    await assert.rejects(instance.wait({status: 'started', job_id: 'new-job'}), /test failure/);
});

test('snapshot fallback is reported instead of export success', async () => {
    const instance = client([{status: 'succeeded', result: {outcome: 'fallback', error: 'snapshot rejected'}}]);
    await assert.rejects(instance.wait({status: 'started', job_id: 'refresh-job'}), /snapshot rejected/);
});
