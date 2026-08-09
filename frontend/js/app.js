// 全局 API 客户端 + 会话管理
var API = {
  BASE: '/api/v1',

  _headers: function() {
    var h = { 'Content-Type': 'application/json' };
    var t = localStorage.getItem('access_token');
    if (t) h['Authorization'] = 'Bearer ' + t;
    return h;
  },

  _fetch: function(method, path, data) {
    var self = this;
    var opts = { method: method, headers: this._headers() };
    if (data && method !== 'GET') opts.body = JSON.stringify(data);
    return fetch(this.BASE + path, opts).then(function(r) { return r.json(); }).then(function(json) {
      if (json.code === 200) return json;
      if (json.code === 401) {
        var rt = localStorage.getItem('refresh_token');
        if (rt && path !== '/auth/refresh' && path !== '/auth/login') {
          return fetch(self.BASE + '/auth/refresh', { method: 'POST', headers: self._headers(), body: JSON.stringify({ refresh_token: rt }) })
            .then(function(rr) { return rr.json(); }).then(function(rj) {
              if (rj.code === 200 && rj.data && rj.data.access_token) {
                localStorage.setItem('access_token', rj.data.access_token);
                return self._fetch(method, path, data);
              }
              Session.clearAndRefresh();
              throw { code: 401, message: '登录已过期，请重新登录' };
            })
            .catch(function(refreshErr) {
              if (refreshErr && refreshErr.code) throw refreshErr;
              Session.clearAndRefresh();
              throw { code: 401, message: '请重新登录' };
            });
        }
        // 无 refresh_token 或已失效，清除状态
        Session.clearAndRefresh();
        throw { code: 401, message: '请重新登录' };
      }
      if (json.code === 403) {
        showToast(json.message || '当前用户等级无权限访问此功能');
      }
      throw { code: json.code, message: json.message || json.detail, data: json.data };
    });
  },

  get: function(path) { return this._fetch('GET', path); },
  post: function(path, data) { return this._fetch('POST', path, data); },
  put: function(path, data) { return this._fetch('PUT', path, data); },
  del: function(path) { return this._fetch('DELETE', path); }
};

// Toast 提示
function showToast(msg, type) {
  type = type || 'error';
  var t = document.createElement('div');
  t.className = 'toast ' + type;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(function() { t.classList.add('show'); }, 10);
  setTimeout(function() { t.classList.remove('show'); setTimeout(function() { t.remove(); }, 300); }, 2500);
}

// 会话管理
var Session = {
  get: function() { try { return JSON.parse(localStorage.getItem('user') || 'null'); } catch(e) { return null; } },
  save: function(data) {
    localStorage.setItem('access_token', data.access_token);
    localStorage.setItem('refresh_token', data.refresh_token);
    localStorage.setItem('user', JSON.stringify({
      id: data.user_id,
      phone: data.phone,
      tier: data.tier,
      member_type: data.member_type || 'free',
      member_expire: data.member_expire || null,
      credits: data.credits || 0
    }));
  },
  clear: function() {
    localStorage.removeItem('access_token'); localStorage.removeItem('refresh_token'); localStorage.removeItem('user');
  },
  clearAndRefresh: function() {
    this.clear();
    var page = window.location.pathname.split('/').pop() || 'index.html';
    var needAuth = ['admin-trigger.html', 'stock-pool.html', 'sector.html', 'sector-rotation.html', 'backtest.html'];
    if (needAuth.indexOf(page) === -1) {
      renderNav();
      return;
    }
    window.location.href = 'login.html?redirect=' + encodeURIComponent(page);
  },
  checkToken: function() {
    var access = localStorage.getItem('access_token');
    if (!access) return;
    var rt = localStorage.getItem('refresh_token');
    if (!rt) { this.clearAndRefresh(); return; }
    try {
      var payload = JSON.parse(atob(access.split('.')[1]));
      var exp = payload.exp * 1000;
      if (Date.now() > exp) return; // 过期了，等 API 调用时自动 refresh
    } catch(e) {
      this.clearAndRefresh();
    }
  },
  loggedIn: function() { return !!this.get(); },
  tier: function() { var u = this.get(); return u ? u.tier : 0; },
  isVip: function() { var t = this.tier(); return t >= 2 || t === 99; },
  isAdmin: function() { return this.tier() === 99; },
  canCreateGroup: function() { var t = this.tier(); return t >= 1 || t === 99; },
  canViewDashboard: function() { var t = this.tier(); return t >= 2 || t === 99; },
  canUseGroupFeature: function() { var t = this.tier(); return t >= 1 || t === 99; },
  getGroupStockLimit: function() { return this.isAdmin() ? 999 : 10; },
  getGroupCountLimit: function() { return this.isAdmin() ? 999 : 5; },
  credits: function() {
    var u = this.get();
    return u ? u.credits || 0 : 0;
  },
  memberLabel: function() {
    var t = this.tier();
    if (t === 99) return '<span class="vip-badge admin">管理员</span>';
    if (t === 3) return '<span class="vip-badge annual">年VIP</span>';
    if (t === 2) return '<span class="vip-badge monthly">月VIP</span>';
    return '';
  },
  memberName: function() {
    var t = this.tier();
    if (t === 99) return '管理员';
    if (t === 3) return '年度VIP';
    if (t === 2) return '月度VIP';
    return '免费用户';
  },
  remainDays: function() {
    var u = this.get();
    if (!u || !u.member_expire) return null;
    return Math.max(0, Math.ceil((new Date(u.member_expire) - new Date()) / (1000 * 60 * 60 * 24)));
  }
};

// 门禁函数
var Gate = {
  checkPage: function(minTier) {
    if (!Session.loggedIn()) {
      window.location.href = 'login.html?redirect=' + encodeURIComponent(window.location.pathname.split('/').pop());
      return false;
    }
    if (Session.tier() < minTier && !Session.isAdmin()) {
      var page = window.location.pathname.split('/').pop() || '';
      window.location.href = 'profile.html?redirect=' + encodeURIComponent(page);
      return false;
    }
    return true;
  },
  showUpgradeBanner: function() {
    if (Session.isVip()) return;
    var banner = document.createElement('div');
    banner.className = 'upgrade-banner';
    banner.innerHTML = '<span>🔒 此功能为会员专享</span> <a href="profile.html" style="color:#fff;text-decoration:underline;margin-left:8px">去升级</a>';
    banner.onclick = function() { banner.remove(); };
    var main = document.querySelector('.main-content');
    if (main) main.insertBefore(banner, main.firstChild);
  }
};

// ── 自选股共享逻辑 ──
var FavStore = {
  _cache: [],
  _loaded: false,
  _loading: null,
  _total: 0,

  _ukey: function(base) {
    var u = Session.get();
    return base + '_' + (u ? u.id : 'guest');
  },

  loadCache: function() {
    if (!Session.loggedIn()) return Promise.resolve([]);
    var self = this;
    if (self._loading) return self._loading;
    self._loading = API.get('/alerts/favorites').then(function(r) {
      self._cache = (r.data.items || []).map(function(f) { return f.stock_code; });
      self._total = r.data.total || 0;
      self._loaded = true;
      self._refreshButtons();
      self._loading = null;
      return self._cache;
    }).catch(function() { self._loading = null; return []; });
    return self._loading;
  },

  isFav: function(code) {
    return this._cache.indexOf(code) >= 0;
  },

  add: function(code) {
    var self = this;
    return API.post('/alerts/favorites', { stock_code: code }).then(function() {
      if (self._cache.indexOf(code) < 0) self._cache.push(code);
      self._refreshButtons();
      return true;
    }).catch(function(e) {
      if (e.message === '已在自选列表中') {
        if (self._cache.indexOf(code) < 0) self._cache.push(code);
        self._refreshButtons();
        return true;
      }
      throw e;
    });
  },

  refresh: function() {
    this._loaded = false;
    this._loading = null;
    return this.loadCache();
  },

  _refreshButtons: function() {
    var self = this;
    document.querySelectorAll('.spc-fav-btn').forEach(function(btn) {
      var c = btn.getAttribute('data-code');
      if (c && self._cache.indexOf(c) >= 0) {
        btn.textContent = '✓';
        btn.classList.add('added');
        btn.title = '已自选';
        btn.disabled = true;
      }
    });
  },

  // 设置单个按钮为已收藏状态
  setBtnAdded: function(btn) {
    btn.textContent = '✓';
    btn.classList.add('added');
    btn.title = '已自选';
    btn.disabled = true;
  },

  toggle: function(btn, code, name) {
    if (!Session.loggedIn()) { location.href = 'login.html'; return; }
    if (btn.classList.contains('added')) return;
    btn.disabled = true;
    var self = this;
    API.post('/alerts/favorites', { stock_code: code }).then(function() {
      if (self._cache.indexOf(code) < 0) self._cache.push(code);
      self.setBtnAdded(btn);
    }).catch(function(e) {
      if (e.code === 401) { location.href = 'login.html'; return; }
      if (e.message === '已在自选列表中') {
        if (self._cache.indexOf(code) < 0) self._cache.push(code);
        self.setBtnAdded(btn);
      } else {
        btn.disabled = false;
        showToast(e.message || '添加失败');
      }
    });
  },

  // ── T日配置（用户绑定 + 旧键迁移）──
  getTDates: function() {
    try {
      var newKey = this._ukey('fav_t_dates');
      var raw = localStorage.getItem(newKey);
      if (raw) return JSON.parse(raw);
      var oldRaw = localStorage.getItem('fav_t_dates');
      if (oldRaw) {
        localStorage.setItem(newKey, oldRaw);
        localStorage.removeItem('fav_t_dates');
        return JSON.parse(oldRaw);
      }
    } catch (e) {}
    return {};
  },

  setTDate: function(groupKey, dateStr) {
    var cfg = this.getTDates();
    if (dateStr) {
      cfg[String(groupKey)] = dateStr;
    } else {
      delete cfg[String(groupKey)];
    }
    localStorage.setItem(this._ukey('fav_t_dates'), JSON.stringify(cfg));
  },

  clearTDates: function() {
    localStorage.removeItem(this._ukey('fav_t_dates'));
  },

  // ── 图表标签切换（用户绑定）──
  getChartTab: function() {
    return localStorage.getItem(this._ukey('fav_chart_tab')) || 'avg_chg';
  },
  setChartTab: function(tab) {
    localStorage.setItem(this._ukey('fav_chart_tab'), tab);
  },

  // ── 个股涨跌幅模式选股（用户绑定 + 旧键迁移）──
  getStockPctCodes: function() {
    try {
      var newKey = this._ukey('fav_stock_pct_codes');
      var raw = localStorage.getItem(newKey);
      if (raw) return JSON.parse(raw);
      var oldRaw = localStorage.getItem(this._ukey('fav_price_codes'));
      if (oldRaw) {
        localStorage.setItem(newKey, oldRaw);
        localStorage.removeItem(this._ukey('fav_price_codes'));
        return JSON.parse(oldRaw);
      }
    } catch(e) {}
    return [];
  },
  setStockPctCodes: function(codes) {
    localStorage.setItem(this._ukey('fav_stock_pct_codes'), JSON.stringify(codes));
    // 清理不在列表中的颜色映射
    var map = this._getStockPctColorMap();
    var kept = {};
    codes.forEach(function(c) { kept[c] = true; });
    var changed = false;
    Object.keys(map).forEach(function(c) { if (!kept[c]) { delete map[c]; changed = true; } });
    if (changed) this._setStockPctColorMap(map);
  },

  // ── 个股颜色映射（code → colorIndex，确定性分配不漂移）──
  _getStockPctColorMap: function() {
    try { return JSON.parse(localStorage.getItem(this._ukey('fav_stock_pct_colors')) || '{}'); } catch(e) { return {}; }
  },
  _setStockPctColorMap: function(map) {
    localStorage.setItem(this._ukey('fav_stock_pct_colors'), JSON.stringify(map));
  },

  getStockPctColor: function(code) {
    var colors = ['#1677ff', '#f59e0b', '#a855f7', '#14b143', '#ef232a', '#06b6d4', '#ec4899', '#f97316', '#84cc16', '#fb923c'];
    var map = this._getStockPctColorMap();
    if (map[code] !== undefined) return colors[map[code]];
    // 找第一个未被占用的索引
    var used = {};
    Object.values(map).forEach(function(v) { used[v] = true; });
    var idx = 0;
    while (used[idx]) idx++;
    if (idx >= colors.length) idx = map[code] !== undefined ? map[code] : 0; // fallback
    map[code] = idx;
    this._setStockPctColorMap(map);
    return colors[idx];
  },

  // ── 个股涨跌幅模式缓存（用户绑定 + 旧键迁移）──
  getStockPctView: function() {
    try {
      var newKey = this._ukey('fav_stock_pct_view');
      var raw = localStorage.getItem(newKey);
      if (raw) return JSON.parse(raw);
      var oldRaw = localStorage.getItem(this._ukey('fav_price_view'));
      if (oldRaw) {
        localStorage.setItem(newKey, oldRaw);
        localStorage.removeItem(this._ukey('fav_price_view'));
        return JSON.parse(oldRaw);
      }
    } catch(e) {}
    return null;
  },
  setStockPctView: function(data) {
    localStorage.setItem(this._ukey('fav_stock_pct_view'), JSON.stringify(data));
  },

  // ── 涨跌幅统计缓存（用户绑定 + 旧键迁移）──
  getStatsView: function() {
    try {
      var newKey = this._ukey('fav_stats_view');
      var raw = localStorage.getItem(newKey);
      if (raw) return JSON.parse(raw);
      var oldRaw = localStorage.getItem('fav_stats_view');
      if (oldRaw) {
        localStorage.setItem(newKey, oldRaw);
        localStorage.removeItem('fav_stats_view');
        return JSON.parse(oldRaw);
      }
    } catch(e) {}
    return null;
  },
  setStatsView: function(data) {
    localStorage.setItem(this._ukey('fav_stats_view'), JSON.stringify(data));
  },

  // ── 分组观测（涨跌幅均值图表中显示哪些分组）──
  getObserveGroupIds: function() {
    try { return JSON.parse(localStorage.getItem(this._ukey('fav_obs_groups')) || '[]'); } catch(e) { return []; }
  },
  setObserveGroupIds: function(ids) {
    localStorage.setItem(this._ukey('fav_obs_groups'), JSON.stringify(ids));
  }
};

// 股票代码标准化 — 纯数字自动补后缀，带后缀直接使用，过滤空格和特殊字符
function normalizeStockCode(raw) {
  if (!raw) return '';
  var cleaned = raw.replace(/[\s\p{C}]+/gu, '').replace(/[^\w\.]/g, '').toUpperCase();
  var dotIdx = cleaned.indexOf('.');
  if (dotIdx >= 0) return cleaned;                     // 带后缀：直接返回
  if (/^\d{6}$/.test(cleaned)) {
    if (cleaned.startsWith('60') || cleaned.startsWith('68')) return cleaned + '.SH';
    if (cleaned.startsWith('00') || cleaned.startsWith('30')) return cleaned + '.SZ';
  }
  return cleaned;
}

// 输入框实时过滤 — 只允许数字、字母、点号
function filterStockInput(val) {
  return val.replace(/[^\w\.]/g, '').replace(/\s/g, '');
}
// HTML属性转义
function escAttr(v) { return v.replace(/'/g, "\\'").replace(/"/g, "\\x22"); }
function renderChrome() {
  var main = document.querySelector('.main-content[data-chrome]');
  if (!main) return;
  var body = document.body;

  var header = document.createElement('header');
  header.className = 'app-header';
  header.innerHTML = '<div class="header-left"><a href="/" style="display:flex;align-items:center;gap:6px;"><svg width="24" height="24" viewBox="0 0 28 28"><polyline points="2,22 8,14 14,18 20,8 26,4" fill="none" stroke="url(#hdrGrad)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/><circle cx="26" cy="4" r="2.5" fill="#00d4aa"/><defs><linearGradient id="hdrGrad" x1="0" y1="1" x2="1" y2="0"><stop offset="0%" stop-color="#00d4aa"/><stop offset="100%" stop-color="#00a8e8"/></linearGradient></defs></svg><span style="font-size:1.05rem;font-weight:700;background:linear-gradient(90deg,#00d4aa,#00a8e8);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;letter-spacing:2px">Stockwin</span></a></div><button class="hamburger-btn" id="hamburgerBtn" aria-label="菜单" onclick="toggleMobileNav()">☰</button><nav id="topNav" class="header-nav"></nav><div class="mobile-nav-overlay" id="mobileOverlay" onclick="closeMobileNav()"></div>';

  var banner = document.createElement('div');
  banner.className = 'risk-banner';
  banner.textContent = '⚠️ 免责声明：本平台所有分析内容仅供参考，不构成任何投资建议。股市有风险，投资需谨慎。';

  var footer = document.createElement('footer');
  footer.className = 'app-footer';
  footer.textContent = '【风险提示】本平台所提供市场数据、技术分析、选股结果、诊股报告等内容仅供学习研究参考，不构成投资建议。投资者据此操作，风险自担。数据来源于Tushare等第三方，本站不对数据准确性做保证。';

  // 页面额外的专属 banner（在 <main> 内部，由各页面自行保留）
  // header 插入到 body 最前，banner 紧随其后，footer 追加到最后
  body.insertBefore(banner, body.firstChild);
  body.insertBefore(header, body.firstChild);
  body.appendChild(footer);

  renderNav();
}

// 导航栏渲染（所有页面共用）
function renderNav() {
  var nav = document.getElementById('topNav');
  if (!nav) return;
  var user = Session.get();
  var links = [
    { href: '/', label: '首页' },
    { href: 'stock-pool.html', label: '选股池' },
    { href: 'diagnosis.html', label: '诊股' },
    { href: 'review.html', label: '复盘' },
    { href: 'sector-rotation.html', label: '板块' },
    { href: 'risk-list.html', label: '风险' },
    { href: 'alerts.html', label: '自选' },
    { href: 'backtest.html', label: '回测' },
  ];
  if (Session.isAdmin()) {
    links.push({ href: 'admin-trigger.html', label: '管理' });
  }
  var html = '<div class="nav-links">';
  var path = window.location.pathname.split('/').pop() || 'index.html';
  links.forEach(function(l) {
    var cls = path === l.href || (path === '' && l.href === '/') ? 'active' : '';
    html += '<a href="' + l.href + '" class="' + cls + '">' + l.label + '</a>';
  });
  html += '</div><div class="nav-right">';
  html += '<button class="btn btn-sm btn-outline qr-share-btn" onclick="showQRCode()" title="手机扫码打开当前页" style="font-size:1rem;line-height:1">📱</button>';
  if (user) {
    html += '<div class="nav-user-area" onclick="toggleUserMenu(event)">';
    html += Session.memberLabel();
    html += '<span class="user-name">' + user.phone + '</span>';
    html += '<span class="nav-dropdown-arrow">▼</span>';
    html += '<div class="nav-dropdown">';
    html += '<a href="profile.html">👤 个人中心</a>';
    if (Session.isAdmin()) {
      html += '<a href="admin-trigger.html">⚙ 管理后台</a>';
    }
    html += '<button onclick="doLogout()">退出登录</button>';
    html += '</div></div>';
  } else {
    html += '<a href="login.html" class="btn btn-sm btn-outline">登录</a>';
    html += '<a href="register.html" class="btn btn-sm btn-primary">注册</a>';
  }
  html += '<select onchange="setTheme(this.value)" class="theme-select"><option value="light">亮色</option><option value="dark">暗色终端</option><option value="warm">暖色护眼</option></select>';
  html += '</div>';
  nav.innerHTML = html;
  setTheme(localStorage.getItem('theme') || 'dark');
}

// 用户菜单下拉切换
function toggleUserMenu(e) {
  e.stopPropagation();
  var dd = document.querySelector('.nav-dropdown');
  if (dd) dd.classList.toggle('show');
}
document.addEventListener('click', function() {
  var dd = document.querySelector('.nav-dropdown');
  if (dd) dd.classList.remove('show');
});

// 主题切换
function setTheme(t) {
  localStorage.setItem('theme', t);
  document.documentElement.setAttribute('data-theme', t);
  var sel = document.querySelector('.theme-select');
  if (sel) sel.value = t;
}

// 汉堡菜单
function toggleMobileNav() {
  var nav = document.getElementById('topNav');
  var overlay = document.getElementById('mobileOverlay');
  var btn = document.getElementById('hamburgerBtn');
  var isOpen = nav && nav.classList.contains('open');
  if (isOpen) { closeMobileNav(); return; }
  if (nav) nav.classList.add('open');
  if (overlay) overlay.classList.add('show');
  if (btn) btn.textContent = '✕';
}
function closeMobileNav() {
  var nav = document.getElementById('topNav');
  var overlay = document.getElementById('mobileOverlay');
  var btn = document.getElementById('hamburgerBtn');
  if (nav) nav.classList.remove('open');
  if (overlay) overlay.classList.remove('show');
  if (btn) btn.textContent = '☰';
}

// 二维码分享弹窗
var _siteUrl = '';

function showQRCode() {
  var overlay = document.getElementById('qrOverlay');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = 'qrOverlay';
    overlay.className = 'modal-overlay';
    overlay.innerHTML = '<div class="modal-box" style="text-align:center">' +
      '<div class="modal-header"><h3>手机扫码打开</h3><button class="modal-close-btn" onclick="closeQRCode()">&times;</button></div>' +
      '<p style="font-size:0.8rem;color:var(--color-text-muted);margin-bottom:12px">扫描二维码在手机上打开当前页面</p>' +
      '<img id="qrImage" src="" alt="QR Code" style="max-width:200px;width:100%;display:block;margin:0 auto" />' +
      '<p style="font-size:0.7rem;color:var(--color-text-muted);margin-top:8px;word-break:break-all" id="qrUrl"></p>' +
      '</div>';
    overlay.addEventListener('click', function(e) { if (e.target === overlay) closeQRCode(); });
    document.body.appendChild(overlay);
  }
  var page = window.location.pathname + window.location.search + window.location.hash;
  var url = (_siteUrl || window.location.origin) + page;
  document.getElementById('qrImage').src = 'https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=' + encodeURIComponent(url);
  document.getElementById('qrUrl').textContent = url;
  overlay.classList.add('show');
}

function closeQRCode() {
  var overlay = document.getElementById('qrOverlay');
  if (overlay) overlay.classList.remove('show');
}

// 预加载站点配置（管理员在后台设置的域名，供二维码使用）
(function loadSiteConfig() {
  API.get('/market/site-config').then(function(r) {
    if (r.data && r.data.site_url) _siteUrl = r.data.site_url;
  }).catch(function() {});
})();

// 退出
function doLogout() {
  Session.clear();
  window.location.href = '/';
}

// 初始化
document.addEventListener('DOMContentLoaded', function() {
  var page = window.location.pathname.split('/').pop() || 'index.html';

  // 主动校验 token 有效性
  Session.checkToken();

  // 需要登录的页面
  var needAuth = ['admin-trigger.html', 'stock-pool.html', 'sector.html', 'sector-rotation.html', 'backtest.html'];
  if (needAuth.indexOf(page) >= 0 && !Session.loggedIn()) {
    window.location.href = 'login.html?redirect=' + encodeURIComponent(page);
  }
  // 管理员页面
  if (page === 'admin-trigger.html' && !Session.isAdmin()) {
    window.location.href = '/';
  }

  var main = document.querySelector('.main-content[data-chrome]');
  if (main) {
    renderChrome();
  } else {
    renderNav();
  }

  // 定时检查 token 是否有效，过期后刷新导航栏
  setInterval(function() {
    var access = localStorage.getItem('access_token');
    if (!access && Session.get()) {
      Session.clearAndRefresh();
      return;
    }
    if (access) {
      try {
        var payload = JSON.parse(atob(access.split('.')[1]));
        if (Date.now() > payload.exp * 1000 && Session.get()) {
          // access_token 过期但 user 还在，尝试静默刷新
          var rt = localStorage.getItem('refresh_token');
          if (!rt) { Session.clearAndRefresh(); }
        }
      } catch(e) {}
    }
  }, 30000); // 每30秒检查一次

  // 注册 Service Worker (PWA离线缓存)
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function() {
      navigator.serviceWorker.register('/sw.js');
    });
  }
});
