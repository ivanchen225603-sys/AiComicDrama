/* datasource.js — 后端 API 客户端 + 离线降级。
 *
 * 两种数据源：
 *  - server（默认探测）：页面由后端同源提供，所有业务动作 POST 到
 *    /api/v1/drama/action，服务端执行后回传完整 state。
 *  - offline：后端不可达时全部走本地 localStorage 实现。
 */
(function(A){
  "use strict";

  A.SERVER_ENDPOINTS = [
    {method:"GET",  path:"/api/v1/drama/config",       use:"能力清单 / 最后一公里"},
    {method:"GET",  path:"/api/v1/drama/state",        use:"完整业务状态快照"},
    {method:"POST", path:"/api/v1/drama/action",       use:"服务端执行全部业务动作"},
    {method:"POST", path:"/api/v1/drama/reset",        use:"重置演示数据"},
    {method:"GET",  path:"/api/v1/drama/export",       use:"导出 JSON"},
    {method:"POST", path:"/api/v1/drama/import",       use:"导入 JSON"},
    {method:"GET",  path:"/api/v1/drama/assets/{asset_id}", use:"真实素材本地留存下载"},
    {method:"POST", path:"/api/v1/drama/uploads",      use:"BGM / 参考图上传（raw body，不扣积分）"},
    {method:"DELETE", path:"/api/v1/drama/uploads/{upload_id}", use:"删除已上传素材"},
    {method:"GET",  path:"/api/v1/drama/reconcile",    use:"账本只读概览"}
  ];

  /* 后端 config 不可用时的静态兜底，仅用于展示，不作为事实来源。 */
  /* 与 comicdrama/mock_media.py::LAST_MILE 保持一致；仅离线时作为展示兜底，
     在线时 admin 页优先采用 /api/v1/drama/config 返回的后端清单。 */
  A.LAST_MILE_FALLBACK = [
    "付费开通上游真实漫剧模型（LLM 剧本 / TTS 配音 / 图生视频 / 渲染合成）",
    "取得并配置上游凭据环境变量（本功能阶段不读、不存任何真实 key）",
    "实测 LLM 端点是否支持 JSON 输出、核对响应包装形态",
    "实测上游单次真实计费字段（官方「参数说明」表当前仍为「暂无数据」）",
    "实测 videos[] 节点参数、模板 uuid、响应包装形态与 OSS 白名单",
    "把 drama 演示账本接到上游真实账单并做对账（当前是内部计价规则）",
    "按首次付费实测结果核对真实配音 / 视频素材的转存链路（上游 URL 只有 7 天时效）"
  ];

  A.dsUrl = function(path){
    /* 同源：8001 下打 8001，8123 下打 8123（后者无 API，探测会降级）。 */
    return path;
  };

  function request(path, options){
    var opts = options || {};
    /* 超时按调用点给，不再全局 8 秒。
       旧实现一律 8s 后 abort，而真实 LLM 单次可达 60s、媒体生成最长 900s：
       结果就是「任务其实成功了，前端却判定后端不可达并降级离线」，
       用户再点一次就是重复提交。 */
    var limit = Number(opts.timeout) > 0 ? Number(opts.timeout) : 20000;
    var controller = typeof AbortController !== "undefined" ? new AbortController() : null;
    var timer = controller ? setTimeout(function(){ controller.abort(); }, limit) : null;
    var init = {
      method: opts.method || "GET",
      credentials: "same-origin",
      headers: {"Accept":"application/json"},
      signal: controller ? controller.signal : undefined
    };
    if(opts.headers){
      for(var h in opts.headers){ if(Object.prototype.hasOwnProperty.call(opts.headers,h)) init.headers[h] = opts.headers[h]; }
    }
    /* 上传刻意走 application/octet-stream 原始 body + 查询串元数据：
       服务端不依赖 python-multipart（干净导出树的离线门禁不保证装了它）。
       注意不能走上面的 JSON 分支，否则文件会被 JSON.stringify 成 "{}"。 */
    if(opts.raw !== undefined){
      init.headers["Content-Type"] = "application/octet-stream";
      init.body = opts.raw;
    } else if(opts.body !== undefined){
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    return fetch(A.dsUrl(path), init).then(function(res){
      return res.json().catch(function(){ return {}; }).then(function(data){
        if(!res.ok){
          throw {network:false, status:res.status, code:data.error || "",
                 message:A.friendlyError(data.error, data.message || data.error || ("HTTP " + res.status))};
        }
        return data;
      });
    }).catch(function(e){
      if(e && e.network === false) throw e;
      /* 超时与不可达必须分开：超时说明请求可能已被服务端接收（甚至已扣费），
         绝不能把它当成「后端挂了」而降级离线，否则界面会与真源脱节。 */
      if(e && e.name === "AbortError"){
        throw {network:true, timeout:true, message:"请求超时"};
      }
      throw {network:true, timeout:false, message:"网络连接已断开"};
    }).finally(function(){ if(timer) clearTimeout(timer); });
  }

  A.remote = {
    ready: false,
    request: request,
    config: function(){ return request("/api/v1/drama/config"); },
    state: function(){ return request("/api/v1/drama/state"); },
    /* opts.timeout 按动作分级；opts.requestId 是幂等键：
       服务端对同一 (owner, request_id) 只执行并扣费一次，重复提交回放上次结果。 */
    action: function(type, args, projectId, opts){
      opts = opts || {};
      var body = {type:type, args:args || [], project_id:projectId || null};
      var headers = {};
      if(opts.requestId){
        body.request_id = opts.requestId;
        headers["Idempotency-Key"] = opts.requestId;
      }
      return request("/api/v1/drama/action", {
        method:"POST", body:body, headers:headers, timeout:opts.timeout
      });
    },
    reset: function(){ return request("/api/v1/drama/reset", {method:"POST", timeout:30000}); },
    /* 用户自填的模型密钥：服务端只回打码视图，原值永不下发。 */
    saveCredentials: function(fields){
      return request("/api/v1/drama/credentials", {method:"PUT", body:fields, timeout:20000});
    },
    clearCredentials: function(scope){
      return request("/api/v1/drama/credentials?scope=" + encodeURIComponent(scope || "all"),
        {method:"DELETE", timeout:20000});
    },
    testCredentials: function(){
      return request("/api/v1/drama/credentials/test", {method:"POST", timeout:30000});
    },
    reconcile: function(){ return request("/api/v1/drama/reconcile", {timeout:20000}); },
    exportDoc: function(){ return request("/api/v1/drama/export", {timeout:30000}); },
    importDoc: function(doc){
      return request("/api/v1/drama/import", {method:"POST", body:{doc:doc}, timeout:60000});
    },
    /* 上传不经过 /action，也就不进 ACTION_PRICES：上传本身不扣积分。
       元数据走查询串，文件字节走 raw body；服务端按魔数判定格式。 */
    uploadMedia: function(kind, raw, meta){
      meta = meta || {};
      var q = "?kind=" + encodeURIComponent(kind)
        + "&name=" + encodeURIComponent(meta.name || "")
        + "&asset_type=" + encodeURIComponent(meta.asset_type || "")
        + "&asset_name=" + encodeURIComponent(meta.asset_name || "");
      return request("/api/v1/drama/uploads" + q,
        {method:"POST", raw:raw, timeout:120000});
    },
    deleteUpload: function(uploadId){
      return request("/api/v1/drama/uploads/" + encodeURIComponent(uploadId),
        {method:"DELETE", timeout:30000});
    }
  };

  /* ---- 账户与充值 ----
     会话是 HttpOnly Cookie：前端既读不到也伪造不了，所以「是否已登录」只能问服务端。
     这里缓存最近一次探测结果，只用来决定页面渲染成登录表单还是账户信息。
     注意 401 是正常分支（还没登录），不是错误，不能弹提示。 */
  A.account = {loaded:false, loading:false, me:null,
               orders:null, ordersLoaded:false, ordersLoading:false,
               draft:null, draftRequestId:"", creating:false};

  A.accountApi = {
    me: function(){ return request("/api/v1/drama/account/me", {timeout:15000}); },
    register: function(username, password){
      return request("/api/v1/drama/account/register",
        {method:"POST", body:{username:username, password:password}, timeout:30000});
    },
    login: function(username, password){
      return request("/api/v1/drama/account/login",
        {method:"POST", body:{username:username, password:password}, timeout:30000});
    },
    logout: function(){
      return request("/api/v1/drama/account/logout", {method:"POST", timeout:30000});
    },
    password: function(oldPassword, newPassword){
      return request("/api/v1/drama/account/password",
        {method:"POST", body:{old_password:oldPassword, new_password:newPassword}, timeout:30000});
    }
  };

  A.paymentApi = {
    createOrder: function(amountPoints, requestId){
      return request("/api/v1/drama/payment/orders",
        {method:"POST", body:{amount_points:amountPoints, request_id:requestId}, timeout:30000});
    },
    orders: function(){ return request("/api/v1/drama/payment/orders", {timeout:20000}); },
    submit: function(orderId){
      return request("/api/v1/drama/payment/orders/submit",
        {method:"POST", body:{order_id:orderId}, timeout:30000});
    },
    cancel: function(orderId){
      return request("/api/v1/drama/payment/orders/cancel",
        {method:"POST", body:{order_id:orderId}, timeout:30000});
    },
    /* 本地体验渠道：不传 payload / signature，由服务端自己构造一次模拟回调并当场验签。 */
    pay: function(orderId){
      return request("/api/v1/drama/payment/orders/callback",
        {method:"POST", body:{order_id:orderId}, timeout:60000});
    }
  };

  /* 探测登录态。只探测一次（loading/loaded 双闸门），失败按未登录处理。
     结果决定账户页与充值区渲染成「表单」还是「已登录」，所以回来后就地重绘一次：
     用户可能是直接落在账单页的，不经过账户页也要拿到正确的登录态。 */
  A.accountProbe = function(){
    var acc = A.account;
    if(acc.loaded || acc.loading || !A.remoteMode()) return null;
    acc.loading = true;
    return request("/api/v1/drama/account/me", {timeout:15000})
      .then(function(me){ acc.me = me || null; })
      .catch(function(e){
        /* 401 = 未登录；其它失败也只是「现在没法确认」，一律按未登录渲染。 */
        acc.me = (e && e.status === 401) ? null : acc.me;
      })
      .then(function(){
        acc.loading = false; acc.loaded = true;
        var page = A.DB && A.DB.ui && A.DB.ui.page;
        if((page === "account" || page === "credits") && typeof A.render === "function"){
          A.render();
        }
        return acc.me;
      });
  };

  /* 订单列表：进入账单页拉一次，缓存在 A.account.orders。
     成功与失败都置 ordersLoaded，否则失败时会在每次重绘里反复请求。 */
  A.loadOrders = function(refresh){
    var acc = A.account;
    if(!A.remoteMode() || !acc.me) return null;
    if(acc.ordersLoading) return null;
    if(acc.ordersLoaded && !refresh) return null;
    acc.ordersLoading = true;
    return A.paymentApi.orders().then(function(r){
      acc.orders = (r && r.orders) || [];
      return acc.orders;
    }).catch(function(){
      return acc.orders;
    }).then(function(list){
      acc.ordersLoading = false; acc.ordersLoaded = true;
      return list;
    });
  };

  /* 按日对账报表。只读接口，管理页专用（服务端闸门在
     DramaService.daily_report 里，非管理员一律 403）。
     导出直接给一个同源 URL 让浏览器下载，不走 fetch+Blob：
     省一次 JS 拼装，也不会把整张表复制进内存。 */
  A.reportApi = {
    daily: function(days){
      return request("/api/v1/drama/report/daily?days=" + encodeURIComponent(days || 30),
        {timeout: 20000});
    },
    csvUrl: function(days){
      return A.dsUrl("/api/v1/drama/report/daily.csv?days=" + encodeURIComponent(days || 30));
    }
  };
  A.report = {loaded:false, loading:false, days:30, data:null};

  /* 上传与删除都回传完整 state，直接灌进 A.DB 再重绘。
     离线 localStorage 态没有服务端素材目录，必须明确报错，不能假装上传成功。 */
  function fileBytes(file){
    if(file && typeof file.arrayBuffer === "function") return file.arrayBuffer();
    return new Promise(function(resolve, reject){
      if(!file || typeof FileReader === "undefined"){
        reject(new Error("当前环境无法读取本地文件")); return;
      }
      var reader = new FileReader();
      reader.onload = function(){ resolve(reader.result); };
      reader.onerror = function(){ reject(new Error("读取文件失败")); };
      reader.readAsArrayBuffer(file);
    });
  }
  A.uploadMedia = async function(kind, file, meta){
    if(!A.remoteMode()){
      throw new Error("当前无法上传，请检查网络连接");
    }
    if(!file){ throw new Error("请先选择要上传的文件"); }
    var raw = await fileBytes(file);
    var state = await A.remote.uploadMedia(kind, raw, meta || {});
    A.applyState(state); A.render();
    return state;
  };
  A.removeUpload = async function(uploadId){
    if(!A.remoteMode()){
      throw new Error("当前无法删除，请检查网络连接");
    }
    var state = await A.remote.deleteUpload(uploadId);
    A.applyState(state); A.render();
    return state;
  };

  /* 在线态判定 + 三个「整库级」动作的服务端通道。
     这些动作不能只改 localStorage，否则下一次 action 返回服务端 state 后会被覆盖回去。 */
  A.remoteMode = function(){
    return A.DB.datasource === "server" && !!(A.remote && A.remote.ready);
  };
  A.resetRemote = async function(){
    var state = await A.remote.reset();
    A.applyState(state); A.render();
    return state;
  };
  A.exportRemote = async function(){ return A.remote.exportDoc(); };
  A.importRemote = async function(doc){
    var state = await A.remote.importDoc(doc);
    A.applyState(state); A.render();
    return state;
  };

  A.probeServer = async function(){
    var r = A.DB.server;
    r.checkedAt = A.now();
    try {
      var cfg = await A.remote.config();
      r.reachable = true; r.error = null; r.config = cfg;
      A.loginRequired = false;
      r.capabilities = cfg.capabilities || [];
      r.lastMile = cfg.last_mile || A.LAST_MILE_FALLBACK;
      A.DB.datasource = "server";
      A.remote.ready = true;
      var state = await A.remote.state();
      A.applyState(state);
      A.save();
      return true;
    } catch(e){
      if(e && e.status === 401){
        /* 服务器要求登录：服务端可达，只是还没登录。转到账户页，不降级成离线。 */
        A.loginRequired = true;
        A.DB.datasource = "server";
        A.remote.ready = true;
        A.DB.ui.page = "account";
        return false;
      }
      var msg = (e && e.message) ? e.message : "未知错误";
      r.reachable = false; r.config = null;
      r.lastMile = A.LAST_MILE_FALLBACK;
      r.error = msg + "。请从服务地址直接打开页面，或保持离线模式。";
      A.DB.datasource = "offline";
      A.remote.ready = false;
      A.save();
      return false;
    }
  };

  A.setDatasource = function(mode){
    if(mode === "offline"){
      A.DB.datasource = "offline";
      A.DB.server.reachable = false;
      A.remote.ready = false;
      A.save();
      return "已切换到离线模式：所有读写都在本地，不发起任何请求";
    }
    return "请先执行『探测后端』，探测成功才会切换到服务器模式";
  };

  /* 只读快照：读取后端已有接口，不覆盖本地业务数据。 */
  A.syncServerSnapshot = async function(){
    if(A.DB.datasource !== "server" || !A.DB.server.reachable){
      throw new Error("当前未连接服务器");
    }
    var paths = ["/api/v1/drama/config", "/api/v1/drama/state", "/api/v1/drama/reconcile"];
    var snapshot = {time:A.now(), items:[]};
    for(var i=0;i<paths.length;i++){
      try {
        var data = await request(paths[i]);
        snapshot.items.push({path:paths[i], ok:true, status:200,
          bytes:JSON.stringify(data).length});
      } catch(e){
        snapshot.items.push({path:paths[i], ok:false,
          status:(e && e.status) || 0, error:String((e && e.message) || e)});
      }
    }
    A.DB.server.snapshot = snapshot;
    A.save();
    return snapshot;
  };
})(App);
