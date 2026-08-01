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
              localStorage.removeItem('access_token'); localStorage.removeItem('refresh_token'); localStorage.removeItem('user');
              window.location.href = '/login.html';
              throw { message: '请重新登录' };
            });
        }
      }
      if (json.code === 403) {
        showToast(json.message || '当前用户等级无权限访问此功能');
      }
      throw { code: json.code, message: json.message, data: json.data };
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
      member_expire: data.member_expire || null
    }));
  },
  clear: function() {
    localStorage.removeItem('access_token'); localStorage.removeItem('refresh_token'); localStorage.removeItem('user');
  },
  loggedIn: function() { return !!this.get(); },
  tier: function() { var u = this.get(); return u ? u.tier : 0; },
  isVip: function() { var t = this.tier(); return t >= 2 || t === 99; },
  isAdmin: function() { return this.tier() === 99; },
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
    { href: 'sector.html', label: '板块' },
    { href: 'risk-list.html', label: '风险' },
    { href: 'alerts.html', label: '预警' },
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
  setTheme(localStorage.getItem('theme') || 'light');
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

// 退出
function doLogout() {
  Session.clear();
  window.location.href = '/';
}

// 初始化
document.addEventListener('DOMContentLoaded', function() {
  renderNav();
  // 需要登录的页面
  var needAuth = ['alerts.html', 'admin-trigger.html'];
  var page = window.location.pathname.split('/').pop();
  if (needAuth.indexOf(page) >= 0 && !Session.loggedIn()) {
    window.location.href = 'login.html?redirect=' + encodeURIComponent(page);
  }
  // 管理员页面
  if (page === 'admin-trigger.html' && !Session.isAdmin()) {
    window.location.href = '/';
  }
});
