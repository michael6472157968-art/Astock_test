# Phase 5 总结：用户系统 — 注册/登录/鉴权 + 访问日志

## 完成内容

### 1. 访问日志 (AccessLog)
- ORM 模型 `AccessLog` 已添加到 `backend/app/models/orm/models.py`
- 表 `access_logs`：user_id, endpoint, ip_address, user_agent, created_at
- `backend/app/middleware/access_log.py` — fire-and-forget 异步写入，不阻塞请求

### 2. 鉴权中间件
- `backend/app/middleware/auth_middleware.py`
  - `require_auth` — FastAPI Depends，强制鉴权，未登录返回 401，同时记录访问日志
  - `require_auth_optional` — 可选鉴权，允许游客访问但也记录日志
  - 从 Bearer token 提取用户，调用 `decode_token()`

### 3. API 鉴权保护
| 接口 | 鉴权方式 |
|------|---------|
| `POST /api/v1/auth/register` | 公开 + 日志 |
| `POST /api/v1/auth/login` | 公开 + 日志 |
| `GET /api/v1/auth/profile` | require_auth |
| `GET /api/v1/health` | 公开（不记录日志） |
| `GET /api/v1/stock-pool/*` | require_auth |
| `GET /api/v1/diagnosis/*` | require_auth_optional |
| `GET /api/v1/market/*` | require_auth_optional |
| `GET /api/v1/sector/*` | require_auth_optional |
| `GET /api/v1/review/*` | require_auth_optional |
| `GET /api/v1/risk-list` | require_auth_optional |
| `GET /api/v1/alerts/*` | require_auth |
| `POST /api/v1/backtest/*` | require_tier(2)（已有） |
| `GET /api/v1/admin/*` | require_tier(99)（已有） |
| `GET/POST /api/v1/membership/*` | require_auth |
| `GET /api/v1/user/*` | require_auth |

### 4. 前端页面鉴权
- `frontend/js/app.js` 更新 `needAuth` 列表，保护 `stock-pool.html`, `diagnosis.html`, `sector.html`, `sector-rotation.html`, `backtest.html`
- 未登录访问受保护页面 → 自动跳转 `login.html?redirect=`
- 所有 fetch 请求自动带 `Authorization: Bearer` header
- 401 响应自动触发 refresh → 失败则清除 token → 跳转登录

### 5. 注册/登录页面
- `frontend/login.html` — 已有，手机号 + 密码，深色金融主题
- `frontend/register.html` — 已有，手机号 + 密码
- 登录成功后 `Session.save()` 存储 token 和用户信息
- 导航栏已登录显示用户名 + 会员标识；未登录显示登录/注册按钮

## 验证结果 (TestClient)
1. 未登录访问 stock-pool → 401 ✓
2. 注册新用户 → 200 ✓
3. 重复注册同一手机号 → 400 ✓
4. 错误密码登录 → 401 ✓
5. 正确密码登录 → 200 + token ✓
6. 带 token 访问 stock-pool/diagnosis/market → 200 ✓
7. 访问日志写入 access_logs 表 ✓
8. 退出清除 token → 跳转首页 ✓
9. 健康检查不需要鉴权 ✓

## 文件清单
- `backend/app/models/orm/models.py` — 新增 AccessLog 模型
- `backend/app/middleware/__init__.py` — 空文件
- `backend/app/middleware/access_log.py` — 访问日志写入
- `backend/app/middleware/auth_middleware.py` — require_auth / require_auth_optional
- `backend/app/api/stock_pool.py` — 添加 require_auth
- `backend/app/api/diagnosis.py` — 添加 require_auth_optional
- `backend/app/api/market.py` — 添加 require_auth_optional
- `backend/app/api/alerts.py` — 替换 _get_user 为 require_auth
- `backend/app/api/auth.py` — 添加 require_auth + 登录日志
- `backend/app/api/membership.py` — 替换 get_current_user 为 require_auth
- `backend/app/api/user.py` — 替换 get_current_user 为 require_auth
- `frontend/js/app.js` — 扩展 needAuth 页面列表
