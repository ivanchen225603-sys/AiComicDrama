/* 漫剧前端的真实模块测试（node --test）。
 *
 * 为什么要有这一层：`comicdrama/web/js/*.js` 是 IIFE 经典脚本，pytest 只能检查
 * 字符串，检不出运行时状态机错误。这里用最小假 DOM 把 6 个脚本真正跑起来，
 * 锁住三件曾经出过事或很容易再出事的行为：
 *   1) applyState 必须把本地 activeProjectId 与服务端项目对齐 —— 否则首次打开
 *      就 `A.project() === null`，概览页直接报 Cannot read properties of null；
 *   2) 15 页在真实状态结构下都能渲染，且每个 data-act 都有处理器；
 *   3) 取消任务只退一次预占，AI 标识只打在真实素材上。
 */
import assert from 'node:assert/strict';
import {existsSync, readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const DIR = new URL('../comicdrama/web/js/', import.meta.url);
const FILES = ['core.js', 'store.js', 'datasource.js', 'ui.js', 'pages.js', 'app.js'];

function element(id = '') {
  return {
    id, className: '', hidden: false, textContent: '', value: '', type: 'text', name: '',
    dataset: {}, style: {}, scrollTop: 0, innerHTML: '',
  };
}

/** 最小假 DOM + localStorage。够跑完 A.load / A.render / 页面函数，不做任何网络请求。 */
function harness() {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, element(id));
    return nodes.get(id);
  };
  const memory = new Map();
  const sandbox = {
    innerWidth: 1440,
    addEventListener() {},
    location: {pathname: '/'},
    document: {
      getElementById: node,
      querySelector: () => node('__q'),
      querySelectorAll: () => [],
      addEventListener() {},
      createElement: () => element(),
      body: element('body'),
      activeElement: null,
      documentElement: element('html'),
    },
    localStorage: {
      getItem: key => (memory.has(key) ? memory.get(key) : null),
      setItem: (key, value) => memory.set(key, String(value)),
      removeItem: key => memory.delete(key),
    },
    setTimeout, clearTimeout, setInterval, clearInterval,
    fetch: () => Promise.reject(new Error('offline test harness')),
    console, JSON, Math, Date, Promise, Number, String, Object, Array, RegExp, Error, isNaN,
  };
  sandbox.window = sandbox;
  const context = vm.createContext(sandbox);
  for (const name of FILES) {
    vm.runInContext(readFileSync(new URL(name, DIR), 'utf8'), context, {filename: name});
  }
  return context.App;
}

const VOID = new Set(['br', 'hr', 'img', 'input', 'meta', 'link', 'source', 'area', 'col']);

/** 极简标签配平检查：漏闭一个 </div> 会让整页布局塌掉，值得单独断言。 */
function unbalanced(html) {
  const stack = [];
  for (const m of html.matchAll(/<(\/?)([a-zA-Z][a-zA-Z0-9]*)\b[^>]*?(\/?)>/g)) {
    const [, closing, rawTag, selfClose] = m;
    const tag = rawTag.toLowerCase();
    if (VOID.has(tag) || selfClose) continue;
    if (!closing) stack.push(tag);
    else if (stack.pop() !== tag) return `</${tag}>`;
  }
  return stack.length ? `<${stack.join('><')}>` : '';
}

function serverState(app, projects, activeProjectId) {
  return {
    ...app.defaultDB(),
    projects,
    activeProjectId,
    tasks: [],
    charges: [],
    media: [],
  };
}

test('applyState 把本地种子项目 id 对齐到服务端，首次打开不再让 project() 变 null', () => {
  const app = harness();
  // 本地离线种子是 p-demo，服务端种子是 p-demo-<owner>
  const local = app.project();
  assert.equal(local.id, 'p-demo');
  const remote = [{...local, id: 'p-demo-local'}, {...local, id: 'p-2'}];
  app.applyState(serverState(app, remote, 'p-demo-local'));
  assert.equal(app.DB.activeProjectId, 'p-demo-local');
  assert.ok(app.project(), '首次打开必须拿得到项目，否则概览页第一行就 TypeError');
  assert.equal(app.project().id, 'p-demo-local');
});

test('applyState 保留用户刚切过的项目，不被旧快照覆盖回去', () => {
  const app = harness();
  const base = app.project();
  const projects = [{...base, id: 'p-a'}, {...base, id: 'p-b'}];
  app.DB.projects = projects;
  app.DB.activeProjectId = 'p-b';
  app.applyState(serverState(app, projects, 'p-a'));
  assert.equal(app.DB.activeProjectId, 'p-b', '服务端快照较旧时不能抢走用户的选择');
});

test('15 页都能渲染、标签配平、每个 data-act 都有处理器', () => {
  const app = harness();
  const handlers = new Set(Object.keys(app.ACTIONS));
  const problems = [];
  for (const page of app.PAGES) {
    app.DB.ui.page = page.id;
    let html;
    try {
      html = app.pages[page.id](app.project());
    } catch (error) {
      problems.push(`${page.id}: ${error.message}`);
      continue;
    }
    if (!html) problems.push(`${page.id}: 输出为空`);
    const broken = unbalanced(html);
    if (broken) problems.push(`${page.id}: 标签不配平 ${broken}`);
    for (const m of html.matchAll(/data-act="([^"]+)"/g)) {
      if (!handlers.has(m[1])) problems.push(`${page.id}: 动作无处理器 ${m[1]}`);
    }
  }
  assert.deepEqual(problems, []);
  assert.equal(app.PAGES.length, 16);
});

test('在线动作清单与后端同名，且不含重复项', () => {
  const app = harness();
  const remote = app.REMOTE_ACTS;
  assert.equal(new Set(remote).size, remote.length, 'REMOTE_ACTS 不能有重复项');
  assert.equal(remote.length, 60);
  for (const name of ['cancelTask', 'purgeMedia']) assert.ok(remote.includes(name), name);
  /* 三个交付动作必须真的走服务端：漏进 REMOTE_ACTS 会让它们在本地假执行，
     用户看到「已提交」而服务端什么都没发生。 */
  for (const name of ['submitVideoTasks', 'restoreShotMedia', 'diffScriptVersions']) {
    assert.ok(remote.includes(name), `REMOTE_ACTS 缺 ${name}`);
  }
});

test('取消任务：预占全额退回、写 release、不允许退两次', () => {
  const app = harness();
  const reserve = app.DB.pricing.cap;
  const before = app.DB.ledger.balance;
  app.DB.ledger.balance -= reserve;
  app.DB.ledger.frozen += reserve;
  app.DB.tasks = [{
    id: 'task-x', projectId: app.DB.activeProjectId, shotId: 'S1', state: 'queued',
    percent: 40, reserve, cost: app.DB.pricing.t2i, settled: false,
    frames: [], idem: 'i', chargeId: 'c', upstreamTaskId: null, note: '',
  }];
  app.act.cancelTask('task-x', '测试取消');
  const task = app.DB.tasks[0];
  assert.equal(task.state, 'cancelled');
  assert.equal(app.DB.ledger.balance, before, '预占必须全额回到可用余额');
  assert.equal(app.DB.ledger.frozen, 0);
  // 本地流水是 unshift 追加（最新在前），服务端是 append，取第一条才是刚写的这条
  assert.equal(app.DB.ledger.tx[0].kind, 'release');
  assert.equal(app.DB.ledger.tx[0].amount, reserve);
  assert.throws(() => app.act.cancelTask('task-x', '再来一次'), /不能再取消/);
  assert.equal(app.DB.ledger.balance, before, '重复取消不能退第二次');
});

test('已结算任务不可取消', () => {
  const app = harness();
  app.DB.tasks = [{
    id: 'task-settled', projectId: app.DB.activeProjectId, shotId: 'S1', state: 'succeeded',
    percent: 100, reserve: 0, cost: 10, settled: true,
    frames: [], idem: 'i', chargeId: 'c', upstreamTaskId: 'u', note: '',
  }];
  assert.throws(() => app.act.cancelTask('task-settled', ''), /不能再取消/);
});

test('取消按钮只在可取消状态出现', () => {
  const app = harness();
  const base = {
    projectId: app.DB.activeProjectId, shotId: 'S1', percent: 10, reserve: 12, cost: 10,
    frames: [], idem: 'i', chargeId: 'c', upstreamTaskId: null, note: '',
  };
  app.DB.ui.taskFilter = 'all';
  app.DB.tasks = [{...base, id: 't-run', state: 'running', settled: false}];
  assert.match(app.pages.tasks(app.project()), /data-act="cancel-task"/);
  app.act.cancelTask('t-run', '');
  assert.doesNotMatch(app.pages.tasks(app.project()), /data-act="cancel-task"/);
  assert.match(app.pages.tasks(app.project()), /已取消/);
});

test('素材角标按来源分流：离线示例、真实模型、用户上传、占位图互不冒充', () => {
  const app = harness();
  const url = '/api/v1/drama/assets/a1';
  // 离线示例素材画面内自带标识，界面不再叠加角标；也绝不能写成 AI 生成。
  assert.doesNotMatch(app.mediaTile(url, 'image', 1, 320, 180, '锚点图'), /示例，非模型生成/);
  assert.doesNotMatch(app.mediaTile(url, 'image', 1, 320, 180, '锚点图'), /AI 生成/);
  assert.doesNotMatch(app.mediaTile('', 'image', 1, 320, 180, '占位'), /示例，非模型生成|AI 生成/);
  // 上传素材既不是模型产物也不是示例，用中性角标覆盖。
  assert.match(app.mediaTile(url, 'image', 1, 320, 180, '参考图', app.uploadBadge()), /上传/);
  // 切到真实 Provider 后，同一批素材才打 AI 标识。
  app.DB.server.config = Object.assign({}, app.DB.server.config || {},
    {provider: 'remote', offline_demo: false});
  assert.match(app.mediaTile(url, 'image', 1, 320, 180, '锚点图'), /AI 生成/);
  assert.equal(app.isOfflineDemo(), false);
});

/* 面向客户的界面不出现开发说明、模拟 / 内部实现字样。 */
const DEV_WORDS = /mock|Provider|provider|最后一公里|本地规则|示例，非模型生成|演示|drama\.sqlite|localStorage|上游|幂等|预占|落库|P0|P1|P2|设计说明|待付费|FFmpeg|t2i|cap /;

test('各页面不出现开发说明与模拟字样，上传面板与逐句配音入口齐全', () => {
  const app = harness();
  for (const page of app.PAGES) {
    const html = app.pages[page.id](app.project());
    const hit = html.replace(/<[^>]*>/g, ' ').match(DEV_WORDS);
    assert.equal(hit, null, `${page.id} 页面出现「${hit && hit[0]}」`);
  }
  for (const page of app.PAGES.filter(x => !x.hidden)) {
    assert.ok(page.icon, `${page.id} 缺导航图标`);
  }
  app.DB.uploads = [
    {id: 'up-ref', kind: 'reference', name: '林砚.png',
      url: '/api/v1/drama/assets/bbbbbbbbbbbbbbbb', bytes: 2048,
      assetType: 'characters', assetId: 'a1', createdAt: '11-11 00:00'},
    {id: 'up-bgm', kind: 'bgm', name: '雨声.wav',
      url: '/api/v1/drama/assets/cccccccccccccccc', bytes: 4096, createdAt: '11-11 00:00'},
  ];
  const assets = app.pages.assets(app.project());
  assert.match(assets, /data-act="upload-media" data-kind="reference"/);
  assert.match(assets, /data-act="delete-upload"/);
  const audio = app.pages.audio(app.project());
  assert.match(audio, /data-act="upload-media" data-kind="bgm"/);
  assert.match(audio, /data-act="use-upload-bgm"/);
  assert.match(audio, /data-act="tts-preview" data-subtitle=/);
  const handlers = Object.keys(app.ACTIONS);
  for (const name of ['upload-media', 'delete-upload', 'use-upload-bgm', 'download-export']) {
    assert.ok(handlers.includes(name), `${name} 缺处理器`);
  }
});

test('导出记录只在真有素材时写 AI 标识，缺字段显「—」不编造文案', () => {
  const app = harness();
  const p = app.project();
  p.qa = [true, true, true, true];
  p.shots.forEach(s => { delete s.videoUrl; delete s.imageUrl; });

  const plain = app.act.exportFilm();
  assert.equal(plain.aiLabel, undefined, '占位图导出不得宣称 AI 生成');
  assert.equal(plain.aiDisclosure, undefined);

  p.qa = [true, true, true, true];
  p.shots[0].videoUrl = '/api/v1/drama/assets/shot-1';
  const marked = app.act.exportFilm();
  assert.equal(marked.aiLabel, app.aiLabel());
  assert.equal(marked.aiDisclosure, app.aiDisclosure());

  const html = app.pages.qa(p);
  assert.match(html, /AI 标识/, '导出记录表要有标识列');
  assert.match(html, /—/, '无标识的记录显占位符而不是假文案');
});

test('账本概览在系统管理页：无数据给按钮，有数据显示余额', () => {
  const app = harness();
  assert.match(app.pages.admin(app.project()), /data-act="load-reconcile"/);
  assert.doesNotMatch(app.pages.credits(app.project()), /load-reconcile/, '账本概览属于运营功能，不放在创作者账单页');
  app.DB.server.reconcile = {
    owner: 'local', pointsPerCny: 100,
    drama: {
      unit: '整数积分', storage: 'drama.sqlite::drama_ledger', balance: 100, frozen: 0,
      retiredCost: 0, totalBalance: 100, settledCharges: 0, settledCost: 0,
      dailyCap: 500, dailyUsed: 0, cnyHint: 1, pricingSource: 'ACTION_PRICES',
    },
    note: '只读', generatedAt: '2026-11-10 12:00:00',
  };
  const html = app.pages.admin(app.project());
  assert.match(html, /账本概览/);
  assert.match(html, /<td class="num">100<\/td>/);
  assert.match(html, /2026-11-10 12:00:00/);
});

test('窄屏断点与渲染入口存在，Esc 能收起抽屉式侧栏', () => {
  const app = harness();
  assert.equal(app.NARROW_WIDTH, 1024);
  assert.equal(typeof app.isNarrow, 'function');
  assert.equal(typeof app.closeNav, 'function');
});

/* 第 7 项：刷新页面后 pending[name] / busybar 都归零，唯一还在的真源是
   /state 回包里的 in_flight 凭据。四个页面都必须从它派生「生成中」。 */
test('刷新后 in_flight 凭据让镜头卡、生成中心、任务中心、积分页都显示「生成中」', () => {
  const app = harness();
  const p = app.project();
  const shotId = p.shots[0].id;
  app.DB.charges = [{
    id: 'CH-busy', state: 'in_flight', action: 'image-to-video',
    projectId: p.id, target: 'shot:' + shotId, requestId: 'rq-busy',
    cap: 36, cost: 0, day: '11-10', note: '',
  }];
  const generate = app.pages.generate(p);
  assert.match(generate, /生成中/);
  assert.match(generate, /disabled/, '生成中的那个镜头按钮要置灰');
  assert.match(generate, /请勿重复提交/, '要告诉创作者不要重复提交');
  assert.match(app.pages.storyboard(p), /生成中/);
  assert.match(app.pages.tasks(p), /生成中/);
  assert.match(app.pages.credits(p), /生成中/);
  assert.equal(app.isBusy(p.id, 'shot', shotId), true);
  assert.equal(app.isBusy(p.id, 'shot', p.shots[1].id), false, '不同镜头不能被误伤');
});

test('账单页把凭据状态显示成中文，生成中不被误读成免费', () => {
  const app = harness();
  const p = app.project();
  app.DB.charges = [
    {id: 'c1', state: 'in_flight', action: 'script-doctor', projectId: p.id,
     requestId: 'r1', cap: 3, cost: 0, day: '11-10', note: ''},
    {id: 'c2', state: 'pending_review', action: 'tts-preview', projectId: p.id,
     requestId: 'r2', cap: 4, cost: 0, day: '11-10', note: ''},
    {id: 'c3', state: 'settled', action: 'image-to-video', projectId: p.id,
     requestId: 'r3', cap: 36, cost: 30, day: '11-10', note: ''},
  ];
  const html = app.pages.credits(p);
  assert.match(html, /<th>状态<\/th>/);
  assert.match(html, /生成中/);
  /* 对创作者说「待确认」而不是内部术语「待复核」：这笔钱已经冻结、还没有结论，
     页面上要能看出它在等什么，以及等谁。 */
  assert.match(html, /待确认/);
  assert.match(html, /已结算/);
  assert.doesNotMatch(html, /<td class="num">0<\/td><\/tr>/, '生成中的实扣不能显示成 0（会被误读为免费）');
  assert.equal(app.inFlightCharges(p.id).length, 1);
  assert.equal(app.inFlightCharges().length, 1);
});

/* 回归：`preview-film` 与 `mark-audio-done` 定价为 0，
   但它们的返回体是界面文案的唯一来源。只要它们不在 GEN_ACTS 里，在线态
   就会走「普通动作」分支、同步返回本地 `{}`，于是成片明明合成成功，提示却写着
   「0 片段 / 0s（无可合成视频轨，先逐镜做图生视频）」；「标记配音完成」同理永远是 0 个。
   这条用例同时锁住三件事：动作归类、真的请求了服务端、返回的是服务端字段。 */
test('预览成片与标记配音完成必须返回服务端结果，不被本地空对象冒充', async () => {
  const app = harness();
  const p = app.project();
  app.applyState(serverState(app, [p], p.id));
  assert.ok(app.GEN_ACTS.includes('preview-film'), '成片合成结果只在服务端，必须走生成动作通道');
  assert.ok(app.GEN_ACTS.includes('mark-audio-done'), '音频留存数只在服务端，必须走生成动作通道');
  app.DB.datasource = 'server';

  const seen = [];
  app.remote = {
    ready: true,
    state: () => Promise.resolve(serverState(app, [p], p.id)),
    action: (name, args, projectId, opts) => {
      seen.push({name, projectId, requestId: opts && opts.requestId});
      const payload = name === 'preview-film'
        ? {
            video_url: '/api/v1/drama/assets/0123456789abcdef',
            composed_clips: 8, duration_seconds: 22,
            voice_alignment: [{start_ms: 8000}, {start_ms: 16000}],
            muxed: {voice_tracks: 2, bgm: true, subtitles: 4},
          }
        : {audio_files: 4, audio_files_scope: 'project'};
      return Promise.resolve({
        state: serverState(app, [p], p.id),
        result: {provider: 'mock', kind: 'video', label: '示例，非模型生成', note: '', payload},
      });
    },
  };

  const film = await app.act['preview-film']();
  assert.equal(seen.length, 1, '必须真的请求服务端，而不是只在本地算一个空结果');
  assert.equal(seen[0].name, 'preview-film');
  assert.ok(seen[0].requestId, '生成动作要带幂等键，重复点击不能触发第二次合成');
  assert.equal(film.video_url, '/api/v1/drama/assets/0123456789abcdef');
  assert.equal(film.composed_clips, 8, '提示里的片段数要用服务端字段 composed_clips');
  assert.equal(film.duration_seconds, 22);
  assert.equal(film.voice_alignment.length, 2);

  // 用户看到的就是这句提示：必须报「已合成 + 片段数」，不能出现「无可合成视频轨」。
  const toasts = [];
  app.toast = msg => toasts.push(msg);
  await app.ACTIONS['preview-film']();
  assert.equal(seen.length, 2);
  assert.match(toasts.at(-1), /成片已合成：8 个片段/);
  assert.match(toasts.at(-1), /逐句配音 2 条/);
  assert.doesNotMatch(toasts.at(-1), /无可合成视频轨/);

  const audio = await app.act['mark-audio-done']();
  assert.equal(seen.length, 3);
  assert.equal(audio.audio_files, 4, '标记完成要显示真实留存数，不能退化成 0');
  await app.ACTIONS['mark-audio-done']();
  assert.match(toasts.at(-1), /共 4 段音频/);
});

/* 回归：换分镜丢素材要确认、过期成片不能导出、轨首混音不冒充逐句配音。 */
test('换一版分镜：有已生成素材时先弹确认，确认后才带 discardGenerated 提交', async () => {
  const app = harness();
  const p = app.project();
  app.applyState(serverState(app, [p], p.id));
  app.DB.datasource = 'server';
  const seen = [];
  app.remote = {
    ready: true,
    state: () => Promise.resolve(serverState(app, [p], p.id)),
    action: (name, args) => {
      seen.push({name, args});
      return Promise.resolve({
        state: serverState(app, [p], p.id),
        result: {provider: 'mock', payload: {count: 3}},
      });
    },
  };
  const modals = [];
  app.openModal = cfg => modals.push(cfg);
  app.toast = () => {};

  // 没有生成素材：直接提交，不打扰用户。
  app.project().shots.forEach(s => { s.videoUrl = ''; s.imageUrl = ''; });
  await app.ACTIONS['resplit-shots-v2']();
  assert.equal(modals.length, 0);
  assert.equal(seen.length, 1);
  assert.deepEqual(Array.from(seen[0].args), []);

  // 未锁定镜头已有视频：必须先确认，确认前不发请求。
  const draft = app.project().shots.find(s => s.state !== 'locked');
  draft.videoUrl = '/api/v1/drama/assets/0123456789abcdef';
  await app.ACTIONS['resplit-shots-v2']();
  assert.equal(modals.length, 1);
  assert.equal(seen.length, 1, '确认之前不能提交');
  assert.match(modals[0].note, /1 段视频/);
  assert.match(modals[0].note, /无法找回/);
  await modals[0].onSubmit({});
  assert.equal(seen.length, 2);
  assert.equal(seen[1].args[0].discardGenerated, true);
});

test('成片过期：时间线与质检页都提示，导出按钮不会先报成功', () => {
  const app = harness();
  const p = app.project();
  app.DB.datasource = 'server';
  p.qa = [true, true, true, true];
  p.film = {videoUrl: '/api/v1/drama/assets/0123456789abcdef', signature: 'abc', stale: true};

  // 镜头缺视频轨：先提示成片不完整，同样不发起导出。
  const early = [];
  app.toast = msg => early.push(msg);
  const realAct = app.act;
  let calls = 0;
  app.act = new Proxy({}, {get: () => () => { calls += 1; return {name: 'x'}; }});
  assert.match(app.pages.qa(p), /成片不完整/);
  app.ACTIONS['export-film']();
  assert.equal(calls, 0);
  assert.match(early.at(-1), /没有视频/);
  app.act = realAct;
  p.shots.forEach(s => { s.videoUrl = '/api/v1/drama/assets/fedcba9876543210'; });
  assert.equal(app.filmStale(p), true);
  assert.match(app.pages.timeline(p), /成片已过期/);
  assert.match(app.pages.qa(p), /成片已过期/);

  const toasts = [];
  app.toast = msg => toasts.push(msg);
  let exported = 0;
  app.act = new Proxy({}, {get: () => () => { exported += 1; return {name: 'x'}; }});
  app.ACTIONS['export-film']();
  assert.equal(exported, 0, '过期成片不能发起导出');
  assert.match(toasts.at(-1), /成片已过期/);

  p.film.stale = false;
  assert.doesNotMatch(app.pages.qa(p), /成片已过期/);
  app.ACTIONS['export-film']();
  assert.equal(exported, 1);

  p.film = null;
  app.ACTIONS['export-film']();
  assert.equal(exported, 1, '没有成片时也不能先报成功');
  assert.match(toasts.at(-1), /请先合成成片/);
});

test('配音文案：只有按字幕对齐才叫逐句配音', () => {
  const app = harness();
  assert.equal(app.voiceMixText('subtitle', 2), '逐句配音 2 条');
  assert.match(app.voiceMixText('legacy_track_head', 1), /混在片头（未按句对齐）/);
  assert.equal(app.voiceMixText('subtitle', 0), '无配音');
});

test('剧本诊断结论在其他生成动作之后仍显示；离线示例导出不标 AI 生成', () => {
  const app = harness();
  const p = app.project();
  p.lastResult = {action: 'tts-preview', payload: {}, at: '11-12 10:00',
    byAction: {'script-doctor': {payload: {findings: ['第二场节奏偏慢']}, at: '11-12 09:58'}}};
  assert.match(app.pages.script(p), /第二场节奏偏慢/);

  p.exports = [
    {id: 'e1', time: '11-12', name: '示例', res: '1080', shots: 8, duration: 22,
      filmUrl: '/api/v1/drama/assets/a', aiLabel: 'AI 生成', sampleLabel: '示例，非模型生成'},
  ];
  assert.doesNotMatch(app.pages.qa(p), /AI 生成<\/span>/);
  p.exports[0].sampleLabel = '';
  assert.match(app.pages.qa(p), /AI 生成<\/span>/, '真实成片仍要显示 AI 标识');
});

test('积分页的使用上限：未设置回落到余额，设置后显示剩余与可用额度', () => {
  const app = harness();
  const p = app.project();
  app.DB.creditLimits = {};
  let v = app.creditView();
  assert.equal(v.usable, app.DB.ledger.balance, '未设置时可用额度 = 全部余额');
  let html = app.pages.credits(p);
  assert.match(html, /data-act="save-credit-limits"/);
  assert.match(html, /placeholder="不限"/);
  assert.doesNotMatch(html, /data-act="clear-credit-limits"/);

  app.DB.charges = [{id: 'c', state: 'in_flight', action: 'image-to-video', price: 300, cap: 360,
    projectId: p.id, cost: 0, day: '11-12'}];
  app.DB.creditLimits = {single: 0, total: 500, used: 100, since: '2026-11-12 10:00'};
  v = app.creditView();
  assert.equal(v.pending, 300, '生成中按单价占用额度');
  assert.equal(v.remaining, 100);
  assert.equal(v.usable, Math.min(100, app.DB.ledger.balance));
  html = app.pages.credits(p);
  assert.match(html, /剩余额度/);
  assert.match(html, /上限为 0 时/);
  assert.match(html, /data-act="clear-credit-limits"/);
  assert.match(html, /data-act="reset-credit-used"/);

  app.DB.charges = [];
  app.DB.creditLimits = {single: null, total: 999999, used: 0};
  assert.match(app.pages.credits(p), /超过当前余额，按余额计算/);

  assert.throws(() => app.act.saveCreditLimits({total: -1}), /上限必须是 0 或正整数/);
  /* Number('Infinity') 是 Infinity，Math.floor(Infinity) === Infinity 会让旧判断误放行，
     序列化后又变成 null。前端必须与服务端 _limit_value 一样先挡非有限值。 */
  assert.throws(() => app.act.saveCreditLimits({single: 'Infinity'}), /上限必须是 0 或正整数/);
  assert.throws(() => app.act.saveCreditLimits({single: 'NaN'}), /上限必须是 0 或正整数/);
  const saved = app.act.saveCreditLimits({single: '', total: ''});
  assert.equal(saved.total, null);
  assert.equal(saved.usable, app.DB.ledger.balance);
});

test('生成视频弹窗提前提示会超出使用上限', async () => {
  const app = harness();
  const p = app.project();
  app.DB.server.config = Object.assign({}, app.DB.server.config || {}, {prices: {'image-to-video': 300}});
  const modals = [];
  app.openModal = cfg => modals.push(cfg);
  app.DB.creditLimits = {single: 100, total: null, used: 0};
  await app.ACTIONS['image-to-video'](null, {id: p.shots[0].id});
  assert.match(modals.at(-1).body, /超出你设置的单次上限 100/);
  app.DB.creditLimits = {single: null, total: 350, used: 100};
  await app.ACTIONS['image-to-video'](null, {id: p.shots[0].id});
  assert.match(modals.at(-1).body, /超出剩余额度 250/);
  app.DB.creditLimits = {};
  await app.ACTIONS['image-to-video'](null, {id: p.shots[0].id});
  assert.doesNotMatch(modals.at(-1).body, /超出/);
});

/* 合规回归：服务端 CSP 是 default-src 'self'，不含 unsafe-inline，
   所以样式里一旦出现行内 style、data: 图片、<style 或 javascript: 就会在
   真实浏览器里被拦下。把这条结论
   固化成静态断言，防止后续改样式悄悄破坏合规；下拉箭头改为同域 SVG。 */
test('静态 CSP 断言：HTML/CSS 无行内样式与 data: 图，箭头为同域 SVG', () => {
  const web = new URL('../comicdrama/web/', import.meta.url);
  const html = readFileSync(new URL('index.html', web), 'utf8');
  const css = readFileSync(new URL('assets/styles.css', web), 'utf8');

  assert.doesNotMatch(html, /style\s*=/i, 'HTML 不得出现行内 style 属性');
  assert.doesNotMatch(css, /data:image/i, 'CSS 不得内嵌 data: 图片（default-src 会拦下）');
  for (const [name, text] of [['index.html', html], ['styles.css', css]]) {
    assert.doesNotMatch(text, /unsafe-inline/i, `${name} 不得出现 unsafe-inline`);
    assert.doesNotMatch(text, /javascript:/i, `${name} 不得出现 javascript:`);
    assert.doesNotMatch(text, /<style/i, `${name} 不得内嵌 <style 标签`);
  }
  assert.ok(existsSync(new URL('assets/arrow.svg', web)), '同域箭头素材 assets/arrow.svg 必须存在');
  assert.match(css, /url\("arrow\.svg"\)/, '下拉箭头必须改为指向同域 arrow.svg');
});

/* 账户与充值入口。三条底线一次锁死：
   1) CSP default-src 'self' 下，新页面不得出现行内样式 / javascript: / <style / data: 图；
   2) 页面文案不得出现字段名与状态机内部值（order_id、pending_review …），一律说人话；
   3) 服务端新增的账户 / 支付错误码都要有中文提示，不能把英文码漏给用户。 */
const INTERNAL_WORDS = ['order_id', 'request_id', 'amount_points', 'created_at',
  'pending_review', 'upstreamTaskId', 'credited', 'account_session_required',
  /* 内容安全：审核结论只在服务端字段里，页面一律说人话。 */
  'auditRejected', 'auditStatus', 'content_blocked'];

test('账户页与充值入口：无行内样式、无内部字段名、错误码齐全', () => {
  const app = harness();
  const account = app.pages.account(app.project());
  const credits = app.pages.credits(app.project());

  for (const [name, html] of [['账户页', account], ['积分与账单页', credits]]) {
    assert.doesNotMatch(html, /style\s*=/i, `${name} 不得出现行内 style 属性`);
    assert.doesNotMatch(html, /javascript:/i, `${name} 不得出现 javascript:`);
    assert.doesNotMatch(html, /<style/i, `${name} 不得内嵌 <style 标签`);
    assert.doesNotMatch(html, /data:image/i, `${name} 不得内嵌 data: 图片`);
    const text = html.replace(/<[^>]*>/g, ' ');
    for (const word of INTERNAL_WORDS) {
      assert.ok(!text.includes(word), `${name} 文案出现内部字段「${word}」`);
    }
  }

  // 未登录：只有登录与注册两个入口，规则文案写在注册表单旁边。
  assert.match(account, /data-act="account-login"/);
  assert.match(account, /data-act="account-register"/);
  assert.match(account, /账号需为 3–64 位字母、数字或 _ \. -/);
  assert.match(account, /密码需为 8–128 位字符/);
  assert.doesNotMatch(account, /data-act="account-logout"/, '未登录不该有退出登录');
  assert.match(credits, /data-act="nav" data-page="account"/, '未登录时充值区给出去账户页的入口');
  assert.doesNotMatch(credits, /data-act="payment-create"/, '未登录不给下单按钮');

  // 已登录：账户信息 + 改密码 + 退出；充值区出现面额、下单与订单记录。
  app.account.me = {username: 'linyan', owner: 'owner-0f3a91c2', role: 'user', admin: false};
  const loggedIn = app.pages.account(app.project());
  assert.match(loggedIn, /data-act="account-password"/);
  assert.match(loggedIn, /data-act="account-logout"/);
  assert.match(loggedIn, /linyan/, '有账号名就显示账号名');
  const recharge = app.pages.credits(app.project());
  assert.match(recharge, /data-act="payment-create"/);
  assert.match(recharge, /data-act="payment-orders-refresh"/);
  assert.match(recharge, /充值记录/);

  // 订单状态一律中文：created 不能把状态机内部值印到页面上。
  app.account.orders = [{order_id: 'ord-1234567890abcdef', amount_points: 500, channel: 'offline',
    status: 'created', request_id: 'ui-1', created_at: 1762900000, paid_at: null, credited_at: null}];
  app.account.ordersLoaded = true;
  const withOrder = app.pages.credits(app.project());
  assert.match(withOrder, /待提交/);
  assert.match(withOrder, /data-act="payment-pay"/);
  assert.match(withOrder, /data-act="payment-cancel"/);
  assert.doesNotMatch(withOrder.replace(/<[^>]*>/g, ' '), /created/);

  for (const key of ['account_locked', 'payment_signature_invalid', 'payment_request_conflict']) {
    assert.ok(app.ERROR_TEXT[key], `core.js 的 ERROR_TEXT 缺 ${key}`);
  }
});

/* 上游判定内容不合规的凭据。钱全额退回、产物已从项目里摘掉，
   账单页必须把结论说人话（「未通过审核」），且不得把内部字段名印出来。 */
test('积分页：未通过审核的凭据显示「未通过审核」且实扣为「—」', () => {
  const app = harness();
  const p = app.project();
  app.DB.charges = [{
    id: 'CH-rej', state: 'released', action: 'image-to-video', price: 300, cap: 360,
    projectId: p.id, cost: 0, day: '11-12', auditRejected: true, upstreamTaskId: 'u-1',
  }];
  const html = app.pages.credits(p);
  assert.match(html, /未通过审核/);
  assert.match(html, /该次生成未通过内容审核，积分已全额退回/);
  assert.match(html, /<td class="num">—<\/td><\/tr>/, '实扣列显示「—」而不是 0');
  const text = html.replace(/<[^>]*>/g, ' ');
  for (const word of ['auditRejected', 'auditStatus', 'content_blocked']) {
    assert.ok(!text.includes(word), `账单页文案出现内部字段「${word}」`);
  }
});

/* 同一张「未通过审核」的凭据，在结算部署下钱是**真扣了**。
   页面必须按实际落点说话——说「已全额退回」等于对用户陈述一笔没发生的退款。 */
test('积分页：结算部署下未通过审核的凭据必须写明已扣费、实扣不为「—」', () => {
  const app = harness();
  const p = app.project();
  app.DB.charges = [{
    id: 'CH-settle', state: 'settled', action: 'image-to-video', price: 300, cap: 360,
    projectId: p.id, cost: 300, day: '11-12', auditRejected: true, upstreamTaskId: 'u-2',
  }];
  const html = app.pages.credits(p);
  assert.match(html, /未通过审核/);
  assert.match(html, /该次生成未通过内容审核，本次已按规则结算，实际扣费 300 积分/);
  assert.doesNotMatch(html, /积分已全额退回/, '钱实际扣了，不能说已全额退回');
  assert.match(html, /<td class="num">360<\/td><td class="num">300<\/td><\/tr>/, '实扣列必须是真实扣费');
});

/* 镜头卡片上的「未通过审核」提示同样不能把结算说成退款。
   退款部署会摘掉产物（videoUrl 为空）；结算部署产物保留、钱照扣。 */
test('画面生成页：结算部署下保留产物的镜头不说「已全额退回」', () => {
  const app = harness();
  const p = app.project();
  const keep = p.shots[0];
  keep.auditRejected = true;
  keep.videoUrl = '/api/v1/drama/assets/demo.mp4';
  const granted = app.pages.generate(p);
  assert.match(granted, /这次生成未通过审核，本次已按规则结算扣费/);
  assert.doesNotMatch(granted, /积分已全额退回/);

  keep.videoUrl = '';
  const refunded = app.pages.generate(p);
  assert.match(refunded, /这次生成未通过审核，积分已全额退回/);
});

/* AI 标识的文案与位置由服务端配置决定，管理页只读展示（不提供开关）。 */
test('管理页：只读展示 AI 标识文案与位置，默认不含烧入画面', () => {
  const app = harness();
  app.DB.server.config = Object.assign({}, app.DB.server.config || {}, {
    ai_disclosure: '含 AI 生成内容（非真人实拍）',
    ai_label_positions: ['container_metadata'],
  });
  const html = app.pages.admin(app.project());
  assert.match(html, /AI 生成标识/);
  assert.match(html, /含 AI 生成内容（非真人实拍）/);
  assert.match(html, /成片容器元数据/);
  /* 位置清单里不能出现「烧入画面」这一项；卡片下方那句固定提示是服务端口径说明，
     本来就该出现，所以只断言位置标签不出现。 */
  assert.doesNotMatch(html, /烧入画面（未实现，需法务确认）/, '默认配置不得带来烧入画面位置');
  assert.match(html, /烧入画面需法务口径确认后开启/, '必须写明烧入画面未开启的原因');
  assert.doesNotMatch(html, /data-act="save-ai-/, '标识只读，页面不提供开关');
});

/* 成片字幕 / 分辨率上限是**交付格式**，离线态也必须真的存下来并夹取。
   改造前 `localActs.saveSettings` 只挑 8 个已知字段赋值，这两项被静默丢弃：
   用户在离线演示里改完保存，页面回显还是旧值，看起来像「保存没生效」。 */
test('项目设定：离线保存存下成片字幕与分辨率，非法值夹到最保守一侧', () => {
  const app = harness();
  app.act.saveSettings({subtitleMode: 'burn', outputResolution: '720p'});
  assert.equal(app.project().subtitleMode, 'burn');
  assert.equal(app.project().outputResolution, '720p');

  /* 拼错不许抛错：这是下拉框的值，写坏它不该让整次保存失败。
     夹取方向与服务端 parse_subtitle_mode / parse_resolution 一致（最保守）。 */
  app.act.saveSettings({subtitleMode: 'BURN!', outputResolution: '4k'});
  assert.equal(app.project().subtitleMode, 'soft', '拼错夹回软字幕');
  assert.equal(app.project().outputResolution, 'source', '拼错夹回不缩放');
});

/* 只改交付格式时，不能把已锁定镜头标成「待更新」。
   画面没变，改的只是封装与缩放；标脏会让用户以为要重出一整集关键帧。 */
test('项目设定：只改交付格式不算画面变更，不标脏已锁定镜头', () => {
  const app = harness();
  const p = app.project();
  p.shots[0].state = 'locked';
  p.shots[0].dirty = false;

  const changed = app.act.saveSettings({subtitleMode: 'burn'});

  assert.equal(changed, false, '只改字幕，changed 必须是 false');
  assert.equal(p.shots[0].state, 'locked', '已锁定镜头不许被标脏');
  assert.equal(app.project().subtitleMode, 'burn', '但字幕设置本身要存下来');
});

/* 新手引导的关闭状态必须落本地。它只是首屏提示，不是业务数据，
   所以不进服务端 state —— 但关过就该一直关着，刷新后又冒出来会很烦。 */
test('概览页：关掉新手引导后刷新不再出现', () => {
  const app = harness();
  app.DB.ui.onboardDismissed = undefined;
  const first = app.pages.overview(app.project());
  assert.match(first, /data-act="onboard-dismiss"/, '首次进入要看到引导');

  app.ACTIONS['onboard-dismiss']();
  assert.equal(app.DB.ui.onboardDismissed, true);

  app.load();
  const again = app.pages.overview(app.project());
  assert.doesNotMatch(again, /data-act="onboard-dismiss"/, '关过一次就不该再冒出来');
});

test('模型设置页：未配置显示示例与内置，配置后只显示打码密钥且不回显原值', () => {
  const app = harness();
  app.DB.server.config = Object.assign({}, app.DB.server.config || {}, {
    credentials: {liblib: {configured: false}, llm: {configured: false}},
  });
  let html = app.pages.models(app.project());
  assert.match(html, /示例素材/);
  assert.match(html, /内置规则/);
  assert.match(html, /data-act="save-liblib"/);
  assert.match(html, /data-act="save-llm"/);
  assert.doesNotMatch(html, /data-act="clear-credentials"/);

  app.DB.server.config.credentials = {
    liblib: {configured: true, access_key: '••••Mngyw', secret_key: '••••_Tyb'},
    llm: {configured: true, base_url: 'https://api.deepseek.com/v1', model: 'deepseek-chat', api_key: '••••abcd'},
  };
  app.DB.server.config.public_base_url_set = false;
  html = app.pages.models(app.project());
  assert.match(html, /placeholder="••••Mngyw"/);
  assert.match(html, /value=""/, '密钥输入框不回显原值');
  assert.match(html, /公网 https 地址/);
  assert.match(html, /data-act="clear-credentials" data-scope="liblib"/);
  assert.match(html, /data-act="test-credentials"/);
});

test('生成进度：显示已等待时长与通常耗时，超时后换成明确说法', () => {
  const app = harness();
  const now = Date.now() / 1000;
  assert.match(app.waitText(now - 75, 'image-to-video'), /已等待 1 分 15 秒，通常需要 2 分钟～5 分钟/);
  assert.match(app.waitText(now - 400, 'image-to-video'), /比平时久/);
  assert.equal(app.clock(9), '9 秒');
  const p = app.project();
  app.DB.charges = [{id: 'c', state: 'in_flight', action: 'image-to-video', projectId: p.id,
    target: 'shot:' + p.shots[0].id, cap: 0, cost: 0, price: 0, createdAt: now - 30, day: '11-12'}];
  const html = app.pages.generate(p);
  assert.match(html, /data-since="/);
  assert.match(html, /data-action="image-to-video"/);
});

test('模型设置页有配音卡片，配置后只显示打码密钥', () => {
  const app = harness();
  app.DB.server.config = Object.assign({}, app.DB.server.config || {}, {
    credentials: {liblib: {configured: true}, llm: {configured: false},
      tts: {configured: true, base_url: 'https://api.siliconflow.cn/v1', model: 'm', voice: 'a,b', api_key: '••••3456'}},
    tts_configured: true,
  });
  const html = app.pages.models(app.project());
  assert.match(html, /data-act="save-tts"/);
  assert.match(html, /placeholder="••••3456"/);
  assert.match(html, /data-scope="tts"/);
  assert.match(html, /配音服务/);
});
