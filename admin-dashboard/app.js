// ── Stockwin Admin Dashboard ──
// 独立管理后台，通过 API 调用生产环境

var API_BASE = localStorage.getItem('admin_api_base') || 'https://astock.fly.dev';
var TOKEN = localStorage.getItem('admin_token') || '';

// ── API 封装 ──
function api(path, opts) {
  opts = opts || {};
  var url = API_BASE + '/api/v1' + path;
  var headers = { 'Content-Type': 'application/json' };
  if (TOKEN) headers['Authorization'] = 'Bearer ' + TOKEN;
  return fetch(url, {
    method: opts.method || 'GET',
    headers: headers,
    body: opts.body ? JSON.stringify(opts.body) : undefined
  }).then(function(r) {
    if (r.status === 401) { doLogout(); throw new Error('登录已过期'); }
    return r.json().then(function(data) {
      if (r.ok) return data;
      throw new Error(data.detail || data.message || '请求失败 (' + r.status + ')');
    });
  });
}
var GET = function(p) { return api(p); };
var POST = function(p, b) { return api(p, { method: 'POST', body: b }); };
var DELETE = function(p) { return api(p, { method: 'DELETE' }); };

// ── 登录 ──
function doLogin() {
  var phone = document.getElementById('lgPhone').value.trim();
  var pwd = document.getElementById('lgPwd').value;
  var server = document.getElementById('lgServer').value.trim();
  if (server) {
    API_BASE = server.replace(/\/+$/, '');
    localStorage.setItem('admin_api_base', API_BASE);
  }
  document.getElementById('lgBtn').disabled = true;
  document.getElementById('lgError').textContent = '';
  fetch(API_BASE + '/api/v1/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ phone: phone, password: pwd })
  }).then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
    .then(function(r) {
      if (!r.ok) throw new Error(r.data.detail || '登录失败');
      if (r.data.data.tier !== 99) { doLogout(); throw new Error('非管理员账号，无法登录管理后台'); }
      TOKEN = r.data.data.access_token;
      localStorage.setItem('admin_token', TOKEN);
      // 验证 token 拿到用户信息
      document.getElementById('topUser').textContent = phone;
      document.getElementById('topServer').textContent = API_BASE.replace('https://','');
      showMain();
    }).catch(function(e) {
      document.getElementById('lgError').textContent = e.message;
      document.getElementById('lgBtn').disabled = false;
    });
  return false;
}

function doLogout() {
  TOKEN = '';
  localStorage.removeItem('admin_token');
  document.getElementById('loginPage').style.display = '';
  document.getElementById('mainPage').style.display = 'none';
}

function showMain() {
  document.getElementById('loginPage').style.display = 'none';
  document.getElementById('mainPage').style.display = '';
  loadOverview();
}

// ── Tab 切换 ──
document.querySelector('.tabs').addEventListener('click', function(e) {
  var tab = e.target.closest('.tab');
  if (!tab) return;
  document.querySelectorAll('.tab').forEach(function(t) { t.classList.remove('active'); });
  tab.classList.add('active');
  var name = tab.getAttribute('data-tab');
  ['overview','users','codes','tasks','cache','logs'].forEach(function(s) {
    document.getElementById('sec-' + s).style.display = s === name ? '' : 'none';
  });
  if (name === 'overview') loadOverview();
  if (name === 'users') loadUsers();
  if (name === 'codes') loadCodes();
  if (name === 'tasks') loadTasks();
  if (name === 'cache') loadCacheStats();
  if (name === 'logs') loadLogs();
});

// ── Modal ──
function openModal(id) { document.getElementById(id).classList.add('show'); }
function closeModal(id) { document.getElementById(id).classList.remove('show'); }
document.querySelectorAll('.modal').forEach(function(m) {
  m.addEventListener('click', function(e) { if (e.target === m) m.classList.remove('show'); });
});

// ══════════════════════════════════════════
// 概览
// ══════════════════════════════════════════
function loadOverview() {
  GET('/admin/users/stats').then(function(r) {
    var d = r.data;
    var labels = { 1:'免费', 2:'月VIP', 3:'年VIP', 99:'管理员' };
    var tierHtml = Object.keys(d.tier_distribution || {}).map(function(k) {
      return labels[k] + ': ' + d.tier_distribution[k];
    }).join('  ');
    document.getElementById('ovStats').innerHTML =
      '<div class="stat-card"><div class="sl">总用户数</div><div class="sv">' + d.total_users + '</div></div>' +
      '<div class="stat-card"><div class="sl">今日新增</div><div class="sv up">' + d.today_new + '</div></div>' +
      '<div class="stat-card"><div class="sl">本周新增</div><div class="sv">' + d.week_new + '</div></div>' +
      '<div class="stat-card"><div class="sl">今日诊股</div><div class="sv">' + d.today_diagnosis_count + '</div></div>' +
      '<div class="stat-card" style="grid-column:span 2"><div class="sl">等级分布</div><div class="sv" style="font-size:0.9rem">' + (tierHtml || '--') + '</div></div>';
  }).catch(function(e) { toast('加载概览失败: ' + e.message); });

  GET('/admin/dashboard/trend').then(function(r) {
    var days = r.data.days || [];
    var dom = document.getElementById('ovChart');
    if (window._ovChart) window._ovChart.dispose();
    var chart = echarts.init(dom);
    window._ovChart = chart;
    chart.setOption({
      tooltip: { trigger: 'axis' },
      legend: { data: ['新增用户','诊股次数','API调用'], bottom: 0, textStyle: { color: '#999', fontSize: 11 } },
      grid: { left: '5%', right: '4%', top: 20, bottom: 35 },
      xAxis: { type: 'category', data: days.map(function(d) { return d.date.slice(5); }), axisLabel: { color: '#888' } },
      yAxis: { type: 'value', axisLabel: { color: '#888' }, splitLine: { lineStyle: { color: 'rgba(255,255,255,0.06)' } } },
      series: [
        { name: '新增用户', type: 'bar', data: days.map(function(d) { return d.new_users; }), itemStyle: { color: '#3b82f6' } },
        { name: '诊股次数', type: 'line', data: days.map(function(d) { return d.diagnosis_count; }), itemStyle: { color: '#22c55e' }, smooth: true },
        { name: 'API调用', type: 'line', data: days.map(function(d) { return d.api_calls; }), itemStyle: { color: '#f59e0b' }, smooth: true }
      ]
    });
  }).catch(function() {});
}

// ══════════════════════════════════════════
// 用户管理
// ══════════════════════════════════════════
var userPage = 1;
function loadUsers(p) {
  userPage = p || userPage;
  var search = document.getElementById('uSearch').value;
  var tier = document.getElementById('uTier').value;
  var qs = '?page=' + userPage + '&page_size=15&search=' + encodeURIComponent(search);
  if (tier) qs += '&tier_filter=' + tier;
  GET('/admin/users' + qs).then(function(r) {
    var d = r.data;
    var html = '';
    (d.items || []).forEach(function(u) {
      var tierLabel = {1:'免费',2:'月VIP',3:'年VIP',99:'管理员'}[u.tier] || '游客';
      var tierCls = u.tier >= 99 ? 'tag-a' : u.tier >= 2 ? 'tag-v' : 'tag-f';
      var status = u.is_active ? '<span style="color:#22c55e">正常</span>' : '<span style="color:#ef4444">禁用</span>';
      html += '<tr><td>' + u.id + '</td><td>' + u.phone_masked + '</td>' +
        '<td><span class="tag ' + tierCls + '">' + tierLabel + '</span></td>' +
        '<td>' + u.credits + '</td><td>' + status + '</td>' +
        '<td>' + (u.member_expire || '--') + '</td><td>' + (u.created_at ? u.created_at.slice(0,10) : '--') + '</td>' +
        '<td><div class="btn-row">' +
        '<button class="btn-outline btn-xs" onclick="viewUser(' + u.id + ')">详情</button>' +
        '<button class="btn-outline btn-xs" onclick="showAdjustCredits(' + u.id + ')">积分</button>' +
        '<button class="btn-outline btn-xs" onclick="showAdjustTier(' + u.id + ')">等级</button>' +
        '<button class="btn-outline btn-xs" onclick="showResetPwd(' + u.id + ')">密码</button>' +
        '<button class="btn-outline btn-xs" onclick="toggleActive(' + u.id + ')">' + (u.is_active ? '禁用' : '启用') + '</button>' +
        '</div></td></tr>';
    });
    document.getElementById('uTbody').innerHTML = html || '<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:20px;">暂无数据</td></tr>';
    var tp = Math.ceil(d.total / d.page_size) || 1;
    document.getElementById('uPager').innerHTML =
      '<button ' + (userPage <= 1 ? 'disabled' : '') + ' onclick="loadUsers(' + (userPage - 1) + ')">上一页</button>' +
      '<span>' + userPage + ' / ' + tp + '</span>' +
      '<button ' + (userPage >= tp ? 'disabled' : '') + ' onclick="loadUsers(' + (userPage + 1) + ')">下一页</button>';
  }).catch(function(e) { toast(e.message); });
}

function viewUser(uid) {
  GET('/admin/users/' + uid).then(function(r) {
    var u = r.data.user, l = r.data.ledger || [], ds = r.data.diagnosis_stats || {};
    var html = '<div class="detail-sec"><h4>基本信息</h4>' +
      '<p>ID: ' + u.id + ' | 手机: ' + u.phone_masked + ' | 等级: ' + u.tier + ' | 积分: ' + u.credits + '</p>' +
      '<p>状态: ' + (u.is_active ? '正常' : '禁用') + ' | 会员到期: ' + (u.member_expire || '无') + ' | 注册: ' + (u.created_at || '--') + '</p>' +
      '<p>诊股 ' + (ds.total || 0) + ' 次 | AI分析 ' + (ds.ai_analysis || 0) + ' 次</p></div>';
    if (l.length) {
      html += '<div class="detail-sec"><h4>积分流水（最近50条）</h4><table><thead><tr><th>类型</th><th>金额</th><th>余额</th><th>备注</th><th>时间</th></tr></thead><tbody>';
      l.forEach(function(x) {
        html += '<tr><td>' + x.type + '</td><td>' + x.amount + '</td><td>' + x.balance_after + '</td><td>' + (x.note||'') + '</td><td>' + (x.created_at ? x.created_at.slice(0,16) : '') + '</td></tr>';
      });
      html += '</tbody></table></div>';
    }
    document.getElementById('userModalBody').innerHTML = html;
    openModal('userModal');
  }).catch(function(e) { toast(e.message); });
}

function showAdjustCredits(uid) {
  document.getElementById('actionModalTitle').textContent = '积分调整 (UID: ' + uid + ')';
  document.getElementById('actionModalBody').innerHTML =
    '<label class="field"><span>金额（正增 / 负减）</span><input type="number" id="actAmt" /></label>' +
    '<label class="field"><span>备注（必填）</span><input type="text" id="actNote" /></label>' +
    '<button class="btn-primary btn-sm" onclick="doAdjustCredits(' + uid + ')">确认调整</button>' +
    '<p id="actErr" style="color:var(--danger);font-size:0.8rem;margin-top:8px;"></p>';
  openModal('actionModal');
}
function doAdjustCredits(uid) {
  var amt = parseInt(document.getElementById('actAmt').value);
  var note = document.getElementById('actNote').value.trim();
  if (!amt && amt !== 0) { document.getElementById('actErr').textContent = '请输入金额'; return; }
  if (!note) { document.getElementById('actErr').textContent = '请输入备注'; return; }
  POST('/admin/users/' + uid + '/credits', { amount: amt, note: note }).then(function(r) {
    closeModal('actionModal'); toast('调整完成，当前积分: ' + r.data.credits); loadUsers();
  }).catch(function(e) { document.getElementById('actErr').textContent = e.message; });
}

function showAdjustTier(uid) {
  document.getElementById('actionModalTitle').textContent = '等级调整 (UID: ' + uid + ')';
  document.getElementById('actionModalBody').innerHTML =
    '<label class="field"><span>等级</span><select id="actTier" class="inp" style="max-width:100%;width:100%"><option value="1">免费</option><option value="2">月VIP</option><option value="3">年VIP</option><option value="99">管理员</option></select></label>' +
    '<label class="field"><span>会员到期日（ISO，可选）</span><input type="text" id="actExpire" placeholder="如 2026-12-31" /></label>' +
    '<button class="btn-primary btn-sm" onclick="doAdjustTier(' + uid + ')">确认调整</button>' +
    '<p id="actErr" style="color:var(--danger);font-size:0.8rem;margin-top:8px;"></p>';
  openModal('actionModal');
}
function doAdjustTier(uid) {
  var tier = parseInt(document.getElementById('actTier').value);
  var expire = document.getElementById('actExpire').value.trim() || null;
  POST('/admin/users/' + uid + '/tier', { tier: tier, member_expire: expire }).then(function() {
    closeModal('actionModal'); toast('等级调整完成'); loadUsers();
  }).catch(function(e) { document.getElementById('actErr').textContent = e.message; });
}

function showResetPwd(uid) {
  document.getElementById('actionModalTitle').textContent = '重置密码 (UID: ' + uid + ')';
  document.getElementById('actionModalBody').innerHTML =
    '<label class="field"><span>新密码（6-32位）</span><input type="text" id="actPwd" /></label>' +
    '<button class="btn-primary btn-sm" onclick="doResetPwd(' + uid + ')">确认重置</button>' +
    '<p id="actErr" style="color:var(--danger);font-size:0.8rem;margin-top:8px;"></p>';
  openModal('actionModal');
}
function doResetPwd(uid) {
  var pwd = document.getElementById('actPwd').value.trim();
  if (pwd.length < 6) { document.getElementById('actErr').textContent = '密码至少6位'; return; }
  POST('/admin/users/' + uid + '/reset-password', { new_password: pwd }).then(function() {
    closeModal('actionModal'); toast('密码已重置');
  }).catch(function(e) { document.getElementById('actErr').textContent = e.message; });
}

function toggleActive(uid) {
  if (!confirm('确认切换账号启用/禁用状态？')) return;
  POST('/admin/users/' + uid + '/toggle-active').then(function(r) {
    toast(r.data.is_active ? '已启用' : '已禁用'); loadUsers();
  }).catch(function(e) { toast(e.message); });
}

// ══════════════════════════════════════════
// 激活码
// ══════════════════════════════════════════
function loadCodes() {
  GET('/admin/membership/codes').then(function(r) {
    var html = '';
    (r.data.items || []).forEach(function(c) {
      var typeLabel = c.code_type === 'monthly' ? '月卡' : '年卡';
      html += '<tr><td>' + c.id + '</td><td>' + c.code + '</td><td>' + typeLabel + '</td>' +
        '<td>' + (c.is_used ? '<span style="color:var(--muted)">已使用</span>' : '<span style="color:#22c55e">未使用</span>') + '</td>' +
        '<td>' + (c.used_by || '--') + '</td><td>' + (c.created_at ? c.created_at.slice(0,10) : '--') + '</td>' +
        '<td>' + (c.used_at ? c.used_at.slice(0,10) : '') + '</td></tr>';
    });
    document.getElementById('cTbody').innerHTML = html || '<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:20px;">暂无激活码</td></tr>';
  }).catch(function(e) { toast(e.message); });
}

function genCodes() {
  var type = document.getElementById('cType').value;
  var count = parseInt(document.getElementById('cCount').value) || 10;
  POST('/admin/membership/codes', { code_type: type, count: count }).then(function(r) {
    var codes = r.data.codes || [];
    var el = document.getElementById('cGenOut');
    el.style.display = ''; el.textContent = '已生成 ' + codes.length + ' 个' + (type === 'monthly'?'月卡':'年卡') + ':\n' + codes.join('\n');
    loadCodes();
  }).catch(function(e) { toast(e.message); });
}

// ══════════════════════════════════════════
// 任务调度
// ══════════════════════════════════════════
function loadTasks() {
  GET('/admin/tasks').then(function(r) {
    var tasks = r.data.tasks || [];
    if (!tasks.length) { document.getElementById('tList').innerHTML = '<p style="color:var(--muted)">无定时任务</p>'; return; }
    var html = '<table><thead><tr><th>任务ID</th><th>名称</th><th>下次执行</th></tr></thead><tbody>';
    tasks.forEach(function(t) {
      html += '<tr><td>' + t.id + '</td><td>' + t.name + '</td><td>' + (t.next_run || '--') + '</td></tr>';
    });
    html += '</tbody></table>';
    document.getElementById('tList').innerHTML = html;
  }).catch(function(e) { toast(e.message); });
}

function runDailyBatch() {
  var el = document.getElementById('tResult');
  el.style.display = ''; el.textContent = '执行中...';
  POST('/admin/tasks/run-daily-batch').then(function(r) {
    el.textContent = JSON.stringify(r.data, null, 2);
    loadTasks();
  }).catch(function(e) { el.textContent = '失败: ' + e.message; });
}

// ══════════════════════════════════════════
// 缓存管理
// ══════════════════════════════════════════
function loadCacheStats() {
  GET('/admin/cache/stats').then(function(r) {
    document.getElementById('cacheStats').innerHTML = '<pre class="gen-out" style="display:block">' + JSON.stringify(r.data, null, 2) + '</pre>';
  }).catch(function(e) { toast(e.message); });
}
function cacheClearAll() { POST('/admin/cache/clear-all').then(function() { toast('缓存已清空'); loadCacheStats(); }).catch(function(e) { toast(e.message); }); }
function cacheRefreshPool() { POST('/admin/cache/refresh/pool').then(function(r) { toast(JSON.stringify(r.data)); loadCacheStats(); }).catch(function(e) { toast(e.message); }); }
function cacheRefreshReview() { POST('/admin/cache/refresh/review').then(function(r) { toast(JSON.stringify(r.data)); loadCacheStats(); }).catch(function(e) { toast(e.message); }); }
function cacheRefreshRisk() { POST('/admin/cache/refresh/risk').then(function(r) { toast(JSON.stringify(r.data)); loadCacheStats(); }).catch(function(e) { toast(e.message); }); }
function cacheRefreshSector() { POST('/admin/cache/refresh/sector').then(function(r) { toast(JSON.stringify(r.data)); loadCacheStats(); }).catch(function(e) { toast(e.message); }); }

// ══════════════════════════════════════════
// 系统日志
// ══════════════════════════════════════════
var logPage = 1;
function loadLogs(p) {
  logPage = p || logPage;
  var uid = document.getElementById('lUid').value;
  var qs = '?page=' + logPage + '&page_size=50';
  if (uid) qs += '&user_id=' + uid;
  GET('/admin/logs' + qs).then(function(r) {
    var d = r.data;
    var html = '';
    (d.items || []).forEach(function(l) {
      html += '<tr><td>' + l.id + '</td><td>' + (l.user_id || '--') + '</td><td>' + (l.phone || '--') + '</td>' +
        '<td>' + l.path + '</td><td>' + (l.access_time || '') + '</td></tr>';
    });
    document.getElementById('lTbody').innerHTML = html || '<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:20px;">暂无日志</td></tr>';
    var tp = Math.ceil(d.total / d.page_size) || 1;
    document.getElementById('lPager').innerHTML =
      '<button ' + (logPage <= 1 ? 'disabled' : '') + ' onclick="loadLogs(' + (logPage - 1) + ')">上一页</button>' +
      '<span>' + logPage + ' / ' + tp + '</span>' +
      '<button ' + (logPage >= tp ? 'disabled' : '') + ' onclick="loadLogs(' + (logPage + 1) + ')">下一页</button>';
  }).catch(function(e) { toast(e.message); });
}

// ── Toast ──
function toast(msg) {
  var el = document.createElement('div');
  el.textContent = msg;
  el.style.cssText = 'position:fixed;bottom:20px;right:20px;background:var(--card);border:1px solid var(--border);color:var(--text);padding:10px 18px;border-radius:8px;font-size:0.85rem;z-index:9999;max-width:360px;';
  document.body.appendChild(el);
  setTimeout(function() { el.remove(); }, 3000);
}

// ── 初始化 ──
(function init() {
  document.getElementById('lgServer').value = API_BASE;
  if (TOKEN) {
    document.getElementById('mainPage').style.display = '';
    document.getElementById('topServer').textContent = API_BASE.replace('https://','');
    loadOverview();
  }
})();
