# Seshat
让每一次论文修改都成为课题组永远的财富

## 配置与模型连接

项目使用 `uv` 管理 Python 环境。`env/.env` 保存密钥等本地敏感值，
`backend/config/application_local.yaml` 保存项目变量和模型环境变量名。

```powershell
uv sync
```

项目首次导入 `backend.config` 时会加载一次 YAML，其他模块可以直接复用。
创建当前模型连接：

```python
from backend.config import config
from backend.providers import create_model_client

tenant = config["default_tenant"]
client, model = create_model_client()
response = client.chat.completions.create(
    model=model,
    messages=[{"role": "user", "content": "你好"}],
)
print(response.choices[0].message.content)
```
