# Ubuntu 24.04 / Tencent Cloud Lighthouse 部署

适用范围：单实例 Docker Compose、SQLite、5–20 人 Beta。核心镜像不包含 OCR。

## 1. 安装 Docker

```bash
sudo apt update
sudo apt install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
newgrp docker
docker --version
docker compose version
```

## 2. 获取代码并配置环境

```bash
git clone https://github.com/Sui-YF/JobAsst.git
cd JobAsst
cp .env.beta.example .env
chmod 600 .env
nano .env
```

必须修改：

- `QWEN_API_KEY`：仅填写在服务器 `.env`；不得提交 Git。
- `BETA_BASE_URL`：例如 `http://PUBLIC_IP:8501/`。

保持：`APP_MODE=beta`、`APP_DEBUG=false`、`LLM_PROVIDER=qwen`、`QWEN_MODEL=qwen-plus`、`ENABLE_OCR=false`。

## 3. 构建并启动

```bash
docker compose config --quiet
docker compose build --pull
docker compose up -d
docker compose ps
docker compose logs --tail=100 career-agent
curl --fail http://127.0.0.1:8501/_stcore/health
```

容器监听 `0.0.0.0:8501`。命名 Volume `career-agent-data` 挂载到 `/app/data`，包含 SQLite、uploads、exports 和其他用户持久数据。不要执行 `docker compose down -v`。

## 4. 创建和撤销 Beta 用户

```bash
docker compose exec career-agent python create_beta_user.py "测试用户"
docker compose exec career-agent python revoke_beta_user.py USER_UUID
```

只把邀请链接私下发送给对应用户。撤销后该 UUID 会在身份解析阶段立即失效。

## 5. 第一次业务验证

1. 用邀请链接访问公网地址。
2. 上传测试 Resume，粘贴测试 JD。
3. 完成 Analysis → Strategy Approval → Draft → Human Review → Final → Preflight → DOCX。
4. 检查调用记录与日志：

```bash
docker compose exec career-agent python -c "import database; print(database.count_llm_requests('USER_UUID'))"
docker compose logs --tail=300 career-agent
```

日志中不得出现 API Key、Resume 正文或 JD 正文。

## 6. 重启与持久化验证

```bash
docker compose restart career-agent
docker compose ps
curl --fail http://127.0.0.1:8501/_stcore/health
```

重新打开同一邀请链接，确认 Resume、Evidence、Application、Final Resume 和导出记录仍存在。服务器重启验证：

```bash
sudo reboot
```

重新登录后：

```bash
cd JobAsst
docker compose ps
curl --fail http://127.0.0.1:8501/_stcore/health
```

## 7. 更新版本

更新前先备份。然后执行：

```bash
cd JobAsst
git pull --ff-only
docker compose build --pull
docker compose up -d
docker compose ps
curl --fail http://127.0.0.1:8501/_stcore/health
```

## 8. SQLite、uploads 与 exports 备份

对小规模 Beta，最简单且一致的方式是短暂停止容器并复制完整 `/app/data`：

```bash
cd JobAsst
mkdir -p backups
STAMP=$(date +%Y%m%d-%H%M%S)
docker compose stop career-agent
docker compose cp career-agent:/app/data/. "./backups/data-$STAMP"
docker compose start career-agent
tar -C backups -czf "backups/jobasst-data-$STAMP.tar.gz" "data-$STAMP"
```

验证备份至少包含 `app.db`、`uploads/` 和 `exports/`。备份文件应限制访问权限并复制到服务器以外的位置。

## 9. 腾讯云防火墙

- TCP `22`：只允许管理员固定公网 IP。
- TCP `8501`：仅允许 Beta 测试者来源 IP；若无法限制来源，则当前 HTTP 邀请链接存在被截获风险。

本阶段不引入 Nginx。正式扩大访问范围前应增加 HTTPS 终止层。
