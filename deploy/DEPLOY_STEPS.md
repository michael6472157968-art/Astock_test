# 一键部署流程（Ununtu / Debian）

# 1. 安装依赖
sudo apt update && sudo apt install -y nginx python3 python3-pip

# 2. 上传项目（scp / git clone 到服务器）
#    scp -r D:\Astock_DetaTest user@your-server:/opt/astock

# 3. 配置 Nginx
sudo cp /opt/astock/deploy/nginx.conf /etc/nginx/sites-available/astock
sudo ln -s /etc/nginx/sites-available/astock /etc/nginx/sites-enabled/
sudo rm /etc/nginx/sites-enabled/default  # 删除默认站点
sudo nginx -t && sudo systemctl reload nginx

# 4. 安装 Python 依赖
cd /opt/astock/backend
pip install -r requirements.txt

# 5. 注册 systemd 服务
sudo cp /opt/astock/deploy/astock.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable astock
sudo systemctl start astock

# 6. 防火墙开放 80 端口
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp

# 7. 检查运行状态
sudo systemctl status astock
sudo systemctl status nginx
curl http://localhost:8000/api/v1/health

# 8. SSL 证书 (Let's Encrypt)
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d yourdomain.com
