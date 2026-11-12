/* ui.js — 通用组件。所有交互通过 data-act 事件委托，无内联脚本。 */
(function(A){
  "use strict";

  /* 按钮上的 data-act 与服务端计价动作名不完全一致，这里做一次映射。 */
  var PRICED = {"gen-asset-candidates":"generateAssetCandidates"};

  /* 生成类按钮：带消耗积分的小角标，用户点之前就知道扣多少。 */
  A.genBtn = function(label, act, attrs, cls){
    var price = A.priceOf(PRICED[act] || act);
    var cost = (typeof price === "number" && price > 0)
      ? '<span class="cost">' + price + '</span>' : "";
    return '<button class="btn ' + (cls || "sm") + '" data-act="' + A.esc(act) + '"'
      + (attrs || "") + '>' + A.esc(label) + cost + '</button>';
  };
  /* 旧名保留：第四个参数过去是说明文字，现在一律忽略。 */
  A.mockBtn = function(label, act, attrs){ return A.genBtn(label, act, attrs, "sm"); };
  A.mockNote = function(){ return ""; };

  A.pageHead = function(title, sub, actions){
    return '<div class="page-head"><div><h1>' + A.esc(title) + '</h1>'
      + (sub ? '<p class="sub">' + A.esc(sub) + '</p>' : '') + '</div>'
      + (actions ? '<div class="btnbar tight">' + actions + '</div>' : '') + '</div>';
  };
  A.specBar = function(pageId, title, sub, actions){
    return A.pageHead(title || A.pageById(pageId).name, sub, actions);
  };
  A.closeDrawer = function(){ document.getElementById("drawer").className = "drawer"; };
  /* 窄屏（≤1024）侧栏是覆盖式抽屉，Esc 关掉浮层时一并收起，避免挡住内容。 */
  A.closeNav = function(){
    if(typeof A.isNarrow !== "function" || !A.isNarrow() || A.DB.ui.navClosed) return;
    A.DB.ui.navClosed = true; A.renderNav();
  };

  /* ---- 表单控件 ---- */
  A.fInput = function(label, name, value, opts){
    opts = opts || {};
    return '<div class="field"><label>'+A.esc(label)+'</label><input name="'+A.esc(name)+'" '
      + 'type="'+(opts.type||"text")+'" value="'+A.esc(value==null?"":value)+'"'
      + (opts.placeholder ? ' placeholder="'+A.esc(opts.placeholder)+'"' : "")
      + (opts.min != null ? ' min="'+opts.min+'"' : "")
      + (opts.max != null ? ' max="'+opts.max+'"' : "") + '></div>';
  };
  A.fSelect = function(label, name, options, value){
    var opts = options.map(function(o){
      var v = typeof o === "string" ? o : o.value, t = typeof o === "string" ? o : o.label;
      return '<option value="'+A.esc(v)+'"'+(String(v)===String(value)?" selected":"")+'>'+A.esc(t)+'</option>';
    }).join("");
    return '<div class="field"><label>'+A.esc(label)+'</label><select name="'+A.esc(name)+'">'+opts+'</select></div>';
  };
  A.fTextarea = function(label, name, value, rows, placeholder){
    return '<div class="field"><label>'+A.esc(label)+'</label><textarea name="'+A.esc(name)+'" rows="'+(rows||4)+'"'
      + (placeholder ? ' placeholder="'+A.esc(placeholder)+'"' : '') + '>'
      + A.esc(value==null?"":value)+'</textarea></div>';
  };
  A.kv = function(rows){
    return rows.map(function(r){
      return '<div class="kv"><span>'+A.esc(r[0])+'</span><b>'+(r[2] ? r[1] : A.esc(r[1]))+'</b></div>';
    }).join("");
  };
  A.empty = function(msg, icon){
    return '<div class="empty">' + A.icon(icon || "inbox") + msg + '</div>';
  };
  A.banner = function(html, kind){
    return '<div class="banner' + (kind ? " " + kind : "") + '"><div>' + html + '</div></div>';
  };

  /* ---- 模态框 ---- */
  A._modalSubmit = null;
  A.openModal = function(cfg){
    var mask = document.getElementById("modal-mask"), box = document.getElementById("modal");
    box.innerHTML = '<h2>'+A.esc(cfg.title)+'</h2>'
      + (cfg.note ? '<div class="notice">'+cfg.note+'</div>' : "")
      + (cfg.body || "")
      + '<div class="btnbar">'
      + '<button class="btn" data-act="close-modal">取消</button>'
      + (cfg.submitLabel === null ? "" : '<button class="btn primary" data-act="modal-submit">'+A.esc(cfg.submitLabel||"保存")+'</button>')
      + '</div>';
    A._modalSubmit = cfg.onSubmit || null;
    mask.hidden = false;
  };
  A.closeModal = function(){
    document.getElementById("modal-mask").hidden = true;
    A._modalSubmit = null;
  };
  A.confirmModal = function(title, message, onYes){
    A.openModal({title:title, body:'<p class="confirm">'+A.esc(message)+'</p>',
                 submitLabel:"确认", onSubmit:function(){ onYes(); }});
  };
  A.submitModal = async function(){
    if(!A._modalSubmit) { A.closeModal(); return; }
    var form = {};
    var nodes = document.querySelectorAll("#modal [name]");
    for(var i=0;i<nodes.length;i++){
      var el = nodes[i];
      form[el.name] = el.type === "checkbox" ? el.checked : el.value;
    }
    try {
      var r = await A._modalSubmit(form);
      if(r !== false){ A.closeModal(); A.render(); }
    } catch(e){
      A.toast(e && e.message ? e.message : String(e));
    }
  };
})(App);
