/* app.js — 启动、路由、事件委托、模态表单与任务状态机定时器。 */
(function(A){
  "use strict";

  A.ACTIONS = {};

  function collect(formId){
    var out = {}, nodes = document.querySelectorAll("#"+formId+" [name]");
    for(var i=0;i<nodes.length;i++) out[nodes[i].name] = nodes[i].value;
    return out;
  }
  function inFlight(){ return A.DB.tasks.filter(function(t){return t.state==="queued"||t.state==="running";}); }
  function projectShotLabel(t){
    var p = A.project(t.projectId);
    if(!p) return t.shotId;
    var s = A.shotById(p, t.shotId);
    return (s ? "镜 " + (p.shots.indexOf(s)+1) : t.shotId);
  }

  /* ---------------- 渲染 ---------------- */
  A.renderNav = function(){
    var html = '<div class="brand"><div class="logo">漫</div><div><b>漫剧工作台</b><span>AI 漫剧创作</span></div></div>';
    A.NAV_GROUPS.forEach(function(g){
      html += '<div class="navgrp"><h4>'+g+'</h4>';
      A.PAGES.filter(function(x){return x.group===g && !x.hidden;}).forEach(function(x){
        html += '<button class="navitem '+(A.DB.ui.page===x.id?"on":"")+'" data-act="nav" data-page="'+x.id+'">'
          + A.icon(x.icon) + '<span>'+A.esc(x.name)+'</span></button>';
      });
      html += '</div>';
    });
    var who = (A.DB.server && A.DB.server.config && A.DB.server.config.owner) || "我的工作区";
    html += '<div class="sidefoot"><div class="avatar">' + A.esc(String(who).slice(0,1).toUpperCase()) + '</div>'
      + '<div><b>' + A.esc(who) + '</b><span>' + (A.isAdmin() ? "管理员" : "创作者") + '</span></div></div>';
    var el = document.getElementById("nav");
    el.innerHTML = html;
    el.className = "side" + (A.DB.ui.navClosed ? " closed" : "");
  };
  A.renderSteps = function(){
    var p = A.project();
    var states = (p && A.stepStates) ? A.stepStates(p) : [];
    var page = A.DB.ui.page;
    var cur = A.STEPS.filter(function(st){ return st.page === page; });
    var on = cur.length ? (cur.some(function(st){ return st.n === A.DB.ui.step; }) ? A.DB.ui.step : cur[0].n) : 0;
    var html = "";
    A.STEPS.forEach(function(st,i){
      var done = states[i] === "done";
      html += '<button class="step '+(on===st.n?"on":"")+(done?" done":"")+'" '
        + 'data-act="go-step" data-n="'+st.n+'"><i>'+(done && on!==st.n?"✓":st.n)+'</i><span>'+A.esc(st.name)+'</span></button>';
      if(i < A.STEPS.length-1) html += '<span class="step-line'+(done?" done":"")+'"></span>';
    });
    var el = document.getElementById("steps");
    el.innerHTML = html;
    var pg = A.pageById(page);
    el.hidden = !(page === "overview" || (pg.id === page && pg.group === "创作"));
  };
  A.renderProjectSwitch = function(){
    var sel = document.getElementById("project-switch");
    if(!sel) return;
    sel.innerHTML = A.activeProjects().map(function(p){
      return '<option value="'+A.esc(p.id)+'"'+(p.id===A.DB.activeProjectId?" selected":"")+'>'+A.esc(p.name)+'</option>';
    }).join("");
  };
  A.updateTop = function(){
    document.getElementById("top-balance").textContent = A.DB.ledger.balance;
    document.getElementById("top-frozen").textContent = A.DB.ledger.frozen;
  };
  A.render = function(){
    A.renderNav(); A.renderSteps(); A.renderProjectSwitch(); A.updateTop();
    var p = A.pageById(A.DB.ui.page);
    document.getElementById("page-title").textContent = p.name;
    var project = A.project();
    var html;
    /* 没有可用项目时必须给人话，不能把 TypeError 原样抛到页面上。
       触发场景：全部项目被归档、或服务端返回的 activeProjectId 与本地不一致。 */
    if(!project){
      html = A.empty('还没有可用的项目。<br><button class="btn primary mt12" data-act="new-project">新建项目</button>', "folder");
    } else {
      try { html = A.pages[A.DB.ui.page](project); }
      catch(e){ html = A.banner('页面加载失败，请刷新后重试。', "danger"); }
    }
    var view = document.getElementById("view");
    view.innerHTML = html;
    A.applyDyn(view);
  };
  /* ---------------- 窄屏适配 ----------------
     styles.css 把 ≤1024 的侧栏改成了覆盖式抽屉（position:fixed + translateX），
     抽屉默认展开就会盖住整页内容，所以首屏必须收起；≤720 只是栅格与内边距变化，
     不需要 JS 参与。断点值必须与 styles.css 的媒体查询保持一致。 */
  A.NARROW_WIDTH = 1024;
  A.isNarrow = function(){
    return typeof window !== "undefined" && window.innerWidth <= A.NARROW_WIDTH;
  };
  A.nav = function(id){
    if(A.loginRequired && id !== "account"){
      id = "account";
      A.toast("请先登录");
    }
    A.DB.ui.page = id;
    /* 抽屉态下点完导航就地收起，否则每次跳页都要再点一次 ☰。 */
    if(A.isNarrow()) A.DB.ui.navClosed = true;
    A.save(); A.render(); document.querySelector(".view").scrollTop = 0;
    /* 地址栏同步当前页（#script 等），刷新或分享链接能回到同一页。 */
    if(typeof history !== "undefined" && history.replaceState){
      try { history.replaceState(null, "", "#" + id); } catch(e){}
    }
  };
  /* 地址栏里的 #页面名 优先于上次停留的页面。 */
  A.pageFromHash = function(){
    var id = (typeof location !== "undefined" && location.hash || "").replace(/^#/, "");
    return A.PAGES.some(function(p){ return p.id === id; }) ? id : "";
  };
  A.goStep = function(n){ A.DB.ui.step = n; A.nav(A.STEPS[n-1].page); };

  /* ---------------- 任务状态机 ---------------- */
  A.ensureTicker = function(){
    if(A._ticker) return;
    A._ticker = setInterval(function(){
      var had = inFlight().length > 0;
      var still = A.act.tick();
      A.updateTop();
      var ae = document.activeElement;
      var editing = ae && /^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName);
      if(had && !editing) A.render();
      if(!still && had){ clearInterval(A._ticker); A._ticker = null; A.render(); }
      if(!still && !had){ clearInterval(A._ticker); A._ticker = null; }
    }, 340);
  };

  /* ---------------- 多标签页进度同步 ----------------
     为什么需要：整份 state 是**覆盖写**。一个标签页拿到服务端快照后 A.DB 被整包替换，
     另一个标签页里的余额、任务进度、镜头产物就停在旧值上，看起来像「点了没反应」。
     服务端没有版本号可供冲突判断，所以这里只做**进度同步**，不做合并：
     两个标签页同时改同一个字段，仍然以后写的为准（后写的那份 state 覆盖先写的）。
     能做的是「让另一个标签页尽快看到最新的那份」，而不是替用户决定谁对。

     通道：优先 BroadcastChannel（跨标签页、不经过存储、不触发 storage 事件）；
     不支持时退回监听 localStorage 的 storage 事件（A.save 写同一把 key）。
     两条通道只走其中一条，避免同一次变化被处理两遍。

     两条硬闸门，缺一条都会打断用户：
       1) 当前焦点在输入框里（正在打剧本 / 改提示词）——不刷新；
       2) 模态开着（正在填表单、正在看对比）——不刷新，否则表单内容被重绘冲掉。
     另外补一个 visibilitychange 兜底：后台标签页的广播可能被浏览器节流甚至丢掉，
     切回来时主动拉一次服务端真源，比纯广播可靠。 */
  A._tabId = "tab-" + Math.random().toString(36).slice(2, 8);
  A._tabToken = 0;      /* 最近一次广播 / 收到的序号，用来丢掉重复与过期消息 */
  A._tabApplying = 0;   /* >0 表示正在应用别的标签页的变化，此时不要再广播回去 */
  A._tabChannel = null;
  A._tabSyncStarted = false;

  function tabEditing(){
    var ae = document.activeElement;
    return !!(ae && /^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName || ""));
  }
  function tabModalOpen(){
    var mask = document.getElementById("modal-mask");
    return !!(mask && mask.hidden === false);
  }

  /* 任何**成功的、会改状态的动作**之后调用一次（在线态在 applyState 之后，
     离线态在本地动作之后，两处都在 store.js 里）。 */
  A.broadcastChange = function(){
    if(A._tabApplying) return;              /* 正在同步别人的变化，别反弹回去 */
    A._tabToken = Date.now();
    if(!A._tabChannel) return;              /* storage 通道：A.save 写库本身就是信号 */
    try { A._tabChannel.postMessage({token:A._tabToken, from:A._tabId}); } catch(e){}
  };

  A.startTabSync = function(){
    if(A._tabSyncStarted) return;
    A._tabSyncStarted = true;

    function receive(msg){
      if(!msg || msg.from === A._tabId) return;
      var token = Number(msg.token) || 0;
      if(token && token <= A._tabToken) return;   /* 已经处理过更新的那份 */
      if(token) A._tabToken = token;
      /* 用户正在输入 / 正在模态里填东西：宁可不刷新，也不能把字冲掉。 */
      if(tabEditing() || tabModalOpen()) return;
      if(A.remoteMode()){
        A._tabApplying++;
        var done = function(){ A._tabApplying--; };
        var pendingState = A.refreshState();
        if(pendingState && typeof pendingState.then === "function") pendingState.then(done, done);
        else done();
        return;
      }
      /* 离线态：重新读本地库再画。ui 必须保留本页自己的——否则另一个标签页
         切了个页面，这边也跟着跳页，用户会以为点击被串了。 */
      var ui = A.DB.ui;
      A.load();
      if(ui && A.DB) A.DB.ui = ui;
      A.render();
    }

    if(typeof BroadcastChannel !== "undefined"){
      try {
        A._tabChannel = new BroadcastChannel("drama_sync");
        A._tabChannel.onmessage = function(e){ receive(e && e.data); };
      } catch(e){ A._tabChannel = null; }
    }
    if(!A._tabChannel && typeof window !== "undefined" && window.addEventListener){
      window.addEventListener("storage", function(e){
        if(!e || e.key !== A.STORE_KEY) return;
        receive({token:Date.now(), from:"storage"});
      });
    }
    if(typeof document !== "undefined" && document.addEventListener){
      document.addEventListener("visibilitychange", function(){
        if(typeof document.visibilityState === "string" && document.visibilityState !== "visible") return;
        if(!A.remoteMode() || tabEditing()) return;
        A.refreshState();
      });
    }
  };

  /* ---------------- 模态表单 ---------------- */
  function modalProject(){
    A.openModal({title:"新建项目",
      body:'<div class="grid g2">'
        + A.fInput("项目名称", "name", "")
        + A.fInput("分集标题", "episodeTitle", "第一集")
        + A.fInput("季", "season", 1, {type:"number",min:1})
        + A.fInput("集", "episode", 1, {type:"number",min:1})
        + A.fSelect("题材", "genre", ["都市悬疑","古风","校园","科幻","家庭伦理"], "都市悬疑")
        + A.fSelect("画风", "style", ["精致国漫","2D 日式动漫","仿真人写实","复古武侠"], "精致国漫")
        + A.fSelect("画面比例", "ratio", ["9:16 竖屏","16:9 横屏"], "9:16 竖屏")
        + A.fInput("集数", "eps", 12, {type:"number",min:1})
        + A.fInput("单集时长（秒）", "duration", 90, {type:"number",min:1})
        + A.fInput("预算上限（积分）", "budget", 3000, {type:"number",min:1})
        + '</div>',
      onSubmit:function(f){ A.act.createProject(f); A.toast("项目已创建并切换为当前项目"); }});
  }
  function modalAsset(type, id){
    var p = A.project();
    var item = id ? A.assetByType(p, type).filter(function(a){return a.id===id;})[0] : null;
    var name = A.ASSET_TYPES.filter(function(x){return x.key===type;})[0].name;
    A.openModal({title:(item?"编辑":"新增")+name,
      body:A.fInput("名称", "name", item?item.name:"")
        + A.fInput("说明", "sub", item?item.sub:"草稿")
        + A.fInput("音色 / 环境音", "voice", item?item.voice:"—")
        + A.fInput("备注", "note", item?item.note:"")
        + A.fTextarea("形象描述", "prompt", item?item.prompt:"", 3),
      onSubmit:function(f){
        if(item) { A.act.updateAsset(type, id, f); A.toast("已保存"); }
        else { A.act.addAsset(type, f); A.toast(name+"已新增"); }
      }});
  }
  function modalShot(id){
    var p = A.project(), s = id ? A.shotById(p,id) : null;
    A.openModal({title:(s?"编辑":"新增")+"镜头",
      body:'<div class="grid g2">'
        + A.fInput("时长（秒）", "dur", s?s.dur:3, {type:"number",min:1})
        + A.fSelect("景别", "size", ["全景","中景","中近景","近景","特写"], s?s.size:"中景")
        + A.fSelect("运镜", "move", ["固定镜头","缓慢下摇","缓慢横移","微推","后拉","上摇"], s?s.move:"固定镜头")
        + A.fInput("角色 / 场景（如 @角色:林砚）", "refs", s?s.refs.join(" "):"")
        + '</div>'
        + A.fTextarea("画面描述", "desc", s?s.desc:"", 2)
        + A.fTextarea("提示词", "prompt", s?s.prompt:"", 2)
        + A.fTextarea("台词", "line", s?s.line:"", 2),
      onSubmit:function(f){
        if(s) { A.act.updateShot(id, f); A.toast("镜头已更新"); }
        else { A.act.addShot(f); A.toast("镜头已新增"); }
      }});
  }
  function modalRelease(taskId){
    A.openModal({title:"释放任务",
      body:A.fTextarea("原因", "reason", "", 3),
      submitLabel:"确认释放",
      onSubmit:function(f){ A.act.releaseHeld(taskId, f.reason); A.toast("已释放，冻结积分已退还"); }});
  }
  function modalFault(){
    var list = inFlight().filter(function(t){return !t.settled;});
    if(!list.length) throw new Error("没有仍持有预占的在途任务；请先提交镜头生成");
    A.openModal({title:"注入提交故障",
      note:"两种上游故障：响应丢失转 held（禁止自动重提，需管理员带原因释放）；"
        + "明确拒绝转 failed（任务未执行，预占自动退还，可用同一 task_id 重试）。",
      body:A.fSelect("选择任务", "taskId", list.map(function(t){
        return {value:t.id, label:t.id.slice(-10)+" · "+projectShotLabel(t)};
      }), list[0].id)
        + A.fSelect("故障类型", "kind", [
            {value:"lost", label:"响应丢失 → held（需管理员释放）"},
            {value:"reject", label:"明确拒绝 → failed（自动退还，可重试）"}
          ], "lost"),
      submitLabel:"注入故障",
      onSubmit:function(f){
        A.act.injectFault(f.taskId, f.kind);
        A.toast(f.kind === "reject"
          ? "任务已转 failed，预占已退还，可点「重试同一任务」"
          : "任务已转 held，请到管理后台释放");
      }});
  }

  function modalReshoot(){
    var p = A.project();
    if(!p.shots.length) throw new Error("当前项目没有镜头可退回");
    A.openModal({title:"退回重拍",
      body:A.fSelect("选择镜头", "shotId", p.shots.map(function(s,i){
        return {value:s.id, label:"镜 "+(i+1)+" · "+s.size+" · "+s.desc.slice(0,16)};
      }), p.shots[0].id)
      + A.fTextarea("退回原因", "reason", "", 3),
      submitLabel:"确认退回",
      onSubmit:function(f){
        var s = A.act.returnForReshoot(f.shotId, f.reason);
        A.toast("已退回镜头 "+(A.project().shots.indexOf(s)+1));
      }});
  }

  /* ---------------- 动作分发 ---------------- */
  A.ACTIONS.nav = function(el,d){ A.nav(d.page); return false; };
  A.ACTIONS["go-step"] = function(el,d){ A.goStep(Number(d.n)); return false; };
  /* 必须自己重绘侧栏：本动作返回 false 会让 dispatch 跳过 A.render()，
     否则 navClosed 已写入但 .side.closed 永远不会生效。只重绘侧栏，不重绘整页，
     避免打断用户正在填写的表单。 */
  A.ACTIONS["toggle-nav"] = function(){
    A.DB.ui.navClosed = !A.DB.ui.navClosed; A.save(); A.renderNav(); return false;
  };
  A.ACTIONS["close-drawer"] = function(){ A.closeDrawer(); return false; };
  A.ACTIONS["close-modal"] = function(){ A.closeModal(); return false; };
  A.ACTIONS["modal-submit"] = function(){ A.submitModal(); return false; };
  /* 顶部 <select> 不复用本动作：它没有 data-id，click 冒泡会把 activeProjectId 置空。
     select 由 document 的 change 事件单独处理。这里保留校验做二次防护。 */
  A.ACTIONS["switch-project"] = function(el,d){
    if(!d.id) return false;
    A.act.switchProject(d.id); A.toast("已切换项目");
  };
  A.ACTIONS["new-project"] = function(){ modalProject(); return false; };
  A.ACTIONS["copy-project"] = function(el,d){ A.act.copyProject(d.id); A.toast("项目已复制并切换"); };
  A.ACTIONS["archive-project"] = function(el,d){
    A.confirmModal("归档项目", "归档后项目不再出现在工作台，可随时恢复。", function(){
      try { A.act.archiveProject(d.id); A.toast("项目已归档"); }
      catch(e){ A.toast(e.message); }
    });
    return false;
  };
  A.ACTIONS["unarchive-project"] = function(el,d){
    A.act.unarchiveProject(d.id); A.toast("项目已恢复");
  };
  A.ACTIONS["delete-project"] = function(el,d){
    A.confirmModal("删除项目", "删除后无法恢复，确定要删除吗？", function(){
      try { A.act.deleteProject(d.id); A.toast("项目已删除"); }
      catch(e){ A.toast(e.message); }
    });
    return false;
  };
  A.ACTIONS["save-settings"] = function(){
    var changed = A.act.saveSettings(collect("settings-form"));
    A.toast(changed ? "已保存，相关分镜已标为待更新" : "已保存");
  };
  A.ACTIONS["restore-defaults"] = function(){
    A.confirmModal("恢复默认设定", "题材、画风、比例等设定将恢复为默认值。",
      function(){
        var changed = A.act.restoreDefaults();
        A.toast(changed ? "已恢复默认，相关分镜已标为待更新" : "已恢复默认");
      });
    return false;
  };
  A.ACTIONS["save-script"] = function(){ A.act.saveScript(collect("script-form")); A.toast("剧本已保存"); };
  A.ACTIONS["new-script-version"] = function(){ var v = A.act.newScriptVersion(); A.toast("已存为 "+v.label); };
  A.ACTIONS["freeze-script"] = function(){ var v = A.act.freezeScript(null); A.toast("已定稿 "+v.label); };
  A.ACTIONS["freeze-version"] = function(el,d){ var v = A.act.freezeScript(d.id); A.toast("已定稿 "+v.label); };
  A.ACTIONS["rollback-script"] = function(el,d){ A.act.rollbackScript(d.id); A.toast("已恢复到该版本"); };
  A.ACTIONS["mark-dirty-script"] = function(){
    A.confirmModal("标记受影响镜头", "已锁定的分镜将标为「待更新」。", function(){
      A.act.markDirtyByScript(); A.toast("受影响镜头已标记");
    });
    return false;
  };
  /* ---------------- 版本对比 ----------------
     两个下拉：起点 / 对比版本，首个选项固定是「当前草稿」（空 id）。
     服务端把空 id 解释成「当前草稿」，所以用户不必先存一版才能看差异。
     每行一条 hunk，行号两侧各一列（该侧没有这一行时留空）。
     剧本是用户输入，所有行内容必须 A.esc 转义。 */
  var DIFF_FIELD_NAMES = {logline:"一句话简介", outline:"大纲", body:"正文"};

  function diffPad(n){
    if(n === null || n === undefined) return "  ";
    var v = Number(n);
    if(!isFinite(v)) return "  ";
    return v < 10 ? " " + v : String(v);
  }
  function diffLineHtml(h){
    var kind = (h && h.kind) || "same";
    var text = A.esc(h && h.line != null ? h.line : "") || "&nbsp;";
    var mark = kind === "add" ? "+ " : (kind === "remove" ? "- " : "  ");
    /* 没有 styles.css 的写权限，不新增 diff-add / diff-del 类：
       用 <ins> / <del> 的浏览器默认样式（下划线 / 删除线）做区分，
       「相同」行复用既有 .hint 变灰，行号列同样用 .hint。 */
    var body = kind === "add" ? "<ins>" + text + "</ins>"
             : kind === "remove" ? "<del>" + text + "</del>" : text;
    return '<div class="mono' + (kind === "same" ? " hint" : "") + '">'
      + '<span class="hint">' + diffPad(h && h.baseLine) + " " + diffPad(h && h.targetLine) + "</span> "
      + mark + body + "</div>";
  }
  function diffHtml(res){
    if(!res || !res.fields || !res.fields.length){
      return '<div class="hint">没有可对比的内容</div>';
    }
    var sum = res.summary || {};
    var labels = res.labels || {};
    var head = sum.changed === false
      ? "两个版本内容一致"
      : "共新增 " + (sum.added || 0) + " 行、删除 " + (sum.removed || 0) + " 行";
    var who = (labels.base || "当前草稿") + " → " + (labels.target || "当前草稿");
    var html = '<div class="notice">' + A.esc(head) + "（" + A.esc(who) + "）</div>";
    res.fields.forEach(function(f){
      if(!f) return;
      var added = Number(f.added) || 0, removed = Number(f.removed) || 0;
      /* 字段名只在内部出现（logline / outline / body），页面一律用中文名；
         认不出来的字段就写「内容」，绝不把内部字段名漏给用户。 */
      html += '<div class="section-title"><h2>'
        + A.esc(DIFF_FIELD_NAMES[f.field] || "内容")
        + ' <span class="hint">+' + added + " / -" + removed + "</span></h2></div>";
      if(!added && !removed){
        html += '<div class="hint">未改动</div>';
        return;
      }
      (f.hunks || []).forEach(function(h){ html += diffLineHtml(h); });
      if(f.truncated){
        html += '<div class="hint">对比内容过长，只显示前 200 条变更</div>';
      }
    });
    return html;
  }

  A.ACTIONS["diff-versions"] = function(){
    var p = A.project();
    var versions = (p.script && p.script.versions) || [];
    var opts = [{value:"", label:"当前草稿"}].concat(versions.map(function(v){
      return {value:v.id, label:v.label};
    }));
    /* ui.js 的 openModal 只认 {title, note, body, submitLabel, onSubmit}，
       没有自定义底部按钮的入口，所以「开始对比」放在 body 里，
       底部只留一个「取消」用来关掉模态。submitLabel 传 null 才不会被塞一个保存按钮。 */
    A.openModal({
      title:"版本对比",
      note:"左边是起点，右边是要对比的那一版。选「当前草稿」就是和你现在改到一半的稿子比。",
      body:'<div class="grid g2">'
        + A.fSelect("起点版本", "diff-base", opts, "")
        + A.fSelect("对比版本", "diff-target", opts, versions.length ? versions[0].id : "")
        + '</div>'
        + '<div class="btnbar"><button class="btn primary" data-act="diff-run">开始对比</button></div>'
        + '<div id="diff-body"></div>',
      submitLabel:null
    });
    return false;
  };
  /* 对比是只读动作，跑完留在模态里继续看，不关窗、不整页重绘。 */
  A.ACTIONS["diff-run"] = genGuard(async function(){
    var box = document.getElementById("diff-body");
    if(!box){ A.toast("对比面板已关闭，请重新打开"); return false; }
    var baseEl = document.querySelector('#modal [name="diff-base"]');
    var targetEl = document.querySelector('#modal [name="diff-target"]');
    var base = baseEl ? baseEl.value : "";
    var target = targetEl ? targetEl.value : "";
    box.innerHTML = '<div class="hint">正在对比…</div>';
    try {
      var r = await A.act.diffScriptVersions(base, target);
      box.innerHTML = diffHtml(r);
    } catch(e){
      box.innerHTML = '<div class="hint">对比失败，请稍后重试</div>';
      throw e;
    }
    return false;
  });
  /* 新手引导的「知道了」。服务端 state 里没有引导字段（它只是首屏提示，
     不是业务数据），所以关闭状态只存在前端 ui 里并落本地：
     换项目、刷新都保持关闭，换浏览器/换账号重新出现，符合预期。 */
  A.ACTIONS["onboard-dismiss"] = function(){
    A.DB.ui.onboardDismissed = true;
    A.save();
    /* 不 return false：让 dispatch 正常重绘，引导块才会真的从页面上消失。 */
  };
  A.ACTIONS["asset-tab"] = function(el,d){ A.DB.ui.assetTab = d.tab; A.save(); };
  A.ACTIONS["add-asset"] = function(el,d){ modalAsset(d.tab, null); return false; };
  A.ACTIONS["edit-asset"] = function(el,d){ modalAsset(d.type, d.id); return false; };
  A.ACTIONS["delete-asset"] = function(el,d){ A.act.deleteAsset(d.type, d.id); A.toast("资产已删除"); };
  A.ACTIONS["toggle-asset-lock"] = function(el,d){ A.act.toggleAssetLock(d.type, d.id); };
  A.ACTIONS["select-shot"] = function(el,d){ A.DB.ui.selectedShot = d.id; A.save(); };
  A.ACTIONS["add-shot"] = function(){ modalShot(null); return false; };
  A.ACTIONS["insert-shot-before"] = function(el,d){
    var s = A.act.insertShotBefore(d.id);
    A.toast("已插入镜头 "+(A.project().shots.indexOf(s)+1));
  };
  A.ACTIONS["edit-shot"] = function(el,d){ modalShot(d.id); return false; };
  A.ACTIONS["delete-shot"] = function(el,d){ A.act.deleteShot(d.id); A.toast("镜头已删除"); };
  A.ACTIONS["move-shot"] = function(el,d){ A.act.moveShot(d.id, Number(d.dir)); };
  A.ACTIONS["toggle-shot-lock"] = function(el,d){ A.act.toggleShotLock(d.id); };
  A.ACTIONS["resplit-shots"] = function(){ A.act.resplitShots(); A.toast("已按剧本标记需要更新的分镜"); };
  /* 批量关键帧。候选张数取页面上的下拉：id 与 name 两种选择器都试，
     因为 pages.js 将来加 id 时不该再改这里。越界（空值 / 0 / 9 / 非数字）
     一律回落 1，不抛错——用户把下拉改坏不该让提交整体失败。 */
  A.ACTIONS["submit-tasks"] = function(){
    var sel = document.querySelector('#page-task-candidates, [name="task-candidates"]');
    var n = sel ? parseInt(sel.value, 10) : 1;
    if(!(n >= 1 && n <= 4)) n = 1;
    var count = A.act.submitTasks(null, n);
    A.ensureTicker();
    A.toast(n > 1
      ? "已提交 " + count + " 个镜头，每镜 " + n + " 张候选，按张计费"
      : "已提交 " + count + " 个镜头");
  };
  /* 批量图生视频。结果必须**如实分层**：queued 可能为 0（全部跳过），
     那不是失败，也不是成功——要把「跳过了几个、为什么」说清楚。
     全 busy 和全没关键帧是两种完全不同的处置，不能合并成一句「没有可提交的镜头」。 */
  A.ACTIONS["submit-video-tasks"] = genGuard(async function(){
    var r = await A.act.submitVideoTasks(null);
    var queued = Number((r && r.queued) || 0);
    var reserve = Number((r && r.reserve) || 0);
    var skipped = (r && r.skipped) || [];
    var noKey = 0, busy = 0;
    skipped.forEach(function(x){
      if(!x) return;
      if(x.reason === "no_keyframe") noKey++;
      else if(x.reason === "busy") busy++;
    });
    if(!queued){
      if(noKey) A.toast("没有可提交的镜头：请先生成关键帧"
        + (busy ? "（另有 " + busy + " 个镜头已有在途任务）" : ""));
      else if(busy) A.toast("这些镜头已有在途任务，请等完成后再提交");
      else A.toast("没有可提交的镜头");
      return;
    }
    A.ensureTicker();
    var msg = "已提交 " + queued + " 个镜头的视频生成，预占 " + reserve + " 积分";
    if(skipped.length){
      var parts = [];
      /* 某一类为 0 就不提：写「没生成关键帧 0 个 / 已有在途任务 2 个」只是噪声。 */
      if(noKey) parts.push("没生成关键帧 " + noKey + " 个");
      if(busy) parts.push("已有在途任务 " + busy + " 个");
      msg += "；跳过 " + skipped.length + " 个（" + parts.join(" / ") + "）";
    }
    /* 离线演示没有视频价，预占是 0：不能让「已提交」听起来像真出了一段视频。 */
    if(!reserve && !A.remoteMode()) msg += "（离线演示：不产出真实视频）";
    A.toast(msg);
  });
  /* 产物回退：把某个镜头换回它历史里用过的一版。会覆盖当前画面，
     所以先确认再发请求——误点一次就把辛苦调好的那一版换掉了。 */
  A.ACTIONS["restore-media"] = function(el, d){
    var id = (d && d.id) || "", url = (d && d.url) || "";
    if(!id || !url){ A.toast("没有选中的历史产物"); return false; }
    var p = A.project();
    var shot = A.shotById(p, id);
    var idx = shot ? (p.shots.indexOf(shot) + 1) : 0;
    A.confirmModal("切换产物版本",
      "镜头" + (idx || "") + "的画面将换回你选中的那一版历史产物。当前这一版会留在历史里，随时可以再切回来。",
      function(){
        var after = function(){
          A.toast("已切换为该版本");
          A.render();
        };
        var fail = function(e){
          A.toast((e && e.message) || "切换失败，请稍后重试");
        };
        var r;
        try { r = A.act.restoreShotMedia(id, url); }
        catch(e){ fail(e); return; }
        /* 在线态返回 Promise（回包里有恢复后的 kind），离线态同步返回。 */
        if(r && typeof r.then === "function") r.then(after, fail);
        else after();
      });
    return false;
  };
  A.ACTIONS["goto-generate-shot"] = function(el,d){
    try { A.act.submitTasks([d.id]); A.ensureTicker(); A.nav("generate"); A.toast("已开始生成"); }
    catch(e){ A.nav("generate"); A.toast(e.message); }
    return false;
  };
  A.ACTIONS["inject-fault"] = function(){ modalFault(); return false; };
  A.ACTIONS["inject-fault-task"] = function(el,d){ A.act.injectFault(d.id); };
  A.ACTIONS["release-held"] = function(el,d){ modalRelease(d.id); return false; };
  A.ACTIONS["retry-task"] = function(el,d){ A.act.retryTask(d.id); A.ensureTicker(); A.toast("已重新提交"); };
  A.ACTIONS["pick-frame"] = function(el,d){ A.act.pickFrame(d.id, Number(d.index)); };
  A.ACTIONS["reroll-task"] = function(el,d){ A.act.rerollTask(d.id); A.ensureTicker(); A.toast("已开始重绘"); };
  function modalCancel(taskId){
    A.openModal({title:"取消任务",
      body:A.fTextarea("取消原因（选填）", "reason", "", 2),
      submitLabel:"确认取消",
      onSubmit:function(f){
        A.act.cancelTask(taskId, f.reason);
        A.toast("已取消，冻结积分已退还");
      }});
  }
  A.ACTIONS["cancel-task"] = function(el,d){ modalCancel(d.id); return false; };
  function modalPurge(){
    var media = (A.DB.server && A.DB.server.config && A.DB.server.config.media) || null;
    var stats = media
      ? ("当前共 " + media.files + " 个文件，占用 " + (Math.round(media.bytes/1048576*10)/10) + " MB")
      : "";
    A.openModal({title:"清理历史素材",
      note:stats,
      body:A.fInput("清理多少天前的素材", "days", 7, {type:"number", min:0})
        + A.fSelect("方式", "mode", [
            {value:"dry", label:"先预览"},
            {value:"run", label:"直接删除"}], "dry"),
      submitLabel:"执行",
      onSubmit:function(f){
        var days = Number(f.days);
        var dry = f.mode !== "run";
        A.purgeMedia(days, dry).then(function(rep){
          A.toast((rep && rep.dry_run ? "可清理 " : "已清理 ")
            + (rep ? rep.files : 0) + " 个文件，" + (Math.round((rep ? rep.freed_bytes : 0)/1048576*10)/10) + " MB");
          A.render();
        }).catch(function(e){ A.toast("清理失败：" + ((e && e.message) || "未知错误")); });
      }});
  }
  A.ACTIONS["purge-media"] = function(){ modalPurge(); return false; };
  /* 对账报表：只读拉取，失败只提示不动页面。非管理员会被服务端挡成 403，
     这里直接展示服务端给的人话，不再自己拼一句「权限不足」去猜原因。 */
  A.ACTIONS["load-report"] = function(){
    if(!A.remoteMode()){
      A.toast("当前无法加载对账报表");
      return false;
    }
    var r = A.report;
    if(r.loading) return false;
    r.loading = true;
    A.reportApi.daily(r.days).then(function(data){
      r.data = data; r.loaded = true; r.loading = false; A.render();
    }).catch(function(e){
      r.loading = false;
      A.toast((e && e.message) || "加载对账报表失败");
    });
    return false;
  };
  A.ACTIONS["load-reconcile"] = function(){
    if(!A.remoteMode()){
      A.toast("当前无法加载对账数据");
      return false;
    }
    A.loadReconcile().then(function(){ A.render(); })
      .catch(function(e){ A.toast("对账失败：" + ((e && e.message) || "未知错误")); });
    return false;
  };
  function doReset(keepDatasource, label){
    if(A.remoteMode()){
      A.resetRemote().then(function(){ A.toast(label); })
        .catch(function(e){ A.toast("重置失败：" + (e.message || "未知错误")); });
    } else {
      A.reset(keepDatasource); A.toast(label);
    }
  }
  A.ACTIONS["reset-demo"] = function(){
    A.confirmModal("重置数据", "项目、任务与流水将恢复为初始状态。", function(){
        doReset(true, "已重置");
      });
    return false;
  };
  A.ACTIONS["add-voice"] = function(){
    A.openModal({title:"新增角色音色",
      body:A.fInput("角色", "character", "") + A.fInput("音色", "voice", "声线 A · 清冷女中音")
        + A.fInput("台词数", "lines", 1, {type:"number",min:0}) + A.fInput("时长", "duration", "3.0s"),
      onSubmit:function(f){ A.act.addVoice(f); A.toast("音色已新增"); }});
    return false;
  };
  A.ACTIONS["delete-voice"] = function(el,d){ A.act.deleteVoice(d.id); A.toast("音色已删除"); };
  A.ACTIONS["add-subtitle"] = function(){
    A.openModal({title:"新增字幕",
      body:'<div class="grid g2">'+A.fInput("入点", "in", "00:00")+A.fInput("出点", "out", "00:03")+'</div>'
        + A.fTextarea("字幕文本", "text", "", 2),
      onSubmit:function(f){ A.act.addSubtitle(f); A.toast("字幕已新增"); }});
    return false;
  };
  A.ACTIONS["toggle-subtitle"] = function(el,d){ A.act.toggleSubtitle(d.id); };
  A.ACTIONS["delete-subtitle"] = function(el,d){ A.act.deleteSubtitle(d.id); A.toast("字幕已删除"); };
  A.ACTIONS["save-bgm"] = function(){ A.act.saveBgm(collect("bgm-form")); A.toast("混音配置已保存"); };
  /* BGM / 参考图上传：raw body 直传 /api/v1/drama/uploads，不经过 /action，
     所以既不进 ACTION_PRICES 也不扣积分。回包是完整 state，直接灌进 A.DB。
     genGuard 定义在下方函数声明里，会被引擎提升，可在此使用。 */
  A.ACTIONS["upload-media"] = genGuard(async function(el, d){
    var kind = (d && d.kind) === "bgm" ? "bgm" : "reference";
    var input = document.getElementById("upload-file-" + kind);
    var file = input && input.files && input.files[0];
    if(!file){ A.toast("请先选择要上传的文件"); return; }
    var meta = {name:file.name};
    if(kind === "reference"){
      var type = document.getElementById("upload-asset-type");
      var aname = document.getElementById("upload-asset-name");
      meta.asset_type = (type && type.value) || "characters";
      meta.asset_name = (aname && aname.value) || "";
    }
    await A.uploadMedia(kind, file, meta);
    if(input) input.value = "";
    A.toast("已上传 " + file.name);
  });
  A.ACTIONS["use-upload-bgm"] = genGuard(async function(el, d){
    var up = A.uploads("bgm").filter(function(x){ return x.id === (d && d.id); })[0];
    var f = collect("bgm-form");
    f.uploadId = (d && d.id) || "";
    if(up) f.name = up.name;
    A.act.saveBgm(f);
    A.toast("已设为背景音乐");
  });
  A.ACTIONS["delete-upload"] = genGuard(async function(el, d){
    await A.removeUpload((d && d.id) || "");
    A.toast("已删除");
  });
  A.ACTIONS["move-clip"] = function(el,d){ A.act.moveClip(d.id, Number(d.dir)); };
  A.ACTIONS["add-note"] = function(){
    A.openModal({title:"添加批注",
      body:A.fTextarea("批注内容", "text", "", 3),
      onSubmit:function(f){ A.act.addNote(f.text); A.toast("批注已添加"); }});
    return false;
  };
  A.ACTIONS["toggle-qa"] = function(el,d){ A.act.toggleQA(Number(d.i)); };
  /* 导出是乐观动作：本地会先落一条记录并提示成功，服务端拒绝后才回滚。
     所以服务端一定会拒绝的三种情况必须在这里先挡住，不能先报成功再报失败。 */
  A.ACTIONS["export-film"] = function(){
    var p = A.project();
    if(!p.qa.every(function(x){ return x; })){ A.toast("请先完成全部审片项"); return; }
    if(A.DB.datasource === "server"){
      if(!(p.film && p.film.videoUrl)){
        A.toast("请先合成成片"); return;
      }
      var missing = A.shotsWithoutVideo(p);
      if(missing){
        A.toast("还有 " + missing + " 个镜头没有视频，请先生成后重新合成");
        return;
      }
      if(A.filmStale(p)){
        A.toast("成片已过期，请重新合成后再导出"); return;
      }
    }
    var r = A.act.exportFilm(); A.toast("已导出：" + r.name);
  };
  A.ACTIONS["return-reshoot"] = function(){ modalReshoot(); return false; };
  A.ACTIONS["task-filter"] = function(el,d){ A.DB.ui.taskFilter = d.filter; A.save(); };
  A.ACTIONS["goto-shot"] = function(el,d){
    A.act.switchProject(d.project); A.DB.ui.selectedShot = d.id; A.DB.ui.page = "storyboard"; A.save();
    A.toast("已定位到镜头");
  };
  /* 积分使用上限：空 = 不限。保存后下一次生成立即按新值检查。 */
  A.ACTIONS["save-credit-limits"] = function(){
    var f = collect("credit-limit-form");
    var v = A.act.saveCreditLimits({single:f.single, total:f.total});
    var after = function(){ A.toast("已保存"); A.render(); };
    if(v && typeof v.then === "function") return v.then(after);
    after();
  };
  A.ACTIONS["clear-credit-limits"] = function(){
    var v = A.act.saveCreditLimits({single:"", total:""});
    var after = function(){ A.toast("已取消使用上限"); A.render(); };
    if(v && typeof v.then === "function") return v.then(after);
    after();
  };
  A.ACTIONS["reset-credit-used"] = function(){
    var lim = A.DB.creditLimits || {};
    A.confirmModal("重新计算已用额度", "已用额度将从 0 开始重新累计。", function(){
      var v = A.act.saveCreditLimits({single:lim.single, total:lim.total, resetUsed:true});
      if(v && typeof v.then === "function") v.then(function(){ A.render(); });
      A.toast("已重新计算");
    });
    return false;
  };
  /* ---------------- 账户与充值 ----------------
     会话 Cookie 只在服务端：登录 / 退出会换 owner，本地 localStorage 里的整份
     快照属于上一个账户，必须先丢掉再重新拉 state，否则页面上还是别人的积分。 */
  A.afterAccountChange = function(msg){
    var acc = A.account;
    /* A.load() 会换掉整份 A.DB（含 ui），所以先把「用户在哪一页」记下来，
       否则登录成功的一瞬间就被弹回项目总览。 */
    var ui = {page:A.DB.ui.page, step:A.DB.ui.step, navClosed:A.DB.ui.navClosed};
    acc.orders = null; acc.ordersLoaded = false; acc.ordersLoading = false;
    acc.draft = null; acc.draftRequestId = ""; acc.creating = false;
    try { if(window.localStorage) window.localStorage.removeItem(A.STORE_KEY); } catch(e){}
    A.load();
    A.DB.ui.page = ui.page; A.DB.ui.step = ui.step; A.DB.ui.navClosed = ui.navClosed;
    A.save();
    if(msg) A.toast(msg);
    return A.probeServer().then(function(){ A.render(); });
  };

  A.ACTIONS["account-login"] = genGuard(async function(){
    var f = collect("account-login-form");
    var r = await A.accountApi.login(f.username || "", f.password || "");
    A.account.me = r || null; A.account.loaded = true;
    await A.afterAccountChange("已登录");
  });
  A.ACTIONS["account-register"] = genGuard(async function(){
    var f = collect("account-register-form");
    if((f.password || "") !== (f.confirm || "")){ A.toast("两次输入的密码不一致"); return; }
    await A.accountApi.register(f.username || "", f.password || "");
    A.toast("注册成功，请用新账号登录");
  });
  A.ACTIONS["account-password"] = genGuard(async function(){
    var f = collect("account-password-form");
    if((f.next || "") !== (f.confirm || "")){ A.toast("两次输入的密码不一致"); return; }
    await A.accountApi.password(f.old || "", f.next || "");
    A.toast("密码已修改");
  });
  A.ACTIONS["account-logout"] = genGuard(async function(){
    await A.accountApi.logout();
    A.account.me = null; A.account.loaded = true;
    await A.afterAccountChange("已退出登录");
  });

  A.ACTIONS["payment-create"] = genGuard(async function(){
    /* creating 是双击闸门：第一笔还在飞的时候第二次点击直接忽略，
       不会因为连点产生两张待支付订单。 */
    if(A.account.creating) return;
    var f = collect("recharge-form");
    var amount = Number(f.amount);
    if(!isFinite(amount) || Math.floor(amount) !== amount || amount < 1 || amount > 1000000){
      A.toast("充值积分必须是 1 到 100 万之间的整数"); return;
    }
    A.account.creating = true;
    /* 同一个请求编号在服务端只对应一笔订单：一次点击生成一次并复用，
       请求失败时保留，重试就是回放同一笔而不是新开一笔。 */
    if(!A.account.draftRequestId){
      A.account.draftRequestId = "ui-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8);
    }
    try {
      var order = await A.paymentApi.createOrder(amount, A.account.draftRequestId);
      A.account.draft = order;
      A.account.draftRequestId = "";
      A.account.orders = null; A.account.ordersLoaded = false;
      A.toast(order && order.replayed ? "这笔充值单已存在，请直接支付" : "充值单已创建，请确认支付");
    } finally {
      A.account.creating = false;
    }
  });
  A.ACTIONS["payment-pay"] = genGuard(async function(el, d){
    var id = (d && d.id) || (A.account.draft && A.account.draft.order_id) || "";
    if(!id){ A.toast("请先创建充值单"); return; }
    await A.paymentApi.submit(id);        /* 待提交 → 待支付 */
    var r = await A.paymentApi.pay(id);   /* 待支付 → 已到账 */
    if(A.account.draft && A.account.draft.order_id === id) A.account.draft = null;
    A.account.draftRequestId = "";
    A.account.orders = null; A.account.ordersLoaded = false;
    A.toast((r && r.message) || "充值已到账");
    A.load();
    await A.refreshState();
    A.render();
  });
  A.ACTIONS["payment-cancel"] = genGuard(async function(el, d){
    var id = (d && d.id) || (A.account.draft && A.account.draft.order_id) || "";
    if(!id){ A.toast("请先创建充值单"); return; }
    await A.paymentApi.cancel(id);
    if(A.account.draft && A.account.draft.order_id === id){
      A.account.draft = null; A.account.draftRequestId = "";
    }
    A.account.orders = null; A.account.ordersLoaded = false;
    A.toast("订单已取消");
  });
  A.ACTIONS["payment-orders-refresh"] = genGuard(async function(){
    if(!A.account.me){ A.toast("请先登录"); return; }
    var pending = A.loadOrders(true);
    if(!pending){ A.toast("正在刷新，请稍候"); return; }
    await pending;
    A.toast("订单已刷新");
  });

  /* ---------------- 模型设置 ---------------- */
  function afterCredentials(msg){
    return A.remote.config().then(function(cfg){
      A.DB.server.config = cfg; A.save(); A.render(); if(msg) A.toast(msg);
    });
  }
  function requireOnline(){
    if(!A.remoteMode()) throw new Error("当前无法连接服务器，请稍后再试");
  }
  A.ACTIONS["save-liblib"] = function(){
    requireOnline();
    var f = collect("liblib-form");
    return A.remote.saveCredentials({liblib_access_key:f.access, liblib_secret_key:f.secret})
      .then(function(r){
        A._credTest = null;
        return Promise.resolve(A.refreshState()).then(function(){
          return afterCredentials(r && r.droppedSamples
            ? "已切换为真实生成，之前的示例画面已清除，请重新生成"
            : "已保存");
        });
      });
  };
  A.ACTIONS["save-llm"] = function(){
    requireOnline();
    var f = collect("llm-form");
    return A.remote.saveCredentials({llm_base_url:f.base, llm_model:f.model, llm_api_key:f.key})
      .then(function(){ A._credTest = null; return afterCredentials("已保存"); });
  };
  A.ACTIONS["save-tts"] = function(){
    requireOnline();
    var f = collect("tts-form");
    return A.remote.saveCredentials({tts_base_url:f.base, tts_model:f.model, tts_voice:f.voice, tts_api_key:f.key})
      .then(function(){ A._credTest = null; return afterCredentials("已保存"); });
  };
  A.ACTIONS["clear-credentials"] = function(el, d){
    requireOnline();
    var scope = d.scope === "llm" ? "llm" : (d.scope === "tts" ? "tts" : "liblib");
    var tips = {llm:"清除后，剧本生成等文本功能改用内置规则。",
                tts:"清除后，真实模式下成片不再包含配音。",
                liblib:"清除后，图片与视频改用示例素材，不再消耗你的 Liblib 积分。"};
    A.confirmModal("清除密钥", tips[scope],
      function(){
        A.remote.clearCredentials(scope).then(function(){
          A._credTest = null; return afterCredentials("已清除");
        }).catch(function(e){ A.toast(e.message || "操作失败"); });
      });
    return false;
  };
  A.ACTIONS["test-credentials"] = function(){
    requireOnline();
    A.toast("正在检测…");
    return A.remote.testCredentials().then(function(r){
      A._credTest = r || {}; A.render();
      var parts = [];
      if(r && r.liblib) parts.push(r.liblib.message);
      if(r && r.llm) parts.push(r.llm.message);
      if(r && r.tts) parts.push(r.tts.message);
      A.toast(parts.join("；") || "还没有填写任何密钥");
    });
  };
  A.ACTIONS["admin-credit"] = function(){
    A.openModal({title:"充值积分",
      body:A.fInput("积分数量", "amount", 1000, {type:"number",min:1})
        + A.fInput("备注", "reason", "充值")
        + '<input type="hidden" name="requestId" value="'+A.esc(A.uid("req"))+'">',
      submitLabel:"确认充值",
      onSubmit:function(f){ A.act.adminCredit(f.amount, f.reason, f.requestId); A.toast("充值成功"); }});
    return false;
  };
  A.ACTIONS["quote-preview"] = function(){
    var p = A.project(), n = p.shots.length;
    /* 单价一律取服务端 config.prices，不在前端写死：
       否则改一次定价就要同步改三处，必然漂移。 */
    function unit(act){ var v = A.priceOf(act); return v == null ? "…" : v; }
    var i2v = unit("image-to-video"), tts = unit("tts-preview"), film = unit("preview-film");
    var subs = ((p.audio && p.audio.subtitles) || []).length;
    var total = n*A.keyframePrice() + (n*Number(i2v)||0) + (subs*Number(tts)||0);
    A.openModal({title:"本集费用估算", submitLabel:null,
      body:A.kv([["关键帧 × "+n, (n*A.keyframePrice())+" 积分"],
                 ["视频 × "+n, (n*Number(i2v)||0)+" 积分"],
                 ["配音 × "+subs, (subs*Number(tts)||0)+" 积分"],
                 ["成片合成", (Number(film)||0) ? film+" 积分" : "免费"],
                 ["合计", "<b>"+total+" 积分</b>", true]])});
    return false;
  };
  A.ACTIONS["save-pricing"] = function(){
    var f = collect("pricing-form");
    /* 只做本地前置提示；真正的判据在服务端 savePricing（cap >= ceil(t2i × 1.2)），
       这里同判据是为了离线态不静默接受一个在线必然 400 的组合。 */
    var t2i = Math.max(0, Number(f.t2i)||0), capOf = Math.ceil(t2i * 12 / 10);
    var cap = (f.cap === "" || f.cap == null) ? capOf : Math.max(1, Number(f.cap)||1);
    if(cap < capOf) throw new Error("冻结上限不能低于 " + capOf);
    if(f.video !== "" && f.video != null && Number(f.video) > 0){ f.video = Number(f.video); }
    else if(f.video !== "" && f.video != null){ throw new Error("视频单价必须是正整数"); }
    else { f.video = null; }
    A.act.savePricing(f); A.toast("已保存");
  };
  /* ---------------- 生成动作（可能出网 / 扣积分） ----------------
     这些按钮在在线态一律等服务端返回再改界面：
     本地不预执行（否则本地占位会盖住真实结果），并且用 pending 拦住重复点击。
     所以这里的 ACTIONS 全部 async + try/catch，提示文案取自服务端返回的
     provider 字段，mock 就明说「本地规则」，绝不写成模型输出。 */
  function genGuard(fn){
    return async function(el, d){
      try { return await fn(el, d); }
      catch(e){
        if(e && e.busy) return;                       // 重复点击已在 store 层提示过
        A.toast((e && e.message) || "操作失败，请稍后重试");
      }
    };
  }

  /* 生成前提示是否会被使用上限拦下（真正的拦截在服务端）。 */
  function limitHint(price){
    var cv = A.creditView();
    if(cv.single !== null && price > cv.single) return "，超出你设置的单次上限 " + cv.single;
    if(cv.remaining !== null && price > cv.remaining) return "，超出剩余额度 " + cv.remaining;
    return "";
  }

  A.ACTIONS["script-doctor"] = genGuard(async function(){
    var r = await A.act["script-doctor"]();
    var n = ((r && r.findings) || []).length;
    A.toast("诊断完成，共 " + n + " 条建议");
  });

  A.ACTIONS["creative-start"] = genGuard(async function(el, d){
    var kind = (d && d.kind) || "idea";
    var label = {idea:"一句话生成剧本", novel:"小说改编", script:"导入剧本"}[kind] || kind;
    // 旧实现把 text 恒传空串，等于「输入框根本不存在」，用户没法给创作起点。
    // 这里补一个真实输入：一句话创意 / 粘贴长文 / 粘贴剧本正文。
    var ph = {idea:"例如：一封迟到十一年的信，把她带回了雾港",
              novel:"粘贴小说或长文内容", script:"粘贴已有剧本"}[kind] || "";
    A.openModal({
      title:label,
      body:A.fTextarea(kind === "idea" ? "你的创意" : "内容", "text", "", 8, ph),
      submitLabel:"生成剧本",
      onSubmit: async function(f){
        await A.act["creative-start"](kind, f.text || "");
        A.toast("剧本已生成");
      }
    });
    return false;
  });

  A.ACTIONS["extract-assets"] = genGuard(async function(){
    var r = await A.act["extract-assets"]();
    A.toast("已提取 " + ((r && r.added) || 0) + " 个角色 / 场景");
  });

  A.ACTIONS["resplit-shots-v2"] = genGuard(async function(){
    var run = async function(opts){
      var r = opts ? await A.act["resplit-shots-v2"](opts) : await A.act["resplit-shots-v2"]();
      A.toast("分镜已更新，共 " + ((r && r.count) || 0) + " 个镜头");
    };
    // 未锁定镜头会被整批替换。它们已生成的关键帧 / 视频是花过积分的，
    // 替换后找不回来，必须先让用户看到数量并确认（服务端同样会拒绝未确认的请求）。
    var drafts = A.project().shots.filter(function(x){ return x.state !== "locked"; });
    var produced = drafts.filter(function(x){ return x.videoUrl || x.imageUrl; });
    if(!produced.length) return run();
    var videos = produced.filter(function(x){ return x.videoUrl; }).length;
    A.openModal({
      title:"重新生成分镜",
      note:"将替换 " + drafts.length + " 个未锁定镜头，其中 " + produced.length
        + " 个已生成画面（含 " + videos + " 段视频），替换后无法找回，已消耗的积分不退还。"
        + "需要保留的镜头请先锁定。",
      submitLabel:"确认替换",
      onSubmit: genGuard(async function(){ await run({discardGenerated:true}); })
    });
    return false;
  });

  A.ACTIONS["gen-asset-candidates"] = genGuard(async function(el, d){
    var r = await A.act.generateAssetCandidates(d.type, d.id);
    var url = (r && r.image_url) || "";
    A.toast(url ? "形象图已生成" : "未生成图片，请稍后重试");
  });

  /* 试听有两个入口：音色行的「试听」(data-id=音色 id) 与字幕行的「按句试听」
     (data-subtitle=字幕 id，音色从字幕表头的下拉取)。第二个参数 subtitleId
     会让服务端把 audio_url 绑到那一句，合成时按该句 in 做 adelay 对齐。 */
  A.ACTIONS["tts-preview"] = genGuard(async function(el, d){
    var voiceId = (d && d.id) || "";
    var subId = (d && d.subtitle) || "";
    if(subId && !voiceId){
      var sel = document.getElementById("subtitle-voice");
      var voices = (A.project().audio.voices || []);
      voiceId = (sel && sel.value) || (voices[0] && voices[0].id) || "";
    }
    if(!voiceId){ A.toast("请先新增一个角色音色"); return; }
    var r = await A.act["tts-preview"](voiceId, subId);
    if(r && r.audio_url){
      A.toast("配音已生成");
    } else {
      A.toast("未生成音频，请稍后重试");
    }
  });

  A.ACTIONS["change-voice"] = genGuard(async function(el, d){
    var r = await A.act["change-voice"](d.id);
    var n = ((r && r.candidates) || []).length;
    A.toast(n ? "已切换音色" : "暂无可推荐的音色");
  });

  A.ACTIONS["mark-audio-done"] = genGuard(async function(){
    var r = await A.act["mark-audio-done"]();
    A.toast("配音已保存，共 " + ((r && r.audio_files) || 0) + " 段音频");
  });

  A.ACTIONS["preview-film"] = genGuard(async function(){
    var r = await A.act["preview-film"]();
    if(r && r.video_url){
      var cues = (r.voice_alignment || []).length;
      A.toast("成片已合成：" + (r.composed_clips || 0) + " 个片段 · " + A.voiceMixText(r.alignment, cues)
        + (A.shotsWithoutVideo(A.project())
            ? "；还有 " + A.shotsWithoutVideo(A.project()) + " 个镜头没有视频"
            : ""));
    } else {
      // 服务端字段是 composed_clips，不是 clips；只有它确实为 0 才是「没有视频轨」。
      // 回归：合成成功却提示「0 片段 / 0s」，就是因为在线态拿的是本地 {}。
      var clips = (r && (r.composed_clips || r.clips)) || 0;
      var secs = (r && r.duration_seconds) || 0;
      A.toast(clips
        ? "合成未完成，请稍后重试"
        : "还没有可合成的视频，请先为镜头生成视频");
    }
  });

  A.ACTIONS["image-to-video"] = genGuard(async function(el, d){
    var p = A.project();
    var shotId = d.id || (p.shots[0] && p.shots[0].id);
    if(!shotId){ A.toast("当前项目没有分镜"); return; }
    var shot = p.shots.filter(function(x){ return x.id === shotId; })[0];
    // 首帧图可以选一个已生成锚点图的资产：这是真实可用的图，不是占位。
    var withImage = [];
    ["characters","scenes","props"].forEach(function(k){
      (p.assets[k] || []).forEach(function(a){ if(a.imageUrl) withImage.push(a); });
    });
    var price = A.priceOf("image-to-video");
    A.openModal({
      title:"生成视频 · 镜头 " + (p.shots.indexOf(shot) + 1),
      body:'<div class="field"><label>参考形象</label><select name="assetId">'
        + '<option value="">自动匹配</option>'
        + withImage.map(function(a){
            return '<option value="'+A.esc(a.id)+'">'+A.esc(a.name)+'</option>';
          }).join("")
        + '</select></div>'
        + (price ? '<div class="hint">本次消耗 ' + price + ' 积分' + limitHint(price) + '</div>' : ''),
      submitLabel:"开始生成",
      onSubmit: async function(f){
        var r = await A.act["image-to-video"](shotId, f.assetId || "");
        A.toast(r && r.video_url ? "视频已生成" : "未生成视频，请稍后重试");
      }
    });
    return false;
  });

  /* 成片下载：只下载本服务签发的本地素材，不会去拉上游带签名的 7 天 URL。 */
  A.ACTIONS["download-film"] = function(el, d){
    var p = A.project();
    // 优先取项目级 film：lastResult 是单槽，会被下一次动作覆盖（实测：
    // 合成成功后再点一次剧本医生，导出就报「尚未合成成片」）。
    var url = (p.film && p.film.videoUrl)
      || (p.lastResult && p.lastResult.payload && p.lastResult.payload.video_url)
      || (p.lastResult && p.lastResult.video_url) || (d && d.url) || "";
    if(!url){ A.toast("还没有成片，请先合成"); return; }
    var a = document.createElement("a");
    a.href = url; a.download = (p.name || "film") + ".mp4";
    document.body.appendChild(a); a.click(); a.remove();
    A.toast("开始下载");
  };

  /* 导出记录现在指向真实文件（filmUrl），不再是纯留痕。
     两种模式都拒绝「没有文件」的导出，所以这里一定能拿到可下载地址。 */
  A.ACTIONS["download-export"] = function(el, d){
    var p = A.project();
    var ex = (p.exports || []).filter(function(x){ return x.id === (d && d.id); })[0];
    if(!ex){ A.toast("导出记录不存在"); return; }
    if(!ex.filmUrl){
      A.toast("该记录没有可下载的文件"); return;
    }
    var a = document.createElement("a");
    a.href = ex.filmUrl;
    a.download = String(ex.name || p.name || "film").replace(/[\\/:*?"<>|]/g, "_") + ".mp4";
    document.body.appendChild(a); a.click(); a.remove();
    A.toast("开始下载");
  };

  /* 复核「上游结果未知」的凭据。只有管理员能点（服务端也会 403，双保险）。
     这不是「重试」：它决定那笔冻结的钱算花了还是退回，必须由人看一眼上游账单。 */
  A.ACTIONS["resolve-charge"] = function(el, d){
    var outcome = d.outcome === "failed" ? "failed" : "succeeded";
    var text = outcome === "failed"
      ? "冻结的 " + d.cap + " 积分将全额退还。"
      : "将按价格扣费，多冻结的部分退还。";
    A.confirmModal(outcome === "failed" ? "确认退款" : "确认结算", text + "此操作不可撤销。", function(){
      A.resolveCharge(d.id, outcome)
        .then(function(){ A.toast(outcome === "failed" ? "已退款" : "已结算"); })
        .catch(function(e){ A.toast("复核未完成：" + ((e && e.message) || "未知错误")); });
    });
    return false;
  };

  A.ACTIONS["probe-backend"] = async function(){
    var ok = await A.probeServer();
    A.toast(ok ? "已连接" : "连接失败");
  };
  A.ACTIONS["set-offline"] = function(){ A.toast(A.setDatasource("offline")); };
  A.ACTIONS["sync-snapshot"] = async function(){
    try { var s = await A.syncServerSnapshot(); A.toast("快照完成：" + s.items.filter(function(x){return x.ok;}).length + " / " + s.items.length + " 个接口成功"); }
    catch(e){ A.toast(e.message); }
  };
  A.ACTIONS["export-data"] = async function(){
    var online = A.remoteMode(), text;
    if(online){
      try { text = JSON.stringify(await A.exportRemote(), null, 2); }
      catch(e){ A.toast("导出失败：" + (e.message || "未知错误")); return; }
    } else text = A.exportJSON();
    var blob = new Blob([text], {type:"application/json;charset=utf-8"});
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url; a.download = "drama-studio-" + Date.now() + ".json";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function(){ URL.revokeObjectURL(url); }, 1000);
    A.toast("已导出");
  };
  A.ACTIONS["import-data"] = function(){
    A.openModal({title:"导入数据",
      body:A.fTextarea("粘贴导出的数据", "json", "", 8), submitLabel:"导入",
      onSubmit:function(f){
        if(A.remoteMode()){
          var doc;
          try { doc = JSON.parse(f.json); }
          catch(e){ A.toast("数据格式不正确"); return; }
          A.importRemote(doc).then(function(){ A.toast("导入成功"); })
            .catch(function(e){ A.toast("导入失败：" + (e.message || "未知错误")); });
        } else {
          A.importJSON(f.json); A.toast("导入成功");
        }
      }});
    return false;
  };
  A.ACTIONS["reset-all"] = function(){
    A.confirmModal("重置全部数据", "所有项目、任务与流水将被清空，且无法恢复。", function(){
        doReset(false, "已重置");
        if(A.remoteMode()){ A.setDatasource("offline"); A.render(); }
      });
    return false;
  };

  /* ---------------- 事件绑定 ---------------- */
  function dispatch(act, el, data){
    var fn = A.ACTIONS[act];
    if(!fn) return;
    Promise.resolve()
      .then(function(){ return fn(el, data); })
      .then(function(result){
        if(result !== false) A.render();
      })
      .catch(function(e){ A.toast(e && e.message ? e.message : String(e)); });
  }

  A.init = function(){
    A.load();
    /* 首屏侧栏按宽度决定：桌面常驻、窄屏收起。不沿用上次会话的状态，
       否则在窄窗口里打开过一次，换到桌面也会一直是收起的。 */
    A.DB.ui.navClosed = A.isNarrow();
    if(A.pageFromHash()) A.DB.ui.page = A.pageFromHash();
    if(typeof window !== "undefined" && window.addEventListener){
      window.addEventListener("hashchange", function(){
        var id = A.pageFromHash();
        if(id && id !== A.DB.ui.page){ A.DB.ui.page = id; A.save(); A.render(); }
      });
    }
    document.addEventListener("click", function(e){
      var t = e.target;
      if(!t || !t.closest) return;
      var el = t.closest("[data-act]");
      if(!el) return;
      e.preventDefault();
      dispatch(el.getAttribute("data-act"), el, el.dataset);
    });
    document.addEventListener("change", function(e){
      if(e.target && e.target.id === "project-switch"){
        A.act.switchProject(e.target.value); A.render();
      }
    });
    document.addEventListener("submit", function(e){ e.preventDefault(); });
    document.addEventListener("keydown", function(e){
      if(e.key === "Escape"){ A.closeModal(); A.closeDrawer(); A.closeNav(); }
    });
    /* 跨断点时只重绘侧栏：拉宽到桌面要还原常驻，缩窄到抽屉要收起，都不影响主内容。
       用「断点状态是否翻转」做闸门，避免拖动窗口时每一帧都重绘。 */
    var narrow = A.isNarrow();
    window.addEventListener("resize", function(){
      var now = A.isNarrow();
      if(now === narrow) return;
      narrow = now;
      A.DB.ui.navClosed = now;
      A.renderNav();
    });
    A.render();
    if(inFlight().length) A.ensureTicker();
    /* 每秒更新「已等待」计时与顶部忙碌条；只改文字，不重绘页面。 */
    if(typeof setInterval === "function" && typeof document !== "undefined"){
      var waitTimer = setInterval(function(){
        A.tickWaits(document.getElementById("view"));
        var bar = document.getElementById("busybar");
        if(bar && A._busy && A._busy.length) bar.textContent = A.busyText();
      }, 1000);
      /* 在 Node 测试环境里不让这个计时器拖住进程退出（浏览器里没有 unref）。 */
      if(waitTimer && typeof waitTimer.unref === "function") waitTimer.unref();
    }
    /* 多标签页进度同步：只在这里启动一次。放在首屏渲染之后，
       这样即便上一个标签页已经推进到一半，也能从它接下来的广播里补上。 */
    A.startTabSync();
    /* 由后端同源提供时自动连接服务器，以服务端数据为准；
       探测失败（例如直接打开本地文件）会降级为离线模式。 */
    if(typeof location !== "undefined" && /^https?:$/.test(location.protocol)){
      A.probeServer().then(function(){ A.render(); });
    }
  };

  A.init();
})(App);
