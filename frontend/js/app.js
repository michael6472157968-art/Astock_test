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
      throw { code: json.code, message: json.message, data: json.data };
    });
  },

  get: function(path) { return this._fetch('GET', path); },
  post: function(path, data) { return this._fetch('POST', path, data); },
  put: function(path, data) { return this._fetch('PUT', path, data); },
  del: function(path) { return this._fetch('DELETE', path); }
};

// 会话管理
var Session = {
  get: function() { try { return JSON.parse(localStorage.getItem('user') || 'null'); } catch(e) { return null; } },
  save: function(data) {
    localStorage.setItem('access_token', data.access_token);
    localStorage.setItem('refresh_token', data.refresh_token);
    localStorage.setItem('user', JSON.stringify({ id: data.user_id, phone: data.phone, tier: data.tier }));
  },
  clear: function() {
    localStorage.removeItem('access_token'); localStorage.removeItem('refresh_token'); localStorage.removeItem('user');
  },
  loggedIn: function() { return !!this.get(); },
  tier: function() { var u = this.get(); return u ? u.tier : 0; }
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
    { href: 'admin-trigger.html', label: '⚙' },
  ];
  var html = '<div class="nav-links">';
  var path = window.location.pathname.split('/').pop() || 'index.html';
  links.forEach(function(l) {
    var cls = path === l.href || (path === '' && l.href === '/') ? 'active' : '';
    html += '<a href="' + l.href + '" class="' + cls + '">' + l.label + '</a>';
  });
  html += '</div><div class="nav-right">';
  if (user) {
    html += '<span class="user-info">' + user.phone + ' · T' + user.tier + '</span>';
    html += '<button class="btn btn-sm btn-outline" onclick="doLogout()">退出</button>';
  } else {
    html += '<a href="login.html" class="btn btn-sm btn-primary">登录</a>';
  }
  html += '<select onchange="setTheme(this.value)" class="theme-select"><option value="light">亮色</option><option value="dark">暗色终端</option><option value="warm">暖色护眼</option></select>';
  html += '</div>';
  nav.innerHTML = html;
  setTheme(localStorage.getItem('theme') || 'light');
}

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
  // 如果当前在需要登录的页面且未登录，跳转登录页
  var needAuth = ['alerts.html'];
  var page = window.location.pathname.split('/').pop();
  if (needAuth.indexOf(page) >= 0 && !Session.loggedIn()) {
    window.location.href = 'login.html?redirect=' + encodeURIComponent(page);
  }
});
