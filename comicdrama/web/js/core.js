/* core.js — 常量与无状态工具。零依赖、零网络。 */
var App = window.App = window.App || {};

App.VERSION = "1.0.0";
App.STORE_KEY = "comic_drama_studio_v1";

/* 页面状态标的是「这一页的交互与数据通路是否已由服务端实现」，不是「上游模型是否已付费」。
   上游真实模型那条单独列为「最后一公里」，见 /api/v1/drama/config 的 last_mile。
   旧文案把已实现的页面标成「待新增」，与事实相反。 */
App.PAGES = [
  {id:"overview",  name:"项目总览",   group:"工作台", icon:"home",     pr:"P0", st:"已实现"},
  {id:"projects",  name:"我的项目",   group:"工作台", icon:"folder",   pr:"P0", st:"已实现"},
  {id:"settings",  name:"项目设定",   group:"工作台", icon:"sliders",  pr:"P0", st:"已实现"},
  {id:"script",    name:"剧本",       group:"创作",   icon:"file",     pr:"P0", st:"已实现"},
  {id:"assets",    name:"角色与场景", group:"创作",   icon:"users",    pr:"P0", st:"已实现"},
  {id:"storyboard",name:"分镜",       group:"创作",   icon:"grid",     pr:"P0", st:"已实现"},
  {id:"generate",  name:"画面生成",   group:"创作",   icon:"sparkle",  pr:"P0", st:"已实现"},
  {id:"audio",     name:"配音与字幕", group:"创作",   icon:"mic",      pr:"P1", st:"已实现"},
  {id:"timeline",  name:"剪辑",       group:"创作",   icon:"film",     pr:"P0", st:"已实现"},
  {id:"qa",        name:"审片与导出", group:"创作",   icon:"check",    pr:"P0", st:"已实现"},
  {id:"tasks",     name:"任务",       group:"管理",   icon:"list",     pr:"P0", st:"已实现"},
  {id:"credits",   name:"积分与账单", group:"管理",   icon:"wallet",   pr:"P0", st:"已实现"},
  {id:"account",   name:"账户",       group:"管理",   icon:"users",    pr:"P0", st:"已实现"},
  {id:"models",    name:"模型设置",   group:"管理",   icon:"key",      pr:"P0", st:"已实现"},
  {id:"admin",     name:"系统管理",   group:"管理",   icon:"gear",     pr:"P1", st:"已实现"},
  /* 画布不在导航里展示（hidden），保留页面实现以免旧状态落到空页。 */
  {id:"canvas",    name:"创作画布",   group:"管理",   icon:"layout",   pr:"P2", st:"后置", hidden:true}
];
App.NAV_GROUPS = ["工作台","创作","管理"];

/* 线性图标（24×24 描边）。只在本文件维护，页面按名字取。 */
App.ICONS = {
  home:'<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/><path d="M10 21v-6h4v6"/>',
  folder:'<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  sliders:'<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
  file:'<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h6"/>',
  users:'<circle cx="9" cy="8" r="3.5"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6"/><path d="M16 4.5a3.5 3.5 0 0 1 0 7M18 14.5c1.8.9 3 2.9 3 5.5"/>',
  grid:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  sparkle:'<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/>',
  mic:'<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>',
  film:'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4"/>',
  check:'<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
  list:'<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1"/><circle cx="4.5" cy="12" r="1"/><circle cx="4.5" cy="18" r="1"/>',
  wallet:'<path d="M4 7h14a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a1 1 0 0 1-1-1V7z"/><path d="M4 7l11-3v3M16 13.5h.01"/>',
  gear:'<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1"/>',
  layout:'<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/>',
  key:'<circle cx="8" cy="15" r="4"/><path d="m10.8 12.2 8.7-8.7M16 6l3 3M14 8l2 2"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  play:'<path d="M7 4v16l13-8z"/>',
  download:'<path d="M12 3v12M7 10l5 5 5-5M5 21h14"/>',
  upload:'<path d="M12 21V9M7 14l5-5 5 5M5 3h14"/>',
  image:'<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="m21 16-5-5-8 8"/>',
  inbox:'<path d="M3 13h5l2 3h4l2-3h5"/><path d="M5 5h14l2 8v6a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-6z"/>'
};
/* 页面受 CSP style-src 'self' 约束，HTML 里的 style 属性会被浏览器丢弃。
   动态尺寸一律写成 data-h / data-w / data-flex，渲染后由这里用 CSSOM 设置。 */
/* 生成通常要多久（秒，下限 / 上限）。只用于给等待中的用户一个预期，不参与任何判定。 */
App.TYPICAL_SECONDS = {
  "image-to-video":[120, 300], "generateAssetCandidates":[15, 60], "keyframe":[15, 60],
  "script-doctor":[5, 40], "creative-start":[10, 60], "extract-assets":[5, 40],
  "resplit-shots-v2":[10, 60], "tts-preview":[3, 20], "change-voice":[5, 30],
  "preview-film":[10, 90], "mark-audio-done":[1, 5]
};
App.clock = function(seconds){
  var s = Math.max(0, Math.floor(Number(seconds) || 0));
  var m = Math.floor(s / 60);
  return m ? (m + " 分 " + (s % 60) + " 秒") : (s + " 秒");
};
App.typicalText = function(action){
  var t = App.TYPICAL_SECONDS[action];
  if(!t) return "";
  var lo = t[0] >= 60 ? Math.round(t[0] / 60) + " 分钟" : t[0] + " 秒";
  var hi = t[1] >= 60 ? Math.round(t[1] / 60) + " 分钟" : t[1] + " 秒";
  return "通常需要 " + lo + "～" + hi;
};
/* 「已等待 X」：since 是秒级时间戳。超过通常上限时换成更明确的说法，别让人以为卡死。 */
App.waitText = function(since, action){
  var elapsed = Date.now() / 1000 - Number(since || 0);
  var t = App.TYPICAL_SECONDS[action];
  var text = "已等待 " + App.clock(elapsed);
  if(t && elapsed > t[1]) return text + "，比平时久，结果返回后会自动更新";
  var typical = App.typicalText(action);
  return typical ? text + "，" + typical : text;
};
/* 每秒刷新页面上所有「已等待」文字，只改这几处文本，不重绘整页、不打断输入。 */
App.tickWaits = function(root){
  if(!root || typeof root.querySelectorAll !== "function") return;
  root.querySelectorAll("[data-since]").forEach(function(e){
    e.textContent = App.waitText(e.getAttribute("data-since"), e.getAttribute("data-action"));
  });
};
App.applyDyn = function(root){
  if(!root || typeof root.querySelectorAll !== "function") return;
  App.tickWaits(root);
  root.querySelectorAll("[data-h]").forEach(function(e){ e.style.height = e.getAttribute("data-h") + "px"; });
  root.querySelectorAll("[data-w]").forEach(function(e){ e.style.width = Math.max(0, Math.min(100, Number(e.getAttribute("data-w")) || 0)) + "%"; });
  root.querySelectorAll("[data-flex]").forEach(function(e){ e.style.flex = (Number(e.getAttribute("data-flex")) || 1) + " 1 0"; });
};
App.icon = function(name){
  return '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true">' + (App.ICONS[name] || "") + '</svg>';
};

/* 生成动作的中文名：忙碌条、提示语使用，避免把内部动作名露给用户。 */
App.ACTION_LABELS = {
  "script-doctor":"剧本诊断", "creative-start":"生成剧本", "extract-assets":"提取角色与场景",
  "resplit-shots-v2":"重新生成分镜", "tts-preview":"生成配音", "change-voice":"推荐音色",
  "generateAssetCandidates":"生成形象图", "image-to-video":"生成视频",
  "preview-film":"合成成片", "mark-audio-done":"保存配音",
  /* 批量视频提交、产物回退、版本对比同样会点亮顶部的忙碌条，
     漏了这三条就会把 diffScriptVersions 这种内部动作名印在页面上。 */
  "submitVideoTasks":"批量生成视频", "restoreShotMedia":"切换产物版本",
  "diffScriptVersions":"版本对比"
};
App.actionLabel = function(name){ return App.ACTION_LABELS[name] || name; };
/* 关键帧单价：以服务端下发为准（自带 Liblib 密钥时为 0），离线态回落本地定价。 */
App.keyframePrice = function(){
  var cfg = App.DB && App.DB.server && App.DB.server.config;
  if(cfg && typeof cfg.keyframe_price === "number") return cfg.keyframe_price;
  return Number((App.DB && App.DB.pricing && App.DB.pricing.t2i) || 0);
};

/* 服务端错误码 → 面向用户的提示。未登记的错误码沿用服务端原文。 */
App.ERROR_TEXT = {
  insufficient_credits:"积分不足，请先充值",
  daily_cap_exceeded:"已达到今日用量上限，请明天再试",
  budget_exceeded:"已超出项目预算，可在项目设定中调整",
  action_in_flight:"正在生成中，请勿重复提交",
  pending_review:"上一次生成结果待确认，请稍后再试",
  upstream_unknown:"生成结果暂未确认，积分已冻结，确认后自动处理",
  drama_generation_held:"生成结果暂未确认，请稍后查看",
  film_stale:"成片已过期，请重新合成",
  film_incomplete:"还有镜头没有视频，请先生成",
  film_missing:"请先合成成片",
  empty_film:"还没有镜头，请先完成分镜",
  qa_incomplete:"请先完成全部审片项",
  no_pending:"没有可生成的镜头，请先锁定分镜",
  resplit_discards_media:"未锁定的镜头已有生成画面，请确认后再替换",
  forbidden:"没有权限执行此操作",
  idempotency_conflict:"请求冲突，请刷新后重试",
  charge_conflict:"请求冲突，请刷新后重试",
  drama_config:"服务暂不可用，请联系管理员",
  drama_tts_config:"未配置配音服务，成片将只包含字幕和背景音乐",
  drama_credentials:"服务暂不可用，请联系管理员",
  drama_ffmpeg:"成片合成失败，请联系管理员",
  drama_llm_unavailable:"生成服务繁忙，请稍后重试",
  drama_tts_unavailable:"配音服务繁忙，请稍后重试",
  drama_upstream:"生成服务繁忙，请稍后重试",
  drama_generation_timeout:"生成超时，请稍后重试",
  drama_llm_payload:"生成结果异常，请重试",
  upload_format:"文件格式不支持",
  upload_too_large:"文件过大",
  upload_limit:"上传数量已达上限",
  upload_kind:"不支持的文件类型",
  upload_empty:"文件为空",
  invalid_credentials:"密钥格式不正确",
  account_session_required:"请先登录",
  invalid_credit_limit:"上限必须是 0 或正整数",
  credit_single_limit:"超出你设置的单次使用上限，可在「积分与账单」中调整",
  credit_total_limit:"已达到你设置的积分使用总量上限，可在「积分与账单」中调整",
  invalid_import:"导入的数据格式不正确，请检查文件内容后重试",
  account_invalid:"账号需为 3–64 位字母、数字或 _ . -，密码至少 8 位",
  account_username_taken:"该账号已被注册，请换一个",
  account_credentials_invalid:"账号或密码不正确",
  account_locked:"尝试次数过多，请 15 分钟后再试",
  account_session_required:"请先登录",
  payment_amount_invalid:"充值积分必须是 1 到 100 万之间的整数",
  payment_invalid_request:"充值请求编号不能为空且不超过 64 位",
  payment_duplicate_request:"该充值请求已提交，请勿重复下单",
  payment_request_conflict:"该充值请求编号已用于另一笔金额，请重新发起",
  payment_order_missing:"订单不存在",
  payment_order_state:"订单状态已变化，请刷新后重试",
  payment_signature_invalid:"支付回调校验失败，订单未变动",
  payment_payload_mismatch:"支付回调内容与订单不一致",
  payment_callback_required:"该支付渠道需要由支付方确认，请稍后重试",
  /* 批量候选 / 产物回退 / 版本对比。都是「服务端拒绝」而不是「前端猜」，
     文案一律说人话，且不出现字段名（candidates / mediaHistory / field）。 */
  candidates_unsupported:"真实生成通道暂不支持一次多张候选，请按 1 张生成",
  version_missing:"版本不存在，可能已被删除，请刷新后重试",
  task_not_settled:"任务还没结算，暂时不能选帧，请等生成完成",
  task_missing:"任务不存在，请刷新后重试",
  shot_missing:"镜头不存在，可能已被删除，请刷新后重试",
  invalid_field:"有一项参数不合法，请检查后重试",
  validation_error:"提交的内容格式不正确，请检查后重试"
};
/* 积分使用上限的展示口径，与服务端 DramaService.credit_limit_view 一致：
   未设置 = 不限，可用额度就是账户余额；设置了总量上限时，可用额度 =
   min(余额, 总量上限 − 已结算 − 已提交未结算)。未结算部分按单价计，不按 1.2 倍预占。 */
App.creditView = function(){
  var lim = (App.DB && App.DB.creditLimits) || {};
  var balance = Number((App.DB && App.DB.ledger && App.DB.ledger.balance) || 0);
  var pending = ((App.DB && App.DB.charges) || []).filter(function(c){
      return c.state === "in_flight" || c.state === "pending_review";
    }).reduce(function(a, c){ return a + Number(c.price || 0); }, 0)
    + ((App.DB && App.DB.tasks) || []).filter(function(t){
      return !t.settled && ["queued","running","in_flight","held"].indexOf(t.state) >= 0;
    }).reduce(function(a, t){ return a + Number(t.cost || 0); }, 0);
  var total = (lim.total === null || lim.total === undefined) ? null : Number(lim.total);
  var single = (lim.single === null || lim.single === undefined) ? null : Number(lim.single);
  var used = Number(lim.used || 0);
  var remaining = total === null ? null : Math.max(0, total - used - pending);
  return {single:single, total:total, used:used, pending:pending, remaining:remaining,
          since:lim.since || "", balance:balance,
          usable: remaining === null ? balance : Math.min(balance, remaining)};
};
App.friendlyError = function(code, fallback){
  return (code && App.ERROR_TEXT[code]) || fallback;
};

App.STEPS = [
  {n:1,name:"写故事",       page:"script"},
  {n:2,name:"定剧本",       page:"script"},
  {n:3,name:"建角色和场景", page:"assets"},
  {n:4,name:"画分镜",       page:"storyboard"},
  {n:5,name:"出成片",       page:"generate"},
  {n:6,name:"AI 剪辑",      page:"timeline"}
];

App.ASSET_TYPES = [
  {key:"characters", name:"角色", unit:"位"},
  {key:"scenes",     name:"场景", unit:"处"},
  {key:"props",      name:"道具", unit:"件"}
];

App.QA_ITEMS = [
  {name:"画面完整", desc:"每个镜头首尾都有有效画面，无黑帧、空镜"},
  {name:"角色一致", desc:"同一角色在各镜头中的外貌与服装保持一致"},
  {name:"音画同步", desc:"对白、字幕与画面节奏对齐"},
  {name:"内容合规", desc:"AI 生成标识完整，素材与音乐授权齐全"}
];

/* 服务器数据源下由服务端执行的业务动作。未列出的（如 switchProject、nav、
   assetTab）是纯 UI 状态，保留在前端本地。新增的 kebab-case 动作对应后端
   mock 生成能力，名字与 DramaService._dispatch 的分支一一对应。 */
App.REMOTE_ACTS = [
  "createProject","copyProject","archiveProject","unarchiveProject","deleteProject",
  "saveSettings","restoreDefaults",
  "saveScript","newScriptVersion","freezeScript","rollbackScript","markDirtyByScript",
  "resplitShots",
  "addAsset","updateAsset","deleteAsset","toggleAssetLock","generateAssetCandidates",
  "addShot","insertShotBefore","updateShot","deleteShot","moveShot","toggleShotLock",
  "submitTasks","tick","injectFault","releaseHeld","retryTask","pickFrame","rerollTask","cancelTask",
  /* 批量图生视频：一条预占 -> N 条 queued，与服务端 submitVideoTasks 分支对应。
     漏在这里的后果不是报错，而是「本地假执行」——点了以后界面自己编一个
     queued 数出来，服务端根本没收过这单。 */
  "submitVideoTasks",
  /* 产物回退：只能回到该镜头历史里出现过的地址，校验在服务端，
     所以必须在线上动作清单里，前端不许自己改 videoUrl / imageUrl。 */
  "restoreShotMedia",
  /* 剧本版本对比：纯读、不写库，但 hunks 只存在于服务端，本地无从编造。 */
  "diffScriptVersions",
  "adminCredit","savePricing","purgeMedia","resolveCharge","saveCreditLimits",
  "addVoice","deleteVoice","addSubtitle","toggleSubtitle","deleteSubtitle","saveBgm",
  "moveClip","addNote","toggleQA","exportFilm","returnForReshoot",
  "script-doctor","creative-start","extract-assets","resplit-shots-v2",
  "tts-preview","change-voice","mark-audio-done","preview-film","image-to-video"
];

/* 生成动作（可能出网、可能真扣费）。与线上交互强相关：
   - 在线态不本地预执行：本地占位会盖住服务端真实结果；
   - 同一个动作不允许并发提交：真实上游重放一次就是真金白银；
   - 超时按动作类型分级，8 秒一刀切会把正常的长任务误判成「后端挂了」。

   判定标准不是「价格是否为 0」，而是**结果是否只存在于服务端**：
   `preview-film`（本机 FFmpeg 合成，产出成片地址与逐句对齐摘要）和
   `mark-audio-done`（返回本项目实际留存的音频文件数）定价都是 0，
   但它们的返回体是界面文案的唯一来源。放进这份清单之前，代理会走
   「普通动作」分支、同步返回本地 `{}`，导致成片合成成功后仍提示
   「0 片段 / 0s（无可合成视频轨）」——实测复现。 */
App.GEN_ACTS = [
  "script-doctor","creative-start","extract-assets","resplit-shots-v2",
  "tts-preview","change-voice","generateAssetCandidates","image-to-video",
  "preview-film","mark-audio-done",
  /* 这三条不产出素材，但**返回体只存在于服务端**，按同一条判据归到这里：
     - submitVideoTasks：queued / reserve / skipped[] 决定提示文案与预占金额，
       本地乐观执行会先编一个「已提交 N 个镜头」，服务端可能一个都没收；
     - restoreShotMedia：恢复后的地址与产物类型由服务端按历史校验后给出；
     - diffScriptVersions：hunks / summary 由服务端算，前端本地算一份就可能与
       服务端看到的稿子不一致（在线态真源是服务端 state）。
     走这条通道 = 不本地预执行 + 同动作不并发 + 带幂等键。 */
  "submitVideoTasks","restoreShotMedia","diffScriptVersions"
];

/* 超时（毫秒）。LLM 单次往返按 120s 上限，媒体生成按 960s（后端 media_timeout 900s + 余量）。 */
App.ACT_TIMEOUT = {
  "script-doctor":120000, "creative-start":120000, "extract-assets":120000,
  "resplit-shots-v2":120000, "change-voice":120000,
  "tts-preview":180000,
  "generateAssetCandidates":960000, "image-to-video":960000,
  "preview-film":960000
};
App.timeoutFor = function(name){ return App.ACT_TIMEOUT[name] || 20000; };

App.newRequestId = function(){
  return "rq-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2,8);
};

/* 服务端返回 {provider,kind,label,payload,note}；把 payload 摊平，
   让调用方直接用 r.findings / r.count / r.clips 这种字段，不用记两层结构。 */
App.flattenResult = function(res){
  if(!res || typeof res !== "object") return null;
  var out = {provider:res.provider, kind:res.kind, label:res.label, note:res.note,
             payload:res.payload || {}};
  var pl = res.payload || {};
  for(var k in pl){ if(Object.prototype.hasOwnProperty.call(pl,k)) out[k] = pl[k]; }
  /* 少数动作的返回体没有 payload 包装，直接就是结果本身：
     版本对比给 {fields,summary,labels}，批量视频给 {queued,reserve,skipped}，
     产物回退给 {shotId,url,kind}。按 payload 摊平会把它们整包丢掉，
     调用方只能拿到一个空对象，于是「对比成功但页面空白」。
     只有确实没有 payload 时才带出顶层字段，带包装的生成动作返回结构完全不变。 */
  if(!res.payload){
    for(var k2 in res){
      if(Object.prototype.hasOwnProperty.call(res, k2) && k2 !== "payload") out[k2] = res[k2];
    }
  }
  return out;
};

/* provider 决定文案口径：mock 就必须说「本地规则」，不能写成模型输出。
   在线态以 /api/v1/drama/config 的 provider 为准；离线态一律 mock。 */
App.providerName = function(){
  var cfg = App.DB && App.DB.server && App.DB.server.config;
  return (cfg && cfg.provider) || "mock";
};
App.providerText = function(provider){
  var p = provider || App.providerName();
  if(p === "remote") return "真实 Provider";
  return "本地规则（mock）";
};
/* 生成动作定价：在线态取服务端 ACTION_PRICES，离线态未知。 */
App.priceOf = function(action){
  var cfg = App.DB && App.DB.server && App.DB.server.config;
  var prices = (cfg && cfg.prices) || {};
  return typeof prices[action] === "number" ? prices[action] : null;
};

App.esc = function(s){
  return String(s == null ? "" : s).replace(/&/g,"&amp;").replace(/</g,"&lt;")
    .replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;");
};
App.uid = function(prefix){
  return (prefix || "id") + "-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2,7);
};
App.now = function(){
  var d = new Date(), p = function(n){ return ("0"+n).slice(-2); };
  return p(d.getMonth()+1)+"-"+p(d.getDate())+" "+p(d.getHours())+":"+p(d.getMinutes());
};
App.clone = function(o){ return JSON.parse(JSON.stringify(o)); };
App.money = function(n){ return (n > 0 ? "+" : "") + n; };

App.badge = function(st){
  if(st && st.indexOf("已实现") === 0) return '<span class="badge b-ok">'+App.esc(st)+'</span>';
  if(st === "待新增") return '<span class="badge b-warn">'+App.esc(st)+'</span>';
  if(st === "后置") return '<span class="badge b-p2">'+App.esc(st)+'</span>';
  return '<span class="badge b-brand">'+App.esc(st)+'</span>';
};
App.prBadge = function(p){
  return p === "P0" ? '<span class="badge b-brand">P0</span>'
       : p === "P1" ? '<span class="badge b-warn">P1</span>'
       : '<span class="badge b-p2">P2</span>';
};
/* label 可选：同一个内部状态在不同页面有不同说法（例如扣费凭据的
   pending_review 在账单页对用户说「待确认」，在管理页对管理员说「待复核」），
   配色仍按 state 取，不额外加 CSS。 */
App.pill = function(state, label){
  var map = {queued:"排队中", running:"生成中", succeeded:"已完成", failed:"失败",
             held:"待复核", released:"已释放", cancelled:"已取消",
             draft:"草稿", locked:"已锁定", dirty:"待更新",
             /* 扣费凭据的四种状态：账单页直接用 A.pill(c.state) 显示中文，
                复用已有的 .pill.running / .held / .succeeded 配色，无需新 CSS。 */
             in_flight:"生成中", pending_review:"待复核",
             settled:"已结算"};
  var cls = state === "in_flight" ? "running"
          : state === "pending_review" ? "held"
          : state === "settled" ? "succeeded" : state;
  return '<span class="pill '+App.esc(cls)+'">'+App.esc(label || map[state] || state)+'</span>';
};
App.pageById = function(id){
  for(var i=0;i<App.PAGES.length;i++){ if(App.PAGES[i].id === id) return App.PAGES[i]; }
  return App.PAGES[0];
};

/* 本地 SVG 合成占位图。不是模型生成结果，不产生任何网络请求。 */
App.art = function(seed, w, h, cap){
  var k = Math.abs(Number(seed) || 1) % 360;
  var hue = (k * 53 + 205) % 360, id = "lg" + k + "_" + w;
  var svg = '<svg viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none" role="img" aria-label="'+App.esc(cap||"占位画面")+'">'
    + '<defs><linearGradient id="'+id+'" x1="0" y1="0" x2="0" y2="1">'
    + '<stop offset="0" stop-color="hsl('+hue+',40%,70%)"/>'
    + '<stop offset="1" stop-color="hsl('+((hue+38)%360)+',36%,26%)"/></linearGradient></defs>'
    + '<rect width="'+w+'" height="'+h+'" fill="url(#'+id+')"/>'
    + '<circle cx="'+(w*0.74)+'" cy="'+(h*0.26)+'" r="'+(h*0.13)+'" fill="rgba(255,255,255,.30)"/>'
    + '<path d="M0 '+(h*0.7)+' Q '+(w*0.28)+' '+(h*0.55)+' '+(w*0.58)+' '+(h*0.74)+' T '+w+' '+(h*0.66)+' L '+w+' '+h+' L 0 '+h+' Z" fill="rgba(9,15,29,.33)"/>'
    + '<rect x="'+(w*0.43)+'" y="'+(h*0.42)+'" width="'+(w*0.085)+'" height="'+(h*0.34)+'" rx="3" fill="rgba(9,15,29,.52)"/>'
    + '<circle cx="'+(w*0.4725)+'" cy="'+(h*0.375)+'" r="'+(h*0.052)+'" fill="rgba(9,15,29,.52)"/>'
    + '</svg>';
  return '<div class="thumb" data-h="'+h+'">'+svg+(cap ? '<div class="cap">'+App.esc(cap)+'</div>' : '')+'</div>';
};

/* AI 生成标识。真实素材（服务端留存/上游返回的文件）一律打标；
   SVG 占位图不是生成结果，打标会让人误以为是模型输出，所以只在有 url 时出现。
   文案优先取服务端 config.ai_label，保证与服务端导出记录、ffmpeg 元数据同一口径。 */
App.aiLabel = function(){
  var cfg = App.DB && App.DB.server && App.DB.server.config;
  return (cfg && cfg.ai_label) || "AI 生成";
};
App.aiDisclosure = function(){
  var cfg = App.DB && App.DB.server && App.DB.server.config;
  return (cfg && cfg.ai_disclosure) || "含 AI 生成内容（非真人实拍）";
};
App.aiBadge = function(){
  return '<span class="badge b-brand xs">'
    + App.esc(App.aiLabel()) + '</span>';
};

/* 离线示例素材标识：与 sample_media.SAMPLE_LABEL、服务端 config.sample_label、
   成片容器 metadata、画面内 drawtext 同一句话。离线演示时所有产物都带它，
   绝不能让人误以为是模型输出。 */
App.sampleLabel = function(){
  var cfg = App.DB && App.DB.server && App.DB.server.config;
  return (cfg && cfg.sample_label) || "示例，非模型生成";
};
App.isOfflineDemo = function(){
  var cfg = App.DB && App.DB.server && App.DB.server.config;
  return !cfg || cfg.offline_demo !== false;
};
App.sampleBadge = function(){
  return '<span class="badge b-warn xs">'
    + App.esc(App.sampleLabel()) + '</span>';
};
/* 离线模式下的真实素材一律是本地确定性示例；remote 下才是模型产物。 */
/* 素材角标：真实模型产物打「AI 生成」标识（合规要求）；离线示例素材画面内已自带标识，
   界面上不再叠加角标。 */
App.mediaBadge = function(){
  return App.providerName() === "mock" ? "" : App.aiBadge();
};
App.sampleHint = function(){
  var cfg = App.DB && App.DB.server && App.DB.server.config;
  return (cfg && cfg.sample_hint)
    || "示例，非模型生成：由本机确定性规则合成，仅用于演示流程与合成链路，不代表模型效果；"
       + "全程未出网、未读取密钥、未产生上游费用";
};
/* 上传元数据在服务端 state 顶层（drama_uploads 表），按 owner 隔离。
   离线 localStorage 态没有 uploads 字段，返回空数组而不是报错。 */
App.uploads = function(kind){
  var all = (App.DB && App.DB.uploads) || [];
  return kind ? all.filter(function(x){ return x.kind === kind; }) : all;
};
App.uploadLimits = function(){
  var cfg = App.DB && App.DB.server && App.DB.server.config;
  return (cfg && cfg.upload_limits) || {};
};
/* 每个上传面板都显示服务端给的限制；config 不可达时用静态兜底，
   但服务端仍独立校验，页面上的数字只是提示。 */
App.uploadLimitText = function(kind){
  var lim = App.uploadLimits()[kind];
  if(!lim) return kind === "bgm" ? "音频 ≤ 20 MB，最多 5 个" : "图片 ≤ 8 MB，最多 24 个";
  return "单个 ≤ " + Math.round(Number(lim.max_bytes) / (1024 * 1024)) + " MB，最多 "
    + lim.max_count + " 个";
};

/* 待复核凭据：「上游结果未知」的那几次调用。
   钱已经预占（冻结），既不能当成功结算，也不能当失败退款 —— 只能由人看一眼上游账单。
   页面必须能看见它们，否则冻结的积分会凭空「消失」在流水里。 */
App.pendingCharges = function(){
  var p = App.project && App.project();
  var all = (App.DB && App.DB.charges) || [];
  return all.filter(function(c){
    return c.state === "pending_review" && (!p || c.projectId === p.id);
  });
};
App.pendingChargesAll = function(){
  return ((App.DB && App.DB.charges) || []).filter(function(c){
    return c.state === "pending_review";
  });
};
/* 成片是否过期：服务端每次落库都会重算 film.stale（合成之后分镜 / 时间线 /
   视频轨 / 字幕配音 / BGM 任一改动即过期）。过期的成片不能导出。 */
App.shotsWithoutVideo = function(p){
  return ((p && p.shots) || []).filter(function(s){ return !s.videoUrl; }).length;
};
App.filmStale = function(p){
  return !!(p && p.film && p.film.videoUrl && p.film.stale);
};
/* 配音混音方式的文案。只有 alignment=subtitle 才是逐句对齐；
   legacy_track_head 是把音色行试听音频整段混在片头，不能写成「逐句配音」。 */
App.voiceMixText = function(alignment, count){
  if(!count) return "无配音";
  return alignment === "legacy_track_head"
    ? "配音 " + count + " 条混在片头（未按句对齐）"
    : "逐句配音 " + count + " 条";
};
/* 「生成中」的唯一可刷新真源：服务端两段式在出网前把预占落成 state=in_flight
   的凭据，随 /state 回包、由 applyState 灌进 A.DB 并存 localStorage。
   pending[name] / busybar 都是内存态，刷新即丢，不能拿来做这个判断。
   target 形如 shot:S1 / asset:A1，用来把状态映射回具体镜头或资产；
   空串表示项目级动作（如剧本医生），整页显示但不指向某个镜头。 */
App.inFlightCharges = function(pid){
  return ((App.DB && App.DB.charges) || []).filter(function(c){
    return c.state === "in_flight" && (!pid || c.projectId === pid);
  });
};
App.busyTargets = function(pid, kind){
  var prefix = kind ? kind + ":" : "";
  return App.inFlightCharges(pid).map(function(c){ return String(c.target || ""); })
    .filter(function(t){ return !prefix || t.indexOf(prefix) === 0; });
};
App.isBusy = function(pid, kind, id){
  return App.busyTargets(pid, kind).indexOf(kind + ":" + id) >= 0;
};
/* 管理动作（改定价 / 导入 / 复核 / 加余额）在页面上必须按身份出现：
   非管理员看不到按钮，也点不动。 */
App.isAdmin = function(){
  var cfg = App.DB && App.DB.server && App.DB.server.config;
  return !!(cfg && cfg.admin);
};

/* 有真实素材就渲染真实素材，没有才退回 SVG 占位。
   这是「生成结果绑定到对象」的渲染侧：资产卡看 imageUrl、镜头看 videoUrl、
   配音行看 audioUrl。占位图永远带「占位」字样，不会被当成模型输出。 */
App.uploadBadge = function(){
  return '<span class="badge b-p2 xs">上传</span>';
};
App.mediaTile = function(url, kind, seed, w, h, cap, badge){
  if(url){
    /* 上传素材既不是模型产物也不是离线示例，可由调用方传中性角标覆盖。 */
    /* badge 显式传 "" 表示不要角标（如项目封面、上传缩略图）。 */
    var mark = badge === undefined || badge === null ? App.mediaBadge() : badge;
    var label = (cap || mark)
      ? '<div class="cap">' + (cap ? '<span>' + App.esc(cap) + '</span>' : '') + mark + '</div>' : '';
    if(kind === "video"){
      return '<div class="thumb" data-h="'+h+'"><video src="'+App.esc(App.frameUrl(url))+'" '
        + 'controls preload="metadata" class="fill"></video>'
        + label + '</div>';
    }
    return '<div class="thumb" data-h="'+h+'"><img src="'+App.esc(url)+'" alt="'+App.esc(cap||"真实素材")+'" '
      + 'class="fill">'
      + label + '</div>';
  }
  return App.art(seed, w, h, cap);
};

/* 视频地址加 #t=0.1，浏览器会把第一帧当封面，而不是显示一块灰色。 */
App.frameUrl = function(url){
  var u = String(url || "");
  return u && u.indexOf("#") < 0 ? u + "#t=0.1" : u;
};
App.audioTile = function(url){
  if(!url) return '<span class="hint">—</span>';
  return '<audio src="'+App.esc(url)+'" controls preload="none" class="aud"></audio>';
};

App.toast = function(msg){
  var t = document.getElementById("toast");
  if(!t) return;
  t.textContent = msg;
  t.className = "toast show";
  clearTimeout(App._toastTimer);
  App._toastTimer = setTimeout(function(){ t.className = "toast"; }, 2800);
};
