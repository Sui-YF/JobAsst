# JobAsst

个人 AI 求职助手 Beta。

## 本地启动

```powershell
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

请先将 `.env.example` 复制为 `.env`，并在本地填写模型配置。不要提交 API Key、简历、数据库、上传文件或导出文件。

## Docker

```bash
docker compose up -d --build
```

运行数据保存在 `/app/data` 持久化 Volume 中。

## 隐私

本仓库不包含真实 API Key、用户简历、职业数据、SQLite 数据库或运行日志。使用第三方模型前，请自行确认其数据处理与隐私规则。
