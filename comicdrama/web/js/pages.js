/* pages.js — 页面。所有按钮只用 data-act，无内联脚本（满足 CSP）。 */
(function(A){
  "use strict";
  A.pages = {};

  /* ---------- 公共片段 ---------- */
  function projectTasks(pid){
    return A.DB.tasks.filter(function(t){ return t.projectId === pid; });
  }
  function shotLabel(p, id){
    var s = A.shotById(p, id);
    return s ? ("镜头 " + (p.shots.indexOf(s)+1)) : id;
  }
  function targetLabel(p, target){
    var t = String(target || "");
    if(!t) return "整个项目";
    var parts = t.split(":"), kind = parts[0], id = parts.slice(1).join(":");
    if(kind === "shot") return p ? shotLabel(p, id) : "镜头";
    if(kind === "asset" && p){
      var hit = null;
      ["characters","scenes","props"].forEach(function(k){
        (p.assets[k] || []).forEach(function(a){ if(a.id === id) hit = a; });
      });
      return hit ? hit.name : "资产";
    }
    return t;
  }
  function card(title, body, actions){
    return '<div class="card">'
      + (title ? '<div class="card-head"><h2>' + title + '</h2>'
          + (actions ? '<div class="btnbar tight">' + actions + '</div>' : '') + '</div>' : '')
      + body + '</div>';
  }
  function table(head, rows){
    return '<div class="table-wrap"><table><thead><tr>' + head + '</tr></thead><tbody>'
      + rows + '</tbody></table></div>';
  }
  function filmUrlOf(p){
    var last = (p.lastResult && p.lastResult.action === "preview-film" && p.lastResult.payload) || null;
    return (p.film && p.film.videoUrl) || (last && last.video_url) || "";
  }
  function totalDur(p){ return p.shots.reduce(function(a,b){ return a + Number(b.dur || 0); }, 0); }

  /* 六步完成度：由当前项目的真实数据算出。顶部步骤条与总览共用。 */
  A.stepStates = function(p){
    var pts = projectTasks(p.id);
    var assets = ["characters","scenes","props"].reduce(function(n,k){ return n + p.assets[k].length; },0);
    var lockedAssets = ["characters","scenes","props"].some(function(k){
      return p.assets[k].some(function(a){ return a.locked || a.imageUrl; });
    });
    var lockedShots = p.shots.filter(function(s){ return s.state === "locked"; }).length;
    var videos = p.shots.filter(function(s){ return s.videoUrl; }).length;
    var succeeded = pts.filter(function(t){ return t.state === "succeeded"; }).length;
    return [
      p.script.layers.body.trim() ? "done" : "todo",
      p.script.frozenVersionId ? "done" : (p.script.versions.length ? "doing" : "todo"),
      lockedAssets ? "done" : (assets ? "doing" : "todo"),
      (p.shots.length && lockedShots === p.shots.length) ? "done" : (p.shots.length ? "doing" : "todo"),
      (p.shots.length && videos === p.shots.length) ? "done" : ((succeeded || videos) ? "doing" : "todo"),
      p.exports.length ? "done" : (filmUrlOf(p) ? "doing" : "todo")
    ];
  };
  function stepBadge(st){
    return st === "done" ? '<span class="badge b-ok">已完成</span>'
         : st === "doing" ? '<span class="badge b-brand">进行中</span>'
         : '<span class="badge b-p2">未开始</span>';
  }

  /* BGM / 参考图上传面板。 */
  A.uploadPanel = function(kind){
    var bgm = kind === "bgm";
    var rows = A.uploads(kind);
    var accept = bgm
      ? "audio/mpeg,audio/wav,audio/ogg,audio/mp4,.mp3,.wav,.ogg,.m4a"
      : "image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp";
    var h = '<div class="field"><label>' + (bgm ? "音频文件" : "图片文件") + '</label>'
      + '<input id="upload-file-' + kind + '" type="file" accept="' + accept + '"></div>';
    if(!bgm){
      h += '<div class="grid g2"><div class="field"><label>类型</label><select id="upload-asset-type">'
        + '<option value="characters">角色</option><option value="scenes">场景</option>'
        + '<option value="props">道具</option></select></div>'
        + '<div class="field"><label>名称</label>'
        + '<input id="upload-asset-name" type="text" placeholder="默认使用文件名"></div></div>';
    }
    h += '<div class="btnbar tight"><button class="btn primary sm" data-act="upload-media" data-kind="'
      + kind + '">' + A.icon("upload") + (bgm ? "上传音乐" : "上传参考图") + '</button>'
      + '<span class="hint">' + A.esc(A.uploadLimitText(kind)) + '</span></div>';
    if(rows.length){
      h += '<div class="mt14">' + table('<th>文件</th><th class="num">大小</th><th>预览</th><th></th>',
        rows.map(function(x){
          var preview = bgm
            ? A.audioTile(x.url)
            : '<div class="w72">' + A.mediaTile(x.url, "image", 1, 72, 44, "", "") + '</div>';
          return '<tr><td>' + A.esc(x.name) + '</td>'
            + '<td class="num">' + Math.round(Number(x.bytes || 0) / 1024) + ' KB</td>'
            + '<td>' + preview + '</td>'
            + '<td><div class="row-actions">'
            + (bgm ? '<button class="btn sm" data-act="use-upload-bgm" data-id="' + A.esc(x.id) + '">设为背景音乐</button>' : '')
            + '<button class="btn sm danger" data-act="delete-upload" data-id="' + A.esc(x.id) + '">删除</button>'
            + '</div></td></tr>';
        }).join("")) + '</div>';
    }
    return card(bgm ? "上传音乐" : "上传参考图", h);
  };

  /* ---------- 1 项目总览 ---------- */
  A.pages.overview = function(p){
    var states = A.stepStates(p);
    var next = states.findIndex(function(s){ return s !== "done"; });
    if(next < 0) next = 5;
    var locked = p.shots.filter(function(s){ return s.state === "locked"; }).length;
    var videos = p.shots.filter(function(s){ return s.videoUrl; }).length;
    var doneSteps = states.filter(function(s){ return s === "done"; }).length;
    var notes = ["一句话、小说或已有剧本","打磨三层剧本并定稿","角色、场景与道具形象","逐镜拆分并锁定",
                 "生成关键帧与视频","配音、剪辑与导出"];
    var h = "";
    /* 首次使用的引导：只在用户还没点过关掉时出现。关闭标记写在前端 ui 状态里
       （服务端没有这个字段），关掉后必须重绘一次才会消失。
       步骤名直接取 A.STEPS，免得改流程时这里和顶部步骤条对不上。 */
    if(A.DB.ui && !A.DB.ui.onboardDismissed){
      var guide = [
        {t:"写剧本", sub:A.STEPS[0].name + " · " + A.STEPS[1].name,
         d:"一句话、一段小说或已有剧本都能起稿，定稿后进入下一步。"},
        {t:"提取角色", sub:A.STEPS[2].name,
         d:"从剧本里抽出角色、场景与道具，逐个确认形象。"},
        {t:"拆分分镜", sub:A.STEPS[3].name,
         d:"逐镜拆分并锁定，锁定后就能生成关键帧与视频。"},
        {t:"生成画面与成片", sub:A.STEPS[4].name + " · " + A.STEPS[5].name,
         d:"关键帧、视频、配音、剪辑，最后合成并导出。"}
      ];
      h += card("第一次使用？按这四步走",
        '<div class="guide">' + guide.map(function(g, i){
          return '<div class="guide-step"><span class="n">' + (i + 1) + '</span>'
            + '<div><b>' + A.esc(g.t) + '</b><small>' + A.esc(g.sub) + '</small>'
            + '<p>' + A.esc(g.d) + '</p></div></div>';
        }).join("") + '</div>'
        + '<div class="hint mt8">每步右侧都有说明；完成一步会自动推进。</div>',
        '<button class="btn primary sm" data-act="onboard-dismiss">知道了，开始创作</button>');
    }
    h += '<div class="hero"><div><h1>' + A.esc(p.name) + '</h1>'
      + '<p>第 ' + p.season + ' 季 · 第 ' + p.episode + ' 集 · ' + A.esc(p.episodeTitle) + ' · '
      + A.esc(p.style) + ' · ' + A.esc(p.ratio) + '</p>'
      + '<div class="meta"><div><b>' + doneSteps + ' / 6</b>完成步骤</div>'
      + '<div><b>' + p.shots.length + '</b>分镜</div>'
      + '<div><b>' + totalDur(p) + 's</b>时长</div>'
      + '<div><b>' + p.exports.length + '</b>成片</div></div></div>'
      + '<button class="btn" data-act="go-step" data-n="' + (next + 1) + '">继续创作：'
      + A.esc(A.STEPS[next].name) + '</button></div>';
    h += '<div class="grid g4">'
      + '<div class="stat"><div class="l">' + A.icon("grid") + '分镜锁定</div><div class="v">' + locked + ' / ' + p.shots.length + '</div>'
      + '<div class="prog mt8"><i data-w="' + (p.shots.length ? locked / p.shots.length * 100 : 0) + '"></i></div></div>'
      + '<div class="stat"><div class="l">' + A.icon("film") + '视频镜头</div><div class="v">' + videos + ' / ' + p.shots.length + '</div>'
      + '<div class="prog mt8"><i data-w="' + (p.shots.length ? videos / p.shots.length * 100 : 0) + '"></i></div></div>'
      + '<div class="stat"><div class="l">' + A.icon("users") + '角色与场景</div><div class="v">'
      + (p.assets.characters.length + p.assets.scenes.length + p.assets.props.length) + '</div>'
      + '<div class="hint">角色 ' + p.assets.characters.length + ' · 场景 ' + p.assets.scenes.length + ' · 道具 ' + p.assets.props.length + '</div></div>'
      + '<div class="stat"><div class="l">' + A.icon("wallet") + '可用积分</div><div class="v">' + A.DB.ledger.balance + '</div>'
      + '<div class="hint">冻结 ' + A.DB.ledger.frozen + '</div></div>'
      + '</div><div class="gap16"></div>';
    h += card("创作流程", '<div class="grid g6">' + A.STEPS.map(function(st, i){
      return '<div class="flowstep' + (states[i] === "done" ? " done" : "") + '" data-act="go-step" data-n="' + st.n + '">'
        + '<span class="n">' + (states[i] === "done" ? "✓" : st.n) + '</span>' + stepBadge(states[i])
        + '<b>' + A.esc(st.name) + '</b><p>' + A.esc(notes[i]) + '</p></div>';
    }).join("") + '</div>');
    var film = filmUrlOf(p);
    if(film){
      h += card("最新成片", '<div class="player"><video src="' + A.esc(A.frameUrl(film)) + '" controls preload="metadata"></video></div>',
        '<button class="btn sm" data-act="download-film">' + A.icon("download") + '下载</button>'
        + '<button class="btn sm primary" data-act="nav" data-page="qa">审片与导出</button>');
    }
    return h;
  };

  /* ---------- 2 我的项目 ---------- */
  A.pages.projects = function(p){
    var live = A.activeProjects();
    var h = A.pageHead("我的项目", "共 " + live.length + " 个项目",
      '<button class="btn primary" data-act="new-project">' + A.icon("plus") + '新建项目</button>');
    if(!live.length){ h += A.empty('还没有项目，点击右上角「新建项目」开始创作。', "folder"); }
    else {
      h += '<div class="grid g3">';
      live.forEach(function(x, i){
        var ts = projectTasks(x.id);
        var cover = x.shots.filter(function(s){ return s.imageUrl; })[0];
        var lockedN = x.shots.filter(function(s){ return s.state === "locked"; }).length;
        h += '<div class="asset">'
          + A.mediaTile(cover ? cover.imageUrl : "", "image", (i + 3) * 11, 300, 132, "", "")
          + '<div class="meta"><div class="title"><b>' + A.esc(x.name) + '</b>'
          + (x.id === p.id ? '<span class="badge b-brand">当前</span>' : '') + '</div>'
          + '<small>第 ' + x.season + ' 季 · 第 ' + x.episode + ' 集 · ' + A.esc(x.episodeTitle) + '</small>'
          + '<div class="kv"><span>画风</span><b>' + A.esc(x.style) + ' · ' + A.esc(x.ratio) + '</b></div>'
          + '<div class="kv"><span>分镜</span><b>' + lockedN + ' / ' + x.shots.length + ' 已锁定</b></div>'
          + '<div class="kv"><span>任务</span><b>' + ts.length + '</b></div>'
          + '<div class="kv"><span>创建于</span><b>' + A.esc(x.createdAt) + '</b></div>'
          + '<div class="btnbar">'
          + (x.id === p.id ? '<button class="btn sm primary" data-act="go-step" data-n="1">打开</button>'
                           : '<button class="btn sm primary" data-act="switch-project" data-id="' + A.esc(x.id) + '">切换</button>')
          + '<button class="btn sm" data-act="copy-project" data-id="' + A.esc(x.id) + '">复制</button>'
          + '<button class="btn sm" data-act="archive-project" data-id="' + A.esc(x.id) + '">归档</button>'
          + '<button class="btn sm danger" data-act="delete-project" data-id="' + A.esc(x.id) + '">删除</button>'
          + '</div></div></div>';
      });
      h += '</div><div class="gap16"></div>';
    }
    var archived = A.DB.projects.filter(function(x){ return x.archived; });
    if(archived.length){
      h += card("已归档（" + archived.length + "）", table('<th>项目</th><th>创建时间</th><th></th>',
        archived.map(function(x){
          return '<tr><td>' + A.esc(x.name) + '</td><td>' + A.esc(x.createdAt) + '</td>'
            + '<td><div class="row-actions"><button class="btn sm" data-act="unarchive-project" data-id="' + A.esc(x.id) + '">恢复</button>'
            + '<button class="btn sm danger" data-act="delete-project" data-id="' + A.esc(x.id) + '">删除</button></div></td></tr>';
        }).join("")));
    }
    return h;
  };

  /* ---------- 3 项目设定 ---------- */
  A.pages.settings = function(p){
    var h = A.pageHead("项目设定", "",
      '<button class="btn" data-act="restore-defaults">恢复默认</button>'
      + '<button class="btn primary" data-act="save-settings">保存</button>');
    h += '<div class="split"><div class="card"><form id="settings-form"><div class="grid g2">'
      + A.fInput("项目名称", "name", p.name)
      + A.fInput("分集标题", "episodeTitle", p.episodeTitle)
      + A.fSelect("题材", "genre", ["都市悬疑","古风","校园","科幻","家庭伦理"], p.genre)
      + A.fSelect("画风", "style", ["精致国漫","2D 日式动漫","仿真人写实","复古武侠"], p.style)
      + A.fSelect("画面比例", "ratio", ["9:16 竖屏","16:9 横屏"], p.ratio)
      + A.fSelect("输出规格", "res", ["1080 × 1920 / 24fps","720 × 1280 / 24fps","480P 样片（省积分）"], p.res)
      /* 字幕与分辨率上限是交付格式，不是画面风格：改它只影响成片，不改分镜。
         subtitleMode = soft 走软字幕轨（播放器可关，不重新编码）；burn 烧进画面。 */
      + A.fSelect("成片字幕", "subtitleMode", [
          {value:"soft", label:"软字幕（播放器可关，不重新编码）"},
          {value:"burn", label:"烧进画面（关不掉，需要重新编码）"}
        ], p.subtitleMode || "soft")
      + A.fSelect("成片分辨率上限", "outputResolution", [
          {value:"source", label:"保持素材原尺寸"},
          {value:"720p", label:"720p（只缩不放）"},
          {value:"1080p", label:"1080p（只缩不放）"}
        ], p.outputResolution || "source")
      + A.fInput("集数", "eps", p.eps, {type:"number",min:1})
      + A.fInput("单集时长（秒）", "duration", p.duration, {type:"number",min:1})
      + A.fInput("预算上限（积分）", "budget", p.budget, {type:"number",min:1})
      + '</div>'
      /* 只缩不放是刻意的：示例素材长边本来就 720（9:16 实际 405×720），
         选 1080p 也不会变大。放大只会用假分辨率骗人，所以代码不做上采样。 */
      + '<div class="hint">分辨率只向下压、不放大：当前示例素材长边只有 720，所以选 720p 不会变化、'
      + '选 1080p 也不会变大。要看到 1080p 效果需要素材本身达到该尺寸。</div>'
      + '</form></div>';
    h += card("项目概况", A.kv([
      ["剧本", p.script.frozenVersionId ? "已定稿" : "未定稿"],
      ["分镜锁定", p.shots.filter(function(s){ return s.state === "locked"; }).length + " / " + p.shots.length],
      ["待更新镜头", String(p.shots.filter(function(s){ return s.dirty; }).length)],
      ["成片", p.exports.length + " 条"]])) + '</div>';
    return h;
  };

  /* ---------- 4 剧本 ---------- */
  A.pages.script = function(p){
    var s = p.script, frozen = s.versions.filter(function(v){ return v.id === s.frozenVersionId; })[0];
    var h = A.pageHead("剧本", frozen ? ("当前定稿：" + frozen.label) : "",
      '<button class="btn" data-act="new-script-version">存为新版本</button>'
      + '<button class="btn" data-act="freeze-script">' + (frozen ? "重新定稿" : "定稿") + '</button>'
      + '<button class="btn primary" data-act="save-script">保存</button>');
    if(s.dirtySinceFreeze){
      h += '<div class="banner"><div>剧本在定稿后有改动，已锁定的分镜可能需要更新。</div>'
        + '<button class="btn sm" data-act="mark-dirty-script">标记受影响镜头</button></div>';
    }
    h += '<div class="split-wide"><div class="card"><form id="script-form">'
      + A.fTextarea("故事梗概", "logline", s.layers.logline, 3)
      + A.fTextarea("分集大纲", "outline", s.layers.outline, 4)
      + A.fTextarea("剧本正文", "body", s.layers.body, 14)
      + '</form></div><div>';
    h += card("AI 创作", '<div class="stack">'
      + [["一句话生成剧本","输入一个创意，生成完整剧本","idea"],
         ["小说改编","粘贴小说或长文，改编为剧本","novel"],
         ["导入剧本","以已有剧本为基础继续创作","script"]].map(function(x){
        return '<div class="pick-row">'
          + '<div><b>' + A.esc(x[0]) + '</b><div class="hint">' + A.esc(x[1]) + '</div></div>'
          + A.genBtn("开始", "creative-start", ' data-kind="' + A.esc(x[2]) + '"', "sm") + '</div>';
      }).join("") + '</div>');
    var last = p.lastResult || {};
    var lr = last.action === "script-doctor" ? last : ((last.byAction || {})["script-doctor"] || {});
    var doctor = "";
    if(lr.payload){
      var pl = lr.payload || {};
      doctor = (pl.findings && pl.findings.length
          ? '<ul class="list">'
            + pl.findings.map(function(x){ return '<li>' + A.esc(x) + '</li>'; }).join("") + '</ul>'
          : '<div class="hint">未发现明显问题。</div>')
        + '<div class="hint mt8">' + A.esc(lr.at || "") + '</div>';
    } else {
      doctor = '<div class="hint">检查剧本结构、节奏与人物出场。</div>';
    }
    h += card("剧本诊断", doctor, A.genBtn("开始诊断", "script-doctor", "", "sm"));
    h += card("版本记录", !s.versions.length ? '<div class="hint">暂无版本</div>'
      : s.versions.map(function(v){
          return '<div class="kv center"><span><b class="strong">' + A.esc(v.label) + '</b>'
            + (v.id === s.frozenVersionId ? ' <span class="badge b-ok">定稿</span>' : '')
            + '<br><small>' + A.esc(v.createdAt) + (v.note ? ' · ' + A.esc(v.note) : '') + '</small></span>'
            + '<div class="row-actions"><button class="btn sm ghost" data-act="rollback-script" data-id="' + A.esc(v.id) + '">恢复</button>'
            + (v.id === s.frozenVersionId ? '' : '<button class="btn sm ghost" data-act="freeze-version" data-id="' + A.esc(v.id) + '">定稿</button>')
            + '</div></div>';
        }).join(""),
      '<button class="btn sm" data-act="diff-versions">对比版本</button>');
    h += '</div></div>';
    return h;
  };

  /* ---------- 5 角色与场景 ---------- */
  A.pages.assets = function(p){
    var tab = A.DB.ui.assetTab, type = A.ASSET_TYPES.filter(function(x){ return x.key === tab; })[0];
    var h = A.pageHead("角色与场景", "",
      A.genBtn("从剧本提取", "extract-assets", "", "")
      + '<button class="btn primary" data-act="add-asset" data-tab="' + tab + '">' + A.icon("plus") + '新增' + A.esc(type.name) + '</button>');
    h += '<div class="tabs">' + A.ASSET_TYPES.map(function(x){
      return '<button class="tab ' + (tab === x.key ? "on" : "") + '" data-act="asset-tab" data-tab="' + x.key + '">'
        + A.esc(x.name) + ' · ' + A.assetByType(p, x.key).length + '</button>';
    }).join("") + '</div>';
    var list = A.assetByType(p, tab);
    if(!list.length) h += A.empty('还没有' + A.esc(type.name) + '，可以手动新增或从剧本提取。', "users");
    else {
      h += '<div class="grid g3">';
      list.forEach(function(a){
        var refs = A.refCount(p, tab, a.name);
        var busy = A.isBusy(p.id, "asset", a.id);
        h += '<div class="asset">'
          + A.mediaTile(a.imageUrl || "", "image", a.seed, 300, 170, "")
          + '<div class="meta"><div class="title"><b>' + A.esc(a.name) + '</b>'
          + (busy ? A.pill("running") : (a.locked ? '<span class="badge b-ok">已锁定</span>' : '<span class="badge b-p2">草稿</span>'))
          + '</div><small>' + A.esc(a.sub || "") + '</small>'
          + '<div class="hint">' + refs + ' 个镜头引用' + (a.voice && a.voice !== "—" ? ' · ' + A.esc(a.voice) : '') + '</div>'
          + '<div class="btnbar">'
          + A.genBtn(a.imageUrl ? "重新生成" : "生成形象", "gen-asset-candidates",
              ' data-type="' + A.esc(tab) + '" data-id="' + A.esc(a.id) + '"' + (busy ? ' disabled' : ''), "sm primary")
          + '<button class="btn sm" data-act="edit-asset" data-type="' + tab + '" data-id="' + A.esc(a.id) + '">编辑</button>'
          + '<button class="btn sm" data-act="toggle-asset-lock" data-type="' + tab + '" data-id="' + A.esc(a.id) + '">' + (a.locked ? "解锁" : "锁定") + '</button>'
          + '<button class="btn sm ghost danger" data-act="delete-asset" data-type="' + tab + '" data-id="' + A.esc(a.id) + '">删除</button>'
          + '</div></div></div>';
      });
      h += '</div><div class="gap16"></div>';
    }
    h += A.uploadPanel("reference");
    return h;
  };

  /* ---------- 6 分镜 ---------- */
  A.pages.storyboard = function(p){
    var warns = A.continuityWarnings(p);
    var h = A.pageHead("分镜", p.shots.length + " 个镜头 · 共 " + totalDur(p) + " 秒",
      '<button class="btn" data-act="resplit-shots">按剧本更新</button>'
      + A.genBtn("AI 重新分镜", "resplit-shots-v2", "", "")
      + '<button class="btn primary" data-act="add-shot">' + A.icon("plus") + '新增镜头</button>');
    if(warns.length) h += A.banner(warns.map(A.esc).join("<br>"));
    if(!p.shots.length){ h += A.empty('还没有分镜，可以新增镜头，或在剧本完成后使用 AI 分镜。', "grid"); return h; }
    h += '<div class="card flush">' + '<div class="scroll-x"><table><thead><tr>'
      + '<th class="num">#</th><th>时长</th><th>景别</th><th>画面</th><th>台词</th><th>运镜</th><th>角色 / 场景</th><th>状态</th><th></th>'
      + '</tr></thead><tbody>';
    p.shots.forEach(function(s, i){
      var sel = A.DB.ui.selectedShot === s.id;
      var busyShot = A.isBusy(p.id, "shot", s.id);
      h += '<tr' + (sel ? ' class="sel"' : '') + '>'
        + '<td class="num"><button class="linklike" data-act="select-shot" data-id="' + A.esc(s.id) + '">' + (i + 1) + '</button></td>'
        + '<td class="nowrap">' + s.dur + 's</td><td class="nowrap">' + A.esc(s.size) + '</td>'
        + '<td class="mw200">' + A.esc(s.desc) + '</td>'
        + '<td class="hint mw120">' + A.esc(s.line || "—") + '</td><td class="nowrap">' + A.esc(s.move) + '</td>'
        + '<td>' + s.refs.map(function(r){ return '<span class="tag">' + A.esc(String(r).replace(/^@[^:]*:/, "")) + '</span>'; }).join("") + '</td>'
        + '<td>' + (busyShot ? A.pill("running") : A.pill(s.state)) + '</td>'
        + '<td><div class="row-actions nowrap">'
        + '<button class="btn sm" data-act="edit-shot" data-id="' + A.esc(s.id) + '">编辑</button>'
        + '<button class="btn sm" data-act="toggle-shot-lock" data-id="' + A.esc(s.id) + '">' + (s.state === "locked" ? "解锁" : "锁定") + '</button>'
        + '<button class="btn sm ghost" data-act="move-shot" data-id="' + A.esc(s.id) + '" data-dir="-1" title="上移">↑</button>'
        + '<button class="btn sm ghost" data-act="move-shot" data-id="' + A.esc(s.id) + '" data-dir="1" title="下移">↓</button>'
        + '<button class="btn sm ghost danger" data-act="delete-shot" data-id="' + A.esc(s.id) + '">删除</button>'
        + '</div></td></tr>';
    });
    h += '</tbody></table></div></div>';
    var s = A.shotById(p, A.DB.ui.selectedShot) || p.shots[0];
    if(s){
      var idx = p.shots.indexOf(s);
      var media = s.videoUrl ? A.mediaTile(s.videoUrl, "video", (idx + 1) * 7, 300, 220, "")
        : A.mediaTile(s.imageUrl || "", "image", (idx + 1) * 7, 300, 220, "");
      h += '<div class="split-wide"><div class="card"><div class="card-head"><h2>镜头 ' + (idx + 1) + '</h2>'
        + '<div class="btnbar tight"><button class="btn sm" data-act="insert-shot-before" data-id="' + A.esc(s.id) + '">在前面插入</button>'
        + '<button class="btn sm" data-act="edit-shot" data-id="' + A.esc(s.id) + '">编辑</button>'
        + '<button class="btn sm primary" data-act="goto-generate-shot" data-id="' + A.esc(s.id) + '">生成画面</button></div></div>'
        + '<div class="grid g2"><div>'
        + A.kv([["时长", s.dur + " 秒"], ["景别", s.size], ["运镜", s.move],
                ["状态", A.isBusy(p.id, "shot", s.id) ? "生成中" : (s.state === "locked" ? "已锁定" : (s.dirty ? "待更新" : "草稿"))]])
        + '</div><div>'
        + A.kv([["画面", s.desc], ["提示词", s.prompt || "—"], ["台词", s.line || "—"],
                ["角色 / 场景", s.refs.length ? s.refs.join(" ") : "—"]])
        + '</div></div></div>'
        + card("画面预览", media) + '</div>';
    }
    return h;
  };

  /* ---------- 7 画面生成 ---------- */
  A.pages.generate = function(p){
    var pts = projectTasks(p.id);
    var running = pts.filter(function(t){ return t.state === "queued" || t.state === "running"; });
    var busy = A.inFlightCharges(p.id);
    var ready = p.shots.filter(function(s){ return s.state === "locked" || s.state === "dirty"; }).length;
    var videos = p.shots.filter(function(s){ return s.videoUrl; }).length;
    var t2i = A.keyframePrice();
    /* 候选张数放在提交按钮旁边而不是藏进弹窗：多候选是按张计费的，用户点之前
       就该看到自己要选几张。下拉单独包一层 form，处理侧按表单名收集这个值。 */
    var h = A.pageHead("画面生成", "",
      '<button class="btn primary" data-act="submit-tasks">' + A.icon("sparkle") + '生成全部关键帧'
      + (t2i ? '<span class="cost">' + t2i + ' / 镜</span>' : '') + '</button>'
      + '<button class="btn" data-act="submit-video-tasks">' + A.icon("film") + '生成全部视频</button>'
      + '<form id="generate-batch-form" class="head-field">'
      + A.fSelect("候选张数", "task-candidates", [
          {value:"1", label:"1 张（默认）"}, {value:"2", label:"2 张"},
          {value:"3", label:"3 张"}, {value:"4", label:"4 张"}
        ], "1")
      + '</form>');
    h += '<div class="grid g4">'
      + '<div class="stat"><div class="l">可生成镜头</div><div class="v">' + ready + '</div></div>'
      + '<div class="stat"><div class="l">进行中</div><div class="v">' + (running.length + busy.length) + '</div></div>'
      + '<div class="stat"><div class="l">已有视频</div><div class="v">' + videos + ' / ' + p.shots.length + '</div></div>'
      + '<div class="stat"><div class="l">冻结积分</div><div class="v">' + A.DB.ledger.frozen + '</div></div>'
      + '</div><div class="gap16"></div>';
    if(busy.length){
      h += '<div class="banner info"><div>正在生成，请勿重复提交，完成后会自动更新：'
        + busy.map(function(c){
            return '<div class="mt8"><b>' + A.esc(A.actionLabel(c.action)) + '</b>（'
              + A.esc(targetLabel(p, c.target)) + '）<span class="hint" data-since="'
              + A.esc(String(c.createdAt || 0)) + '" data-action="' + A.esc(c.action) + '"></span></div>';
          }).join("") + '</div></div>';
    }
    if(!p.shots.length){ h += A.empty('当前项目还没有分镜，请先完成分镜并锁定镜头。', "grid"); return h; }
    h += '<div class="section-title"><h2>镜头</h2></div><div class="grid g4">';
    p.shots.forEach(function(s, i){
      var t = pts.filter(function(x){ return x.shotId === s.id && x.state !== "released"; }).slice(-1)[0];
      var busyShot = A.isBusy(p.id, "shot", s.id);
      var st = busyShot ? "running" : (s.videoUrl ? "succeeded" : (t ? t.state : s.state));
      h += '<div class="asset">'
        + (s.videoUrl ? A.mediaTile(s.videoUrl, "video", (i + 1) * 7 + 2, 300, 168, "")
           : A.mediaTile(s.imageUrl || "", "image", (i + 1) * 7 + 2, 300, 168, ""))
        + '<div class="meta"><div class="title"><b>镜头 ' + (i + 1) + '</b>' + A.pill(st) + '</div>'
        + '<small>' + A.esc(s.size) + ' · ' + s.dur + 's · ' + A.esc(s.desc.slice(0, 22)) + '</small>';
      if(s.auditRejected){
        /* 判违规的产物已被摘掉，这里必须说明原因，否则看起来只是「没生成」。
           退款部署下产物已被摘掉；结算部署下产物保留、钱照扣，此时绝不能说
           「已全额退回」——那是对用户陈述一笔并未发生的退款。 */
        h += '<small class="hint">' + (s.videoUrl
          ? '这次生成未通过审核，本次已按规则结算扣费；如需可用请修改后重新生成'
          : '这次生成未通过审核，积分已全额退回；可修改后重试') + '</small>';
      }
      if(st === "queued" || st === "running"){
        var pct = t ? Number(t.percent || 0) : (busyShot ? 100 : 0);
        h += '<div class="prog"><i data-w="' + pct + '"></i></div>';
        var flying = A.inFlightCharges(p.id).filter(function(c){ return c.target === "shot:" + s.id; })[0];
        if(flying){
          h += '<small class="hint" data-since="' + A.esc(String(flying.createdAt || 0))
            + '" data-action="' + A.esc(flying.action) + '"></small>';
        } else if(t && t.state === "queued"){
          h += '<small class="hint">排队中</small>';
        }
      }
      /* 只有多于一张候选才值得让人挑。旧条件是「任务有候选且镜头还没有图」——
         结算时已经把选中的那张写进 imageUrl，于是这段在在线/离线服务端
         模式下永远不成立，用户根本点不到「选这张」。 */
      if(t && t.frames && t.frames.length > 1){
        h += '<div class="hint">点一张选定为本镜关键帧，选定后仍可改</div>';
        h += '<div class="frames">';
        t.frames.forEach(function(f, k){
          var picked = t.picked === k;
          /* 候选是真的图片地址（服务端回 image_urls），必须按地址渲染。
             原来这里喂给 A.art() 当种子，画出来是一组渐变色块，三张候选长得一样，
             用户根本看不出选了哪张。 */
          h += '<div class="frame' + (picked ? ' picked' : '') + '" '
            + 'data-act="pick-frame" data-id="' + A.esc(t.id) + '" data-index="' + k + '">'
            + A.mediaTile(f, "image", k + 3, 80, 44, "") + '</div>';
        });
        h += '</div>';
      }
      /* 镜头产物历史：重新生成会换掉视频/关键帧，但旧的那张仍然可用，
         这里给一条可以横向翻的带子让用户按「用这张」退回去。 */
      var hist = s.mediaHistory || [];
      if(hist.length){
        var shown = hist.slice(0, 6);
        h += '<div class="hist"><div class="hist-label">历史产物</div><div class="hist-row">';
        shown.forEach(function(entry, k){
          var inUse = !!entry.url && (entry.url === s.videoUrl || entry.url === s.imageUrl);
          h += '<div class="hist-item' + (inUse ? ' picked' : '') + '">'
            + A.mediaTile(entry.url, entry.kind, k + 5, 72, 40, "")
            + (inUse ? '<span class="hist-tag">当前</span>'
                     : '<button class="btn sm ghost hist-btn" data-act="restore-media" data-id="' + A.esc(s.id)
                       + '" data-url="' + A.esc(entry.url) + '">用这张</button>')
            + '</div>';
        });
        h += '</div>';
        if(hist.length > shown.length){
          h += '<div class="hint">另有 ' + (hist.length - shown.length) + ' 条</div>';
        }
        h += '</div>';
      }
      h += '<div class="btnbar">'
        + A.genBtn(s.videoUrl ? "重新生成视频" : "生成视频", "image-to-video",
            ' data-id="' + A.esc(s.id) + '"' + (busyShot ? ' disabled' : ''), "sm primary")
        + (t && t.settled ? '<button class="btn sm" data-act="reroll-task" data-id="' + A.esc(t.id) + '">重绘关键帧</button>' : '')
        + (t && t.state === "failed" ? '<button class="btn sm" data-act="retry-task" data-id="' + A.esc(t.id) + '">重试</button>' : '')
        + (t && t.state === "held" ? '<button class="btn sm" data-act="release-held" data-id="' + A.esc(t.id) + '">释放</button>' : '')
        + '</div></div></div>';
    });
    h += '</div><div class="gap16"></div>';
    var film = filmUrlOf(p);
    h += card("成片", film
        ? '<div class="player"><video src="' + A.esc(A.frameUrl(film)) + '" controls preload="metadata"></video></div>'
        : '<div class="hint">所有镜头生成视频后即可合成成片。</div>',
      A.genBtn("合成成片", "preview-film", "", "sm primary")
      + '<button class="btn sm" data-act="download-film">' + A.icon("download") + '下载</button>');
    return h;
  };

  /* ---------- 8 配音与字幕 ---------- */
  A.pages.audio = function(p){
    var h = A.pageHead("配音与字幕", "", A.genBtn("保存配音", "mark-audio-done", "", ""));
    var voices = !p.audio.voices.length ? A.empty('还没有配置角色音色。', "mic")
      : table('<th>角色</th><th>音色</th><th>试听</th><th class="num">台词</th><th></th>',
          p.audio.voices.map(function(v){
            return '<tr><td class="nowrap"><b>' + A.esc(v.character) + '</b></td><td class="nowrap">' + A.esc(v.voice) + '</td>'
              + '<td>' + A.audioTile(v.audio_url) + '</td><td class="num">' + v.lines + '</td>'
              + '<td><div class="row-actions">'
              + A.genBtn("试听", "tts-preview", ' data-id="' + A.esc(v.id) + '"')
              + A.genBtn("换音色", "change-voice", ' data-id="' + A.esc(v.id) + '"')
              + '<button class="btn sm ghost danger" data-act="delete-voice" data-id="' + A.esc(v.id) + '">删除</button>'
              + '</div></td></tr>';
          }).join(""));
    h += card("角色音色", voices, '<button class="btn sm" data-act="add-voice">' + A.icon("plus") + '新增音色</button>');
    var subs;
    if(!p.audio.subtitles.length) subs = A.empty('还没有字幕。', "file");
    else {
      subs = '<div class="field mw320"><label>配音音色</label><select id="subtitle-voice">'
        + p.audio.voices.map(function(v){
            return '<option value="' + A.esc(v.id) + '">' + A.esc(v.character) + ' · ' + A.esc(v.voice) + '</option>';
          }).join("") + '</select></div>'
        + table('<th>时间</th><th>字幕</th><th>配音</th><th>校对</th><th></th>',
          p.audio.subtitles.map(function(s){
            return '<tr><td class="mono nowrap">' + A.esc(s["in"]) + ' – ' + A.esc(s.out) + '</td>'
              + '<td class="mw200">' + A.esc(s.text) + '</td>'
              + '<td>' + (s.audio_url ? A.audioTile(s.audio_url) : '<span class="hint">未配音</span>') + '</td>'
              + '<td>' + (s.checked ? '<span class="badge b-ok">已校对</span>' : '<span class="badge b-p2">未校对</span>') + '</td>'
              + '<td><div class="row-actions nowrap">'
              + A.genBtn(s.audio_url ? "重新配音" : "配音", "tts-preview", ' data-subtitle="' + A.esc(s.id) + '"')
              + '<button class="btn sm" data-act="toggle-subtitle" data-id="' + A.esc(s.id) + '">' + (s.checked ? "取消校对" : "校对") + '</button>'
              + '<button class="btn sm ghost danger" data-act="delete-subtitle" data-id="' + A.esc(s.id) + '">删除</button></div></td></tr>';
          }).join(""));
    }
    h += card("字幕与逐句配音", subs, '<button class="btn sm" data-act="add-subtitle">' + A.icon("plus") + '新增字幕</button>');
    h += '<div class="grid g2"><div>';
    h += card("背景音乐与混音", '<form id="bgm-form">'
        + A.fInput("背景音乐", "name", p.audio.bgm.name)
        + '<div class="grid g2">'
        + A.fInput("人声音量（%）", "voiceLevel", p.audio.bgm.voiceLevel, {type:"number",min:0})
        + A.fInput("音乐音量（%）", "bgmLevel", p.audio.bgm.bgmLevel, {type:"number",min:0})
        + '</div>'
        + A.fInput("环境音", "ambient", p.audio.bgm.ambient)
        + '</form>'
        + (p.audio.bgm.url ? '<div class="field"><label>当前音乐</label>' + A.audioTile(p.audio.bgm.url) + '</div>' : '')
        + '<div class="btnbar tight"><button class="btn primary sm" data-act="save-bgm">保存</button></div>');
    h += '</div><div>' + A.uploadPanel("bgm") + '</div></div>';
    return h;
  };

  /* ---------- 9 剪辑 ---------- */
  A.pages.timeline = function(p){
    var order = p.timeline.order.filter(function(id){ return A.shotById(p, id); });
    var h = A.pageHead("剪辑", "第 " + p.episode + " 集 · " + order.length + " 个片段 · " + totalDur(p) + " 秒",
      '<button class="btn" data-act="add-note">添加批注</button>'
      + A.genBtn("合成成片", "preview-film", "", "primary"));
    if(!order.length){ h += A.empty('还没有片段，请先完成分镜。', "film"); return h; }
    var film = filmUrlOf(p);
    if(A.filmStale(p)){
      h += '<div class="banner"><div><b>成片已过期</b>：合成之后内容有改动，请重新合成。</div></div>';
    }
    if(film){
      h += card("成片预览", '<div class="player"><video src="' + A.esc(A.frameUrl(film)) + '" controls preload="metadata"></video></div>',
        '<button class="btn sm" data-act="download-film">' + A.icon("download") + '下载</button>'
        + '<button class="btn sm primary" data-act="nav" data-page="qa">审片与导出</button>');
    }
    var total = Math.max(1, totalDur(p));
    var marks = [0, 0.25, 0.5, 0.75, 1].map(function(r){
      var sec = Math.round(total * r);
      return '<span>' + ("0" + Math.floor(sec / 60)).slice(-2) + ':' + ("0" + (sec % 60)).slice(-2) + '</span>';
    }).join("");
    h += card("时间线", '<div class="timeline">'
      + '<div class="track"><div class="lbl">画面</div>'
      + order.map(function(id){
          var s = A.shotById(p, id);
          return '<div class="clip' + (s.videoUrl ? '' : ' off') + '" data-flex="' + s.dur + '\">' + A.esc(shotLabel(p, id)) + ' · ' + s.dur + 's</div>';
        }).join("") + '</div>'
      + '<div class="track"><div class="lbl">配音</div>'
      + (p.audio.subtitles.length
          ? p.audio.subtitles.map(function(s){ return '<div class="clip a' + (s.audio_url ? '' : ' off') + '" data-flex="3">' + A.esc(s.text.slice(0, 6)) + '</div>'; }).join("")
          : '<div class="clip a off" data-flex="10">—</div>') + '</div>'
      + '<div class="track"><div class="lbl">字幕</div>'
      + (p.audio.subtitles.length
          ? p.audio.subtitles.map(function(s){ return '<div class="clip s" data-flex="3">' + A.esc(s.text.slice(0, 6)) + '</div>'; }).join("")
          : '<div class="clip s off" data-flex="10">—</div>') + '</div>'
      + '<div class="track"><div class="lbl">音乐</div><div class="clip v' + (p.audio.bgm.url ? '' : ' off') + '" data-flex="20">'
      + A.esc(p.audio.bgm.name || "—") + '</div></div>'
      + '<div class="ruler">' + marks + '</div></div>');
    h += '<div class="split"><div>' + card("片段顺序", table('<th class="num">#</th><th>镜头</th><th>景别</th><th>时长</th><th></th>',
      order.map(function(id, i){
        var s = A.shotById(p, id);
        return '<tr><td class="num">' + (i + 1) + '</td><td>' + A.esc(shotLabel(p, id)) + ' · ' + A.esc(s.desc.slice(0, 22)) + '</td>'
          + '<td>' + A.esc(s.size) + '</td><td>' + s.dur + 's</td>'
          + '<td><div class="row-actions"><button class="btn sm ghost" data-act="move-clip" data-id="' + A.esc(id) + '" data-dir="-1">↑</button>'
          + '<button class="btn sm ghost" data-act="move-clip" data-id="' + A.esc(id) + '" data-dir="1">↓</button></div></td></tr>';
      }).join(""))) + '</div>';
    h += card("批注（" + p.timeline.notes.length + "）", !p.timeline.notes.length ? '<div class="hint">暂无批注</div>'
      : p.timeline.notes.map(function(n){
          return '<div class="kv"><span>' + A.esc(n.time) + '</span><b class="note">' + A.esc(n.text) + '</b></div>';
        }).join("")) + '</div>';
    return h;
  };

  /* ---------- 10 审片与导出 ---------- */
  A.pages.qa = function(p){
    var passed = p.qa.filter(Boolean).length;
    var h = A.pageHead("审片与导出", "",
      '<button class="btn danger" data-act="return-reshoot">退回重拍</button>'
      + '<button class="btn primary" data-act="export-film">' + A.icon("download") + '导出成片</button>');
    if(A.DB.datasource === "server" && p.shots.length && A.shotsWithoutVideo(p)){
      h += '<div class="banner"><div><b>成片不完整</b>：还有 ' + A.shotsWithoutVideo(p) + ' / ' + p.shots.length
        + ' 个镜头没有视频。</div><button class="btn sm" data-act="nav" data-page="generate">去生成</button></div>';
    }
    if(A.filmStale(p)){
      h += '<div class="banner"><div><b>成片已过期</b>：合成之后内容有改动，请重新合成。</div>'
        + '<button class="btn sm" data-act="nav" data-page="timeline">去剪辑</button></div>';
    }
    var film = filmUrlOf(p);
    h += '<div class="split-wide"><div>';
    if(film){
      h += card("成片", '<div class="player"><video src="' + A.esc(A.frameUrl(film)) + '" controls preload="metadata"></video></div>');
    }
    h += card("导出记录（" + p.exports.length + "）", !p.exports.length ? A.empty('还没有导出记录。', "film")
      : table('<th>时间</th><th>名称</th><th>规格</th><th class="num">镜头</th><th class="num">时长</th><th>AI 标识</th><th></th>',
          p.exports.map(function(e){
            /* 离线示例成片不是模型产物，不显示「AI 生成」；真实成片照常标注。
               披露文案取导出记录里的真值（服务端写容器元数据用的是同一个字符串），
               旧记录没这一列时回落到 /config 下发的值，保证「页面说的 = 文件里写的」。 */
            var mark = (e.aiLabel && !e.sampleLabel)
              ? '<span class="badge b-brand">' + A.esc(e.aiLabel) + '</span>'
                + '<div class="hint">' + A.esc(e.aiDisclosure || A.aiDisclosure()) + '</div>'
              : '<span class="hint">—</span>';
            return '<tr><td>' + A.esc(e.time) + '</td><td>' + A.esc(e.name) + '</td><td>' + A.esc(e.res) + '</td>'
              + '<td class="num">' + e.shots + '</td><td class="num">' + e.duration + 's</td><td>' + mark + '</td>'
              + '<td>' + (e.filmUrl ? '<button class="btn sm" data-act="download-export" data-id="' + A.esc(e.id) + '">' + A.icon("download") + '下载</button>' : '') + '</td></tr>';
          }).join("")));
    h += '</div><div>';
    h += card("审片清单 · " + passed + " / 4", A.QA_ITEMS.map(function(q, i){
      return '<div class="chk" data-act="toggle-qa" data-i="' + i + '"><div class="box ' + (p.qa[i] ? "on" : "") + '">' + (p.qa[i] ? "✓" : "") + '</div>'
        + '<div><b>' + A.esc(q.name) + '</b><p>' + A.esc(q.desc) + '</p></div></div>';
    }).join(""));
    h += card("导出规格", A.kv([["分辨率", p.res], ["画面比例", p.ratio], ["格式", "MP4"], ["时长", totalDur(p) + " 秒"]]));
    h += '</div></div>';
    return h;
  };

  /* ---------- 11 任务 ---------- */
  A.pages.tasks = function(p){
    var filter = A.DB.ui.taskFilter;
    var h = A.pageHead("任务", "");
    var busyAll = A.inFlightCharges();
    var reviewAll = A.pendingChargesAll();
    if(busyAll.length || reviewAll.length){
      h += card("进行中的生成", table('<th>项目</th><th>内容</th><th>对象</th><th>状态</th><th class="num">冻结积分</th>',
        busyAll.concat(reviewAll).reverse().map(function(c){
          var proj = A.project(c.projectId);
          return '<tr><td>' + A.esc(proj ? proj.name : "—") + '</td><td>' + A.esc(A.actionLabel(c.action)) + '</td>'
            + '<td>' + A.esc(targetLabel(proj, c.target)) + '</td><td>' + A.pill(c.state) + '</td>'
            + '<td class="num">' + c.cap + '</td></tr>';
        }).join("")));
    }
    h += '<div class="tabs">'
      + [["all","全部"],["queued","排队中"],["running","生成中"],["succeeded","已完成"],["held","待复核"],["failed","失败"],["cancelled","已取消"]]
        .map(function(x){ return '<button class="tab ' + (filter === x[0] ? "on" : "") + '" data-act="task-filter" data-filter="' + x[0] + '">' + x[1] + '</button>'; }).join("")
      + '</div>';
    var list = A.DB.tasks.filter(function(t){ return filter === "all" || t.state === filter; });
    if(!list.length){ h += A.empty('暂无任务。', "list"); return h; }
    h += '<div class="card flush"><div class="scroll-x"><table><thead><tr>'
      + '<th>项目</th><th>镜头</th><th>类型</th><th>状态</th><th>进度</th><th>创建时间</th><th></th></tr></thead><tbody>';
    list.slice().reverse().forEach(function(t){
      var proj = A.project(t.projectId);
      h += '<tr><td>' + A.esc(proj ? proj.name : "—") + '</td>'
        + '<td>' + A.esc(proj ? shotLabel(proj, t.shotId) : t.shotId) + '</td>'
        + '<td>' + A.esc(String(t.type || "关键帧").replace(/\s*t2i/i, "")) + '</td>'
        + '<td>' + A.pill(t.state) + '</td>'
        + '<td class="mw110"><div class="prog"><i data-w="' + t.percent + '"></i></div></td>'
        + '<td>' + A.esc(t.createdAt || "") + '</td>'
        + '<td><div class="row-actions nowrap">'
        + (t.state === "held" ? '<button class="btn sm" data-act="release-held" data-id="' + A.esc(t.id) + '">释放</button>' : "")
        + (t.state === "failed" ? '<button class="btn sm" data-act="retry-task" data-id="' + A.esc(t.id) + '">重试</button>' : "")
        + ((t.state === "queued" || t.state === "running" || t.state === "draft" || t.state === "quoted") && !t.settled
            ? '<button class="btn sm danger" data-act="cancel-task" data-id="' + A.esc(t.id) + '">取消</button>' : "")
        + (proj ? '<button class="btn sm ghost" data-act="goto-shot" data-id="' + A.esc(t.shotId) + '" data-project="' + A.esc(t.projectId) + '">查看镜头</button>' : "")
        + '</div></td></tr>';
    });
    h += '</tbody></table></div></div>';
    return h;
  };

  /* ---------- 12 积分与账单 ---------- */
  var TX_KIND = {credit:"充值", reserve:"冻结", settle:"扣费", refund:"退还", release:"退还", retire:"归档", hold:"待确认"};
  /* 流水摘要由服务端按内部动作名生成（如 image-to-video 结算），这里转成用户能读懂的说法。 */
  A.txText = function(label){
    var s = String(label || "");
    Object.keys(A.ACTION_LABELS).sort(function(a, b){ return b.length - a.length; }).forEach(function(k){
      s = s.split(k).join(A.ACTION_LABELS[k]);
    });
    return s.replace(/单图\s*t2i/gi, "图片").replace(/\s*t2i/gi, "")
      .replace(/held\s*/gi, "").replace(/\s*\/\s*requestId=\S+/g, "")
      .replace(/预占退还/g, "冻结退还").replace(/重新预占/g, "重新冻结").replace(/预占/g, "冻结")
      .replace(/结算退差/g, "结算退还").replace(/复核退差/g, "确认退还").replace(/复核/g, "确认")
      .replace(/管理员加余额/g, "充值").replace(/\s*·\s*[A-Za-z0-9-]{4,}$/, "");
  };
  /* 充值订单的五个状态（服务端状态机的五个值）→ 人话。
     与扣费凭据是两套状态机，不要混用 A.pill 的映射。 */
  var ORDER_STATUS = {created:"待提交", pending:"待支付", paid:"支付确认中",
                      credited:"已到账", cancelled:"已取消"};
  /* 复用已有 pill 配色：待办=灰、等待=橙、处理中=蓝、成功=绿、取消=灰。 */
  var ORDER_PILL = {created:"queued", pending:"held", paid:"running",
                    credited:"succeeded", cancelled:"cancelled"};
  function orderStatus(st){ return ORDER_STATUS[st] || "处理中"; }
  function orderPill(st){
    return '<span class="pill ' + (ORDER_PILL[st] || "queued") + '">'
      + A.esc(orderStatus(st)) + '</span>';
  }
  /* 服务端给的是秒级时间戳，页面上按「月-日 时:分」呈现，与流水表格同一口径。 */
  function orderTime(v){
    var n = Number(v);
    if(!v || !isFinite(n)) return "—";
    var d = new Date(n * 1000);
    if(isNaN(d.getTime())) return "—";
    var pad = function(x){ return ("0" + x).slice(-2); };
    return pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " "
      + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }
  function orderCanAct(st){ return st === "created" || st === "pending"; }

  /* 充值卡片：已登录用户都能自助充值；未登录只给一个去账户页的入口。 */
  function rechargeCard(){
    var acc = A.account || {};
    if(!acc.me){
      return card("充值积分",
        '<div class="hint">登录后可自助充值。</div>'
        + '<div class="btnbar"><button class="btn primary sm" data-act="nav" data-page="account">前往登录</button></div>');
    }
    var draft = acc.draft;
    var body = '<form id="recharge-form">'
      + A.fInput("充值积分", "amount", 1000, {type:"number", min:1, max:1000000})
      + '</form>'
      + '<div class="hint">本地体验渠道：确认支付后积分自动到账，不产生真实付款。</div>';
    if(draft && draft.order_id){
      body += '<div class="notice mt12">'
        + A.kv([["订单号", draft.order_id],
                ["积分", (draft.amount_points || 0) + " 积分"],
                ["状态", orderStatus(draft.status)]])
        + '</div>'
        + '<div class="btnbar">'
        + '<button class="btn primary sm" data-act="payment-pay" data-id="' + A.esc(draft.order_id) + '">去支付</button>'
        + '<button class="btn sm" data-act="payment-cancel" data-id="' + A.esc(draft.order_id) + '">取消订单</button>'
        + '</div>';
    }
    body += '<div class="btnbar"><button class="btn primary sm" data-act="payment-create">创建充值单</button></div>';
    return card("充值积分", body);
  }

  /* 充值记录：时间 / 订单号 / 积分 / 状态 / 操作。 */
  function ordersCard(){
    var acc = A.account || {};
    if(!acc.me) return "";
    var refresh = '<button class="btn sm ghost" data-act="payment-orders-refresh">刷新</button>';
    if(!acc.orders){
      return card("充值记录", '<div class="hint">订单正在加载…</div>', refresh);
    }
    if(!acc.orders.length){
      return card("充值记录", '<div class="hint">还没有充值记录。</div>', refresh);
    }
    return card("充值记录", table('<th>时间</th><th>订单号</th><th class="num">积分</th><th>状态</th><th>操作</th>',
      acc.orders.map(function(o){
        var act = orderCanAct(o.status);
        return '<tr><td>' + A.esc(orderTime(o.created_at)) + '</td>'
          + '<td class="mono">' + A.esc(o.order_id) + '</td>'
          + '<td class="num">' + A.esc(o.amount_points) + '</td>'
          + '<td>' + orderPill(o.status) + '</td>'
          + '<td><div class="row-actions nowrap">'
          + (act ? '<button class="btn sm" data-act="payment-pay" data-id="' + A.esc(o.order_id) + '">去支付</button>' : '')
          + (act ? '<button class="btn sm danger" data-act="payment-cancel" data-id="' + A.esc(o.order_id) + '">取消订单</button>' : '')
          + '</div></td></tr>';
      }).join("")), refresh);
  }

  A.pages.credits = function(p){
    /* 登录态与订单列表都由进入本页触发，各只拉一次；拉完在回调里重绘。 */
    A.accountProbe();
    var acc = A.account || {};
    if(acc.me && !acc.ordersLoaded && !acc.ordersLoading){
      var pending = A.loadOrders();
      if(pending && pending.then){
        pending.then(function(){
          if(A.DB.ui.page === "credits"){ A.render(); }
        });
      }
    }
    var L = A.DB.ledger;
    var cfg = A.DB.server.config || {};
    var h = A.pageHead("积分与账单", "",
      (A.isAdmin() ? '<button class="btn" data-act="admin-credit">充值积分</button>' : '')
      + '<button class="btn" data-act="quote-preview">费用估算</button>');
    h += '<div class="grid g4">'
      + '<div class="stat"><div class="l">可用积分</div><div class="v">' + L.balance + '</div><div class="hint">'
      + (A.creditView().usable < L.balance ? '受上限约束，可用 ' + A.creditView().usable : '约 ¥' + (L.balance / 100).toFixed(2)) + '</div></div>'
      + '<div class="stat"><div class="l">冻结中</div><div class="v warn">' + L.frozen + '</div><div class="hint">生成完成后结算</div></div>'
      + '<div class="stat"><div class="l">今日已用</div><div class="v">' + (cfg.daily_used || 0) + '</div><div class="hint">每日上限 ' + (cfg.daily_cap || "—") + '</div></div>'
      + '<div class="stat"><div class="l">项目预算</div><div class="v">' + p.budget + '</div><div class="hint">' + A.esc(p.name) + '</div></div>'
      + '</div><div class="gap16"></div>';
    var charges = (A.DB.charges || []).slice(-30).reverse();
    h += '<div class="split"><div>';
    h += rechargeCard();
    h += ordersCard();
    h += card("消费记录", !charges.length ? '<div class="hint">暂无消费记录</div>'
      : table('<th>日期</th><th>项目</th><th>内容</th><th>状态</th><th class="num">冻结</th><th class="num">实扣</th>',
          charges.map(function(c){
            var proj = A.project(c.projectId);
            var st = c.state || "settled";
            /* 上游判定内容不合规：这笔凭据要按**实际资金落点**说人话。
               退款部署下钱全额退回、实扣写「—」；结算部署下钱照扣（cost 是真实值），
               必须把实扣印出来并改口径，否则页面在对用户陈述一笔没发生的退款。 */
            var rejected = c.auditRejected === true;
            /* 待确认时钱已冻结、还没结算：必须写清「什么时候、由谁」给出结论，
               否则这笔冻结积分看起来就像凭空消失。上游任务编号只在代码里判断，
               不印到页面上。 */
            var note = "";
            if(rejected){
              note = '<div class="hint">' + (st === "settled"
                ? '该次生成未通过内容审核，本次已按规则结算，实际扣费 ' + c.cost + ' 积分'
                : '该次生成未通过内容审核，积分已全额退回') + '</div>';
            } else if(st === "pending_review"){
              note = '<div class="hint">' + (c.upstreamTaskId
                ? "正在向上游核实，确认后自动结算或原路退回，通常几分钟内完成"
                : "已记录，需要管理员复核后结算或退款") + '</div>';
            }
            return '<tr><td>' + A.esc(String(c.day || "")) + '</td><td>' + A.esc(proj ? proj.name : "—") + '</td>'
              + '<td>' + A.esc(A.actionLabel(c.action)) + note + '</td>'
              + '<td>' + A.pill(st, rejected ? "未通过审核" : (st === "pending_review" ? "待确认" : null)) + '</td>'
              + '<td class="num">' + c.cap + '</td><td class="num">' + (st === "settled" ? c.cost : "—") + '</td></tr>';
          }).join("")));
    h += card("积分流水", !L.tx.length ? '<div class="hint">暂无流水</div>'
      : table('<th>时间</th><th>类型</th><th>说明</th><th class="num">变动</th>',
          L.tx.map(function(t){
            return '<tr><td>' + A.esc(t.time) + '</td><td><span class="tag ' + (t.amount > 0 ? "" : "gray") + '">' + A.esc(TX_KIND[t.kind] || t.kind) + '</span></td>'
              + '<td>' + A.esc(A.txText(t.label)) + '</td>'
              + '<td class="num amt' + (t.amount > 0 ? ' pos' : '') + '">' + A.money(t.amount) + '</td></tr>';
          }).join("")));
    h += '</div><div>';
    function price(act){ var v = A.priceOf(act); return v == null ? "—" : (v ? v + " 积分" : "免费"); }
    var cv = A.creditView();
    var limitBody = '<form id="credit-limit-form">'
      + A.fInput("单次上限（积分）", "single", cv.single === null ? "" : cv.single, {type:"number", min:0, placeholder:"不限"})
      + A.fInput("总量上限（积分）", "total", cv.total === null ? "" : cv.total, {type:"number", min:0, placeholder:"不限"})
      + '</form>'
      + A.kv([["当前可用额度", cv.usable + " 积分"]]
          .concat(cv.total === null ? [] : [
            ["已使用", cv.used + (cv.pending ? " + 生成中 " + cv.pending : "") + " 积分"],
            ["剩余额度", cv.remaining + " 积分"]])
          .concat(cv.since ? [["开始计算", cv.since]] : []))
      + (cv.total !== null && cv.total > cv.balance + cv.used + cv.pending
          ? '<div class="hint mt8">总量上限超过当前余额，按余额计算可用额度。</div>' : '')
      + (cv.single === 0 || cv.total === 0 ? '<div class="hint mt8">上限为 0 时，所有需要消耗积分的生成都会暂停。</div>' : '')
      + '<div class="btnbar"><button class="btn primary sm" data-act="save-credit-limits">保存</button>'
      + ((cv.single !== null || cv.total !== null) ? '<button class="btn sm" data-act="clear-credit-limits">取消上限</button>' : '')
      + (cv.total !== null ? '<button class="btn sm ghost" data-act="reset-credit-used">重新计算已用</button>' : '')
      + '</div>';
    h += card("使用上限", limitBody);
    h += card("价格", A.kv([
      ["关键帧（每镜）", A.keyframePrice() ? A.keyframePrice() + " 积分" : "免费"],
      ["角色 / 场景形象", price("generateAssetCandidates")],
      ["视频（每镜）", price("image-to-video")],
      ["剧本生成 / 诊断", price("script-doctor")],
      ["配音（每句）", price("tts-preview")],
      ["成片合成", "免费"]]));
    h += '</div></div>';
    return h;
  };

  /* ---------- 12.5 账户 ---------- */
  /* 会话是 HttpOnly Cookie，只有服务端知道有没有登录，所以首次进页面先探测一次
     （401 视为未登录，不弹错误）。探测结果缓存在 A.account 上，供本页与充值卡片共用。 */
  A.pages.account = function(){
    A.accountProbe();
    var acc = A.account || {};
    var me = acc.me;
    var h = A.pageHead("账户", "登录后可自助充值，并查看本账号的积分与消费记录");

    if(!me){
      var loginForm = '<form id="account-login-form">'
        + A.fInput("账号", "username", "", {placeholder:"请输入账号"})
        + A.fInput("密码", "password", "", {type:"password", placeholder:"请输入密码"})
        + '</form>'
        + '<div class="btnbar"><button class="btn primary" data-act="account-login">登录</button></div>';
      var registerForm = '<form id="account-register-form">'
        + A.fInput("账号", "username", "", {placeholder:"3–64 位字母、数字或 _ . -"})
        + A.fInput("密码", "password", "", {type:"password", placeholder:"至少 8 位字符"})
        + A.fInput("确认密码", "confirm", "", {type:"password", placeholder:"再输入一次"})
        + '</form>'
        + '<div class="hint">账号需为 3–64 位字母、数字或 _ . -</div>'
        + '<div class="hint">密码需为 8–128 位字符</div>'
        + '<div class="btnbar"><button class="btn primary" data-act="account-register">注册</button></div>';
      h += '<div class="row">'
        + '<div>' + card("登录", loginForm) + '</div>'
        + '<div>' + card("注册", registerForm) + '</div>'
        + '</div>';
      return h;
    }

    /* /me 不带用户名（会话里只有 owner），所以优先用登录时拿到的账号名，
       页面刷新后回落到 owner 尾号——两者都能让用户确认「现在是谁在用」。 */
    var name = me.username || ("账户 " + String(me.owner || "").slice(-8));
    var passwordForm = '<form id="account-password-form">'
      + A.fInput("原密码", "old", "", {type:"password", placeholder:"请输入当前密码"})
      + A.fInput("新密码", "next", "", {type:"password", placeholder:"至少 8 位字符"})
      + A.fInput("确认新密码", "confirm", "", {type:"password", placeholder:"再输入一次"})
      + '</form>'
      + '<div class="hint">密码需为 8–128 位字符</div>'
      + '<div class="btnbar"><button class="btn primary" data-act="account-password">保存新密码</button></div>';

    h += '<div class="split"><div>'
      + card("当前账号", A.kv([
          ["账号", name],
          ["身份", me.admin ? "管理员" : "创作者"],
          ["可用积分", A.DB.ledger.balance + " 积分"]]))
      + card("修改密码", passwordForm)
      + '</div><div>'
      + card("退出登录",
          '<div class="hint">退出后需要重新登录才能继续使用当前工作区。</div>'
          + '<div class="btnbar"><button class="btn danger" data-act="account-logout">退出登录</button></div>')
      + '</div></div>';
    return h;
  };

  /* ---------- 模型设置 ---------- */
  A.pages.models = function(){
    var cfg = A.DB.server.config || {};
    var cr = cfg.credentials || {liblib:{}, llm:{}};
    var lib = cr.liblib || {}, llm = cr.llm || {}, tts = cr.tts || {};
    var test = A._credTest || {};
    var online = A.remoteMode();
    function status(on, yes, no){
      return on ? '<span class="badge b-ok">' + yes + '</span>' : '<span class="badge b-p2">' + no + '</span>';
    }
    function result(r){
      if(!r) return "";
      return '<div class="hint mt8"><span class="badge ' + (r.ok ? "b-ok" : "b-danger") + '">'
        + (r.ok ? "连接正常" : "连接失败") + '</span> ' + A.esc(r.message || "") + '</div>';
    }
    var h = A.pageHead("模型设置", "",
      (lib.configured || llm.configured || tts.configured)
        ? '<button class="btn" data-act="test-credentials">检测连接</button>' : '');
    if(!online){
      h += A.banner("当前无法连接服务器，暂时不能修改模型设置。", "danger");
    }
    h += '<div class="grid g3">'
      + '<div class="stat"><div class="l">' + A.icon("image") + '图片与视频</div><div class="v">'
      + status(lib.configured, "Liblib", "示例素材") + '</div></div>'
      + '<div class="stat"><div class="l">' + A.icon("file") + '剧本与分镜</div><div class="v">'
      + status(llm.configured, "文本模型", "内置规则") + '</div></div>'
      + '<div class="stat"><div class="l">' + A.icon("mic") + '配音</div><div class="v">'
      + status(cfg.tts_configured, "配音服务", lib.configured ? "未配置" : "内置语音") + '</div></div>'
      + '</div><div class="gap16"></div>';
    if(lib.configured && !cfg.public_base_url_set){
      h += A.banner("视频生成需要服务器配置公网 https 地址后才能使用，请联系部署人员。");
    }
    h += '<div class="grid g2"><div>';
    h += card("Liblib（图片与视频）", '<form id="liblib-form">'
        + A.fInput("AccessKey", "access", "", {placeholder: lib.access_key || "在 Liblib 开放平台获取"})
        + A.fInput("SecretKey", "secret", "", {type: "password", placeholder: lib.secret_key || "在 Liblib 开放平台获取"})
        + '</form>'
        + '<div class="hint">生成形象图、关键帧和视频时，消耗的是你 Liblib 账户里的积分。'
        + '<a href="https://www.liblib.art/apis" target="_blank" rel="noopener">获取密钥</a></div>'
        + result(test.liblib)
        + '<div class="btnbar"><button class="btn primary sm" data-act="save-liblib">保存</button>'
        + (lib.configured ? '<button class="btn sm danger" data-act="clear-credentials" data-scope="liblib">清除</button>' : '')
        + '</div>',
      status(lib.configured, "已配置", "未配置"));
    h += card("配音（可选）", '<form id="tts-form">'
        + A.fInput("接口地址", "base", tts.base_url || "", {placeholder: "例如 https://api.siliconflow.cn/v1"})
        + A.fInput("模型名称", "model", tts.model || "", {placeholder: "例如 FunAudioLLM/CosyVoice2-0.5B"})
        + A.fInput("音色", "voice", tts.voice || "", {placeholder: "多个用逗号分隔，按角色自动分配"})
        + A.fInput("API Key", "key", "", {type: "password", placeholder: tts.api_key || "支持 OpenAI 兼容的语音合成接口"})
        + '</form>'
        + '<div class="hint">不填写时，真实模式下成片只包含字幕和背景音乐。</div>'
        + result(test.tts)
        + '<div class="btnbar"><button class="btn primary sm" data-act="save-tts">保存</button>'
        + (tts.configured ? '<button class="btn sm danger" data-act="clear-credentials" data-scope="tts">清除</button>' : '')
        + '</div>',
      status(tts.configured, "已配置", "未配置"));
    h += '</div><div>';
    h += card("文本模型（可选）", '<form id="llm-form">'
        + A.fInput("接口地址", "base", llm.base_url || "", {placeholder: "例如 https://api.deepseek.com/v1"})
        + A.fInput("模型名称", "model", llm.model || "", {placeholder: "例如 deepseek-chat"})
        + A.fInput("API Key", "key", "", {type: "password", placeholder: llm.api_key || "支持 OpenAI 兼容接口"})
        + '</form>'
        + '<div class="hint">不填写时，剧本生成、诊断与分镜使用内置规则。</div>'
        + result(test.llm)
        + '<div class="btnbar"><button class="btn primary sm" data-act="save-llm">保存</button>'
        + (llm.configured ? '<button class="btn sm danger" data-act="clear-credentials" data-scope="llm">清除</button>' : '')
        + '</div>',
      status(llm.configured, "已配置", "未配置"));
    h += '</div></div>';
    return h;
  };

  /* ---------- 13 系统管理 ---------- */
  /* ---------- 对账报表（管理员） ---------- */
  function pct(rate){
    if(rate === null || rate === undefined) return "—";
    return (Number(rate) * 100).toFixed(1) + "%";
  }
  /* 导出走同源链接直接下载：不把整张表读进内存再拼 Blob，也不需要额外权限。 */
  function reportActions(){
    var days = (A.report && A.report.days) || 30;
    return '<a class="btn sm" href="' + A.esc(A.reportApi.csvUrl(days)) + '" download>'
      + A.icon("download") + '导出</a>'
      + '<button class="btn sm" data-act="load-report">'
      + ((A.report && A.report.data) ? "刷新" : "加载") + '</button>';
  }
  function reportBody(){
    var rep = A.report && A.report.data;
    if(!rep) return '<div class="hint">尚未加载。按日汇总售价与实际扣费，只读，不会改动任何价格。</div>';
    var t = rep.totals || {};
    var h = A.kv([
      ["已结算笔数", (t.orders || 0) + " 笔"],
      ["售价合计", (t.salePoints || 0) + " 积分"],
      ["实际扣费合计", (t.upstreamPoints || 0) + " 积分"],
      ["毛利（仅含已确认扣费）", (t.marginPoints || 0) + " 积分 · " + pct(t.marginRate)],
      ["扣费未确认", (t.upstreamUnknown || 0) + " 笔（真实渠道 " + (t.actionableMissing || 0) + " 笔）"],
      ["本地模拟", (t.mockOrders || 0) + " 笔（没有实际扣费账单，不计入缺数）"]
    ]);
    h += '<div class="gap16"></div>'
      + table('<th>日期</th><th class="num">笔数</th><th class="num">售价</th><th class="num">实际扣费</th>'
          + '<th class="num">毛利</th><th class="num">毛利率</th><th class="num">缺数</th>',
          (rep.byDay || []).length
            ? rep.byDay.map(function(row){
                return '<tr><td>' + A.esc(String(row.day || ""))
                  + (row.isToday ? ' <span class="tag">今天</span>' : '') + '</td>'
                  + '<td class="num">' + (row.orders || 0) + '</td>'
                  + '<td class="num">' + (row.salePoints || 0) + '</td>'
                  + '<td class="num">' + (row.upstreamPoints || 0) + '</td>'
                  + '<td class="num">' + (row.marginPoints || 0) + '</td>'
                  + '<td class="num">' + pct(row.marginRate) + '</td>'
                  + '<td class="num">' + (row.upstreamUnknown || 0) + '</td></tr>';
              }).join("")
            : '<tr><td colspan="7" class="hint">所选区间内没有已结算记录。</td></tr>');
    if(rep.undatedCount){
      h += '<div class="hint mt8">另有 ' + rep.undatedCount + ' 笔早期记录没有日期，共 '
        + rep.undatedSalePoints + ' 积分，未计入上面的逐日表。</div>';
    }
    var sug = rep.pricingSuggestion || {items:[]};
    h += '<div class="gap16"></div>'
      + table('<th>动作</th><th class="num">现价</th><th class="num">实测均价</th>'
          + '<th class="num">建议价</th><th class="num">样本</th>',
          (sug.items || []).map(function(it){
            return '<tr><td>' + A.esc(A.actionLabel(it.action)) + '</td>'
              + '<td class="num">' + it.currentPrice + '</td>'
              + '<td class="num">' + (it.avgUpstreamPoints === null ? "—" : it.avgUpstreamPoints) + '</td>'
              + '<td class="num">' + (it.suggestedPrice === null ? "—" : it.suggestedPrice) + '</td>'
              + '<td class="num">' + (it.samples || 0) + (it.lowConfidence ? " · 样本不足" : "") + '</td>'
              + '</tr>';
          }).join(""))
      + '<div class="hint mt8">建议只按已记录的实际扣费反推，不会自动改价；要改请在「计价」里显式保存。</div>';
    return h;
  }

  A.pages.admin = function(p){
    var held = A.DB.tasks.filter(function(t){ return t.state === "held"; });
    var pend = A.pendingChargesAll();
    var h = A.pageHead("系统管理", "");
    h += '<div class="split"><div>';
    h += card("待复核", (!held.length && !pend.length) ? '<div class="hint">没有需要复核的记录。</div>'
      : table('<th>项目</th><th>内容</th><th class="num">冻结</th><th>时间</th><th></th>',
          held.map(function(t){
            var proj = A.project(t.projectId);
            return '<tr><td>' + A.esc(proj ? proj.name : "—") + '</td><td>关键帧 · ' + A.esc(proj ? shotLabel(proj, t.shotId) : "") + '</td>'
              + '<td class="num">' + t.reserve + '</td><td>' + A.esc(t.createdAt || "") + '</td>'
              + '<td><button class="btn sm" data-act="release-held" data-id="' + A.esc(t.id) + '">释放</button></td></tr>';
          }).join("")
          + pend.map(function(c){
            var proj = A.project(c.projectId);
            return '<tr><td>' + A.esc(proj ? proj.name : "—") + '</td><td>' + A.esc(A.actionLabel(c.action)) + '</td>'
              + '<td class="num">' + c.cap + '</td><td>' + A.esc(String(c.day || "")) + '</td>'
              + '<td><div class="row-actions nowrap">'
              + '<button class="btn sm" data-act="resolve-charge" data-id="' + A.esc(c.id) + '" data-outcome="failed" data-cap="' + c.cap + '">退款</button>'
              + '<button class="btn sm" data-act="resolve-charge" data-id="' + A.esc(c.id) + '" data-outcome="succeeded" data-cap="' + c.cap + '">结算</button></div></td></tr>';
          }).join("")));
    h += card("计价", '<form id="pricing-form"><div class="grid g2">'
        + A.fInput("关键帧单价（积分）", "t2i", A.DB.pricing.t2i, {type:"number",min:0})
        + A.fInput("关键帧冻结上限（积分）", "cap", A.DB.pricing.cap, {type:"number",min:1})
        + A.fInput("视频单价（积分）", "video", A.DB.pricing.video == null ? "" : A.DB.pricing.video, {type:"number",min:1, placeholder:"默认价格"})
        + A.fInput("每秒请求数", "qps", A.DB.pricing.qps, {type:"number",min:0.1})
        + A.fInput("并发上限", "concurrency", A.DB.pricing.concurrency, {type:"number",min:1})
        + '</div></form><div class="btnbar tight"><button class="btn primary sm" data-act="save-pricing">保存</button></div>');
    var rec = (A.DB.server && A.DB.server.reconcile) || null;
    var recBody;
    if(!rec){ recBody = '<div class="hint">尚未加载。</div>'; }
    else {
      var d = rec.drama || {};
      var n = function(v){ return (v === null || v === undefined || v === "") ? "—" : String(v); };
      recBody = table('<th>项目</th><th class="num">数值</th>',
          '<tr><td>可用余额</td><td class="num">' + n(d.balance) + '</td></tr>'
        + '<tr><td>冻结</td><td class="num">' + n(d.frozen) + '</td></tr>'
        + '<tr><td>今日已用 / 上限</td><td class="num">' + n(d.dailyUsed) + ' / ' + n(d.dailyCap) + '</td></tr>'
        + '<tr><td>已结算</td><td class="num">' + n(d.settledCharges) + ' 笔 / ' + n(d.settledCost) + '</td></tr>'
        + '<tr><td>折合人民币</td><td class="num">¥ ' + n(d.cnyHint) + '</td></tr>')
        + '<div class="hint mt8">' + A.esc(rec.generatedAt || "") + '</div>';
    }
    h += card("账本概览", recBody, '<button class="btn sm" data-act="load-reconcile">' + (rec ? "刷新" : "加载") + '</button>');
    h += card("对账报表", reportBody(), reportActions());
    h += '</div><div>';
    var mediaStats = (A.DB.server.config && A.DB.server.config.media) || null;
    var cfgAll = (A.DB.server && A.DB.server.config) || {};
    var keepDays = Number(cfgAll.media_retention_days || 0);
    h += card("素材存储", (mediaStats
        ? A.kv([["文件数", mediaStats.files + " 个"],
                ["占用空间", (Math.round((mediaStats.bytes || 0) / 1048576 * 10) / 10) + " MB"],
                ["容量上限", mediaStats.limit_bytes ? Math.round(mediaStats.limit_bytes / 1048576) + " MB" : "不限"]])
        : '<div class="hint">暂无数据</div>')
      /* 「清理」两个字很容易被读成「会删掉我正在用的成片」。把保留范围写在
         按钮上方，用户才知道哪些东西一定不会动。 */
      + '<div class="hint mt8">清理只会删除<b>没有任何项目引用</b>的文件；正在用的成片、'
      + '镜头视频、关键帧候选与产物历史一律保留。</div>'
      + (keepDays > 0
          ? '<div class="hint">保留策略：' + keepDays + ' 天（部署方设定）</div>' : '')
      + '<div class="btnbar"><button class="btn sm" data-act="purge-media">清理历史素材</button></div>');
    /* AI 生成标识：只读。改配置走环境变量，页面不提供开关——标识位置里的
       「烧入画面」一旦生效就不可撤回，不能让任何人顺手点开。 */
    var aiCfg = (A.DB.server && A.DB.server.config) || {};
    var POSITION_LABEL = {
      container_metadata: "成片容器元数据",
      video_overlay: "画面叠加层",
      in_frame_drawtext: "烧入画面（未实现，需法务确认）"
    };
    var positions = (aiCfg.ai_label_positions && aiCfg.ai_label_positions.length
      ? aiCfg.ai_label_positions : ["container_metadata"]).map(function(x){
        return POSITION_LABEL[x] || String(x);
      });
    h += card("AI 生成标识", A.kv([
        ["标识文案", aiCfg.ai_disclosure || A.aiDisclosure()],
        ["标识位置", positions.join("、")]])
      + '<div class="hint mt8">标识位置由服务端配置决定；烧入画面需法务口径确认后开启。</div>');
    h += card("数据", '<div class="btnbar tight">'
      + '<button class="btn sm" data-act="export-data">' + A.icon("download") + '导出</button>'
      + '<button class="btn sm" data-act="import-data">' + A.icon("upload") + '导入</button>'
      + '<button class="btn sm danger" data-act="reset-all">重置全部数据</button></div>');
    h += '</div></div>';
    return h;
  };

  /* ---------- 14 创作画布（未在导航中展示） ---------- */
  A.pages.canvas = function(){
    return A.pageHead("创作画布", "") + A.empty('敬请期待。', "layout");
  };
})(App);
