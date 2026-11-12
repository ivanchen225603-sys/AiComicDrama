/* store.js — 状态、持久化、业务规则与资金账本。
   口径：流水 amount = 余额变动。reserve 把余额转冻结；settle 在冻结内核销（余额变动 0）；
   只有退差 / 释放才把积分退回余额。所有写操作经 App.save() 落 localStorage。 */
(function(A){
  "use strict";

  /* 定价单一源的服务端对应物是 comicdrama/pricing.py；这里的数字必须与它同值，
     否则离线态会按旧价真实扣费（submitTasks 用 pricing.cap 预占、pricing.t2i 结算），
     用户断网重连就会看到余额跳变。cap = ceil(售价 × 1.2)：30→36。
     售价 30 的依据见 pricing.py：上游实测 25，+20% 毛利。 */
  A.DEFAULT_PRICING = {t2i:30, cap:36, qps:1, concurrency:5, video:null};
  /* 「恢复默认」只重置影响下游节点的全局设定，不动项目名 / 分集标题 / 剧本 / 资产 / 分镜 / 账本。 */
  A.DEFAULT_SETTINGS = {genre:"都市悬疑", style:"精致国漫", ratio:"9:16 竖屏",
    eps:12, duration:90, budget:3000, res:"1080 × 1920 / 24fps",
    /* 交付格式的两个默认值，与服务端 comicdrama/config.py 的
       drama_subtitle_mode / drama_output_resolution 保持同值：
       软字幕（不重编码、播放器可关）、不缩放。 */
    subtitleMode:"soft", outputResolution:"source"};

  /* 成片交付格式的合法取值。服务端 compose.parse_subtitle_mode / parse_resolution
     对非法值一律回落默认且**不抛错**（那是配置项，配坏了不该让成片流程整体失败），
     离线态必须同口径：页面下拉里出现未知值就按默认走，不把坏值写进本地库。 */
  var SUBTITLE_MODES = ["soft", "burn"];
  var OUTPUT_RESOLUTIONS = ["source", "720p", "1080p"];
  function pickEnum(raw, allowed, fallback){
    var v = String(raw == null ? "" : raw);
    return allowed.indexOf(v) >= 0 ? v : fallback;
  }

  var SHOT_SEED = [
    {dur:3, size:"全景",   desc:"雨夜的雾港码头，远处灯塔扫过湿漉漉的石阶", prompt:"雨夜 码头 冷蓝调 湿石阶 电影感", line:"（旁白）那封信，比船晚到了十一年。", move:"缓慢下摇", refs:["@场景:雾港码头"]},
    {dur:3, size:"中景",   desc:"林砚撑黑伞站定，抬头看向邮局方向",       prompt:"中景 女性 黑伞 侧脸 冷光",       line:"林砚：我还是来了。",           move:"固定镜头", refs:["@角色:林砚","@场景:雾港码头"]},
    {dur:2, size:"特写",   desc:"信封边角被雨水浸软，字迹微微晕开",       prompt:"特写 信封 雨滴 纸张纹理",       line:"",                           move:"微推",     refs:["@道具:旧信封"]},
    {dur:4, size:"中近景", desc:"周叙从邮局门廊走出，两人隔着雨幕对视",   prompt:"双人 中近景 雨幕 对视 浅景深", line:"周叙：你收到信了。",         move:"缓慢横移", refs:["@角色:林砚","@角色:周叙","@场景:街角邮局"]},
    {dur:2, size:"特写",   desc:"林砚握紧伞柄，指节发白",                 prompt:"特写 手部 伞柄 情绪张力",       line:"",                           move:"固定镜头", refs:["@角色:林砚"]},
    {dur:3, size:"全景",   desc:"街灯亮起，两人之间隔着整条湿街",         prompt:"全景 街灯 湿街 对称构图",       line:"（旁白）有些话，一开口就成了告别。", move:"后拉", refs:["@场景:街角邮局","@角色:林砚","@角色:周叙"]},
    {dur:3, size:"近景",   desc:"旧公寓窗内亮起一盏暖灯",                 prompt:"近景 窗 暖光 内外冷暖对比",     line:"",                           move:"上摇",     refs:["@场景:旧公寓"]},
    {dur:2, size:"特写",   desc:"信纸摊开，最后一行字被灯光照亮",         prompt:"特写 信纸 暖光 浅景深",         line:"林砚：那就到这里吧。",       move:"微推",     refs:["@道具:旧信封"]}
  ];

  function seedProject(){
    var shots = SHOT_SEED.map(function(s,i){
      return {
        id:"S"+(i+1), dur:s.dur, size:s.size, desc:s.desc, prompt:s.prompt,
        line:s.line, move:s.move, refs:s.refs.slice(), state:i<4?"locked":"draft", dirty:false
      };
    });
    var layers = {
      logline:"十一年前寄丢的一封信，在雾港的雨夜被重新送回。收信人必须决定：拆开它，还是让它继续沉在海底。",
      outline:"雨夜，林砚按信上的地址来到雾港码头；周叙在街角邮局等她；两人隔着雨幕对视，谁都没有先开口；结尾亮起旧公寓的一盏灯。",
      body:"【场景 1 · 雾港码头 · 夜 · 雨】\n（旁白）那封信，比船晚到了十一年。\n林砚撑黑伞站定，抬头看向邮局方向。\n林砚：我还是来了。\n\n【场景 2 · 街角邮局 · 夜 · 雨】\n周叙从门廊走出，两人隔着雨幕对视。\n周叙：你收到信了。\n（停顿）\n（旁白）有些话，一开口就成了告别。"
    };
    return {
      id:"p-demo", name:"雾港来信", season:1, episode:1, episodeTitle:"雨夜的信",
      genre:"都市悬疑", style:"精致国漫", ratio:"9:16 竖屏", eps:12, duration:90,
      budget:3000, res:"1080 × 1920 / 24fps", archived:false, createdAt:"11-09 10:00",
      script:{
        layers:A.clone(layers), dirtySinceFreeze:false, frozenVersionId:"v3",
        versions:[
          {id:"v3", label:"v3 · 制作基准", layers:A.clone(layers), createdAt:"11-09 18:40", note:"冻结"},
          {id:"v2", label:"v2 · 归档",     layers:A.clone(layers), createdAt:"11-09 17:02", note:"第二稿"},
          {id:"v1", label:"v1 · 归档",     layers:A.clone(layers), createdAt:"11-09 15:20", note:"初稿"}
        ]
      },
      assets:{
        characters:[
          {id:"a1", name:"林砚",     sub:"女主",       seed:3,  voice:"声线 A · 清冷女中音", note:"深灰风衣 / 齐肩短发 / 左眉小疤", prompt:"女性 深灰风衣 短发 冷调", locked:true},
          {id:"a2", name:"周叙",     sub:"男主",       seed:7,  voice:"声线 B · 低沉男声",   note:"藏青大衣 / 金丝眼镜 / 身形偏高", prompt:"男性 藏青大衣 眼镜 冷调", locked:true},
          {id:"a3", name:"房东太太", sub:"配角 · 草稿", seed:11, voice:"未绑定",              note:"待补充三视图",                   prompt:"", locked:false}
        ],
        scenes:[
          {id:"s1", name:"雾港码头", sub:"主场景",   seed:2, voice:"环境音：雨 / 海浪", note:"冷蓝调 / 夜 / 湿石阶",       prompt:"码头 雨夜 冷蓝 灯塔", locked:true},
          {id:"s2", name:"街角邮局", sub:"主场景",   seed:5, voice:"环境音：雨 / 门铃", note:"暖黄门廊 / 夜 / 玻璃雾气",   prompt:"邮局 门廊 暖黄 雨夜", locked:true},
          {id:"s3", name:"旧公寓",   sub:"草稿",     seed:9, voice:"环境音：室内",     note:"待补场景四视图",             prompt:"", locked:false}
        ],
        props:[
          {id:"d1", name:"旧信封", sub:"关键道具", seed:13, voice:"—", note:"牛皮纸 / 蓝黑墨水 / 边角磨损", prompt:"信封 牛皮纸 特写", locked:true},
          {id:"d2", name:"黑伞",   sub:"草稿",     seed:17, voice:"—", note:"待补多角度",                 prompt:"", locked:false}
        ]
      },
      shots:shots,
      frames:{},                 /* shotId -> [seed,...] */
      timeline:{order:shots.map(function(s){return s.id;}), notes:[]},
      audio:{
        voices:[
          {id:"v1", character:"林砚", voice:"声线 A · 清冷女中音", lines:2, duration:"6.4s"},
          {id:"v2", character:"周叙", voice:"声线 B · 低沉男声",   lines:1, duration:"3.1s"},
          {id:"v3", character:"旁白", voice:"声线 C · 中性旁白",   lines:2, duration:"7.8s"}
        ],
        subtitles:[
          {id:"t1", in:"00:00", out:"00:03", text:"那封信，比船晚到了十一年。", checked:true},
          {id:"t2", in:"00:03", out:"00:06", text:"我还是来了。",               checked:true},
          {id:"t3", in:"00:08", out:"00:12", text:"你收到信了。",               checked:false},
          {id:"t4", in:"00:16", out:"00:19", text:"有些话，一开口就成了告别。", checked:false}
        ],
        bgm:{name:"雨夜氛围", voiceLevel:100, bgmLevel:25, ambient:"雨 / 海浪"}
      },
      qa:[false,false,false,false],
      exports:[]
    };
  }

  A.defaultDB = function(){
    return {
      version:1, appVersion:A.VERSION,
      activeProjectId:"p-demo",
      datasource:"offline",       /* offline | server */
      server:{base:"", reachable:false, checkedAt:null, config:null, error:null,
              reconcile:null, reconcileAt:null},
      pricing:A.clone(A.DEFAULT_PRICING),
      projects:[seedProject()],
      tasks:[],
      media:[],
      ledger:{
        balance:980, frozen:0,
        /* seed 流水是初始账面的一部分，守恒自检不重复计入 */
        tx:[
          {id:"x1", seed:true, time:"11-09 10:12", kind:"credit",  label:"开户赠送",         amount:1000, note:""},
          {id:"x2", seed:true, time:"11-09 10:24", kind:"reserve", label:"单图 t2i 预占",    amount:-120, note:"余额转冻结 120（estimate 100 / cap 120）"},
          {id:"x3", seed:true, time:"11-09 10:24", kind:"settle",  label:"单图 t2i 结算",    amount:0,    note:"冻结内核销 80，余额不变"},
          {id:"x4", seed:true, time:"11-09 10:24", kind:"refund",  label:"结算退差",         amount:40,   note:"120 - 80 退回余额"},
          {id:"x5", seed:true, time:"11-09 10:31", kind:"release", label:"生成失败释放预占", amount:60,   note:"上游明确拒绝，未执行"}
        ]
      },
      ui:{page:"overview", step:1, assetTab:"characters", selectedShot:"S1", taskFilter:"all", navClosed:false}
    };
  };

  A.DB = null;

  A.load = function(){
    try {
      var raw = window.localStorage ? window.localStorage.getItem(A.STORE_KEY) : null;
      if(raw){
        var parsed = JSON.parse(raw);
        if(parsed && parsed.version === 1 && Array.isArray(parsed.projects)){ A.DB = parsed; }
      }
    } catch(e){ /* file:// 或隐私模式下 localStorage 不可用，回退内存态 */ }
    if(!A.DB) A.DB = A.defaultDB();
    if(!A.DB.ui) A.DB.ui = A.defaultDB().ui;
    if(!A.DB.server) A.DB.server = A.defaultDB().server;
    if(!A.DB.pricing) A.DB.pricing = A.clone(A.DEFAULT_PRICING);
    if(!Array.isArray(A.DB.projects) || !A.DB.projects.length) A.DB.projects = [seedProject()];
    if(!A.DB.projects.some(function(p){ return p.id === A.DB.activeProjectId; })){
      A.DB.activeProjectId = A.DB.projects[0].id;
    }
    return A.DB;
  };
  A.save = function(){
    try { if(window.localStorage) window.localStorage.setItem(A.STORE_KEY, JSON.stringify(A.DB)); }
    catch(e){ /* 内存态仍可继续运行 */ }
  };
  A.reset = function(keepDatasource){
    var ds = keepDatasource ? A.DB.datasource : "offline";
    var fresh = A.defaultDB();
    fresh.datasource = ds;
    A.DB = fresh; A.save();
  };
  A.exportJSON = function(){ return JSON.stringify(A.DB, null, 2); };
  A.importJSON = function(text){
    var obj = JSON.parse(text);
    if(!obj || obj.version !== 1 || !Array.isArray(obj.projects) || !obj.ledger){
      throw new Error("数据结构不匹配：需要 version=1、projects[]、ledger");
    }
    A.DB = obj;
    if(!A.DB.ui) A.DB.ui = A.defaultDB().ui;
    A.save();
  };

  A.project = function(id){
    var pid = id || A.DB.activeProjectId;
    for(var i=0;i<A.DB.projects.length;i++){ if(A.DB.projects[i].id === pid) return A.DB.projects[i]; }
    return pid ? null : (A.DB.projects[0] || null);
  };
  A.activeProjects = function(){ return A.DB.projects.filter(function(p){ return !p.archived; }); };
  A.shotById = function(p, id){
    for(var i=0;i<p.shots.length;i++){ if(p.shots[i].id === id) return p.shots[i]; }
    return null;
  };
  A.assetByType = function(p, type){ return p.assets[type] || []; };
  A.assetLabel = function(type){
    return type === "characters" ? "@角色" : type === "scenes" ? "@场景" : "@道具";
  };
  A.refCount = function(p, type, name){
    var token = A.assetLabel(type) + ":" + name;
    return p.shots.filter(function(s){ return s.refs.indexOf(token) >= 0; }).length;
  };
  A.addTx = function(kind, label, amount, note, meta){
    var tx = {id:A.uid("tx"), time:A.now(), kind:kind, label:label, amount:amount,
              note:note || "", projectId:(meta&&meta.projectId)||null, taskId:(meta&&meta.taskId)||null};
    A.DB.ledger.tx.unshift(tx);
    return tx;
  };

  /* ---------------- 业务动作 ---------------- */
  /* 本地实现（离线降级用）。在线模式下同名方法会被下面的 Proxy 代理到后端。 */
  var localActs = {};

  localActs.createProject = function(f){
    var name = String(f.name || "").trim();
    if(!name) throw new Error("项目名称不能为空");
    var p = A.clone(seedProject());
    p.id = A.uid("p"); p.name = name;
    p.season = Number(f.season) || 1; p.episode = Number(f.episode) || 1;
    p.episodeTitle = String(f.episodeTitle || "未命名").trim();
    p.genre = f.genre || p.genre; p.style = f.style || p.style;
    p.ratio = f.ratio || p.ratio; p.eps = Number(f.eps) || 12;
    p.duration = Number(f.duration) || 90; p.budget = Number(f.budget) || 3000;
    p.createdAt = A.now();
    /* 复制项目时示例内容保留为可编辑起点；新建项目给空骨架更贴近真实空态 */
    p.script.versions = [{id:"v1", label:"v1 · 草稿", layers:A.clone(p.script.layers), createdAt:A.now(), note:"新建"}];
    p.script.frozenVersionId = null; p.script.dirtySinceFreeze = false;
    p.shots = []; p.frames = {}; p.timeline = {order:[], notes:[]};
    p.assets = {characters:[], scenes:[], props:[]};
    p.audio = {voices:[], subtitles:[], bgm:{name:"未设置", voiceLevel:100, bgmLevel:25, ambient:"无"}};
    p.qa = [false,false,false,false]; p.exports = [];
    A.DB.projects.push(p); A.DB.activeProjectId = p.id;
    A.DB.ui.selectedShot = null;
    A.save();
    return p;
  };
  /* 复制项目必须重生成所有子对象 id：在线态子对象 id 是 SQLite 全局主键，
     直接克隆会撞主键；同时同步 frozenVersionId / timeline.order / frames。 */
  localActs.copyProject = function(id){
    var src = A.project(id); if(!src) throw new Error("项目不存在");
    var p = A.clone(src);
    p.id = A.uid("p"); p.name = src.name + " · 副本"; p.createdAt = A.now();
    p.archived = false;
    var vmap = {}, smap = {}, i, old, fresh;
    (p.script.versions || []).forEach(function(v){
      old = v.id; fresh = A.uid("v"); vmap[old] = fresh; v.id = fresh;
    });
    if(p.script.frozenVersionId && vmap[p.script.frozenVersionId])
      p.script.frozenVersionId = vmap[p.script.frozenVersionId];
    (p.shots || []).forEach(function(s){
      old = s.id; fresh = A.uid("S"); smap[old] = fresh; s.id = fresh;
    });
    if(p.timeline && p.timeline.order)
      p.timeline.order = p.timeline.order.map(function(x){ return smap[x] || x; });
    if(p.timeline && p.timeline.notes) p.timeline.notes.forEach(function(n){
      if(n && n.id) n.id = A.uid("n");
    });
    var frames = {};
    Object.keys(p.frames || {}).forEach(function(k){ frames[smap[k] || k] = p.frames[k]; });
    p.frames = frames;
    Object.keys(p.assets || {}).forEach(function(t){
      (p.assets[t] || []).forEach(function(a){ a.id = A.uid("a"); });
    });
    (p.exports || []).forEach(function(e){ e.id = A.uid("ex"); });
    ["voices","subtitles"].forEach(function(k){
      ((p.audio || {})[k] || []).forEach(function(x){ if(x.id) x.id = A.uid("t"); });
    });
    A.DB.projects.push(p); A.DB.activeProjectId = p.id; A.save();
    return p;
  };
  localActs.archiveProject = function(id){
    var p = A.project(id); if(!p) return;
    if(A.DB.projects.filter(function(x){return !x.archived;}).length <= 1) throw new Error("至少保留一个未归档项目");
    p.archived = true;
    if(A.DB.activeProjectId === id){
      var next = A.activeProjects()[0]; A.DB.activeProjectId = next ? next.id : id;
    }
    A.save();
  };
  localActs.unarchiveProject = function(id){
    var p = A.project(id); if(!p) throw new Error("项目不存在");
    p.archived = false; A.save();
  };
  localActs.deleteProject = function(id){
    if(A.DB.projects.length <= 1) throw new Error("至少保留一个项目");
    A.DB.projects = A.DB.projects.filter(function(p){ return p.id !== id; });
    A.DB.tasks = A.DB.tasks.filter(function(t){ return t.projectId !== id; });
    if(A.DB.activeProjectId === id) A.DB.activeProjectId = A.DB.projects[0].id;
    A.save();
  };
  localActs.switchProject = function(id){ A.DB.activeProjectId = id; A.DB.ui.selectedShot = null; A.save(); };
  localActs.saveSettings = function(f){
    var p = A.project();
    /* 判「是否需要把已锁定分镜标脏」只看本次**真的带了的**两项：只改字幕/分辨率时
       style/ratio 缺键，按旧写法会拿 undefined 去比、恒判变化，把纯交付格式的改动
       说成「画面变了」并顺手标脏全部分镜。页面总是整份提交，既有行为不变。 */
    var changedRatio = (f.style !== undefined && p.style !== f.style)
                    || (f.ratio !== undefined && p.ratio !== f.ratio);
    p.name = String(f.name||p.name).trim() || p.name;
    /* 缺键 = 别动它；显式传空串才是用户的真实改动。 */
    if(f.genre !== undefined) p.genre = f.genre;
    if(f.style !== undefined) p.style = f.style;
    if(f.ratio !== undefined) p.ratio = f.ratio;
    p.eps = Math.max(1, Number(f.eps)||1);
    p.duration = Math.max(1, Number(f.duration)||1);
    p.budget = Math.max(1, Number(f.budget)||1);
    if(f.res !== undefined) p.res = f.res;
    if(f.episodeTitle !== undefined) p.episodeTitle = f.episodeTitle;
    /* 缺键（undefined）时保持原值，空串才回落默认 —— 这两种情况语义不同：
       「表单里没这个字段」不该把用户选过的 burn 悄悄改回 soft。 */
    if(f.subtitleMode !== undefined) p.subtitleMode = pickEnum(f.subtitleMode, SUBTITLE_MODES, "soft");
    if(f.outputResolution !== undefined) p.outputResolution = pickEnum(f.outputResolution, OUTPUT_RESOLUTIONS, "source");

    if(changedRatio){
      p.shots.forEach(function(s){ if(s.state === "locked") { s.state = "dirty"; s.dirty = true; } });
    }
    A.save();
    return changedRatio;
  };
  localActs.restoreDefaults = function(){
    var p = A.project(), d = A.DEFAULT_SETTINGS;
    /* 复用 saveSettings 的校验与「改画风/比例标脏已锁定分镜」语义，保持单一实现。
       恢复默认要连交付格式一起复位，否则「恢复默认」之后成片还烧着字幕。 */
    return localActs.saveSettings({
      name:p.name, episodeTitle:p.episodeTitle,
      genre:d.genre, style:d.style, ratio:d.ratio, eps:d.eps,
      duration:d.duration, budget:d.budget, res:d.res,
      subtitleMode:d.subtitleMode, outputResolution:d.outputResolution
    });
  };

  /* --- 剧本 --- */
  localActs.saveScript = function(f){
    var p = A.project();
    /* 与服务端 saveScript 同口径：缺键 = 别动它，显式传空串才清空。
       少传一层就抹掉它，等于让「只改正文」顺手删掉简介和大纲。 */
    if(f.logline !== undefined) p.script.layers.logline = f.logline || "";
    if(f.outline !== undefined) p.script.layers.outline = f.outline || "";
    if(f.body !== undefined) p.script.layers.body = f.body || "";
    if(p.script.frozenVersionId) p.script.dirtySinceFreeze = true;
    A.save();
  };
  localActs.newScriptVersion = function(){
    var p = A.project(), n = p.script.versions.length + 1;
    var v = {id:A.uid("v"), label:"v"+n+" · 草稿", layers:A.clone(p.script.layers), createdAt:A.now(), note:"手动保存"};
    p.script.versions.unshift(v); A.save(); return v;
  };
  localActs.freezeScript = function(versionId){
    var p = A.project();
    if(!p.script.layers.body.trim()) throw new Error("剧本正文为空，无法定稿");
    var v = versionId ? p.script.versions.filter(function(x){return x.id===versionId;})[0] : null;
    if(!v) v = localActs.newScriptVersion();
    p.script.frozenVersionId = v.id;
    p.script.dirtySinceFreeze = false;
    v.label = v.label.replace("草稿", "制作基准"); v.note = "冻结";
    A.save(); return v;
  };
  localActs.rollbackScript = function(versionId){
    var p = A.project();
    var v = p.script.versions.filter(function(x){return x.id===versionId;})[0];
    if(!v) throw new Error("版本不存在");
    p.script.layers = A.clone(v.layers);
    if(p.script.frozenVersionId && p.script.frozenVersionId !== versionId) p.script.dirtySinceFreeze = true;
    A.save();
  };
  localActs.markDirtyByScript = function(){
    var p = A.project();
    p.shots.forEach(function(s){ if(s.state === "locked"){ s.state = "dirty"; s.dirty = true; } });
    A.save();
  };

  /* --- 资产 --- */
  localActs.addAsset = function(type, f){
    var p = A.project(), name = String(f.name||"").trim();
    if(!name) throw new Error("名称不能为空");
    var list = A.assetByType(p, type);
    if(list.some(function(a){return a.name===name;})) throw new Error("同名资产已存在");
    var item = {id:A.uid("a"), name:name, sub:f.sub||"草稿", seed:Math.floor(Math.random()*60)+1,
                voice:f.voice||"—", note:f.note||"", prompt:f.prompt||"", locked:false};
    list.push(item); A.save(); return item;
  };
  localActs.updateAsset = function(type, id, f){
    var p = A.project(), list = A.assetByType(p, type);
    var item = list.filter(function(a){return a.id===id;})[0];
    if(!item) throw new Error("资产不存在");
    var oldName = item.name;
    if(f.name && f.name.trim() && f.name !== oldName){
      if(list.some(function(a){return a.name===f.name && a.id!==id;})) throw new Error("同名资产已存在");
      var oldToken = A.assetLabel(type)+":"+oldName, newToken = A.assetLabel(type)+":"+f.name;
      p.shots.forEach(function(s){ s.refs = s.refs.map(function(r){ return r===oldToken ? newToken : r; }); });
      item.name = f.name.trim();
    }
    ["sub","voice","note","prompt"].forEach(function(k){ if(f[k] != null) item[k] = f[k]; });
    /* 改锚点只标脏引用镜头，不自动重跑 */
    p.shots.forEach(function(s){
      if(s.refs.indexOf(A.assetLabel(type)+":"+item.name) >= 0 && s.state === "locked"){
        s.state = "dirty"; s.dirty = true;
      }
    });
    A.save();
  };
  localActs.deleteAsset = function(type, id){
    var p = A.project(), list = A.assetByType(p, type);
    var item = list.filter(function(a){return a.id===id;})[0];
    if(!item) return;
    var token = A.assetLabel(type)+":"+item.name;
    var used = p.shots.filter(function(s){return s.refs.indexOf(token)>=0;}).length;
    if(used) throw new Error("该资产被 "+used+" 个镜头引用，请先移除引用");
    p.assets[type] = list.filter(function(a){return a.id!==id;});
    A.save();
  };
  localActs.toggleAssetLock = function(type, id){
    var p = A.project();
    var item = A.assetByType(p, type).filter(function(a){return a.id===id;})[0];
    if(!item) return;
    if(!item.locked && !item.prompt.trim()) throw new Error("请先填写形象描述");
    item.locked = !item.locked;
    item.sub = item.locked ? "已锁定锚点" : "草稿";
    A.save();
  };
  localActs.generateAssetCandidates = function(type, id){
    var p = A.project();
    var item = A.assetByType(p, type).filter(function(a){return a.id===id;})[0];
    if(!item) return;
    item.seed = Math.floor(Math.random()*60)+1;
    A.save();
  };

  /* --- 分镜 --- */
  localActs.addShot = function(f){
    var p = A.project();
    var shot = {id:A.uid("S"), dur:Math.max(1, Number(f.dur)||3), size:f.size||"中景",
      desc:f.desc||"待补充画面描述", prompt:f.prompt||"", line:f.line||"", move:f.move||"固定镜头",
      refs:(f.refs||"").split(/[,，\s]+/).filter(Boolean), state:"draft", dirty:false};
    p.shots.push(shot); p.timeline.order.push(shot.id); A.save(); return shot;
  };
  /* 「在此镜前插入」：与「新增镜头」（追加到末尾）语义不同，保留原型里的插入位置。 */
  localActs.insertShotBefore = function(id){
    var p = A.project();
    var i = p.shots.map(function(s){return s.id;}).indexOf(id);
    if(i < 0) throw new Error("镜头不存在");
    var shot = {id:A.uid("S"), dur:3, size:"中景", desc:"待补充画面描述", prompt:"",
      line:"", move:"固定镜头", refs:[], state:"draft", dirty:false};
    p.shots.splice(i, 0, shot);
    p.timeline.order = p.shots.map(function(s){return s.id;});
    A.DB.ui.selectedShot = shot.id;
    A.save();
    return shot;
  };
  localActs.updateShot = function(id, f){
    var p = A.project(), s = A.shotById(p, id);
    if(!s) return;
    s.dur = Math.max(1, Number(f.dur)||s.dur);
    s.size = f.size || s.size; s.desc = f.desc || s.desc; s.prompt = f.prompt || "";
    s.line = f.line || ""; s.move = f.move || s.move;
    s.refs = String(f.refs||"").split(/[,，\s]+/).filter(Boolean);
    A.save();
  };
  localActs.deleteShot = function(id){
    var p = A.project();
    p.shots = p.shots.filter(function(s){return s.id!==id;});
    p.timeline.order = p.timeline.order.filter(function(x){return x!==id;});
    delete p.frames[id];
    if(A.DB.ui.selectedShot === id) A.DB.ui.selectedShot = p.shots[0] ? p.shots[0].id : null;
    A.save();
  };
  localActs.moveShot = function(id, dir){
    var p = A.project(), arr = p.shots;
    var i = arr.map(function(s){return s.id;}).indexOf(id);
    var j = i + dir;
    if(i < 0 || j < 0 || j >= arr.length) return;
    var tmp = arr[i]; arr[i] = arr[j]; arr[j] = tmp;
    p.timeline.order = arr.map(function(s){return s.id;});
    A.save();
  };
  localActs.toggleShotLock = function(id){
    var p = A.project(), s = A.shotById(p, id);
    if(!s) return;
    if(s.state === "locked"){ s.state = "draft"; s.dirty = false; }
    else { s.state = "locked"; s.dirty = false; }
    A.save();
  };
  localActs.resplitShots = function(){
    var p = A.project();
    if(!p.script.layers.body.trim()) throw new Error("剧本正文为空，无法拆分分镜");
    p.shots.forEach(function(s){ if(s.state === "locked"){ s.state = "dirty"; s.dirty = true; } });
    A.save();
  };
  A.continuityWarnings = function(p){
    var out = [], run = 1;
    for(var i=1;i<p.shots.length;i++){
      if(p.shots[i].size === p.shots[i-1].size){ run++; if(run >= 3) out.push("镜头 "+(i+1)+"：连续 3 个同景别（"+p.shots[i].size+"）"); }
      else run = 1;
    }
    return out;
  };

  /* --- 生成与账本 --- */
  /* candidates 是「每镜几张候选」，1–4，省略等同 1。服务端按张线性放大预占与成本
     （上游按张计费），本地必须同一口径，否则离线态按 1 张预占、在线态按 N 张扣费，
     切模式时余额会跳变。非法值一律回落 1，不抛错：这是页面下拉框的值，
     用户把它改坏不该让整个提交失败。 */
  function candidateCount(raw){
    var n = Math.floor(Number(raw));
    return (n >= 1 && n <= 4) ? n : 1;
  }
  localActs.submitTasks = function(shotIds, candidates){
    var p = A.project(), pricing = A.DB.pricing;
    var per = candidateCount(candidates);
    var perReserve = pricing.cap * per, perCost = pricing.t2i * per;
    var pending = (shotIds && shotIds.length ? shotIds : p.shots.filter(function(s){return s.state==="locked"||s.state==="dirty";}).map(function(s){return s.id;}))
      /* 只按同类任务判重（与服务端 submitTasks 同口径）：一条已结算的视频任务
         不该把这个镜头的关键帧提交永久挡死。老任务行没有 kind，按关键帧算。 */
      .filter(function(id){ return !A.DB.tasks.some(function(t){return t.projectId===p.id && t.shotId===id && t.kind!=="video" && t.state!=="failed" && t.state!=="released" && t.state!=="cancelled";}); });
    if(!pending.length) throw new Error("没有可生成的镜头，请先锁定分镜");
    var reserve = pending.length * perReserve;
    if(A.DB.ledger.balance < reserve) throw new Error("积分不足，请先充值");
    A.DB.ledger.balance -= reserve;
    A.DB.ledger.frozen += reserve;
    A.addTx("reserve", "批量关键帧预占", -reserve,
            pending.length + " 镜 × " + perReserve + " 积分"
              + (per > 1 ? "（每镜 " + per + " 张候选）" : ""), {projectId:p.id});
    pending.forEach(function(sid){
      A.DB.tasks.push({
        id:A.uid("T"), projectId:p.id, shotId:sid, type:"关键帧 t2i",
        state:"queued", percent:0, reserve:perReserve, cost:perCost, candidates:per,
        settled:false, picked:null, frames:[], idem:A.uid("IK"), chargeId:A.uid("C"),
        upstreamTaskId:null,
        note:"预占 " + perReserve + " / 预计实际 " + perCost
          + (per > 1 ? "；每镜 " + per + " 张候选" : ""),
        createdAt:A.now()
      });
    });
    A.save();
    return pending.length;
  };
  /* 批量图生视频（离线实现）。准入与服务端 submitVideoTasks 完全同口径：
     镜头必须已有关键帧、且没有在途的视频任务；不满足的**不静默丢**，
     逐镜回一条 skipped，由提示如实分层告诉用户「哪几镜没进队列、为什么」。
     离线态关键帧存在 p.frames[shotId] 里（本地 tick 的产物），不是 imageUrl。 */
  localActs.submitVideoTasks = function(ids){
    var p = A.project();
    var list = (ids && ids.length) ? ids.map(String) : p.shots.map(function(s){ return s.id; });
    var skipped = [], todo = [], missing = [];
    list.forEach(function(sid){
      var s = A.shotById(p, sid);
      if(!s){ missing.push(sid); return; }
      var hasKey = !!s.imageUrl || ((p.frames && p.frames[sid]) || []).length > 0;
      if(!hasKey){ skipped.push({shotId:sid, reason:"no_keyframe"}); return; }
      var busy = A.DB.tasks.some(function(t){
        return t.projectId === p.id && t.shotId === sid && t.kind === "video"
          && ["failed","released","cancelled"].indexOf(t.state) < 0;
      });
      if(busy){ skipped.push({shotId:sid, reason:"busy"}); return; }
      todo.push(sid);
    });
    if(missing.length) throw new Error("镜头不存在，可能已被删除，请刷新后重试");
    if(!todo.length) return {queued:0, reserve:0, skipped:skipped};
    var price = Number(A.DB.pricing && A.DB.pricing.video) || 0;
    var cap = price > 0 ? Math.ceil(price * 12 / 10) : 0;
    var reserve = cap * todo.length;
    if(reserve > 0 && A.DB.ledger.balance < reserve) throw new Error("积分不足，请先充值");
    if(reserve > 0){
      A.DB.ledger.balance -= reserve;
      A.DB.ledger.frozen += reserve;
      A.addTx("reserve", "批量视频预占", -reserve, todo.length + " 镜 × " + cap + " 积分", {projectId:p.id});
    }
    todo.forEach(function(sid){
      A.DB.tasks.push({
        id:A.uid("T"), projectId:p.id, shotId:sid, type:"视频 i2v", kind:"video",
        state:"queued", percent:0, reserve:cap, cost:price, candidates:1, settled:false,
        picked:null, frames:[], idem:A.uid("IK"), chargeId:A.uid("C"),
        upstreamTaskId:null,
        note:price > 0
          ? "预占 " + cap + " / 预计实际 " + price
          : "离线演示：未配置视频价格，不产生扣费",
        createdAt:A.now()
      });
    });
    A.save();
    return {queued:todo.length, reserve:reserve, skipped:skipped};
  };
  /* 产物回退（离线实现）。只能选「这一镜历史里出现过的地址」——不做这道校验，
     就等于开了一个把镜头换成任意地址的写口子。恢复本身也记一条历史，
     否则「再切回来」在列表里看不见，等于不可撤销。 */
  localActs.restoreShotMedia = function(shotId, url){
    var p = A.project(), s = A.shotById(p, shotId);
    if(!s) throw new Error("镜头不存在，可能已被删除，请刷新后重试");
    var target = String(url || "").trim();
    var hist = (s.mediaHistory || []).filter(function(x){ return x && typeof x === "object"; });
    var entry = null;
    for(var i=0;i<hist.length;i++){
      if(String(hist[i].url || "") === target){ entry = hist[i]; break; }
    }
    if(!target || !entry) throw new Error("这版产物不在该镜头的历史里，请刷新后重试");
    var kind = entry.kind === "video" ? "video" : "image";
    if(kind === "video") s.videoUrl = target; else s.imageUrl = target;
    s.mediaHistory = [{url:target, kind:kind, at:A.now()}].concat(hist).slice(0, 10);
    A.save();
    return {shotId:s.id, url:target, kind:kind};
  };
  /* 剧本版本对比（离线实现）。服务端实现在 comicdrama/script_diff.py，这里是
     同结构的一份简化版：字段顺序、行号口径（从 1 起，该侧没有这一行时是 null）、
     hunk 合并规则（两块之间相同行不超过 2×context 就并成一块）都对齐。
     离线态没有服务端可问，但「对比」按钮不能是摆设。 */
  var DIFF_FIELDS = ["logline","outline","body"];
  var DIFF_CONTEXT = 2;
  var DIFF_MAX_LINES = 600;      /* 离线不做 2000 行的全量 DP，超长按截断处理 */
  var DIFF_MAX_HUNKS = 200;

  function diffLines(text){
    if(typeof text !== "string" || !text) return [];
    /* \r\n 与 \r 不算变更（编辑器差异），行尾空格算变更（用户的真实改动）。 */
    var raw = text.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
    if(raw.length && raw[raw.length-1] === "") raw.pop();
    return raw;
  }
  /* 后缀 LCS 表：dp[i][j] = base[i..] 与 target[j..] 的最长公共子序列长度。
     剧本文本有界，O(n×m) 足够；超长时调用方先截断并标 truncated。 */
  function diffRows(base, target){
    var n = base.length, m = target.length, i, j;
    var dp = [];
    for(i=0;i<=n;i++){ dp.push([]); for(j=0;j<=m;j++) dp[i][j] = 0; }
    for(i=n-1;i>=0;i--){
      for(j=m-1;j>=0;j--){
        dp[i][j] = base[i] === target[j] ? dp[i+1][j+1] + 1 : Math.max(dp[i+1][j], dp[i][j+1]);
      }
    }
    var rows = [];
    i = 0; j = 0;
    while(i < n && j < m){
      if(base[i] === target[j]){
        rows.push({kind:"same", line:base[i], baseLine:i+1, targetLine:j+1}); i++; j++;
      } else if(dp[i+1][j] >= dp[i][j+1]){
        rows.push({kind:"remove", line:base[i], baseLine:i+1, targetLine:null}); i++;
      } else {
        rows.push({kind:"add", line:target[j], baseLine:null, targetLine:j+1}); j++;
      }
    }
    while(i < n){ rows.push({kind:"remove", line:base[i], baseLine:i+1, targetLine:null}); i++; }
    while(j < m){ rows.push({kind:"add", line:target[j], baseLine:null, targetLine:j+1}); j++; }
    return rows;
  }
  function pickHunks(rows, context){
    var changed = [], i;
    for(i=0;i<rows.length;i++){ if(rows[i].kind !== "same") changed.push(i); }
    if(!changed.length) return [];
    var groups = [[changed[0], changed[0]]];
    for(i=1;i<changed.length;i++){
      var last = groups[groups.length-1];
      if(changed[i] - last[1] - 1 <= 2*context) last[1] = changed[i];
      else groups.push([changed[i], changed[i]]);
    }
    var out = [];
    groups.forEach(function(g){
      var low = Math.max(0, g[0]-context), high = Math.min(rows.length, g[1]+1+context);
      out = out.concat(rows.slice(low, high));
    });
    return out;
  }
  function layerText(layers, field){
    if(!layers || typeof layers !== "object") return "";
    var v = layers[field];
    return typeof v === "string" ? v : "";
  }
  localActs.diffScriptVersions = function(baseId, targetId){
    var p = A.project();
    var versions = (p.script && p.script.versions) || [];
    function pick(raw){
      var key = String(raw || "").trim();
      /* 空 id = 当前草稿：最常用的一次对比，不该逼用户先存一版。 */
      if(!key) return {label:"当前草稿", layers:(p.script && p.script.layers) || {}};
      for(var i=0;i<versions.length;i++){
        if(versions[i].id === key) return {label:versions[i].label || "", layers:versions[i].layers || {}};
      }
      throw new Error("版本不存在，可能已被删除，请刷新后重试");
    }
    var from = pick(baseId), to = pick(targetId);
    var fields = [], addedTotal = 0, removedTotal = 0;
    DIFF_FIELDS.forEach(function(field){
      var baseAll = diffLines(layerText(from.layers, field));
      var targetAll = diffLines(layerText(to.layers, field));
      var clipped = baseAll.length > DIFF_MAX_LINES || targetAll.length > DIFF_MAX_LINES;
      var rows = diffRows(baseAll.slice(0, DIFF_MAX_LINES), targetAll.slice(0, DIFF_MAX_LINES));
      var added = 0, removed = 0;
      rows.forEach(function(r){ if(r.kind === "add") added++; else if(r.kind === "remove") removed++; });
      var hunks = pickHunks(rows, DIFF_CONTEXT), truncated = false;
      if(clipped || hunks.length > DIFF_MAX_HUNKS){
        hunks = hunks.slice(0, DIFF_MAX_HUNKS); truncated = true;
      }
      addedTotal += added; removedTotal += removed;
      fields.push({field:field, added:added, removed:removed, hunks:hunks, truncated:truncated});
    });
    return {
      fields: fields,
      summary: {added:addedTotal, removed:removedTotal, changed:(addedTotal + removedTotal) > 0},
      labels: {base:from.label, target:to.label}
    };
  };
  localActs.tick = function(){
    var changed = 0;
    A.DB.tasks.forEach(function(t){
      if(t.state === "queued"){ t.state = "running"; t.percent = 6; changed++; }
      else if(t.state === "running"){
        t.percent += 14 + Math.floor(Math.random()*16); changed++;
        if(t.percent >= 100){
          t.percent = 100; t.state = "succeeded"; t.settled = true;
          t.upstreamTaskId = "up-" + t.id.slice(-6);
          if(t.kind === "video"){
            /* 离线不产出视频文件，也不许覆盖这一镜已经选好的关键帧候选位：
               只留一条「本地示意」记录，页面照旧显示占位画面。 */
            pushMedia("image-to-video", "视频生成（本地示意）· " + t.shotId, {video_url:null});
          } else {
            /* 每镜 4 个候选位，多候选时按张数放宽（candidates=1 时与旧行为逐字节相同）。 */
            var slots = 4 * candidateCount(t.candidates);
            t.frames = [];
            for(var k=0;k<slots;k++){ t.frames.push((Number(t.shotId.replace(/\D/g,""))||1)*7 + k + 1); }
            var p = A.project(t.projectId);
            if(p) p.frames[t.shotId] = t.frames.slice();
          }
          A.DB.ledger.frozen = Math.max(0, A.DB.ledger.frozen - t.reserve);
          A.addTx("settle", "镜头结算 · " + t.id.slice(-6), 0,
                  "冻结内核销 " + t.cost + " 积分，余额不变", {projectId:t.projectId, taskId:t.id});
          var diff = t.reserve - t.cost;
          if(diff > 0){
            A.DB.ledger.balance += diff;
            A.addTx("refund", "结算退差 · " + t.id.slice(-6), diff,
                    "预占 " + t.reserve + " - 实际 " + t.cost + " 退回余额", {projectId:t.projectId, taskId:t.id});
          }
        }
      }
    });
    if(changed) A.save();
    return A.DB.tasks.some(function(t){ return t.state === "queued" || t.state === "running"; });
  };
  localActs.injectFault = function(taskId, kind){
    var t = A.DB.tasks.filter(function(x){ return x.id === taskId; })[0];
    if(!t) throw new Error("任务不存在");
    if(t.settled || t.state === "succeeded" || t.state === "released")
      throw new Error("已结算任务不能再注入故障");
    kind = kind || "lost";
    if(kind === "reject"){
      /* 上游明确拒绝：未执行，自动退还预占；failed 状态使「重试同一 task_id」可达。 */
      t.state = "failed"; t.percent = 0;
      t.note = "上游明确拒绝：任务未执行，预占自动退还（同一 task_id 可重试）";
      A.DB.ledger.frozen = Math.max(0, A.DB.ledger.frozen - t.reserve);
      A.DB.ledger.balance += t.reserve;
      A.addTx("release", "明确拒绝退还", t.reserve, "上游明确拒绝、未执行，自动释放预占");
      A.save();
      return;
    }
    if(kind !== "lost") throw new Error("故障类型只能是 lost / reject");
    t.state = "held"; t.percent = 0;
    t.note = "提交后响应丢失：上游可能已执行，禁止自动重提";
    A.save();
  };
  localActs.releaseHeld = function(taskId, reason){
    if(!String(reason||"").trim()) throw new Error("释放必须填写原因");
    var t = A.DB.tasks.filter(function(x){ return x.id === taskId; })[0];
    if(!t) throw new Error("任务不存在");
    if(t.state !== "held") throw new Error("只有 held 任务可以释放");
    t.state = "released";
    t.note = "管理员确认不重提 / 原因：" + reason;
    var reserve = Number(t.reserve) || 0;
    if(reserve > 0){
      A.DB.ledger.frozen = Math.max(0, A.DB.ledger.frozen - reserve);
      A.DB.ledger.balance += reserve;
    }
    A.addTx("release", "held 释放 · " + t.id.slice(-6), reserve,
            "管理员带原因释放 / " + reason, {projectId:t.projectId, taskId:t.id});
    A.save();
  };
  /* 取消本地队列里的任务：只有「还没花出去的钱」能退。
     已 settled / 已成功 / 已释放 / 已取消 一律拒绝，避免一次取消退两次钱。
     与注入故障的区别：injectFault 是模拟上游异常，cancelTask 是用户主动放弃。 */
  localActs.cancelTask = function(taskId, reason){
    var t = A.DB.tasks.filter(function(x){ return x.id === taskId; })[0];
    if(!t) throw new Error("任务不存在");
    if(t.settled || t.state === "succeeded" || t.state === "released" || t.state === "cancelled")
      throw new Error("已结算 / 已释放 / 已取消的任务不能再取消");
    if(["queued","running","draft","quoted"].indexOf(t.state) < 0)
      throw new Error("当前状态 " + t.state + " 不能取消");
    var text = String(reason || "").trim() || "用户主动取消";
    var reserve = Number(t.reserve) || 0;
    t.state = "cancelled"; t.percent = 0;
    t.note = "已取消 / 原因：" + text;
    if(reserve > 0){
      A.DB.ledger.frozen = Math.max(0, A.DB.ledger.frozen - reserve);
      A.DB.ledger.balance += reserve;
    }
    A.addTx("release", "任务取消 · " + t.id.slice(-6), reserve,
            "本地队列取消，预占全额退还 / " + text, {projectId:t.projectId, taskId:t.id});
    A.save();
  };
  /* 素材清理的离线实现。离线态素材只保存 URL 记录、不落磁盘，所以如实回报 0 个文件，
     不假装「清理过了」——报告结构与服务端 /purgeMedia 完全一致，页面不用改。 */
  localActs.purgeMedia = function(days, dryRun){
    var d = (days === null || days === undefined) ? 7 : Number(days);
    if(isNaN(d) || d < 0) throw new Error("days 不能为负");
    return {days:d, dry_run:dryRun !== false, files:0, freed_bytes:0, asset_ids:[],
            forgotten_rows:0,
            storage:{files:0, bytes:0, limit_bytes:0, by_extension:{}},
            note:"离线态素材只保存 URL 记录、不落盘，没有文件可清理"};
  };
  /* 离线态不存在「上游结果未知」：离线本来就不出网、不扣费。
     所以本地没有待复核凭据可复核，这里如实返回 null，而不是造一条假流水。 */
  localActs.resolveCharge = function(){ return null; };
  localActs.retryTask = function(taskId){    var t = A.DB.tasks.filter(function(x){ return x.id === taskId; })[0];
    if(!t) throw new Error("任务不存在");
    if(t.state !== "failed") throw new Error("只有失败的任务可以重试");
    /* failed 时预占已自动退还；重试视为新一次执行，重新占用 cap（沿用同一 task_id）。 */
    var cap = A.DB.pricing.cap;
    if(A.DB.ledger.balance < cap) throw new Error("可用积分不足");
    A.DB.ledger.balance -= cap; A.DB.ledger.frozen += cap;
    A.addTx("reserve", "重试重新预占", -cap, "明确拒绝时已退还预占，重试重新占用 cap",
            {projectId:t.projectId, taskId:t.id});
    t.reserve = cap;
    t.state = "queued"; t.percent = 0;
    t.note = "重试同一 upstream task_id，不重新提交生成；已重新预占 cap";
    A.save();
  };
  localActs.pickFrame = function(taskId, index){
    var t = A.DB.tasks.filter(function(x){ return x.id === taskId; })[0];
    if(!t || !t.frames.length) return;
    t.picked = Math.max(0, Math.min(t.frames.length-1, Number(index)||0));
    A.save();
  };
  localActs.rerollTask = function(taskId){
    /* 重抽 = 新任务、新预占，绝不复用旧 chargeId */
    var t = A.DB.tasks.filter(function(x){ return x.id === taskId; })[0];
    if(!t) throw new Error("任务不存在");
    if(!t.settled) throw new Error("该任务尚未完成");
    var cap = A.DB.pricing.cap;
    if(A.DB.ledger.balance < cap) throw new Error("可用积分不足");
    A.DB.ledger.balance -= cap; A.DB.ledger.frozen += cap;
    A.addTx("reserve", "重抽候选预占", -cap, "新 chargeId，与旧任务无关", {projectId:t.projectId});
    A.DB.tasks.push({
      id:A.uid("T"), projectId:t.projectId, shotId:t.shotId, type:"关键帧 t2i（重抽）",
      state:"queued", percent:0, reserve:cap, cost:A.DB.pricing.t2i, settled:false,
      picked:null, frames:[], idem:A.uid("IK"), chargeId:A.uid("C"),
      upstreamTaskId:null, note:"重抽：新任务新预占", createdAt:A.now()
    });
    A.save();
  };
  /* 离线态只保存设置；真正的拦截在服务端。 */
  localActs.saveCreditLimits = function(opts){
    opts = opts || {};
    function val(v){
      if(v === null || v === undefined || String(v).trim() === "") return null;
      var n = Number(v);
      /* Number("Infinity") 是 Infinity，Math.floor(Infinity) === Infinity 会让旧判断
         误放行；序列化后又变成 null。与服务端 _limit_value 一样先挡非有限值。 */
      if(!Number.isFinite(n) || !(n >= 0) || Math.floor(n) !== n) throw new Error("上限必须是 0 或正整数");
      return n;
    }
    var lim = A.DB.creditLimits || {};
    var single = val(opts.single), total = val(opts.total);
    if(total === null){ lim.used = 0; lim.since = ""; }
    else if(lim.total === null || lim.total === undefined || opts.resetUsed){ lim.used = 0; lim.since = A.now(); }
    lim.single = single; lim.total = total; lim.used = lim.used || 0;
    A.DB.creditLimits = lim; A.save();
    return A.creditView();
  };
  localActs.adminCredit = function(amount, reason, requestId){
    var n = Math.floor(Number(amount));
    if(!(n > 0)) throw new Error("加余额金额必须为正整数");
    if(!String(reason||"").trim()) throw new Error("加余额必须填写原因");
    if(!String(requestId||"").trim()) throw new Error("requestId 不能为空（幂等键）");
    if(A.DB.ledger.tx.some(function(x){ return x.kind === "credit" && x.note.indexOf(requestId) >= 0; })){
      throw new Error("requestId 已存在，重复请求被幂等拒绝");
    }
    A.DB.ledger.balance += n;
    A.addTx("credit", "管理员加余额", n, reason + " / requestId=" + requestId);
    A.save();
  };

  /* --- 音频 / 时间线 / 质检 --- */
  localActs.addVoice = function(f){
    var p = A.project();
    if(!String(f.character||"").trim()) throw new Error("角色不能为空");
    p.audio.voices.push({id:A.uid("vo"), character:f.character.trim(), voice:f.voice||"未绑定",
                         lines:Number(f.lines)||0, duration:f.duration||"—"});
    A.save();
  };
  localActs.deleteVoice = function(id){
    var p = A.project();
    p.audio.voices = p.audio.voices.filter(function(v){return v.id!==id;});
    A.save();
  };
  localActs.addSubtitle = function(f){
    var p = A.project();
    if(!String(f.text||"").trim()) throw new Error("字幕文本不能为空");
    p.audio.subtitles.push({id:A.uid("st"), in:f["in"]||"00:00", out:f.out||"00:03",
                            text:f.text.trim(), checked:false});
    A.save();
  };
  localActs.toggleSubtitle = function(id){
    var p = A.project();
    p.audio.subtitles.forEach(function(s){ if(s.id===id) s.checked = !s.checked; });
    A.save();
  };
  localActs.deleteSubtitle = function(id){
    var p = A.project();
    p.audio.subtitles = p.audio.subtitles.filter(function(s){return s.id!==id;});
    A.save();
  };
  localActs.saveBgm = function(f){
    var p = A.project();
    var prev = p.audio.bgm || {};
    /* url / uploadId 是「已选中的 BGM 文件」，不在混音表单里。
       旧实现每次保存都把它们抹掉，点一次「保存混音」就丢了刚设好的 BGM。 */
    p.audio.bgm = {name:f.name||"未设置", voiceLevel:Math.max(0,Math.min(100,Number(f.voiceLevel)||100)),
                   bgmLevel:Math.max(0,Math.min(100,Number(f.bgmLevel)||25)), ambient:f.ambient||"无",
                   url:f.url||prev.url||"", uploadId:f.uploadId||prev.uploadId||""};
    A.save();
  };
  localActs.moveClip = function(shotId, dir){
    var p = A.project(), order = p.timeline.order;
    var i = order.indexOf(shotId), j = i + dir;
    if(i < 0 || j < 0 || j >= order.length) return;
    var tmp = order[i]; order[i] = order[j]; order[j] = tmp;
    p.shots.sort(function(a,b){ return order.indexOf(a.id) - order.indexOf(b.id); });
    A.save();
  };
  localActs.addNote = function(text){
    var p = A.project();
    if(!String(text||"").trim()) throw new Error("批注不能为空");
    p.timeline.notes.push({id:A.uid("n"), time:A.now(), text:text.trim()});
    A.save();
  };
  localActs.toggleQA = function(i){
    var p = A.project(); p.qa[i] = !p.qa[i]; A.save();
  };
  /* 质检退回：只标脏目标镜头 + 清空质检闸口 + 留一条帧级批注。
     不自动重跑生成、不新建任务、不产生任何账本流水（不扣费也不退款）。 */
  localActs.returnForReshoot = function(shotId, reason){
    var text = String(reason || "").trim();
    if(!text) throw new Error("退回必须填写原因");
    var p = A.project(), s = A.shotById(p, shotId);
    if(!s) throw new Error("镜头不存在");
    if(s.state === "locked") s.state = "dirty";
    s.dirty = true;
    p.qa = [false,false,false,false];
    p.timeline.notes.push({id:A.uid("n"), time:A.now(),
      text:"质检退回局部重拍 · 镜 "+(p.shots.indexOf(s)+1)+" · "+text});
    A.save();
    return s;
  };
  localActs.exportFilm = function(){
    var p = A.project();
    if(!p.qa.every(function(x){return x;})) throw new Error("请先勾选全部 4 项质检");
    var rec = {id:A.uid("ex"), time:A.now(), name:p.name + " · 第 " + p.episode + " 集",
               res:p.res, shots:p.shots.length, duration:p.shots.reduce(function(a,b){return a+b.dur;},0)};
    /* AI 标识与服务端同口径，但离线态只在真有素材时写：离线导出只留一条记录，
       画面仍是占位图，此时打「AI 生成」等于宣称了不存在的生成行为。
       页面读到缺失字段会显「—」，不会编造文案。 */
    var real = p.shots.some(function(s){ return !!(s.videoUrl || s.imageUrl); });
    if(real){ rec.aiLabel = A.aiLabel(); rec.aiDisclosure = A.aiDisclosure(); }
    p.exports.unshift(rec); A.save();
    return rec;
  };

  /* 资金守恒自检（供测试与页面展示） */
  A.conservation = function(){
    /* 与服务端 _assert_conservation 完全同口径。成本有三个来源，漏一个就会
       在界面上算出「凭空少钱/多钱」，误导用户以为账本坏了。 */
    var cost = A.DB.tasks.filter(function(t){return t.settled;})
      .reduce(function(a,t){ return a + t.cost; }, 0);
    cost += (A.DB.charges || []).filter(function(c){ return c.state === "settled"; })
      .reduce(function(a,c){ return a + c.cost; }, 0);
    cost += (A.DB.ledger.retiredCost || 0);
    var credits = A.DB.ledger.tx.filter(function(x){return x.kind==="credit" && !x.seed;})
      .reduce(function(a,x){ return a + x.amount; }, 0);
    return {expected:980 + credits - cost, actual:A.DB.ledger.balance + A.DB.ledger.frozen};
  };

  /* ---------- 服务端 state 覆盖 ---------- */
  A.applyState = function(state){
    if(!state || !state.projects) return;
    var keep = {
      ui: A.DB.ui,
      activeProjectId: A.DB.activeProjectId,
      datasource: A.DB.datasource,
      server: A.DB.server
    };
    A.DB = Object.assign({}, state, keep);
    /* 本地的 activeProjectId 只在服务端也存在时才保留（那是用户刚切过的项目，
       不能被一份较旧的服务端快照覆盖回去）。
       但**首次打开**时本地是离线种子 id（p-demo），服务端种子是 p-demo-<owner>，
       直接保留本地值会让 A.project() 返回 null —— 概览页第一行就炸
       「Cannot read properties of null (reading 'shots')」。所以这里必须回落到
       服务端给的活动项目，再不行才取第一个。 */
    if(!A.DB.projects.some(function(p){ return p.id === A.DB.activeProjectId; })){
      A.DB.activeProjectId = state.activeProjectId
        || (A.DB.projects[0] && A.DB.projects[0].id) || "";
    }
    if(!A.DB.media) A.DB.media = [];
    if(!A.DB.ui) A.DB.ui = A.defaultDB().ui;
    A.save();
    /* 服务端回包已经把整份 state 覆盖进 A.DB：这是最需要通知其它标签页的时刻
       （余额、任务进度、镜头产物都变了）。应用别人的广播时这个调用会被
       A._tabApplying 拦掉，不会形成来回推送。 */
    if(A.broadcastChange) A.broadcastChange();
  };
  A.localTick = function(){ return localActs.tick(); };

  /* ---------- 离线降级用的本地实现 ----------
     同源模式下这些动作由服务端执行完整规则；离线模式只做最小可见效果，
     并明确标注不是模型输出。 */
  function pushMedia(kind, label, payload){
    if(!A.DB.media) A.DB.media = [];
    A.DB.media.unshift({id:A.uid("m"), kind:kind, label:label,
      payload:payload || {}, provider:"mock", createdAt:A.now()});
    A.save();
  }
  var MOCK_LOCAL = {
    "script-doctor": function(){
      var body = A.project().script.layers.body || "";
      var scenes = (body.match(/【场景/g) || []).length;
      var lines = (body.match(/：/g) || []).length;
      var findings = ["识别场景 " + scenes + " 个", "可识别对白 " + lines + " 条",
        "离线降级为简化规则；同源模式下由服务端执行完整检查"];
      pushMedia("script-doctor", "剧本结构检查（本地规则）", {findings:findings});
      return {findings:findings};
    },
    "creative-start": function(kind, text){
      var p = A.project();
      var seed = (text || "").trim() || "一封迟到的信，在雨夜被重新送回";
      p.script.layers.logline = seed + "。主人公必须在一个夜晚做出决定。";
      p.script.layers.outline = "开场：线索出现；推进：人物回到旧地；对峙：两人对话；收束：留一盏灯。";
      p.script.layers.body = "【场景 1 · 旧地 · 夜 · 雨】\n（旁白）" + seed
        + "。\n【场景 2 · 门廊 · 夜 · 雨】\n甲：你还是来了。\n乙：我收到信了。";
      if(p.script.frozenVersionId) p.script.dirtySinceFreeze = true;
      pushMedia("creative-start", "创作起点骨架（本地模板）", {kind:kind || "idea"});
      A.save();
      return {};
    },
    "extract-assets": function(){
      var p = A.project(), body = p.script.layers.body || "", added = 0;
      var roles = [];
      body.split("\n").forEach(function(line){
        var m = line.match(/^([^\s：（）【】]{2,6})：/);
        if(m && roles.indexOf(m[1]) < 0) roles.push(m[1]);
      });
      roles.forEach(function(name, i){
        if(!p.assets.characters.some(function(x){return x.name === name;})){
          p.assets.characters.push({id:A.uid("a"), name:name, sub:"剧本抽取 · 草稿",
            voice:"—", note:"本地正则抽取，需人工确认", prompt:"",
            seed:i * 3 + 5, locked:false});
          added++;
        }
      });
      pushMedia("extract-assets", "剧本实体抽取（本地正则）", {added:added});
      A.save();
      return {added:added};
    },
    "resplit-shots-v2": function(){
      var p = A.project(), body = p.script.layers.body || "";
      if(!body.trim()) throw new Error("剧本正文为空，无法拆分分镜");
      var kept = p.shots.filter(function(x){return x.state === "locked";});
      body.split(/(?=【场景)/).forEach(function(block){
        if(!block.trim()) return;
        var head = block.match(/【场景\s*\d+\s*·\s*([^】]+)】/);
        var place = head ? head[1].trim() : "未标注场景";
        kept.push({id:A.uid("S"), dur:3, size:"中近景",
          desc:place + " · " + block.trim().slice(0, 32), prompt:place + " 冷调 电影感",
          line:"", move:"缓慢横移", refs:[], state:"draft", dirty:false});
      });
      p.shots = kept;
      p.timeline.order = kept.map(function(x){return x.id;});
      pushMedia("resplit-shots", "分镜草稿（本地按场景拆分）", {count:kept.length});
      A.save();
      return {count:kept.length};
    },
    "tts-preview": function(id){
      var v = A.project().audio.voices.filter(function(x){return x.id === id;})[0];
      pushMedia("tts-preview", "试听占位 · " + (v ? v.character : ""),
        {audio_url:null, note:"未生成音频文件"});
      return {};
    },
    "change-voice": function(id){
      var p = A.project();
      var v = p.audio.voices.filter(function(x){return x.id === id;})[0];
      if(v){
        var options = ["声线 A · 清冷女中音","声线 B · 低沉男声","声线 C · 中性旁白",
                       "声线 D · 少年感","声线 E · 温润女声"];
        var next = options.filter(function(x){return x !== v.voice;})[0];
        v.voice = next || options[0];
        A.save();
      }
      pushMedia("change-voice", "音色候选 · " + (v ? v.character : ""), {});
      return {};
    },
    "mark-audio-done": function(){
      A.project().audio.done = true;
      pushMedia("mark-audio-done", "配音与字幕配置完成（本地状态）", {audio_files:0});
      A.save();
      return {};
    },
    "preview-film": function(){
      var p = A.project();
      var total = p.shots.reduce(function(a,b){return a + b.dur;},0);
      pushMedia("preview-film", "成片结构预览（本地编排示意）",
        {clips:p.shots.length, duration_seconds:total, video_url:null});
      return {};
    },
    "image-to-video": function(id){
      pushMedia("image-to-video", "图生视频占位 · " + (id || ""), {video_url:null});
      return {};
    }
  };
  Object.keys(MOCK_LOCAL).forEach(function(k){ localActs[k] = MOCK_LOCAL[k]; });

  localActs.savePricing = function(f){
    var t2i = Math.max(0, Number(f.t2i) || 0);
    /* cap 下界与服务端一致：cap >= ceil(t2i × 1.2)，不只是 cap >= t2i。
       1.0 倍售价时退差为 0/负数，结算会当场失败——离线态也必须同样拒绝，
       否则离线能保存、在线保存 400，切换模式后账本立刻不一致。 */
    var capOf = function(price){ return Math.ceil(price * 12 / 10); };
    /* 未传 cap → 跟随售价；显式传 → 必须 >= 硬上限，否则拒绝（与服务端同判据）。
       不要用 Math.max 静默抬高：服务端是 400，离线静默通过会让两种模式分歧。 */
    var cap = (f.cap === "" || f.cap == null) ? capOf(t2i) : Math.max(1, Number(f.cap) || 1);
    if(cap < capOf(t2i)) throw new Error("cap 必须 ≥ 单次硬上限 " + capOf(t2i) + "（售价 " + t2i + " × 1.2 取整）");
    /* video 是管理员可维护的图生视频售价：未传就保持现值，显式空值才清空。
       旧实现硬写 null，管理员改完价一切换页面就被抹掉。 */
    var previous = (A.DB.pricing && A.DB.pricing.video) || null;
    var video;
    if(Object.prototype.hasOwnProperty.call(f, "video") && f.video !== "" && f.video != null){
      video = Math.max(1, Number(f.video) || 0) || null;
    } else if(Object.prototype.hasOwnProperty.call(f, "video")){
      video = null;
    } else {
      video = previous;
    }
    A.DB.pricing = {t2i:t2i, cap:cap, qps:Math.max(0.1, Number(f.qps) || 1),
                    concurrency:Math.max(1, Number(f.concurrency) || 5), video:video};
    A.save();
  };

  /* ============ 在线态：服务端为真源 ============
     两类动作走两条不同的路，原因是它们的失败代价完全不同：

     1) 生成动作（A.GEN_ACTS）—— 可能出网、可能真扣费。
        * **本地不预执行**：本地占位结果会盖住服务端真实结果，还会让用户以为
          「已经生成了」。
        * **同一动作不并发**：连点两次就是两次真实调用。这里用 pending 拦截并
          返回被拒绝的 Promise，调用方 try/catch 后提示「正在执行」。
        * 带 requestId 作为幂等键，服务端只执行一次。
        * 返回 Promise，调用方 await 后拿服务端结果。

     2) 普通动作 —— 纯本地状态机，必须保持**同步返回**语义（很多 ACTIONS 依赖
        返回值，例如 `var v = A.act.newScriptVersion()`）。
        做法仍是乐观本地执行 + 异步 POST，但改成**串行队列**：
        旧实现在 pending 期间直接跳过 POST，导致第二次点击只改了本地、
        随后被服务端回包覆盖。队列不丢同步请求。 */
  var pending = {};
  var chains = {};

  function enqueue(name, job){
    var prev = chains[name] || Promise.resolve();
    var next = prev.then(job, job);
    chains[name] = next.then(function(){ return null; }, function(){ return null; });
    return next;
  }

  A.isActPending = function(name){ return !!pending[name]; };
  A.pendingActs = function(){ return Object.keys(pending); };

  A.act = new Proxy(localActs, {
    get: function(target, name){
      if(typeof name !== "string" || !A.REMOTE_ACTS || A.REMOTE_ACTS.indexOf(name) < 0){
        return target[name];
      }
      var online = function(){
        return A.DB.datasource === "server" && A.remote && A.remote.ready;
      };
      var isGen = A.GEN_ACTS.indexOf(name) >= 0;

      return function(){
        var args = Array.prototype.slice.call(arguments);

        if(!online()){
          // 离线：纯本地实现，同步返回。
          var local = typeof target[name] === "function"
            ? target[name].apply(target, args) : undefined;
          /* 本地动作已经改完 A.DB 并落盘：告诉其它标签页「这一页变了」。
             tick 也走这里，所以另一个标签页能看见进度推进——这是**进度同步**，
             不是状态合并（见 app.js 里 A.startTabSync 的说明）。
             动作抛错时这一行根本到不了，不会广播一个没发生的变化。 */
          if(A.broadcastChange) A.broadcastChange();
          return local;
        }

        if(isGen){
          if(pending[name]){
            var busy = {busy:true, message:A.actionLabel(name) + "正在进行，请稍候"};
            A.toast(busy.message);
            return Promise.reject(busy);
          }
          pending[name] = true;
          A.busyStart(name);
          return A.remote.action(name, args, A.DB.activeProjectId, {
              timeout: A.timeoutFor(name), requestId: A.newRequestId()
            })
            .then(function(res){
              if(res && res.state) A.applyState(res.state);
              if(res && res.message) A.toast(res.message);
              A.render();
              return A.flattenResult(res ? res.result : null);
            })
            .catch(function(e){
              if(e && e.network){
                if(e.timeout){
                  // 超时 ≠ 失败：上游可能已经执行完了，只提示并回拉真源。
                  A.toast("请求超时，正在刷新状态");
                  A.refreshState();
                } else {
                  A.setDatasource("offline");
                  A.toast("网络连接已断开");
                }
              } else {
                A.toast((e && e.message) || "操作失败，请稍后重试");
              }
              A.render();
              throw e;
            })
            .finally(function(){ pending[name] = false; A.busyEnd(name); });
        }

        // 普通动作：乐观本地执行（同步返回），随后串行同步到服务端。
        var result = typeof target[name] === "function"
          ? target[name].apply(target, args) : undefined;
        enqueue(name, function(){
          pending[name] = true;
          return A.remote.action(name, args, A.DB.activeProjectId, {timeout: A.timeoutFor(name)})
            .then(function(res){
              if(res && res.state) A.applyState(res.state);
              A.render();
            })
            .catch(function(e){
              if(e && e.network){
                if(e.timeout){
                  A.toast("请求超时，正在刷新状态");
                  A.refreshState();
                } else {
                  A.setDatasource("offline");
                  A.toast("网络连接已断开");
                }
              } else {
                A.toast((e && e.message) || "操作失败，请稍后重试");
                // 本地已经改了但服务端拒绝了：必须回拉真源，否则界面与库不一致。
                A.refreshState();
              }
              A.render();
            })
            .finally(function(){ pending[name] = false; });
        });
        return result;
      };
    }
  });

  /* 只回拉 state，不重放任何动作。用于「服务端拒绝了本地乐观修改」与「超时后确认」。 */
  A.refreshState = function(){
    if(A.DB.datasource !== "server" || !A.remote || !A.remote.ready) return null;
    return A.remote.state().then(function(state){
      A.applyState(state); A.render(); return state;
    }).catch(function(){ return null; });
  };

  /* 素材清理不能走 A.act 的乐观通道：它返回的是一份**报告**（命中文件数 /
     释放字节 / 对应素材 id），本地乐观执行会拿一个「0 个文件」的假报告盖住
     服务端真实结果，用户会以为清理没生效。所以这里在线态直接等服务端回包，
     离线态才用本地实现（离线本来就不落盘）。 */
  A.purgeMedia = function(days, dryRun){
    var dry = dryRun !== false;
    var local = localActs.purgeMedia(days, dry);
    if(!A.remoteMode()) return Promise.resolve(local);
    return A.remote.action("purgeMedia", [days, dry], null,
      {timeout:60000, requestId:A.newRequestId()})
      .then(function(res){
        if(res && res.state) A.applyState(res.state);
        return (res && res.result) || local;
      });
  };

  /* 复核「上游结果未知」的凭据。同样不走乐观通道：复核会改冻结与余额，
     本地先改一遍再被服务端覆盖，用户会看到余额跳两次。 */
  A.resolveCharge = function(chargeId, outcome){
    if(!A.remoteMode()){
      return Promise.reject({
        busy:false,
        message:"离线态没有服务端待复核凭据：离线不产生真实扣费，也就没有需要复核的记录"
      });
    }
    return A.remote.action("resolveCharge", [chargeId, outcome], null,
      {timeout:60000, requestId:A.newRequestId()})
      .then(function(res){
        if(res && res.state) A.applyState(res.state);
        A.render();
        return (res && res.result) || null;
      });
  };

  /* 两套账本对账：只在有服务端真源时成立，离线态没有 created 账本可比，
     返回 null 由页面给出「离线无真源」的说明，而不是编一份 0 余额。 */
  A.loadReconcile = function(){
    if(!A.remoteMode()) return Promise.resolve(null);
    return A.remote.reconcile().then(function(doc){
      A.DB.server.reconcile = doc;
      A.DB.server.reconcileAt = A.now();
      A.save();
      return doc;
    });
  };

  /* 生成动作的全局忙碌指示：让用户看见「正在等服务端」，而不是以为按钮没反应。 */
  A.busyText = function(){
    var now = Date.now() / 1000;
    return (A._busy || []).map(function(name){
      var since = (A._busySince || {})[name] || now;
      return A.actionLabel(name) + "进行中 · " + A.waitText(since, name);
    }).join("；");
  };
  A.busyStart = function(name){
    A._busy = A._busy || [];
    A._busy.push(name);
    A._busySince = A._busySince || {};
    A._busySince[name] = Date.now() / 1000;
    var el = document.getElementById("busybar");
    if(el){
      el.hidden = false;
      el.textContent = A.busyText();
    }
  };
  A.busyEnd = function(name){
    A._busy = (A._busy || []).filter(function(x){ return x !== name; });
    var el = document.getElementById("busybar");
    if(el){
      el.hidden = A._busy.length === 0;
      el.textContent = A._busy.length ? A.busyText() : "";
    }
  };
})(App);
