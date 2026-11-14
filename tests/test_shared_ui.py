import importlib
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("page", ["creation", "library", "ops", "outline", "radar", "review_center"])
def test_pages_share_pinned_runtime_and_visible_fallback(page):
    module = importlib.import_module("app.ui." + page + "_page")
    html = getattr(module, "render_" + page + "_page")()
    asset = page.replace("_", "-")
    assert '<div id="' + asset + '-root"></div>' in html
    assert "/static/common.css" in html
    react = html.index('src="/static/vendor/react.production.min.js')
    react_dom = html.index('src="/static/vendor/react-dom.production.min.js')
    assert react < react_dom < html.index("/static/common.js") < html.index("/static/" + asset + "-app.js")
    assert "crossorigin" not in html
    assert "unpkg" not in html
    assert 'id="app-loading-fallback"' in html
    assert "页面加载" in html
    assert "MutationObserver" in html


def run_js(script):
    node = shutil.which("node")
    assert node, "Node is required for shared UI behavior checks"
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr


def test_common_requests_error_details_and_navigation():
    assert (ROOT / "app/static/common.js").exists(), "Shared request and navigation module is missing"
    run_js(r"""
const assert = require('assert/strict');
global.window = { React: { createElement: (tag, props, ...children) => ({tag, props, children}) } };
require('./app/static/common.js');
const ui = window.WriteAgentUI;
(async () => {
  let received;
  global.fetch = async (url, options) => { received = [url, options]; return {ok:true,status:200,json:async()=>({saved:true})}; };
  const options = {method:'PUT',body:'{}'};
  assert.deepEqual(await ui.requestJson('/save', options), {saved:true});
  assert.equal(received[1], options);
  assert.deepEqual(await ui.fetchJson('/read', options), {saved:true});
  assert.equal(received[1], options);
  global.fetch = async()=>({ok:false,status:422,json:async()=>({detail:[{loc:['body','title'],msg:'标题不能为空'}]})});
  await assert.rejects(ui.fetchJson('/bad'), /标题不能为空/);
  global.fetch = async()=>({ok:false,status:409,json:async()=>({detail:'正在处理中'})});
  await assert.rejects(ui.requestJson('/busy',{}), /正在处理中/);
  global.fetch = async()=>({ok:true,status:204});
  assert.equal(await ui.requestJson('/delete',{}), null);
  assert.equal(ui.statusLabel('review_pending'),'待审核');
  assert.equal(ui.statusLabel('future_status'),'future_status');
  const nav = ui.Navigation({current:'/creation'});
  assert.equal(nav.tag,'nav');
  const links = nav.children.flat();
  assert(links.some(item=>item.props.href==='/review-center'));
  const active = links.find(item=>item.props.href==='/creation');
  assert.equal(active.props.primary,true);
  assert.equal(ui.NavLink(active.props).props['aria-current'],'page');
})().catch(error=>{console.error(error);process.exit(1)});
""")


def test_shared_fields_badges_and_feedback_preserve_page_options():
    run_js(r"""
const assert = require('node:assert/strict');
const React = require('./app/static/vendor/react.production.min.js');
global.window = { React };
require('./app/static/common.js');
const {FormField, StatusBadge, Feedback} = window.WriteAgentUI;

let wordCount;
const field = FormField({id:'words', label:'目标字数', wrapped:true, labelClassName:'task-field',
  type:'number', min:1, max:50000, disabled:true, onValueChange:value => wordCount = value});
assert.equal(field.type, 'label');
assert.equal(field.props.htmlFor, 'words');
assert.equal(field.props.className, 'task-field');
const [label, input] = field.props.children;
assert.equal(label, '目标字数');
assert.equal(input.type, 'input');
assert.deepEqual([input.props.id, input.props.min, input.props.max, input.props.disabled], ['words', 1, 50000, true]);
input.props.onChange({target:{value:'2400'}});
assert.equal(wordCount, '2400');

const option = React.createElement('option', {value:'ready'}, 'Ready');
const pair = FormField({id:'status', label:'状态', as:'select', children:option});
assert.equal(pair.type, React.Fragment);
const [statusLabel, select] = pair.props.children;
assert.equal(statusLabel.props.htmlFor, select.props.id);
assert.equal(select.type, 'select');
assert.equal(select.props.children, option);

const badge = StatusBadge({status:'review_pending', className:'state-chip'});
assert.equal(badge.type, 'span');
assert.equal(badge.props.children, '待审核');
assert.equal(badge.props.className, 'state-chip');
assert.equal(StatusBadge({children:'无任务'}).props.children, '无任务');
assert.equal(Feedback({message:''}), null);
const retry = React.createElement('button', {onClick:() => 'retry'}, '重试');
const feedback = Feedback({id:'feedback-output', className:'feedback', role:'alert',
  'data-error':true, message:'请求失败', children:retry});
assert.equal(feedback.type, 'div');
assert.deepEqual([feedback.props.id, feedback.props.className, feedback.props.role, feedback.props['data-error']],
  ['feedback-output', 'feedback', 'alert', true]);
assert.deepEqual(feedback.props.children, ['请求失败', retry]);
assert.equal(feedback.props.children[1].props.onClick(), 'retry');
""")


def test_outline_repeated_generate_is_guarded_and_failure_preserves_preview():
    run_js(r"""
const assert = require('assert/strict');
const fs = require('fs');
let app, pending, requests=0, states=[], cursor=0;
const react = {
  createElement(tag, props, ...children) {
    if (typeof tag === 'function' && tag.name !== 'App') {
      return tag({...props, children: children.length <= 1 ? children[0] : children});
    }
    return {tag,props:props||{},children};
  },
  useState:(initial)=>{const i=cursor++; if(!(i in states)) states[i]=initial;return [states[i],value=>states[i]=typeof value==='function'?value(states[i]):value]},
  useRef:(initial)=>{const i=cursor++; if(!(i in states)) states[i]={current:initial};return states[i]},
  useEffect:()=>{}, Fragment:'fragment'
};
const request = ()=>{requests++;return new Promise((resolve,reject)=>{pending={resolve,reject}})};
global.window = {React:react,ReactDOM:{createRoot:()=>({render:el=>app=el.tag})},location:{search:''}};
require('./app/static/common.js');
Object.assign(window.WriteAgentUI, {fetchJson:request, requestJson:request});
global.document = {getElementById:()=>({})};
global.fetch=request;
eval(fs.readFileSync('./app/static/outline-app.js','utf8'));
function render(){cursor=0;return app()}
function find(node,label){if(!node||typeof node!=='object')return null;if(node.tag==='button'&&node.children.includes(label))return node;for(const child of node.children.flat(Infinity)){const found=find(child,label);if(found)return found}return null}
(async()=>{
  let tree=render();
  const generate=find(tree,'生成设定');
  generate.props.onClick();generate.props.onClick();
  assert.equal(requests,1,'same-tick duplicate generation must be blocked');
  tree=render();assert.equal(find(tree,'生成中...').props.disabled,true);
  pending.resolve({book_outline:{acts:['existing preview']}});
  await new Promise(resolve=>setImmediate(resolve));
  tree=render();assert(JSON.stringify(tree).includes('existing preview'));
  find(tree,'生成设定').props.onClick();
  pending.reject(new Error('生成服务暂不可用'));
  await new Promise(resolve=>setImmediate(resolve));
  tree=render();assert(JSON.stringify(tree).includes('existing preview'));
  assert(JSON.stringify(tree).includes('生成服务暂不可用'));
})().catch(error=>{console.error(error);process.exit(1)});
""")


def test_review_handoff_polling_obeys_visibility_and_effect_lifetime():
    run_js(r"""
const assert = require('node:assert/strict');
const timers = new Map(), listeners = new Set(), jobs = [], effects = [];
let nextTimer = 0, effectIndex = 0, handoff = null, App;
global.document = {
  hidden: true, getElementById: () => ({}),
  addEventListener(type, fn) { assert.equal(type, 'visibilitychange'); listeners.add(fn); },
  removeEventListener(type, fn) { assert.equal(type, 'visibilitychange'); listeners.delete(fn); }
};
const React = {
  createElement: (type, props, ...children) => ({type, props, children}),
  useState: initial => [initial === null ? handoff : initial, () => {}],
  useMemo: compute => compute(),
  useEffect(setup, deps) {
    const index = effectIndex++, previous = effects[index];
    if (previous && deps.every((value, i) => Object.is(value, previous.deps[i]))) return;
    if (previous?.cleanup) previous.cleanup();
    effects[index] = {deps, cleanup: setup()};
  }
};
global.window = {
  React, ReactDOM: {createRoot: () => ({render: element => { App = element.type; }})},
  setInterval(fn, delay) { assert.equal(delay, 4000); timers.set(++nextTimer, fn); return nextTimer; },
  clearInterval(id) { timers.delete(id); }
};
require('./app/static/common.js');
Object.assign(window.WriteAgentUI, {
  fetchJson: async () => [],
  requestJson: async url => { jobs.push(url); return {status:'published'}; }
});
require('./app/static/review-center-app.js');
function render(jobId) {
  handoff = jobId && {publishJobId:jobId};
  effectIndex = 0;
  App();
}
function visible(isVisible) {
  document.hidden = !isVisible;
  for (const fn of listeners) fn();
}
function tick() { for (const fn of timers.values()) fn(); }
function active(timerCount, listenerCount) {
  assert.equal(timers.size, timerCount);
  assert.equal(listeners.size, listenerCount);
}

render(null); active(0, 0);
render('first'); active(0, 1);
tick(); assert.deepEqual(jobs, []);
visible(true); active(1, 1);
assert.deepEqual(jobs, ['/jobs/first']);
tick(); assert.deepEqual(jobs, ['/jobs/first', '/jobs/first']);
visible(true); render('first'); active(1, 1);
visible(false); visible(false); active(0, 1);
const paused = jobs.length;
tick(); assert.equal(jobs.length, paused);
visible(true); active(1, 1);
assert.equal(jobs.length, paused + 1);
render('second'); active(1, 1);
assert.equal(jobs.at(-1), '/jobs/second');
const replaced = jobs.length;
tick(); assert.deepEqual(jobs.slice(replaced), ['/jobs/second']);
visible(false); active(0, 1);
visible(true); active(1, 1);
assert.deepEqual(jobs.slice(replaced), ['/jobs/second', '/jobs/second']);
for (const effect of effects) if (effect.cleanup) effect.cleanup();
active(0, 0);
const stopped = jobs.length;
visible(false); visible(true); tick();
assert.equal(jobs.length, stopped);
""")


def test_shared_session_dependency_closes_on_error():
    from types import SimpleNamespace
    assert (ROOT / "app/api/dependencies.py").exists(), "Independent session dependency is missing"
    dependencies = importlib.import_module("app.api.dependencies")
    class Session:
        closed = False
        def close(self):
            self.closed = True
    session = Session()
    state = SimpleNamespace(session_factory=lambda: session)
    generator = dependencies.get_session(SimpleNamespace(app=SimpleNamespace(state=state)))
    assert next(generator) is session
    with pytest.raises(RuntimeError):
        generator.throw(RuntimeError("request failed"))
    assert session.closed
